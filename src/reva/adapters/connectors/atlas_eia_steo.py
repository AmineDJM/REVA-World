"""U.S. EIA Short-Term Energy Outlook (bulk file) -> atlas tables, observations and forecasts apart (V4, V4ENERGY).

The STEO is EIA's monthly outlook: ~1,500 series × annual / quarterly / monthly frequency, each with its own
history (from 1990 or 1997 for most series) and an 18-24 month forecast. Contents relevant to energy and
resources: crude oil spot prices (Brent ``BREPUUS``, WTI ``WTIPUUS``), Henry Hub natural gas spot price
(``NGHHUUS`` $/MMBtu, ``NGHHMCF`` $/Mcf), retail fuel prices, crude oil and liquids **production by country**
(OPEC members, Russia, Canada, Brazil, China, … — ISO3 in EIA's ``geography`` field), world supply and
consumption balances, inventories, U.S. production / consumption / generation by fuel and by U.S. region
(EIA's ``geography`` lists the ISO 3166-2 states of the region, e.g. ``USA-AL+USA-KY+USA-MS+USA-TN``).

Source: ``https://www.eia.gov/opendata/bulk/STEO.zip`` (one member ``STEO.txt``: one JSON object per line,
series or category; ~5.4 MB zipped). The bulk manifest ``https://www.eia.gov/opendata/bulk/manifest.txt`` is
archived and its STEO ``accessURL`` must be the URL fetched. No key (the EIA API v2 needs one, the bulk files
do not). Licence: U.S. public domain (``https://www.eia.gov/about/copyrights_reuse.php``); every series carries
``"copyright": "None"`` — another value is flagged on each cell.

**Observations and projections are never mixed.** Every STEO series declares ``lastHistoricalPeriod``:
periods up to and including it go to ``steo_observed``; later periods go to ``steo_forecast`` with
``flags.estimate_kind = "projection"`` and ``flags.projection_upstream`` = {model: "EIA Short-Term Energy
Outlook", release: the series ``last_updated``}. A series without ``lastHistoricalPeriod`` (16 in the
2026-10 release) cannot be split: all its cells go to ``steo_forecast`` with
``estimate_kind = "unknown_boundary"`` — never into the observed table.

Cells: ``indicator`` = the STEO series id verbatim (``STEO.BREPUUS.M``: series code + frequency; the
code embeds the geography and unit), ``provider_geo`` = the series ``geography`` verbatim (ISO3,
``USA-..+..`` state lists) or ``not_stated`` when EIA gives none (world / OPEC totals, prices; the name says
which); ``iso3`` only for a single ISO3 code; ``value`` = the JSON number text verbatim
(``parse_float=str``); JSON null → no cell (counted). Periods: ``YYYY`` (A), ``YYYYQn`` (Q), ``YYYYMM`` (M).
``flags``: ``series_name``, ``series_last_updated``, ``last_historical_period``, ``frequency``.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.eia_steo"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas_eia_steo/1"
SOURCE_KEY = "eia.opendata_bulk"
SOURCE = "eia_steo"
HOST = "www.eia.gov"
ZIP_URL = f"https://{HOST}/opendata/bulk/STEO.zip"
MANIFEST_URL = f"https://{HOST}/opendata/bulk/manifest.txt"
TERMS_URL = "https://www.eia.gov/about/copyrights_reuse.php"
MEMBER = "STEO.txt"
LICENCE = "U.S. public domain (EIA: 'U.S. government publications are in the public domain')"
ZIP_MEDIA = ("application/x-zip-compressed", "application/zip", "application/octet-stream")
MAX_ZIP_BYTES = 40_000_000
MAX_MEMBER_BYTES = 400_000_000
DS_OBS = "steo_observed"
DS_FC = "steo_forecast"
NOT_STATED = "not_stated"
MODEL = "EIA Short-Term Energy Outlook"
DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
PERIOD_RE = {
    "A": re.compile(r"^([0-9]{4})$"),
    "Q": re.compile(r"^([0-9]{4})Q([1-4])$"),
    "M": re.compile(r"^([0-9]{4})(0[1-9]|1[0-2])$"),
}
ISO3_RE = re.compile(r"^[A-Z]{3}$")


class SteoParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="U.S. Energy Information Administration (EIA), Short-Term Energy Outlook (bulk download)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DS_OBS, DS_FC),
        dataset_ids=("STEO",),
        cadence_seconds=30 * 86_400,
        planned_requests=2,
        max_response_bytes=MAX_ZIP_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{DS_OBS}", f"{SOURCE}.{DS_FC}"], "geo_scheme": "iso3"},
        notes="EIA bulk STEO.zip (one JSON series per line) + manifest.txt; split at lastHistoricalPeriod",
    )


def rights() -> dict[str, Any]:
    return {
        "licence": "U.S. public domain",
        "licence_url": TERMS_URL,
        "terms_quote": "U.S. government publications are in the public domain and are not subject to copyright"
        " protection.",
        "attribution": "Source: U.S. Energy Information Administration (EIA), Short-Term Energy Outlook.",
        "provider": "U.S. Energy Information Administration",
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


def parse_manifest(body: bytes) -> dict[str, Any]:
    try:
        d = json.loads(body)["dataset"]["STEO"]
    except (ValueError, KeyError, TypeError) as e:
        raise SteoParseError(f"bulk manifest: no dataset STEO ({e})") from e
    if d.get("accessURL") != ZIP_URL:
        raise SteoParseError(f"bulk manifest: STEO accessURL {d.get('accessURL')!r} != {ZIP_URL}")
    return {k: d.get(k) for k in ("last_updated", "title", "accessURL")}


def period_bounds(period: str, f: str) -> tuple[int, int, int, int]:
    rx = PERIOD_RE.get(f)
    m = rx.match(period) if rx else None
    if not m:
        raise SteoParseError(f"period {period!r} does not match frequency {f!r}")
    y = int(m.group(1))
    if f == "A":
        return y, 1, y + 1, 1
    if f == "Q":
        q = int(m.group(2))
        m0 = 3 * (q - 1) + 1
        return y, m0, (y + 1 if q == 4 else y), (1 if q == 4 else m0 + 3)
    mo = int(m.group(2))
    return y, mo, (y + 1 if mo == 12 else y), (1 if mo == 12 else mo + 1)


def _key(period: str, f: str) -> tuple[int, int]:
    y0, m0, _, _ = period_bounds(period, f)
    return y0, m0


@dataclass
class SteoReport:
    lines: int = 0
    categories: int = 0
    series: int = 0
    frequencies: dict[str, int] = field(default_factory=dict)
    cells: dict[str, int] = field(default_factory=dict)
    null_cells: int = 0
    no_boundary_series: list[str] = field(default_factory=list)
    copyright_other: dict[str, int] = field(default_factory=dict)
    geos: dict[str, int] = field(default_factory=dict)
    last_updated: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["last_updated"] = sorted(self.last_updated)
        d["geos"] = len(self.geos)
        return d


def _flags(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _lines(body: bytes):  # type: ignore[no-untyped-def]
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise SteoParseError(f"STEO.zip is not a zip archive ({e})") from e
    if MEMBER not in zf.namelist():
        raise SteoParseError(f"STEO.zip has no member {MEMBER} ({zf.namelist()[:5]})")
    if zf.getinfo(MEMBER).file_size > MAX_MEMBER_BYTES:
        raise SteoParseError(f"{MEMBER}: inflated size exceeds {MAX_MEMBER_BYTES}")
    with zf.open(MEMBER) as fh:
        yield from enumerate(fh, start=1)


def parse_bulk(
    body: bytes, raw_sha: str, datasets: set[str] | None = None
) -> tuple[dict[str, pl.DataFrame], SteoReport]:
    want = set(datasets) if datasets else {DS_OBS, DS_FC}
    rep = SteoReport()
    rows: dict[str, dict[str, list[Any]]] = {
        d: {k: [] for k in ("ind", "geo", "y0", "m0", "y1", "m1", "value", "flags", "unit", "sel")} for d in want
    }
    for line_no, line in _lines(body):
        rep.lines += 1
        try:
            d = json.loads(line, parse_float=str, parse_int=str)
        except ValueError as e:
            raise SteoParseError(f"{MEMBER} line {line_no}: not JSON ({e})") from e
        if "series_id" not in d:
            rep.categories += 1
            continue
        rep.series += 1
        sid = str(d["series_id"])
        f = str(d.get("f") or "")
        if f not in PERIOD_RE:
            raise SteoParseError(f"line {line_no}: series {sid} has frequency {f!r}")
        rep.frequencies[f] = rep.frequencies.get(f, 0) + 1
        unit = str(d.get("units") or "").strip() or "not stated by EIA"
        geo = str(d.get("geography") or "").strip() or NOT_STATED
        rep.geos[geo] = rep.geos.get(geo, 0) + 1
        lu = str(d.get("last_updated") or "")
        rep.last_updated.add(lu)
        lhp = d.get("lastHistoricalPeriod")
        base: dict[str, Any] = {"series_name": str(d.get("name") or ""), "series_last_updated": lu, "frequency": f}
        cr = str(d.get("copyright", "None"))
        if cr != "None":
            base["copyright"] = cr
            rep.copyright_other[cr] = rep.copyright_other.get(cr, 0) + 1
        if lhp:
            base["last_historical_period"] = str(lhp)
            boundary: tuple[int, int] | None = _key(str(lhp), f)
        else:
            boundary = None
            rep.no_boundary_series.append(sid)
        seen: set[str] = set()
        for pair in d.get("data") or []:
            period, v = str(pair[0]), pair[1]
            if period in seen:
                raise SteoParseError(f"line {line_no}: series {sid} repeats period {period}")
            seen.add(period)
            if v is None:
                rep.null_cells += 1
                continue
            if not DECIMAL_RE.match(str(v)):
                raise SteoParseError(f"line {line_no}: series {sid} period {period}: value {v!r} is not a number")
            y0, m0, y1, m1 = period_bounds(period, f)
            if boundary is None:
                ds, fl = DS_FC, {**base, "estimate_kind": "unknown_boundary"}
            elif (y0, m0) <= boundary:
                ds, fl = DS_OBS, base
            else:
                ds = DS_FC
                fl = {**base, "estimate_kind": "projection", "projection_upstream": {"model": MODEL, "release": lu}}
            rep.cells[ds] = rep.cells.get(ds, 0) + 1
            if ds not in want:
                continue
            c = rows[ds]
            c["ind"].append(sid)
            c["geo"].append(geo)
            c["y0"].append(y0)
            c["m0"].append(m0)
            c["y1"].append(y1)
            c["m1"].append(m1)
            c["value"].append(str(v))
            c["flags"].append(_flags(fl))
            c["unit"].append(unit)
            c["sel"].append(_flags({"file": MEMBER, "line": line_no, "series_id": sid, "period": period}))
    if rep.series == 0:
        raise SteoParseError(f"{MEMBER}: no series")
    frames: dict[str, pl.DataFrame] = {}
    for ds in sorted(want):
        c = rows[ds]
        if not c["ind"]:
            raise SteoParseError(f"{ds}: no cell")
        geo_map = {g: (country_iso3(g, "iso3") if ISO3_RE.match(g) else None) for g in set(c["geo"])}
        df = pl.DataFrame(
            c,
            schema={
                "ind": pl.String,
                "geo": pl.String,
                "y0": pl.Int32,
                "m0": pl.Int8,
                "y1": pl.Int32,
                "m1": pl.Int8,
                "value": pl.String,
                "flags": pl.String,
                "unit": pl.String,
                "sel": pl.String,
            },
        )
        out = df.select(
            pl.lit(SOURCE).alias("source"),
            pl.lit(ds).alias("dataset"),
            pl.col("ind").alias("indicator"),
            pl.lit(None, dtype=pl.String).alias("concept_id"),
            pl.col("geo").replace_strict(geo_map, default=None, return_dtype=pl.String).alias("iso3"),
            pl.col("geo").alias("provider_geo"),
            pl.lit(None, dtype=pl.String).alias("entity_qid"),
            pl.date(pl.col("y0"), pl.col("m0"), 1).alias("period_start"),
            pl.date(pl.col("y1"), pl.col("m1"), 1).alias("period_end"),
            "value",
            pl.lit(None, dtype=pl.String).alias("value_text"),
            pl.col("unit").str.slice(0, 100),
            "flags",
            pl.lit(raw_sha).alias("raw_sha256"),
            pl.col("sel").alias("selector"),
        ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
        frames[ds] = out
    return frames, rep


def coverage_note(ds: str, rep: SteoReport, manifest: dict[str, Any]) -> str:
    kind = (
        "observations only (periods up to each series' lastHistoricalPeriod)"
        if ds == DS_OBS
        else "EIA forecasts (periods after lastHistoricalPeriod, estimate_kind=projection) and series without a "
        "declared boundary (estimate_kind=unknown_boundary); never observations"
    )
    return (
        f"EIA Short-Term Energy Outlook bulk {ZIP_URL} (manifest last_updated {manifest.get('last_updated')}), "
        f"member {MEMBER}: {rep.series} series (frequencies {rep.frequencies}); this table: {kind}; "
        f"{rep.cells.get(ds, 0)} cells. JSON nulls (no cell): {rep.null_cells}. Series without lastHistoricalPeriod: "
        f"{len(rep.no_boundary_series)}. provider_geo = EIA geography verbatim ('{NOT_STATED}' when EIA gives none). "
        f"Licence: U.S. public domain ({TERMS_URL})."
    )[:4000]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = {d for d in (DS_OBS, DS_FC) if run.wants(d)}
    raw_man = await run.get(MANIFEST_URL, key="bulk:manifest", media_types=("text/plain", "application/json"))
    man_sha = run.archive(raw_man)
    manifest = parse_manifest(raw_man.body)
    raw = await run.get(
        ZIP_URL,
        key=f"bulk:STEO:{manifest.get('last_updated')}",
        media_types=ZIP_MEDIA,
        max_bytes=MAX_ZIP_BYTES,
        params={"last_updated": manifest.get("last_updated")},
    )
    sha = run.archive(raw)
    frames, rep = parse_bulk(raw.body, sha, wanted)
    del raw
    out: dict[str, Any] = {"manifest": manifest, "tables": {}, "bulk": rep.as_dict()}
    for ds in sorted(frames):
        reg = run.register(
            dataset=ds,
            frame=frames[ds],
            raw_sha256s=[sha, man_sha],
            coverage_note=coverage_note(ds, rep, manifest),
            rights=rights(),
        )
        out["tables"][ds] = {
            "dataset_key": reg["dataset_key"],
            "rows": reg["row_count"],
            "countries": reg["coverage"]["countries"],
            "created": reg["created"],
        }
    return out


__all__ = [
    "CONNECTOR_NAME",
    "DS_FC",
    "DS_OBS",
    "SteoParseError",
    "atlas_ingest",
    "atlas_source",
    "parse_bulk",
    "parse_manifest",
]
