"""World Bank Commodity Price Data ("The Pink Sheet") -> atlas tables (V4, agent V4ENERGY).

Monthly (1960M01 → latest month) and annual (1960 → latest year) prices of ~70 commodities — crude oil
(average, Brent, Dubai, WTI), coal (Australia, South Africa, Colombia), natural gas (US Henry Hub, Europe TTF,
Japan LNG), fertilisers, metals and minerals (aluminium, iron ore, copper, lead, tin, nickel, zinc), precious
metals, agricultural goods — plus the World Bank commodity price indices (energy, non-energy, metals…), in
nominal US dollars and, for annual series, in real 2010 US dollars (deflated by the MUV index).

Source (researched 2026-10-17): the "Commodity Markets" page ``https://www.worldbank.org/en/research/commodity-markets``
links the current workbooks on ``thedocs.worldbank.org`` (the document id changes with every monthly release,
so the connector reads the two links from the archived page, never a hard-coded id):
``CMO-Historical-Data-Monthly.xlsx`` (sheets "Monthly Prices", "Monthly Indices", "Description",
"Index Weights", "Mismatch Details") and ``CMO-Historical-Data-Annual.xlsx`` ("Annual Prices (Nominal)",
"Annual Indices (Nominal)", "Annual Prices (Real)", "Annual Indices (Real)", "Description", "Definitions",
"Index Weights"). No key, no form.

Licence: the same page links "Data Access and Licensing" to ``datacatalog.worldbank.org/public-licenses#cc-by``
(Creative Commons Attribution 4.0); the run fails closed if the archived page no longer links the CC BY anchor.
The workbooks cite their own underlying providers per series (Bloomberg, OPEC, EIG, ICO, LME…) in the
"Description" sheet; the World Bank publishes the compiled series under CC BY 4.0 — the attribution is kept on
every table.

Cells (nothing computed):

* ``indicator`` = the column header verbatim, whitespace-trimmed (e.g. ``Crude oil, Brent``,
  ``Coal, South African **``); for index sheets the header is spread over three rows (group levels) and the
  indicator is the deepest non-empty header cell of the column.
* ``unit`` = the unit row verbatim plus the sheet subtitle that states the price basis, e.g.
  ``($/bbl) | annual prices, 1960 to present, real 2010 US dollars``; index sheets have no unit row and take the
  subtitle alone (``monthly indices based on nominal US dollars, 2010=100``).
* ``value`` = the cell text verbatim (the workbook's stored number; e.g. ``80.400000000000006`` is kept as
  written). ``…`` (the workbook's "not available" mark) and empty cells produce **no cell** (counted);
  spreadsheet error cells (``#VALUE!``, ``#N/A``) produce a value-less cell with
  ``flags.missing = provider_error`` and the error text — never a zero.
* header markers ``*`` / ``**`` are kept in the indicator and in ``flags.header_marker``; the "Description"
  sheet says ``* denotes forecast series`` (the World Bank also forecasts that series elsewhere; the values here
  are all historical) and lists series replacements under ``**`` — its notes are quoted in the coverage note.
* ``provider_geo`` = ``global_market`` (these are market prices at named delivery points, not country values;
  never mapped to a country). ``flags.workbook_updated`` = the sheet's own "Updated on …" line.

Atlas tables (source ``wb_cmo``): ``monthly_prices``, ``monthly_indices``, ``annual_prices_nominal``,
``annual_indices_nominal``, ``annual_prices_real``, ``annual_indices_real``.
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.wb_pinksheet"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas_pinksheet/1"
SOURCE_KEY = "worldbank.cmo"
SOURCE = "wb_cmo"
PAGE_HOST = "www.worldbank.org"
DOCS_HOST = "thedocs.worldbank.org"
PAGE_URL = f"https://{PAGE_HOST}/en/research/commodity-markets"
LICENCE_LINK = "https://datacatalog.worldbank.org/public-licenses#cc-by"
LICENCE = "CC-BY-4.0"
PROVIDER_GEO = "global_market"
XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "binary/octet-stream",
)
MAX_PAGE_BYTES = 3_000_000
MAX_XLSX_BYTES = 30_000_000
LINK_RE = re.compile(
    r"https://thedocs\.worldbank\.org/en/doc/[0-9a-f]{32}-[0-9]{7,12}/related/CMO-Historical-Data-(Monthly|Annual)\.xlsx"
)
DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
MONTH_RE = re.compile(r"^([0-9]{4})M(0[1-9]|1[0-2])$")
YEAR_RE = re.compile(r"^[0-9]{4}$")
NOT_AVAILABLE = {"…", "...", ".."}
ERROR_RE = re.compile(r"^#[A-Z0-9/!?]+$")


@dataclass(frozen=True)
class Sheet:
    dataset: str
    workbook: str  # "Monthly" | "Annual"
    sheet: str
    kind: str  # "prices" | "indices"
    freq: str  # "M" | "A"


SHEETS: tuple[Sheet, ...] = (
    Sheet("monthly_prices", "Monthly", "Monthly Prices", "prices", "M"),
    Sheet("monthly_indices", "Monthly", "Monthly Indices", "indices", "M"),
    Sheet("annual_prices_nominal", "Annual", "Annual Prices (Nominal)", "prices", "A"),
    Sheet("annual_indices_nominal", "Annual", "Annual Indices (Nominal)", "indices", "A"),
    Sheet("annual_prices_real", "Annual", "Annual Prices (Real)", "prices", "A"),
    Sheet("annual_indices_real", "Annual", "Annual Indices (Real)", "indices", "A"),
)
SHEETS_BY_DATASET = {s.dataset: s for s in SHEETS}


class PinkSheetParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="schema_drift")


# --------------------------------------------------------------------------- spec
def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="World Bank, Commodity Markets (Pink Sheet) — CMO historical data workbooks",
        licence=LICENCE,
        allowed_hosts=(PAGE_HOST, DOCS_HOST),
        datasets=tuple(s.dataset for s in SHEETS),
        dataset_ids=("CMO-Historical-Data-Monthly", "CMO-Historical-Data-Annual"),
        cadence_seconds=30 * 86_400,
        planned_requests=3,
        max_response_bytes=MAX_XLSX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{s.dataset}" for s in SHEETS], "geo": PROVIDER_GEO},
        notes="commodity-markets page (links + licence) then the monthly and annual CMO workbooks",
    )


def rights() -> dict[str, Any]:
    return {
        "licence": "CC BY 4.0",
        "licence_url": LICENCE_LINK,
        "terms_quote": "Data Access and Licensing (link on the Commodity Markets page to the World Bank public "
        "licenses page, anchor cc-by: Creative Commons Attribution 4.0)",
        "attribution": "Source: World Bank, Commodity Price Data (The Pink Sheet); underlying price providers per "
        "series are listed in the workbook's Description sheet.",
        "provider": "World Bank",
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


# --------------------------------------------------------------------------- landing page
def parse_page(body: bytes) -> dict[str, str]:
    """The two workbook URLs linked from the Commodity Markets page; fails closed without the CC BY link."""
    text = html.unescape(body.decode("utf-8", errors="replace"))
    if LICENCE_LINK not in text:
        raise PinkSheetParseError(f"commodity-markets page no longer links {LICENCE_LINK}: licence unverified")
    found: dict[str, set[str]] = {"Monthly": set(), "Annual": set()}
    for m in LINK_RE.finditer(text):
        found[m.group(1)].add(m.group(0))
    out: dict[str, str] = {}
    for k, urls in found.items():
        if len(urls) != 1:
            raise PinkSheetParseError(f"commodity-markets page: expected one {k} workbook link, found {sorted(urls)}")
        out[k] = next(iter(urls))
    return out


# --------------------------------------------------------------------------- workbook parsing
@dataclass
class SheetReport:
    dataset: str
    sheet: str
    columns: int = 0
    cells: int = 0
    not_available: int = 0
    empty: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    periods: list[str] = field(default_factory=list)
    updated: str | None = None
    unit_note: str | None = None
    indicators: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["indicators"] = len(self.indicators)
        return d


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _flags(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _period_bounds(label: str, freq: str) -> tuple[int, int, int, int]:
    if freq == "M":
        m = MONTH_RE.match(label)
        if not m:
            raise PinkSheetParseError(f"bad month label {label!r}")
        y, mo = int(m.group(1)), int(m.group(2))
        return y, mo, (y + 1 if mo == 12 else y), (1 if mo == 12 else mo + 1)
    if not YEAR_RE.match(label):
        raise PinkSheetParseError(f"bad year label {label!r}")
    y = int(label)
    return y, 1, y + 1, 1


def parse_sheet(table: list[list[Any]], sh: Sheet, raw_sha: str, file_name: str) -> tuple[pl.DataFrame, SheetReport]:
    """One CMO sheet (dense table) -> atlas frame."""
    rep = SheetReport(sh.dataset, sh.sheet)
    is_period = MONTH_RE.match if sh.freq == "M" else YEAR_RE.match
    first = next((i for i, r in enumerate(table) if r and r[0] and is_period(str(r[0]).strip())), None)
    if first is None or first < 3:
        raise PinkSheetParseError(f"{sh.sheet}: no data rows")
    title_rows = [_clean(r[0]) for r in table[:first] if r and r[0]]
    upd = [t for t in title_rows if t.lower().startswith("updated on")]
    rep.updated = upd[0] if upd else None
    if rep.updated is None:
        raise PinkSheetParseError(f"{sh.sheet}: no 'Updated on' line")
    subtitle = next((t for t in title_rows if not t.startswith(("World Bank Commodity", "Updated", "("))), "")
    if not subtitle:
        raise PinkSheetParseError(f"{sh.sheet}: no subtitle (price basis) line")
    width = max(len(r) for r in table[first:])
    header_rows = [r for r in table[:first] if r and not (r[0] and _clean(r[0]))]
    names: dict[int, str] = {}
    units: dict[int, str] = {}
    if sh.kind == "prices":
        # the header row is the last row before the unit row; the unit row's cells all start with "("
        unit_idx = max(
            (i for i in range(first) if sum(1 for c in table[i][1:] if _clean(c).startswith("(")) >= 3), default=None
        )
        if unit_idx is None or unit_idx == 0:
            raise PinkSheetParseError(f"{sh.sheet}: no unit row")
        for j in range(1, width):
            nm = _clean(table[unit_idx - 1][j] if j < len(table[unit_idx - 1]) else None)
            un = _clean(table[unit_idx][j] if j < len(table[unit_idx]) else None)
            if nm:
                names[j] = nm
                if not un:
                    raise PinkSheetParseError(f"{sh.sheet}: column {j} {nm!r} has no unit")
                units[j] = f"{un} | {subtitle}"
    else:
        rep.unit_note = subtitle
        for j in range(1, width):
            levels = [_clean(r[j]) for r in header_rows if j < len(r) and _clean(r[j])]
            if levels:
                names[j] = levels[-1]
                units[j] = subtitle
    if len(names) < 5:
        raise PinkSheetParseError(f"{sh.sheet}: only {len(names)} series columns")
    dup = [n for n, c in Counter(names.values()).items() if c > 1]
    if dup:
        raise PinkSheetParseError(f"{sh.sheet}: duplicate series headers {dup}")
    rep.columns = len(names)
    rep.indicators = [names[j] for j in sorted(names)]
    cols: dict[str, list[Any]] = {k: [] for k in ("indicator", "y0", "m0", "y1", "m1", "value", "flags", "unit", "sel")}
    labels: list[str] = []
    for i in range(first, len(table)):
        r = table[i]
        lab = _clean(r[0]) if r and r[0] else ""
        if not lab:
            continue
        if not is_period(lab):
            break  # notes below the data block
        labels.append(lab)
        y0, m0, y1, m1 = _period_bounds(lab, sh.freq)
        for j, nm in names.items():
            raw = r[j] if j < len(r) else None
            txt = (raw or "").strip()
            base: dict[str, Any] = {"workbook_updated": rep.updated}
            marker = re.search(r"(\*+)$", nm)
            if marker:
                base["header_marker"] = marker.group(1)
            if not txt:
                rep.empty += 1
                continue
            if txt in NOT_AVAILABLE:
                rep.not_available += 1
                continue
            if DECIMAL_RE.match(txt):
                value, fl = txt, base
            elif ERROR_RE.match(txt):
                rep.errors[txt] = rep.errors.get(txt, 0) + 1
                value, fl = None, {**base, "missing": "provider_error", "provider_code": txt}
            else:
                raise PinkSheetParseError(f"{sh.sheet}: unexpected cell {txt!r} at row {i + 1} column {j}")
            cols["indicator"].append(nm)
            cols["y0"].append(y0)
            cols["m0"].append(m0)
            cols["y1"].append(y1)
            cols["m1"].append(m1)
            cols["value"].append(value)
            cols["flags"].append(_flags(fl))
            cols["unit"].append(units[j])
            cols["sel"].append(
                _flags({"file": file_name, "sheet": sh.sheet, "row": i + 1, "column": j + 1, "period": lab})
            )
    if not labels:
        raise PinkSheetParseError(f"{sh.sheet}: no period")
    rep.periods = [labels[0], labels[-1]]
    df = pl.DataFrame(
        cols,
        schema={
            "indicator": pl.String,
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
        pl.lit(sh.dataset).alias("dataset"),
        "indicator",
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.lit(PROVIDER_GEO).alias("provider_geo"),
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
    rep.cells = out.height
    return out, rep


def description_notes(table: list[list[Any]]) -> list[str]:
    """The free-text notes under the series descriptions (marker meanings, replacements, corrections)."""
    out: list[str] = []
    for r in table:
        cells = [_clean(c) for c in r if _clean(c)]
        if not cells:
            continue
        if cells[0] in ("*", "**") and len(cells) == 2 and len(cells[1]) < 200:
            out.append(f"{cells[0]} {cells[1]}")
        elif len(cells) == 1 and re.match(r"^[A-Z][a-z]+ 20[0-9]{2}", cells[0]):
            out.append(cells[0])
    return out


def read_workbook(body: bytes, name: str) -> Workbook:
    try:
        return Workbook(body)
    except XlsxError as e:
        raise PinkSheetParseError(f"{name}: not a readable workbook ({e})") from e


def parse_workbook(
    body: bytes, which: str, raw_sha: str, datasets: set[str]
) -> tuple[dict[str, pl.DataFrame], dict[str, SheetReport], list[str]]:
    file_name = f"CMO-Historical-Data-{which}.xlsx"
    wb = read_workbook(body, file_name)
    sheets = wb._sheet_targets()
    frames: dict[str, pl.DataFrame] = {}
    reps: dict[str, SheetReport] = {}
    for sh in SHEETS:
        if sh.workbook != which or sh.dataset not in datasets:
            continue
        if sh.sheet not in sheets:
            raise PinkSheetParseError(f"{file_name}: sheet {sh.sheet!r} missing ({sorted(sheets)})")
        frames[sh.dataset], reps[sh.dataset] = parse_sheet(wb.table(sh.sheet), sh, raw_sha, file_name)
    notes = description_notes(wb.table("Description")) if "Description" in sheets else []
    return frames, reps, notes


def coverage_note(sh: Sheet, rep: SheetReport, url: str, notes: list[str]) -> str:
    parts = [
        f"World Bank Pink Sheet workbook {url}, sheet '{sh.sheet}' ({rep.updated}): {rep.columns} series, "
        f"{rep.cells} cells, periods {rep.periods[0]}-{rep.periods[1]}.",
        f"No cell for {rep.not_available} '…' (not available) and {rep.empty} empty cells; spreadsheet error "
        f"cells kept value-less: {rep.errors or 'none'}.",
        "provider_geo 'global_market': market prices at the delivery points named in the Description sheet, not "
        "country values.",
    ]
    if rep.unit_note:
        parts.append(f"Index basis (sheet subtitle): {rep.unit_note}.")
    if notes:
        parts.append("Description notes (verbatim): " + " | ".join(notes))
    parts.append("Licence CC BY 4.0 (World Bank). Cite: World Bank Commodity Price Data (The Pink Sheet).")
    return " ".join(parts)[:4000]


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = {s.dataset for s in SHEETS if run.wants(s.dataset)}
    raw_page = await run.get(
        PAGE_URL, key="page:commodity-markets", media_types=("text/html",), max_bytes=MAX_PAGE_BYTES
    )
    page_sha = run.archive(raw_page)
    links = parse_page(raw_page.body)
    out: dict[str, Any] = {"links": links, "tables": {}}
    for which in ("Monthly", "Annual"):
        if not any(SHEETS_BY_DATASET[d].workbook == which for d in wanted):
            continue
        url = links[which]
        raw = await run.get(url, key=f"workbook:{which}", media_types=XLSX_MEDIA, max_bytes=MAX_XLSX_BYTES)
        sha = run.archive(raw)
        frames, reps, notes = parse_workbook(raw.body, which, sha, wanted)
        del raw
        for ds, frame in frames.items():
            sh = SHEETS_BY_DATASET[ds]
            reg = run.register(
                dataset=ds,
                frame=frame,
                raw_sha256s=[sha, page_sha],
                coverage_note=coverage_note(sh, reps[ds], url, notes),
                rights=rights(),
            )
            out["tables"][ds] = {
                "dataset_key": reg["dataset_key"],
                "rows": reg["row_count"],
                "created": reg["created"],
                "report": reps[ds].as_dict(),
            }
    return out


__all__ = [
    "CONNECTOR_NAME",
    "SHEETS",
    "PinkSheetParseError",
    "atlas_ingest",
    "atlas_source",
    "parse_page",
    "parse_sheet",
    "parse_workbook",
]
