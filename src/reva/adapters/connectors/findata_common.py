"""Shared plumbing of the V6FINDATA finance connectors (V6, ``docs/dev/v6_findata.md``).

The V6FINDATA connectors (:mod:`atlas_bis_fin`, :mod:`atlas_imf_fin`, :mod:`atlas_cb_balance`,
:mod:`atlas_ff_factors`) publish weekly / monthly / quarterly / annual finance series. They reuse the PRED-HF
daily plumbing (:mod:`hf_daily_common`: metered fetch + archive, licence / as-of / publication / revision flags on
every row, policy quotes re-checked against archived pages, table registration) and add what that plumbing
does not have:

* :class:`PeriodRows` — atlas rows over an arbitrary half-open period ``[start, end)`` (a week, month, quarter,
  year, or one day for a point-in-time stock); ``flags.precision`` says which. Nothing is averaged, carried
  forward or interpolated: a period the provider does not report is **no row** (counted in the run report).
* :func:`iso_week` — SDMX weekly periods (``2026-W41``) as the ISO week ``[Monday, next Monday)``.
* :func:`stream_bis_flat` — the BIS Data Portal bulk "flat" CSV (zip member) read as a stream, with the
  series-level attribute rows (empty ``TIME_PERIOD``) applied to the series they match (as in
  :mod:`atlas_hf_bis`, but for every frequency).

As-of semantics (same as PRED-HF): ``flags.as_of`` = UTC date REVA fetched the raw bytes. A re-fetch whose content
differs registers a new version of the atlas table, and the atlas keeps every earlier version with its raw bytes:
**the table versions are the vintages**. A backtest reading the current version for old periods is final-vintage
pseudo-real-time unless it reads the version registered at the time.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import hf_daily_common as hc

#: provider frequency code -> ``flags.precision``
PRECISION = {"D": "day", "B": "day", "W": "week", "M": "month", "Q": "quarter", "S": "half_year", "A": "year"}


class PeriodRows(hc.DailyRows):
    """Columnar atlas rows; one row per (indicator, provider_geo, period_start, period_end)."""

    def __init__(self, source: str, dataset: str) -> None:
        super().__init__(source, dataset)
        self._pseen: set[tuple[str, str, date, date]] = set()

    def add_period(
        self,
        *,
        indicator: str,
        provider_geo: str,
        start: date,
        end: date,
        value: str,
        unit: str,
        flags: str,
        raw_sha256: str,
        selector: Mapping[str, Any],
        iso3: str | None = None,
    ) -> None:
        if not end > start:
            raise hc.err(f"{self.dataset}: empty period {start}..{end}", kind="invalid")
        if len(indicator) > 200:
            raise hc.err(f"{self.dataset}: indicator longer than 200 characters: {indicator[:60]}", kind="invalid")
        k = (indicator, provider_geo, start, end)
        if k in self._pseen:
            raise hc.err(f"{self.dataset}: duplicate cell {k}", kind="invalid")
        self._pseen.add(k)
        c = self.cols
        for col, v in (
            ("indicator", indicator),
            ("iso3", iso3),
            ("provider_geo", provider_geo),
            ("period_start", start),
            ("period_end", end),
            ("value", value),
            ("value_text", None),
            ("unit", unit),
            ("flags", flags),
            ("raw_sha256", raw_sha256),
            ("selector", hc.canon(selector)),
        ):
            c[col].append(v)


def iso_week(text: str) -> tuple[date, date]:
    """``2026-W41`` -> ``[Monday of ISO week 41, Monday + 7 days)``."""
    t = text.strip()
    try:
        y, w = t.split("-W")
        start = date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, TypeError) as e:
        raise hc.err(f"not an ISO week period: {t[:20]!r}") from e
    return start, start + timedelta(days=7)


def period_of(freq: str, text: str) -> tuple[date, date]:
    """Provider period text at frequency ``freq`` -> half-open ``[start, end)`` (D/B day, W ISO week, M/Q/S/A)."""
    t = text.strip()
    if freq in ("D", "B"):
        try:
            return hc.day_span(date.fromisoformat(t))
        except ValueError as e:
            raise hc.err(f"bad daily period {t[:20]!r}") from e
    if freq == "W":
        return iso_week(t)
    try:
        return fc.sdmx_period(freq, t)
    except fc.FiscalParseError as e:
        raise hc.err(f"bad {freq} period {t[:20]!r}") from e


def code(cell: str) -> str:
    """``"Q: Quarterly"`` -> ``"Q"`` (BIS flat files write ``<code>: <label>``)."""
    return cell.split(":", 1)[0].strip()


def label(cell: str) -> str:
    return cell.split(":", 1)[1].strip() if ":" in cell else cell.strip()


def zip_member(f: hc.Fetched, member: str, max_bytes: int = 1_500_000_000) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(f.body))
    except zipfile.BadZipFile as e:
        raise hc.err(f"{f.key}: not a zip ({e})") from e
    if member not in z.namelist():
        raise hc.err(f"{f.key}: member {member!r} missing (members {z.namelist()[:5]})")
    if z.getinfo(member).file_size > max_bytes:
        raise hc.err(f"{f.key}: member too large", kind="too_large")
    return z


#: columns of a BIS flat file that are neither dimensions nor attributes
_BIS_NON_ATTR = ("STRUCTURE", "STRUCTURE_ID", "ACTION", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "OBS_CONF",
                 "OBS_PRE_BREAK")  # fmt: skip


def stream_bis_flat(
    f: hc.Fetched, member: str, dims: tuple[str, ...]
) -> Iterator[tuple[int, tuple[str, ...], tuple[str, ...], dict[str, str], dict[str, str]]]:
    """Observations of a BIS flat CSV: ``(record line, dimension codes, dimension labels, merged attributes,
    observation cells)``.

    ``merged attributes`` = series-level attribute rows (empty ``TIME_PERIOD``; partial keys match as wildcards)
    overlaid by the observation row's own attribute cells, as ``<code>: <label>`` texts. ``observation cells`` =
    ``TIME_PERIOD``, ``OBS_VALUE``, ``OBS_STATUS``, ``OBS_CONF``, ``OBS_PRE_BREAK`` verbatim. Two passes over the
    zip member (no full read into memory)."""
    z = zip_member(f, member)

    def records() -> Iterator[list[str]]:
        with z.open(member) as raw:
            yield from csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))

    it = records()
    header = next(it, None) or []
    codes = [h.split(":", 1)[0] for h in header]
    col = {c: i for i, c in enumerate(codes)}
    need = {*dims, "TIME_PERIOD", "OBS_VALUE"}
    if not need <= set(col):
        raise hc.err(f"{f.key}: columns missing {sorted(need - set(col))}")
    attr_cols = [c for c in codes if c not in dims and c not in _BIS_NON_ATTR]
    groups: list[tuple[tuple[str, ...], dict[str, str]]] = []
    tcol = col["TIME_PERIOD"]
    for line, rec in enumerate(it, start=2):
        if len(rec) != len(header):
            raise hc.err(f"{f.key}: record {line} has {len(rec)} fields, header {len(header)}")
        if not rec[tcol].strip():
            gd = tuple(code(rec[col[d]]) for d in dims)
            groups.append((gd, {a: rec[col[a]].strip() for a in attr_cols if rec[col[a]].strip()}))
    cache: dict[tuple[str, ...], dict[str, str]] = {}
    it = records()
    next(it, None)
    obs_cols = [c for c in ("TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "OBS_CONF", "OBS_PRE_BREAK") if c in col]
    for line, rec in enumerate(it, start=2):
        if not rec[tcol].strip():
            continue
        d = tuple(code(rec[col[x]]) for x in dims)
        labels = tuple(label(rec[col[x]]) for x in dims)
        attrs = cache.get(d)
        if attrs is None:
            attrs = {}
            for gd, ga in groups:
                if all(g in ("", s) for g, s in zip(gd, d, strict=True)):
                    attrs.update(ga)
            cache[d] = attrs
        own = {a: rec[col[a]].strip() for a in attr_cols if rec[col[a]].strip()}
        yield line, d, labels, {**attrs, **own}, {c: rec[col[c]].strip() for c in obs_cols}


def counter_report(c: Counter[str]) -> dict[str, int]:
    return dict(sorted(c.items()))


async def ingest_files(
    run: Any,
    files: tuple[hc.FileSpec, ...],
    datasets: tuple[str, ...],
    build: Callable[[Mapping[str, hc.Fetched], tuple[str, ...]], tuple[dict[str, hc.TableBuild], dict[str, Any]]],
    *,
    rights: Mapping[str, Mapping[str, Any]],
    notes: Mapping[str, str],
    source_key: str,
    geo_scheme: str | None,
) -> dict[str, Any]:
    """Fetch (metered, archived) the files of the wanted datasets, build, register one table per dataset."""
    wanted = tuple(d for d in datasets if run.wants(d))
    fetched = await hc.fetch_all(run, files, wanted)
    builds, report = build(fetched, wanted)
    tables = hc.register_tables(
        run, builds, rights=rights, notes=notes, source_key=source_key, geo_scheme=geo_scheme
    )
    return {"ok": True, "tables": tables, "report": report}


__all__ = [
    "PRECISION",
    "PeriodRows",
    "code",
    "counter_report",
    "ingest_files",
    "iso_week",
    "label",
    "period_of",
    "stream_bis_flat",
    "zip_member",
]
