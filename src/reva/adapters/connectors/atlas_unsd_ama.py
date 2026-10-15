"""UNSD National Accounts — Analysis of Main Aggregates (AMA) -> atlas table ``unsd.ama`` (V3, A-FACTBOOK).

**Why this source (universal fallback).** The UN Statistics Division's AMA database gives national
accounts main aggregates for **every country and area** reporting to the UN, 1970 to the latest
year, **complete by construction**: UNSD fills missing years/countries with its own estimates. It
covers economies that the World Bank and the IMF skip or report only partly — Anguilla, Montserrat,
Cook Islands, Sint Maarten, Curaçao, Cayman Islands, Turks and Caicos, Bermuda, Greenland, Monaco,
Liechtenstein, San Marino, Andorra, French Polynesia, New Caledonia, Puerto Rico, Cuba, North Korea,
Somalia, Kosovo, Zanzibar — plus former states (USSR, Yugoslavia, Czechoslovakia, Netherlands
Antilles, former Sudan/Ethiopia, the two Yemens) for history. 220 countries/areas.

**Files** (``https://unstats.un.org/unsd/amaapi/api/file/<id>``, the bulk downloads of the AMA web
app; the file list ``/api/file`` is archived with them): :data:`FILES` — GDP and its breakdown at
current and constant 2020 US dollars, per-capita GDP (current, constant), growth rates (constant
2020 prices), shares of GDP/value added, GNI and per-capita GNI (current US$), exchange rates and
population. One atlas table ``unsd.ama``; indicator = ``<series>|<AMA item name>``
(e.g. ``gdp_current_usd|Gross Domestic Product (GDP)``).

**Honesty.** Values are the exact cell texts (strict decimals, never rescaled). UNSD footnotes
attached to a (country, item, year) cell are copied into ``flags.footnotes``. Every row carries
``flags.provider_nature`` ("official data reported to UNSD, gaps filled by UNSD estimates"): AMA
does not mark which cells are estimates, so no row is presented as a national release.
Geography: AMA ``CountryID`` = UN M49 code, scheme ``unsd_ama`` (M49 + Kosovo 412 -> XKX, from the
AMA country list). Former states and Tanzania Mainland/Zanzibar (no United Republic of Tanzania
total in AMA) stay unmapped (kept, listed).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.unsd_ama"
VERSION = "1.0.0"
SOURCE_KEY = "unsd.ama"
ATLAS_SOURCE = "unsd"
DATASET = "ama"
PARSER_VERSION = "atlas-unsd-ama/1"
HOST = "unstats.un.org"
BASE = f"https://{HOST}/unsd/amaapi/api/file"
GEO_SCHEME = "unsd_ama"
MAX_BYTES = 30_000_000
RIGHTS = {
    "licence": "UNdata terms of use (free use with attribution)",
    "licence_url": "https://data.un.org/Host.aspx?Content=UNdataUse",
    "attribution": "United Nations Statistics Division, National Accounts - Analysis of Main Aggregates (AMA)",
    "provider": "United Nations Statistics Division",
    "source_key": SOURCE_KEY,
}
NATURE = "UNSD AMA: official national accounts reported to UNSD, gaps filled by UNSD estimates (cells not marked)"

#: file id -> (series key, unit, expected sheet-title prefix)
FILES: dict[int, tuple[str, str, str]] = {
    2: ("gdp_current_usd", "current US$", "GDP/breakdown at current prices in US Dollars"),
    6: ("gdp_constant2020_usd", "constant 2020 US$", "GDP/breakdown at constant 2020 prices in US Dollars"),
    9: ("gdp_pc_current_usd", "current US$ per person", "Per Capita GDP at current prices in US Dollars"),
    12: ("gdp_pc_constant2020_usd", "constant 2020 US$ per person", "Per Capita GDP at constant 2020 prices"),
    16: ("growth_constant2020", "% annual growth (constant 2020 prices)", "Growth Rate of GDP/breakdown"),
    22: ("share_current", "% (share of GDP / value added, current prices)", "Shares of breakdown of GDP"),
    24: ("gni_current_usd", "current US$", "GNI at current prices in US Dollars"),
    27: ("gni_pc_current_usd", "current US$ per person", "Per Capita GNI at current prices in US Dollars"),
    30: ("xr_pop", "", "Exchange rates/Population"),
}
XPOP_UNITS = {"Population": "persons"}  # exchange rates: national currency per US dollar
_DEC = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
_YEAR_HDR = re.compile(r"^(19|20)\d{2}$")


def _digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="United Nations Statistics Division (National Accounts - Analysis of Main Aggregates)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(f"amaapi/file/{i}" for i in FILES),
        cadence_seconds=365 * 86400,
        planned_requests=len(FILES) + 1,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=_digest(),
        field_mappings={"files": {str(k): v[0] for k, v in FILES.items()}},
        notes="Bulk XLSX downloads (all countries) of the AMA web app; annual December update.",
    )


class AmaParseError(SourceResponseError):
    pass


def _err(msg: str) -> AmaParseError:
    return AmaParseError(msg, retryable=False, kind="schema_drift")


def decimal_text(v: str) -> str | None:
    t = v.strip()
    if _DEC.match(t):
        return t
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if _DEC.match(s) else None


def _table(wb: Any, sheet: str) -> tuple[int, dict[int, str], list[tuple[int, dict[int, Any]]]]:
    """(header row number, {col: header}, data rows) of a sheet whose header starts with CountryID."""
    rows = list(wb.rows(sheet))
    for i, (rn, cells) in enumerate(rows):
        first = cells.get(0)
        if first is not None and first.text.strip() == "CountryID":
            hdr = {k: c.text.strip() for k, c in cells.items()}
            return rn, hdr, rows[i + 1 :]
    raise _err(f"sheet {sheet!r}: no 'CountryID' header row")


def _notes(wb: Any, sheet: str) -> dict[tuple[str, str | None, int], list[str]]:
    """(country id, item or None, year) -> footnote texts, from a footnotes sheet ('X' marks)."""
    out: dict[tuple[str, str | None, int], list[str]] = {}
    try:
        _, hdr, rows = _table(wb, sheet)
    except AmaParseError:
        return out
    years = {k: int(v) for k, v in hdr.items() if _YEAR_HDR.match(v)}
    item_col = next((k for k, v in hdr.items() if v in ("Item", "Name")), None)
    note_col = next((k for k, v in hdr.items() if v in ("Footnote", "Note")), None)
    if note_col is None:
        return out
    for _, cells in rows:
        cid = cells.get(0)
        note = cells.get(note_col)
        if cid is None or note is None:
            continue
        item = cells[item_col].text.strip() if item_col is not None and item_col in cells else None
        for k, y in years.items():
            c = cells.get(k)
            if c is not None and c.text.strip().upper() == "X":
                out.setdefault((cid.text.strip(), item, y), []).append(note.text.strip()[:500])
    return out


def parse_file(body: bytes, file_id: int, raw_sha256: str, cols: dict[str, list[Any]], stats: dict[str, int]) -> str:
    """Append the cells of one AMA workbook to ``cols``; returns the workbook title."""
    from reva.adapters.connectors.atlas_xlsx import Workbook

    series, unit, title_prefix = FILES[file_id]
    try:
        wb = Workbook(body)
    except Exception as e:  # bounded reader raises typed errors for oversized/invalid workbooks
        raise _err(f"AMA file {file_id} is not a readable workbook: {e}") from e
    sheets = list(wb.sheets)
    if not sheets:
        raise _err(f"AMA file {file_id}: no sheet")
    first = next(wb.rows(sheets[0]), None)
    title = first[1][0].text.strip() if first and 0 in first[1] else ""
    if not title.startswith(title_prefix):
        raise _err(f"AMA file {file_id}: unexpected title {title[:120]!r}")
    _, hdr, rows = _table(wb, sheets[0])
    years = {k: int(v) for k, v in hdr.items() if _YEAR_HDR.match(v)}
    if not years:
        raise _err(f"AMA file {file_id}: no year columns")
    item_col = next((k for k, v in hdr.items() if v in ("IndicatorName", "Measure")), None)
    cur_col = next((k for k, v in hdr.items() if v == "Currency"), None)
    notes: dict[tuple[str, str | None, int], list[str]] = {}
    for s in sheets[1:]:
        notes.update(_notes(wb, s))
    seen: set[tuple[str, str, int]] = set()
    for rn, cells in rows:
        cid_cell = cells.get(0)
        if cid_cell is None or not cid_cell.text.strip():
            continue
        cid = cid_cell.text.strip()
        if cid.endswith(".0"):
            cid = cid[:-2]
        cname = cells[1].text.strip() if 1 in cells else ""
        item = cells[item_col].text.strip() if item_col is not None and item_col in cells else None
        indicator = f"{series}|{item}" if item else series
        if len(indicator) > 200:
            indicator = indicator[:190] + "~" + hashlib.sha256(indicator.encode()).hexdigest()[:9]
        if series == "xr_pop":
            row_unit = XPOP_UNITS.get(item or "", "national currency per US dollar")
        else:
            row_unit = unit
        currency = cells[cur_col].text.strip() if cur_col is not None and cur_col in cells else None
        for k, y in years.items():
            c = cells.get(k)
            if c is None or not c.text.strip():
                continue
            v = None if c.kind in ("b", "e") else decimal_text(c.text)  # booleans/errors are not numbers
            if v is None:
                stats["non_numeric_cells"] = stats.get("non_numeric_cells", 0) + 1
                continue
            key = (indicator, cid, y)
            if key in seen:
                stats["duplicate_cells"] = stats.get("duplicate_cells", 0) + 1
                continue
            seen.add(key)
            flags: dict[str, Any] = {"provider_nature": NATURE, "country_name": cname}
            if currency and series == "xr_pop" and row_unit != "persons":
                flags["currency"] = currency
            fn = notes.get((cid, item, y)) or (notes.get((cid, None, y)) if item is None else None)
            if fn is None and item is None:
                fn = next((v2 for (c2, _i, y2), v2 in notes.items() if c2 == cid and y2 == y), None)
            if fn:
                flags["footnotes"] = fn[:5]
            for col, val in (
                ("source", ATLAS_SOURCE),
                ("dataset", DATASET),
                ("indicator", indicator),
                ("concept_id", None),
                ("iso3", None),
                ("provider_geo", cid),
                ("entity_qid", None),
                ("period_start", date(y, 1, 1)),
                ("period_end", date(y + 1, 1, 1)),
                ("value", v),
                ("value_text", None),
                ("unit", row_unit),
                ("flags", json.dumps(flags, sort_keys=True, separators=(",", ":"), ensure_ascii=False)),
                ("raw_sha256", raw_sha256),
                (
                    "selector",
                    json.dumps(
                        {"file_id": file_id, "sheet": sheets[0], "row": rn, "column": str(y)},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            ):
                cols[col].append(val)
            stats["cells"] = stats.get("cells", 0) + 1
    return title


def empty_columns() -> dict[str, list[Any]]:
    from reva.atlas.tables import ATLAS_COLUMNS

    return {c: [] for c in ATLAS_COLUMNS}


def frame_of(cols: Mapping[str, list[Any]]) -> pl.DataFrame:
    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(dict(cols), schema=ATLAS_SCHEMA)


async def ingest(run: Any, file_ids: Iterable[int] | None = None) -> dict[str, Any]:
    ids = [i for i in FILES if file_ids is None or i in set(file_ids)]
    out: dict[str, Any] = {"files": {}, "failed": []}
    shas: list[str] = []
    try:  # the file list documents what each file id is at retrieval time
        lst = await run.get(BASE, key="file-list", media_types=("application/json",), max_bytes=1_000_000)
        shas.append(run.archive(lst))
        listing = {int(x["fileId"]): x for x in json.loads(lst.body) if isinstance(x, dict) and "fileId" in x}
    except (SourceResponseError, ValueError) as e:
        listing = {}
        out["failed"].append({"item": "file-list", "error": str(e)[:300]})
    cols = empty_columns()
    stats: dict[str, int] = {}
    for fid in ids:
        try:
            raw = await run.get(
                f"{BASE}/{fid}",
                key=f"file/{fid}",
                media_types=(
                    "application/vnd.ms-excel",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
                max_bytes=MAX_BYTES,
            )
        except SourceResponseError as e:
            out["failed"].append({"item": f"file/{fid}", "error": str(e)[:300]})
            continue
        sha = run.archive(raw)
        before = stats.get("cells", 0)
        title = parse_file(raw.body, fid, sha, cols, stats)
        shas.append(sha)
        out["files"][fid] = {
            "series": FILES[fid][0],
            "title": title,
            "file_name": (listing.get(fid) or {}).get("fileName"),
            "cells": stats.get("cells", 0) - before,
            "bytes": len(raw.body),
        }
    out["stats"] = stats
    if run.wants(DATASET) and cols["indicator"]:
        frame = frame_of(cols)
        last = max(d.year for d in cols["period_start"])
        reg = run.register(
            dataset=DATASET,
            frame=frame,
            raw_sha256s=shas,
            geo_scheme=GEO_SCHEME,
            rights=RIGHTS,
            coverage_note=f"UNSD AMA bulk files {sorted(out['files'])}: 1970-{last}, all countries/areas; "
            f"failed {len(out['failed'])}. Official data + UNSD gap-filling estimates (not marked per cell).",
        )
        cov = reg.get("coverage") or {}
        out[DATASET] = {
            "dataset_key": reg.get("dataset_key"),
            "row_count": reg.get("row_count"),
            "created": reg.get("created"),
            "countries": cov.get("countries"),
            "unmapped_provider_geo": [u.get("provider_geo") for u in reg.get("unmapped_provider_geo", [])],
        }
    return out


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Run option ``files`` (list or comma-separated AMA file ids) restricts the files fetched."""
    v = (getattr(run, "options", None) or {}).get("files")
    ids = None
    if v is not None:
        items = v.split(",") if isinstance(v, str) else list(v)
        ids = [int(x) for x in items if str(x).strip()]
    return await ingest(run, ids)


__all__ = ["DATASET", "FILES", "NAME", "atlas_ingest", "atlas_source", "decimal_text", "ingest", "parse_file"]
