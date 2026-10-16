"""Shared plumbing of the A-FISCAL atlas connectors (V3): OECD SDMX (taxes, social and public spending,
pensions, student/adult skills) and BIS statistics (policy rates, credit, property prices, debt service,
effective exchange rates).

Both providers answer SDMX REST data queries as CSV with code **and** label for every coded component, so a
connector needs no separate structure request:

* OECD ``format=csvfilewithlabels`` — a label column follows each code column (``REF_AREA,Reference area``);
* BIS ``Accept: application/vnd.sdmx.data+csv;version=2.0.0;labels=both`` — a coded cell is ``"<code>: <label>"``
  and the header names ``"<ID>:<Name>"``.

:func:`read_labelled_csv` turns either into rows of ``{component: (code, label)}``; :func:`parse_flow` turns those
rows into one atlas frame (``reva.atlas.tables.ATLAS_SCHEMA``) following a :class:`Flow` declaration:

* ``indicator`` = the flow's prefix or first folded dimension code, then ``|<dim>=<code>`` for the other folded
  dimensions (``_Z`` "not applicable" codes are left out);
* ``unit`` = the provider's ``UNIT_MEASURE`` label (code when there is no label), ``x10^n`` appended when the
  provider's ``UNIT_MULT`` is not 0 (the value is never rescaled; ``flags.unit_mult`` keeps the code and label);
* ``value`` = the provider's number verbatim as a strict decimal (``strict_decimal``); a row without a value is
  **not a cell** (counted per observation status in the report and the coverage note: OECD ``M`` "data cannot
  exist", ``L`` "not collected", ``K`` "included in another category"…; BIS ``NaN``);
* ``flags`` = the observation status when it is not ``A`` ("Normal value": the default, left out) with its
  provider label verbatim (``E`` estimated, ``B`` time series break, ``P`` provisional, ``I`` imputed…), BIS
  confidentiality when not ``F`` (free), BIS pre-break value, currency, and the flow's declared extra attributes;
* periods: ``A`` year, ``S`` semester, ``Q`` quarter, ``M`` month as half-open ``[start, end)`` intervals.

Nothing is converted, averaged, rescaled or imputed. Fetching happens only through ``run.get`` (budget-metered
live client or the fixture client); every response is archived (``run.archive``) before it is parsed.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

_STRICT_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
_YEAR = re.compile(r"^([0-9]{4})$")
_SEMESTER = re.compile(r"^([0-9]{4})-S([12])$")
_QUARTER = re.compile(r"^([0-9]{4})-Q([1-4])$")
_MONTH = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")
#: provider "no value" spellings (BIS writes NaN for a series gap); never a number, never a zero
NO_VALUE = frozenset({"", "NaN", "nan", "NA"})
#: SDMX observation status "Normal value": the default, not copied into flags
NORMAL_STATUS = frozenset({"", "A"})
#: BIS confidentiality "F: Free": the default, not copied into flags
FREE_CONF = frozenset({"", "F"})


class FiscalParseError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


def module_digest(*modules: Any) -> str:
    """sha256 over this module and the given connector modules (the spec's ``code_digest``)."""
    import sys

    h = hashlib.sha256()
    for m in (sys.modules[__name__], *modules):
        h.update(inspect.getsource(m).encode())
    return "sha256:" + h.hexdigest()


def canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def strict_decimal(text: str) -> str:
    """The provider's number as a strict decimal string (verbatim when it already is one)."""
    from decimal import Decimal, InvalidOperation

    t = text.strip()
    if _STRICT_DECIMAL.match(t):
        return t
    try:
        d = Decimal(t)
    except InvalidOperation as e:
        raise FiscalParseError("provider value is not a number", value=t[:40]) from e
    if not d.is_finite():
        raise FiscalParseError("provider value is not finite", value=t[:40])
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    s = "0" if s in ("", "-0") else s
    if not _STRICT_DECIMAL.match(s):  # pragma: no cover - format(Decimal, 'f') always matches
        raise FiscalParseError("provider value could not be written as a strict decimal", value=t[:40])
    return s


def sdmx_period(freq: str, text: str) -> tuple[date, date]:
    """SDMX ``TIME_PERIOD`` of frequency ``A``/``S``/``Q``/``M`` -> half-open ``[start, end)``."""
    t = text.strip()
    m = _YEAR.match(t) if freq == "A" else None
    if m:
        y = int(m.group(1))
        return date(y, 1, 1), date(y + 1, 1, 1)
    m = _SEMESTER.match(t) if freq == "S" else None
    if m:
        y, s = int(m.group(1)), int(m.group(2))
        return (date(y, 1, 1), date(y, 7, 1)) if s == 1 else (date(y, 7, 1), date(y + 1, 1, 1))
    m = _QUARTER.match(t) if freq == "Q" else None
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        start = date(y, 3 * q - 2, 1)
        return start, (date(y, 3 * q + 1, 1) if q < 4 else date(y + 1, 1, 1))
    m = _MONTH.match(t) if freq == "M" else None
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, 1), (date(y, mo + 1, 1) if mo < 12 else date(y + 1, 1, 1))
    raise FiscalParseError("unsupported SDMX period", freq=freq[:4], period=t[:20])


# --------------------------------------------------------------------------- labelled CSV
Row = dict[str, tuple[str, str]]


@dataclass(frozen=True)
class LabelledCsv:
    style: str  # "oecd" | "bis"
    components: tuple[str, ...]
    rows: list[Row]


def _decode(body: bytes, what: str) -> str:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise FiscalParseError(f"{what}: not UTF-8") from e
    head = text[:512].lstrip().lower()
    if head.startswith(("<!doctype", "<html", "<?xml")):
        raise FiscalParseError(f"{what}: HTML/XML body where SDMX-CSV was expected")
    if not text.strip():
        raise FiscalParseError(f"{what}: empty body")
    return text


def read_labelled_csv(body: bytes, what: str, *, coded: frozenset[str] = frozenset()) -> LabelledCsv:
    """Rows ``{component: (code, label)}`` of an OECD ``csvfilewithlabels`` or BIS ``labels=both`` SDMX-CSV.

    ``coded`` names the BIS components whose cells are ``"<code>: <label>"`` (dimensions and coded
    attributes); other BIS cells (``TIME_PERIOD``, ``OBS_VALUE``, titles, pre-break values) are plain text.
    """
    rows = list(csv.reader(io.StringIO(_decode(body, what))))
    if len(rows) < 2:
        raise FiscalParseError(f"{what}: no data rows")
    header = rows[0]
    if header[:2] == ["STRUCTURE", "STRUCTURE_ID"] and len(header) > 3 and header[2] == "STRUCTURE_NAME":
        # OECD csvfilewithlabels: STRUCTURE,STRUCTURE_ID,STRUCTURE_NAME,ACTION, then <ID>,<Name> pairs
        if len(header) % 2:
            raise FiscalParseError(f"{what}: odd number of OECD label columns")
        comps = ["STRUCTURE_ID", *(header[i] for i in range(4, len(header), 2))]
        out: list[Row] = []
        for n, r in enumerate(rows[1:], start=1):
            if len(r) != len(header):
                raise FiscalParseError(f"{what}: ragged CSV row", row=n)
            d: Row = {"STRUCTURE_ID": (r[1], r[2])}
            for i in range(4, len(header), 2):
                d[header[i]] = (r[i], r[i + 1])
            out.append(d)
        return LabelledCsv("oecd", tuple(comps), out)
    if header[:3] == ["STRUCTURE", "STRUCTURE_ID", "ACTION"]:
        comps = [h.split(":", 1)[0] for h in header]
        out = []
        for n, r in enumerate(rows[1:], start=1):
            if len(r) != len(header):
                raise FiscalParseError(f"{what}: ragged CSV row", row=n)
            d = {}
            for c, cell in zip(comps, r, strict=True):
                if c in coded and cell:
                    code, sep, label = cell.partition(": ")
                    d[c] = (code, label if sep else "")
                else:
                    d[c] = (cell, "")
            out.append(d)
        return LabelledCsv("bis", tuple(comps[3:]), out)
    raise FiscalParseError(f"{what}: not an SDMX-CSV with labels", header=header[:6])


# --------------------------------------------------------------------------- flow -> atlas frame
@dataclass(frozen=True)
class Flow:
    """How one provider dataflow becomes one atlas table."""

    dataset: str  # atlas dataset key
    flow: str  # provider dataflow id (OECD 'DSD_X@DF_Y', BIS 'WS_TC')
    agency: str  # OECD agency id ('OECD.CTP.TPS'), 'BIS'
    title: str
    dims: tuple[str, ...]  # expected dimension order (the SDMX key order), validated against the CSV
    area_dim: str  # REF_AREA / BORROWERS_CTY
    fold: tuple[str, ...]  # dimensions folded into the indicator (in this order)
    keys: tuple[str, ...]  # live SDMX keys ('{areas}' = all areas); one request each
    fixture_keys: tuple[str, ...]  # restricted keys of the recorded offline fixtures
    prefix: str = ""  # indicator prefix; '' = the first folded dimension's code is the prefix
    freqs: tuple[str, ...] = ("A",)
    attrs: tuple[str, ...] = ()  # extra attributes copied into flags (code + label)
    text_attrs: tuple[str, ...] = ()  # extra uncoded attributes copied into flags verbatim
    max_bytes: int = 200_000_000
    notes: str = ""


@dataclass
class FlowReport:
    dataset: str
    files: int = 0
    rows: int = 0
    cells: int = 0
    no_value: Counter[str] = field(default_factory=Counter)
    statuses: Counter[str] = field(default_factory=Counter)
    zeros: int = 0
    indicators: int = 0
    geos: dict[str, str] = field(default_factory=dict)  # code -> label
    years: tuple[int, int] | None = None
    freqs: Counter[str] = field(default_factory=Counter)

    def unmapped(self, scheme: str) -> dict[str, str]:
        return {g: lab for g, lab in sorted(self.geos.items()) if country_iso3(g, scheme) is None}

    def as_dict(self, scheme: str) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "files": self.files,
            "rows_read": self.rows,
            "cells": self.cells,
            "no_value_rows_by_status": dict(sorted(self.no_value.items())),
            "flagged_status": dict(sorted(self.statuses.items())),
            "reported_zeros": self.zeros,
            "indicators": self.indicators,
            "geos": len(self.geos),
            "unmapped": self.unmapped(scheme),
            "years": list(self.years) if self.years else None,
            "freqs": dict(self.freqs),
        }


_COLS = ("indicator", "provider_geo", "period_start", "period_end", "value", "unit", "flags", "raw_sha256", "selector")


def _get(r: Row, comp: str) -> tuple[str, str]:
    return r.get(comp, ("", ""))


def parse_flow(
    flow: Flow,
    files: Sequence[tuple[str, bytes, str]],
    *,
    source: str,
    geo_scheme: str,
    coded: frozenset[str] = frozenset(),
) -> tuple[pl.DataFrame, FlowReport]:
    """``files`` = ``[(file label, body, raw sha256)]`` (one per key) -> one atlas frame + report."""
    rep = FlowReport(flow.dataset)
    cols: dict[str, list[Any]] = {c: [] for c in _COLS}
    years: set[int] = set()
    inds: set[str] = set()
    for label, body, sha in files:
        rep.files += 1
        csvd = read_labelled_csv(body, f"{flow.flow} ({label})", coded=coded | frozenset(flow.dims) | _CODED_ATTRS)
        missing = [c for c in (*flow.dims, "TIME_PERIOD", "OBS_VALUE") if c not in csvd.components]
        if missing:
            raise FiscalParseError(f"{flow.flow}: missing CSV components", missing=missing)
        order = [c for c in csvd.components if c in flow.dims]
        if tuple(order) != flow.dims:
            raise FiscalParseError(f"{flow.flow}: dimension order changed", got=order, expected=list(flow.dims))
        for n, r in enumerate(csvd.rows, start=1):
            rep.rows += 1
            sid = _get(r, "STRUCTURE_ID")[0]
            if flow.flow not in sid:
                raise FiscalParseError(f"{flow.flow}: row of another structure", row=n, structure=sid[:120])
            freq = _get(r, "FREQ")[0] or "A"
            if freq not in flow.freqs:
                raise FiscalParseError(f"{flow.flow}: unexpected frequency", row=n, freq=freq[:4])
            raw_value = _get(r, "OBS_VALUE")[0].strip()
            status, status_label = _get(r, "OBS_STATUS")
            if raw_value in NO_VALUE:
                rep.no_value[status or "(none)"] += 1
                continue
            value = strict_decimal(raw_value)
            area, area_label = _get(r, flow.area_dim)
            if not area:
                raise FiscalParseError(f"{flow.flow}: row without {flow.area_dim}", row=n)
            parts = [flow.prefix] if flow.prefix else []
            for i, d in enumerate(flow.fold):
                code = _get(r, d)[0]
                if not flow.prefix and i == 0:
                    parts.append(code)
                elif code and code != "_Z":
                    parts.append(f"{d.lower()}={code}")
            indicator = "|".join(parts)
            if len(indicator) > 200:
                raise FiscalParseError(f"{flow.flow}: indicator longer than 200 characters", indicator=indicator[:80])
            start, end = sdmx_period(freq, _get(r, "TIME_PERIOD")[0])
            unit_code, unit_label = _get(r, "UNIT_MEASURE")
            mult, mult_label = _get(r, "UNIT_MULT")
            unit = unit_label or unit_code or "not stated by provider"
            flags: dict[str, Any] = {}
            if mult not in ("", "0"):
                unit = f"{unit} x10^{mult}"
                flags["unit_mult"] = mult
                if mult_label:
                    flags["unit_mult_label"] = mult_label
            if len(unit) > 100:
                flags["unit_full"] = unit
                unit = unit[:97] + "..."
            if status not in NORMAL_STATUS:
                flags["obs_status"] = status
                flags["obs_status_label"] = status_label
                rep.statuses[status] += 1
            conf, conf_label = _get(r, "OBS_CONF")
            if conf not in FREE_CONF:
                flags["obs_conf"] = conf
                flags["obs_conf_label"] = conf_label
            pre = _get(r, "OBS_PRE_BREAK")[0].strip()
            if pre and pre not in NO_VALUE:
                flags["pre_break_value"] = strict_decimal(pre)
            cur = _get(r, "CURRENCY")[0]
            if cur and cur != "_Z":
                flags["currency"] = cur
            for a in flow.attrs:
                code, lab = _get(r, a)
                if code and code != "_Z":
                    flags[a.lower()] = code
                    if lab:
                        flags[f"{a.lower()}_label"] = lab
            for a in flow.text_attrs:
                txt = _get(r, a)[0].strip()
                if txt:
                    flags[a.lower()] = txt[:1000]
            if float(value) == 0:
                rep.zeros += 1
            rep.freqs[freq] += 1
            rep.geos.setdefault(area, area_label)
            years.add(start.year)
            inds.add(indicator)
            cols["indicator"].append(indicator)
            cols["provider_geo"].append(area)
            cols["period_start"].append(start)
            cols["period_end"].append(end)
            cols["value"].append(value)
            cols["unit"].append(unit)
            cols["flags"].append(canon(flags))
            cols["raw_sha256"].append(sha)
            cols["selector"].append(canon({"file": label, "row": n}))
    if not cols["indicator"]:
        raise FiscalParseError(f"{flow.flow}: no cell with a value")
    rep.cells = len(cols["indicator"])
    rep.indicators = len(inds)
    rep.years = (min(years), max(years))
    geo_map = {g: country_iso3(g, geo_scheme) for g in rep.geos}
    df = pl.DataFrame(
        cols,
        schema={
            "indicator": pl.String,
            "provider_geo": pl.String,
            "period_start": pl.Date,
            "period_end": pl.Date,
            "value": pl.String,
            "unit": pl.String,
            "flags": pl.String,
            "raw_sha256": pl.String,
            "selector": pl.String,
        },
    )
    out = df.select(
        pl.lit(source).alias("source"),
        pl.lit(flow.dataset).alias("dataset"),
        "indicator",
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.col("provider_geo").replace_strict(geo_map, default=None, return_dtype=pl.String).alias("iso3"),
        "provider_geo",
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        "period_start",
        "period_end",
        "value",
        pl.lit(None, dtype=pl.String).alias("value_text"),
        "unit",
        "flags",
        "raw_sha256",
        "selector",
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
    dup = out.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
    if dup:
        raise FiscalParseError(f"{flow.flow}: {dup} duplicate (indicator, geography, period) cells")
    return out, rep


#: BIS coded attributes (cells "<code>: <label>"); OECD labels come in their own columns
_CODED_ATTRS = frozenset(
    {"FREQ", "OBS_STATUS", "OBS_CONF", "UNIT_MEASURE", "UNIT_MULT", "COLLECTION", "DECIMALS", "TIME_FORMAT"}
)


def coverage_note(flow: Flow, rep: FlowReport, *, provider: str, url: str, scheme: str, licence: str) -> str:
    unm = rep.unmapped(scheme)
    years = f"{rep.years[0]}-{rep.years[1]}" if rep.years else "-"
    parts = [
        f"{provider} {flow.agency}:{flow.flow} '{flow.title}' ({url}); {rep.files} request(s), {rep.rows} rows read,"
        f" {rep.cells} cells, {rep.indicators} indicators, {len(rep.geos)} geographies, periods {years}"
        f" (frequencies {dict(rep.freqs)}).",
        f"Rows without a value are not cells: {dict(sorted(rep.no_value.items()))} (by observation status).",
        f"Flagged observation statuses kept with the provider label: {dict(sorted(rep.statuses.items()))};"
        f" reported zeros kept ({rep.zeros}). Values verbatim, unit multiplier never applied.",
        f"Unmapped provider geographies (aggregates, kept): {', '.join(f'{k} {v}' for k, v in unm.items()) or 'none'}.",
        flow.notes,
        f"Licence: {licence}.",
    ]
    return " ".join(p for p in parts if p)[:4000]


# --------------------------------------------------------------------------- ingest loop
@dataclass(frozen=True)
class Request:
    key: str  # acquisition item key
    url: str
    label: str  # file label used in selectors
    media_types: tuple[str, ...]
    headers: Mapping[str, str] | None = None
    max_bytes: int = 200_000_000


async def ingest_flows(
    run: Any,
    flows: Sequence[Flow],
    requests_of: Callable[[Flow, bool], list[Request]],
    build: Callable[[Flow, list[tuple[str, bytes, str]]], tuple[pl.DataFrame, FlowReport, str]],
    *,
    geo_scheme: str,
    rights: Mapping[str, Any],
) -> dict[str, Any]:
    """Per wanted flow: fetch its requests (``run.get``), archive each, parse, register one atlas table.

    A provider error on one flow is reported and the other flows go on; a budget refusal
    (``BudgetExhausted``) always propagates (the platform ends the run as ``budget_exhausted``).
    """
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": [], "reports": {}}
    for fl in flows:
        if not run.wants(fl.dataset):
            continue
        files: list[tuple[str, bytes, str]] = []
        failed = False
        for rq in requests_of(fl, fixture):
            try:
                raw = await run.get(
                    rq.url,
                    key=rq.key,
                    media_types=rq.media_types,
                    headers=dict(rq.headers) if rq.headers else None,
                    max_bytes=rq.max_bytes,
                )
                if not raw.body:
                    raise SourceResponseError(f"{rq.key}: empty response body", retryable=True)
                if raw.media_type not in rq.media_types:
                    raise SourceResponseError(f"{rq.key}: unexpected media type {raw.media_type!r}", retryable=False)
            except BudgetExhausted:
                raise
            except Exception as e:
                out["errors"].append({"dataset": fl.dataset, "item": rq.key, "error": type(e).__name__, "message": str(e)[:300]})
                failed = True
                break
            sha = run.archive(raw)
            files.append((rq.label, raw.body, sha))
            out["fetched"].append({"item": rq.key, "bytes": len(raw.body), "sha256": sha})
        if failed:
            continue
        try:
            frame, rep, note = build(fl, files)
            del files
            reg = run.register(
                dataset=fl.dataset,
                frame=frame,
                raw_sha256s=sorted({s for s in frame["raw_sha256"].unique().to_list()}),
                coverage_note=note,
                geo_scheme=geo_scheme,
                rights=dict(rights),
            )
        except BudgetExhausted:
            raise
        except Exception as e:
            details = getattr(e, "details", None)
            out["errors"].append(
                {
                    "dataset": fl.dataset,
                    "stage": "parse/register",
                    "error": type(e).__name__,
                    "message": str(e)[:500],
                    "details": json.loads(json.dumps(details, default=str)) if details else None,
                }
            )
            continue
        cov = reg.get("coverage") or {}
        out["reports"][fl.dataset] = rep.as_dict(geo_scheme)
        out["tables"].append(
            {
                "table": f"{run.spec.atlas_source}.{fl.dataset}",
                "dataset_key": reg.get("dataset_key"),
                "created": reg.get("created"),
                "rows": reg.get("row_count"),
                "countries": cov.get("countries"),
                "unmapped_provider_geo": [u["provider_geo"] for u in reg.get("unmapped_provider_geo") or []],
                "artifact_sha256": reg.get("artifact_sha256"),
            }
        )
    out["ok"] = not out["errors"]
    return out


__all__ = [
    "Flow",
    "FlowReport",
    "FiscalParseError",
    "LabelledCsv",
    "Request",
    "canon",
    "coverage_note",
    "ingest_flows",
    "module_digest",
    "parse_flow",
    "read_labelled_csv",
    "sdmx_period",
    "strict_decimal",
]
