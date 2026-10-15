"""FAOSTAT bulk downloads -> atlas tables (V3, agent A-FAO): food, agriculture, land, forests, rural life.

Source: FAOSTAT bulk files, ``https://bulks-faostat.fao.org/production/<stem>_E_All_Data_(Normalized).zip``
(one zip per FAOSTAT domain; researched 2026-10-15). The domain listing
``https://bulks-faostat.fao.org/production/datasets_E.json`` gives each domain's file location, size and
``DateUpdate`` (archived with every run; the update date of each domain goes into the table's coverage note).
The FAOSTAT API (``faostatservices.fao.org/api/v1``) now requires an authorization token (HTTP 401
"Missing Authorization Header", checked 2026-10-15) and is not used.

Licence: FAO Statistical Database terms of use (https://www.fao.org/contact-us/terms/db-terms-of-use/en):
"Unless specified otherwise in their metadata or webpage, all datasets disseminated through FAO corporate
statistical databases ... are licensed under the Creative Commons Attribution-4.0 International licence
(CC BY 4.0)". Citation format: "FAO. <year of last update>. FAOSTAT: <domain>. Licence: CC-BY-4.0."
Some FAOSTAT domains *redistribute* other producers' series (UN WPP population, ILO employment, WHO /
UNICEF JMP water and nutrition series in the food-security suite, World Bank GDP): those rows carry FAO's
flag ``X`` ("value from external organization") verbatim and the domain's coverage note names the
original producer. They are kept FAO-labelled (``source = fao``) and are never presented as FAO's own.

Not used: FAO AQUASTAT direct (the FAO data catalog lists the AQUASTAT dissemination dataset as
CC BY-NC-SA 4.0, a non-commercial licence: its SDG 6.4.1/6.4.2 indicators are taken from the FAOSTAT SDG
domain instead, CC BY 4.0), FAOSTAT Trade Indices and the detailed trade matrix (0.4-1.6 GB of
item-by-partner detail; the agricultural trade *totals* come from the TCL domain), FishStat (per-species
production only; country totals would be REVA sums, see docs/dev/v3_a_fao.md).

Parsing (:func:`parse_domain`): the zip is read in memory (bounded by the spec's response cap); the
``*_All_Data_(Normalized).csv`` member is inflated as a stream, in blocks of complete lines, each block read
by polars as strings (no type inference) and filtered to the domain's bounded item / element lists. Every
kept row becomes one atlas cell:

* ``indicator`` = ``<item code>|<element code>`` (FAOSTAT codes; extra dimensions folded in:
  ``|sex=<code>``, ``|src=<code>``; domains with both an item and an indicator use
  ``<item>|<indicator>``);
* ``provider_geo`` = the FAOSTAT *Area Code*; ``iso3`` from the row's own ``Area Code (M49)`` through
  scheme ``fao_m49`` (``registry/atlas/geo_aliases/a_fao.yaml`` = crosswalk M49 + FAO's Channel Islands).
  The domain's ``*_AreaCodes.csv`` (FAO's own country definitions, archived inside the zip) must agree with
  the rows, else the parse is refused. FAO aggregates (World, regions, income groups, "China" = mainland +
  Hong Kong + Macao + Taiwan) and historical areas (USSR, Yugoslav SFR, Czechoslovakia, Ethiopia PDR,
  Sudan (former), Belgium-Luxembourg, Serbia and Montenegro, Netherlands Antilles (former), Yemen Ar Rp /
  Dem, Pacific Islands Trust Territory) stay unmapped and are listed in each manifest; Taiwan
  ("China, Taiwan Province of") is not a crosswalk entity and stays unmapped too;
* the period from ``Year`` (``2020`` -> 2020; FAOSTAT 3-year averages ``2000-2002`` -> [2000, 2003)) and,
  for monthly domains, ``Months Code`` (7001..7012 -> that month; 7021 = annual value);
* ``value`` = the reported number verbatim (strict decimal check, never rescaled); a reported
  non-numeric value (``<2.5`` undernourishment) goes to ``value_text``;
* ``flags`` = ``{"flag", "flag_meaning", "note"?, "estimate_kind"?}`` with the flag letter verbatim and its
  meaning from the domain's own ``*_Flags.csv``; ``estimate_kind`` = ``estimated`` (E), ``imputed`` (I),
  ``forecast`` (F) so estimated / imputed rows are kept but visibly flagged;
* rows flagged as missing (``L``, ``M``, ``O``, ``Q`` — "missing value", "data cannot exist",
  "suppressed") and rows with an empty value produce **no cell** (never a zero); they are counted in the
  table report.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import re
import sys
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.fao"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_fao/1"
SOURCE_KEY = "fao.faostat"
SOURCE = "fao"
HOST = "bulks-faostat.fao.org"
BASE = f"https://{HOST}/production"
LISTING_URL = f"{BASE}/datasets_E.json"
GEO_SCHEME = "fao_m49"
LICENCE = "CC BY 4.0 (FAO Statistical Database terms of use; third-party series redistributed by FAO flagged X)"
TERMS_URL = "https://www.fao.org/contact-us/terms/db-terms-of-use/en"
ZIP_MEDIA = ("application/x-zip-compressed", "application/zip", "application/octet-stream")
BLOCK_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 6_000_000_000  # TCL inflates to ~2.4 GB; refuse zip bombs beyond this
MAX_NOTE = 600
#: FAO flags that mean "no value" (FAOSTAT flag lists / SDMX observation status): no cell is produced.
MISSING_FLAGS = frozenset({"L", "M", "O", "Q"})
ESTIMATE_KIND = {"E": "estimated", "I": "imputed", "F": "forecast"}
DECIMAL_RE = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$"
YEAR_RE = r"^[0-9]{4}(-[0-9]{4})?$"
UNIT_NOT_STATED = "not stated by FAOSTAT (see indicator)"


class FaoParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


# --------------------------------------------------------------------------- domains
def _codes(*codes: int | str) -> frozenset[str]:
    return frozenset(str(c) for c in codes)


#: FBS / FBSH items: totals, groups, fish & seafood detail and staple commodities.
FBS_ITEMS = _codes(
    2901, 2903, 2941,  # Grand Total, Vegetal Products, Animal Products
    2905, 2907, 2908, 2909, 2911, 2912, 2913, 2914, 2918, 2919, 2922, 2923, 2924,  # vegetal groups
    2943, 2945, 2946, 2948, 2949, 2960, 2961,  # animal groups incl. Fish, Seafood / Aquatic Products, Other
    2761, 2762, 2763, 2764, 2765, 2766, 2767, 2769, 2775,  # fish & seafood detail, aquatic plants
    2511, 2513, 2514, 2805, 2807, 2531, 2532, 2542, 2555, 2577,  # wheat, barley, maize, rice, potatoes...
    2731, 2732, 2733, 2734, 2848,  # bovine, mutton & goat, pig, poultry meat; milk
)  # fmt: skip
#: food supply (kcal/cap/day, protein/fat g/cap/day, kg/cap/yr), production, trade, domestic supply, uses.
FBS_ELEMENTS = _codes(664, 674, 684, 645, 5511, 5611, 5911, 5301, 5142, 5521, 5123)
#: ~60 key crop / livestock items + FAO's own item aggregates (QCL codes, also used for producer prices).
QCL_ITEMS = _codes(
    15, 27, 56, 44, 83, 79, 71, 75, 116, 125, 122, 137, 236, 156, 157, 254, 242, 270, 267, 328, 767,
    656, 661, 667, 486, 489, 515, 490, 560, 388, 403, 260, 176, 187, 191, 826, 836, 577, 571, 1182,
    867, 1035, 1058, 977, 1017, 882, 1062, 987, 951, 1020,  # meat, milk, eggs, wool
    866, 946, 976, 1016, 1034, 1057, 1126, 1096, 1107,  # live animals (stocks)
    1717, 1720, 1723, 1726, 1729, 1730, 1732, 1735, 1738, 1753, 1765, 1780, 1783, 1806, 1807, 1808,
    1746, 1749, 2029, 1714, 1804,  # FAO aggregates: cereals, roots, pulses, meat total, milk total...
)  # fmt: skip
QV_ITEMS = _codes(2051, 2041, 2044, 2054, 2057, 1717, 1720, 1723, 1730, 1735, 1738, 1739, 1753, 1770, 1780)
GT_ELEMENTS = _codes(723113, 7225, 7230, 7273)  # CO2eq (AR5), CH4, N2O, CO2
FO_ITEMS = _codes(1861, 1862, 1863, 1864, 1865, 1868, 1872, 1873, 1875, 1876, 1630, 1877)
MK_ELEMENTS = _codes(6110, 6103, 6184, 6119, 6185, 6182, 6224)
TCL_ITEMS = _codes(1882)  # "Crops and livestock products" (FAO agricultural trade total)
TCL_ELEMENTS = _codes(5622, 5922)  # import value, export value (1000 USD)


@dataclass(frozen=True)
class Domain:
    """One FAOSTAT domain = one bulk zip = one atlas table (bounded by items / elements)."""

    code: str  # FAOSTAT domain code (datasets_E.json DatasetCode)
    dataset: str  # atlas dataset key
    stem: str  # bulk file stem
    title: str
    items: frozenset[str] | None = None  # None = every item
    elements: frozenset[str] | None = None  # None = every element
    monthly: bool = False  # keep monthly rows (Months Code 7001..7012) as month periods
    max_bytes: int = 100_000_000
    third_party: str = ""  # original producer(s) of redistributed series (coverage note)
    unit_rules: tuple[tuple[str, str], ...] = ()  # (substring of the item name, unit) when Unit is empty

    @property
    def file(self) -> str:
        return f"{self.stem}_E_All_Data_(Normalized).zip"

    @property
    def url(self) -> str:
        return f"{BASE}/{self.file}"


DOMAINS: tuple[Domain, ...] = (
    Domain("FBS", "fbs", "FoodBalanceSheets", "Food Balances (2010-)", FBS_ITEMS, FBS_ELEMENTS),
    Domain(
        "FBSH",
        "fbsh",
        "FoodBalanceSheetsHistoric",
        "Food Balances (-2013, old methodology and population)",
        FBS_ITEMS,
        FBS_ELEMENTS,
    ),
    Domain(
        "FS",
        "fs",
        "Food_Security_Data",
        "Suite of Food Security Indicators",
        third_party="WHO/UNICEF (JMP water & sanitation, child malnutrition, anaemia, obesity), World Bank "
        "(GDP per capita PPP, rail density) series redistributed by FAO, flagged X",
    ),
    Domain("QCL", "qcl", "Production_Crops_Livestock", "Production: Crops and livestock products", QCL_ITEMS),
    Domain(
        "QV",
        "qv",
        "Value_of_Production",
        "Value of Agricultural Production",
        QV_ITEMS,
        _codes(152, 57, 58),
    ),
    Domain(
        "PP",
        "pp",
        "Prices",
        "Producer Prices (annual values)",
        QCL_ITEMS,
        _codes(5530, 5532, 5539),
    ),
    Domain(
        "CP",
        "cp",
        "ConsumerPriceIndices",
        "Consumer Price Indices (monthly)",
        monthly=True,
        third_party="ILO / IMF national CPI series compiled by FAO",
        unit_rules=(("(2015 = 100)", "index (2015 = 100)"),),
    ),
    Domain(
        "TCL",
        "tcl",
        "Trade_CropsLivestock",
        "Trade: Crops and livestock products (agricultural trade totals)",
        TCL_ITEMS,
        TCL_ELEMENTS,
        max_bytes=400_000_000,
    ),
    Domain("TCLI", "tcli", "Trade_CropsLivestockIndicators", "Trade: Crops and livestock products indicators"),
    Domain("RL", "rl", "Inputs_LandUse", "Land Use"),
    Domain(
        "GT",
        "gt",
        "Emissions_Totals",
        "Agrifood systems emissions: Emissions totals",
        elements=GT_ELEMENTS,
        third_party="UNFCCC national inventories (rows with src=3051) redistributed by FAO",
    ),
    Domain("FO", "fo", "Forestry", "Forestry Production and Trade", FO_ITEMS),
    Domain("RFN", "rfn", "Inputs_FertilizersNutrient", "Fertilizers by Nutrient"),
    Domain("RP", "rp", "Inputs_Pesticides_Use", "Pesticides Use"),
    Domain(
        "OA",
        "oa",
        "Population",
        "Annual population (FAO-distributed UN WPP)",
        third_party="UN DESA World Population Prospects (FAO redistribution; A-UN ingests WPP at the source)",
    ),
    Domain(
        "OEA",
        "oea",
        "Employment_Indicators_Agriculture",
        "Employment Indicators: Agriculture and agrifood systems",
        third_party="ILO (ILOSTAT micro-data and ILO modelled estimates) redistributed by FAO; FAO model "
        "for agrifood-systems employment (src=3044)",
    ),
    Domain(
        "OER",
        "oer",
        "Employment_Indicators_Rural",
        "Employment Indicators: Rural",
        third_party="ILO (ILOSTAT) redistributed by FAO",
    ),
    Domain(
        "MK",
        "mk",
        "Macro-Statistics_Key_Indicators",
        "Macro Indicators (agriculture value added, GDP)",
        elements=MK_ELEMENTS,
        third_party="UN Statistics Division national accounts (AMA) redistributed by FAO, flagged X",
    ),
    Domain(
        "SDGB",
        "sdgb",
        "SDG_BulkDownloads",
        "SDG Indicators (FAO custodian: 2.1.1, 2.1.2, 2.4.1, 2.5, 2.a.1, 2.c.1, 6.4.1, 6.4.2, 12.3.1, 14.x, 15.x)",
    ),
)
DOMAINS_BY_DATASET = {d.dataset: d for d in DOMAINS}


# --------------------------------------------------------------------------- spec
def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="Food and Agriculture Organization of the United Nations (FAO), FAOSTAT",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(d.dataset for d in DOMAINS),
        dataset_ids=tuple(d.code for d in DOMAINS),
        cadence_seconds=7 * 86_400,
        planned_requests=len(DOMAINS) + 2,
        max_response_bytes=max(d.max_bytes for d in DOMAINS),
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{d.dataset}" for d in DOMAINS], "geo_scheme": GEO_SCHEME},
        notes="FAOSTAT bulk normalized zips (one per domain), bounded item/element lists; see atlas_fao.py",
    )


# --------------------------------------------------------------------------- listing
def parse_listing(body: bytes) -> dict[str, dict[str, Any]]:
    """``datasets_E.json`` -> {DatasetCode: {name, date_update, file_size, file_rows, file_location}}."""
    try:
        doc = json.loads(body)
        rows = doc["Datasets"]["Dataset"]
    except (ValueError, KeyError, TypeError) as e:
        raise FaoParseError(f"datasets_E.json: unexpected layout ({e})") from e
    out = {}
    for r in rows:
        out[str(r.get("DatasetCode"))] = {
            "name": r.get("DatasetName"),
            "date_update": r.get("DateUpdate"),
            "file_size": r.get("FileSize"),
            "file_rows": r.get("FileRows"),
            "file_location": r.get("FileLocation"),
        }
    return out


# --------------------------------------------------------------------------- zip helpers
def _decode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def _small_csv(zf: zipfile.ZipFile, suffix: str) -> list[dict[str, str]] | None:
    names = [n for n in zf.namelist() if n.endswith(suffix)]
    if not names:
        return None
    if len(names) > 1:
        raise FaoParseError(f"several members end with {suffix}: {names}")
    text, _enc = _decode(zf.read(names[0]))
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, (c.strip() for c in r), strict=False)) for r in rows[1:] if r]


def flag_meanings(zf: zipfile.ZipFile) -> dict[str, str]:
    rows = _small_csv(zf, "_Flags.csv") or []
    return {r.get("Flag", ""): r.get("Description", "") for r in rows if r.get("Flag")}


def area_codes(zf: zipfile.ZipFile) -> dict[str, tuple[str, str]]:
    """FAO Area Code -> (M49 code without the leading apostrophe, FAO area name)."""
    rows = _small_csv(zf, "_AreaCodes.csv") or []
    out = {}
    for r in rows:
        m49 = (r.get("M49 Code") or r.get("Area Code (M49)") or "").lstrip("'")
        out[r.get("Area Code", "")] = (m49, r.get("Area", ""))
    return out


def iter_blocks(zf: zipfile.ZipFile, member: str, block_bytes: int = BLOCK_BYTES) -> Iterator[tuple[bytes, bytes]]:
    """Yield ``(header_line, block)`` with ``block`` = complete data lines (streaming inflate, CRC checked)."""
    info = zf.getinfo(member)
    if info.file_size > MAX_MEMBER_BYTES:
        raise FaoParseError(f"{member}: {info.file_size} bytes uncompressed exceeds {MAX_MEMBER_BYTES}")
    with zf.open(member) as fh:
        header = fh.readline()
        if not header.endswith(b"\n"):
            raise FaoParseError(f"{member}: no header line")
        carry = b""
        while True:
            chunk = fh.read(block_bytes)
            if not chunk:
                break
            data = carry + chunk
            cut = data.rfind(b"\n")
            if cut < 0:
                carry = data
                continue
            carry = data[cut + 1 :]
            yield header, data[: cut + 1]
        if carry.strip():
            yield header, carry + b"\n"


# --------------------------------------------------------------------------- parsing
@dataclass
class DomainReport:
    domain: str
    member: str = ""
    encoding: str = "utf-8"
    rows_in_file: int = 0
    rows_in_scope: int = 0
    cells: int = 0
    text_cells: int = 0
    dropped_missing_flag: dict[str, int] = field(default_factory=dict)
    dropped_empty_value: int = 0
    dropped_months_not_kept: int = 0
    flags: dict[str, int] = field(default_factory=dict)
    indicators: dict[str, dict[str, str]] = field(default_factory=dict)
    areas: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _col(df: pl.DataFrame, *names: str) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _indicator_expr(df: pl.DataFrame) -> tuple[pl.Expr, dict[str, str | None]]:
    item = _col(df, "Item Code")
    ind = _col(df, "Indicator Code")
    elem = _col(df, "Element Code")
    parts: list[pl.Expr] = []
    roles: dict[str, str | None] = {"item": item, "indicator": ind, "element": elem}
    if item is not None:
        parts.append(pl.col(item))
    if ind is not None:
        parts.append(pl.col(ind))
    if not parts:
        raise FaoParseError("no 'Item Code' or 'Indicator Code' column")
    if elem is not None:
        parts.append(pl.col(elem))
    if "Sex Code" in df.columns:
        parts.append(pl.lit("sex=") + pl.col("Sex Code"))
    if "Source Code" in df.columns:
        parts.append(pl.lit("src=") + pl.col("Source Code"))
    return pl.concat_str(parts, separator="|"), roles


def _flags_json(flag: str, note: str, meanings: Mapping[str, str]) -> str:
    obj: dict[str, Any] = {}
    if flag:
        obj["flag"] = flag
        obj["flag_meaning"] = meanings.get(flag, "not listed in the domain's Flags.csv")
        if flag in ESTIMATE_KIND:
            obj["estimate_kind"] = ESTIMATE_KIND[flag]
    else:
        obj["flag"] = ""
        obj["flag_meaning"] = "no flag in the file"
    if note:
        obj["note"] = note[:MAX_NOTE]
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_block(
    domain: Domain, header: bytes, block: bytes, first_line: int, rep: DomainReport
) -> pl.DataFrame | None:
    text = header + block
    try:
        text.decode("utf-8")
        data = text
    except UnicodeDecodeError:
        data = text.decode("cp1252", errors="replace").encode("utf-8")
        rep.encoding = "cp1252"
    df = pl.read_csv(io.BytesIO(data), infer_schema=False, quote_char='"')
    n_lines = block.count(b"\n")
    if df.height != n_lines:
        raise FaoParseError(f"{domain.code}: block at line {first_line} has {df.height} rows for {n_lines} lines")
    rep.rows_in_file += df.height
    df = df.with_row_index("line", offset=first_line)
    for c in ("Area Code", "Area Code (M49)", "Year", "Value", "Flag"):
        if c not in df.columns:
            raise FaoParseError(f"{domain.code}: missing column {c!r} (header {df.columns})")
    item = _col(df, "Item Code", "Indicator Code")
    if domain.items is not None and item is not None:
        df = df.filter(pl.col(item).is_in(list(domain.items)))
    if domain.elements is not None and "Element Code" in df.columns:
        df = df.filter(pl.col("Element Code").is_in(list(domain.elements)))
    if "Months Code" in df.columns:
        keep = pl.col("Months Code") == "7021"
        if domain.monthly:
            keep = keep | pl.col("Months Code").is_in([str(m) for m in range(7001, 7013)])
        rep.dropped_months_not_kept += int(df.filter(~keep).height)
        df = df.filter(keep)
    if df.height == 0:
        return None
    rep.rows_in_scope += df.height
    df = df.with_columns(
        pl.col("Value").str.strip_chars().alias("Value"),
        pl.col("Flag").fill_null("").str.strip_chars().alias("Flag"),
        (pl.col("Note") if "Note" in df.columns else pl.lit("")).fill_null("").str.strip_chars().alias("_note"),
    )
    missing = df.filter(pl.col("Flag").is_in(list(MISSING_FLAGS)))
    for f, n in missing.group_by("Flag").len().iter_rows():
        rep.dropped_missing_flag[f] = rep.dropped_missing_flag.get(f, 0) + int(n)
    df = df.filter(~pl.col("Flag").is_in(list(MISSING_FLAGS)))
    empty = df.filter(pl.col("Value").is_null() | (pl.col("Value") == ""))
    rep.dropped_empty_value += empty.height
    df = df.filter(pl.col("Value").is_not_null() & (pl.col("Value") != ""))
    if df.height == 0:
        return None
    bad_year = df.filter(~pl.col("Year").str.contains(YEAR_RE))
    if bad_year.height:
        raise FaoParseError(f"{domain.code}: line {bad_year['line'][0]}: unexpected Year {bad_year['Year'][0]!r}")
    ind_expr, roles = _indicator_expr(df)
    df = df.with_columns(ind_expr.alias("_indicator"))
    # indicator labels (report / catalog)
    name_cols = [c for c in ("Item", "Indicator", "Element", "Sex", "Source", "Unit") if c in df.columns]
    for row in df.select(["_indicator", *name_cols]).unique(subset=["_indicator"]).iter_rows(named=True):
        rep.indicators.setdefault(row["_indicator"], {k.lower(): (row[k] or "") for k in name_cols})
    for code, m49, name, y in (
        df.group_by(["Area Code", "Area Code (M49)", "Area"] if "Area" in df.columns else ["Area Code", "Area Code (M49)"])
        .agg(pl.col("Year").min().alias("y0"), pl.col("Year").max().alias("y1"))
        .with_columns(pl.concat_list("y0", "y1").alias("y"))
        .select(
            "Area Code",
            "Area Code (M49)",
            pl.col("Area") if "Area" in df.columns else pl.lit(""),
            "y",
        )
        .iter_rows()
    ):
        a = rep.areas.setdefault(code, {"m49": (m49 or "").lstrip("'"), "name": name, "years": [y[0], y[1]]})
        if a["m49"] != (m49 or "").lstrip("'"):
            raise FaoParseError(f"{domain.code}: area {code} has two M49 codes ({a['m49']}, {m49})")
        a["years"] = [min(a["years"][0], y[0]), max(a["years"][1], y[1])]
    # periods
    y0 = pl.col("Year").str.slice(0, 4).cast(pl.Int32)
    y1 = pl.when(pl.col("Year").str.len_chars() == 9).then(pl.col("Year").str.slice(5, 4).cast(pl.Int32)).otherwise(y0)
    if "Months Code" in df.columns:
        month = pl.when(pl.col("Months Code") == "7021").then(None).otherwise(pl.col("Months Code").cast(pl.Int32) - 7000)
        start = pl.when(month.is_null()).then(pl.date(y0, 1, 1)).otherwise(pl.date(y0, month, 1))
        end = (
            pl.when(month.is_null())
            .then(pl.date(y1 + 1, 1, 1))
            .when(month == 12)
            .then(pl.date(y0 + 1, 1, 1))
            .otherwise(pl.date(y0, month + 1, 1))
        )
    else:
        start, end = pl.date(y0, 1, 1), pl.date(y1 + 1, 1, 1)
    numeric = pl.col("Value").str.contains(DECIMAL_RE)
    rep.text_cells += int(df.filter(~numeric).height)
    # units
    unit = pl.col("Unit").fill_null("").str.strip_chars() if "Unit" in df.columns else pl.lit("")
    fallback: pl.Expr = pl.lit(UNIT_NOT_STATED)
    name_col = _col(df, "Item", "Indicator")
    for sub, u in reversed(domain.unit_rules):
        if name_col is not None:
            fallback = pl.when(pl.col(name_col).str.contains(sub, literal=True)).then(pl.lit(u)).otherwise(fallback)
    df = df.with_columns(
        pl.when(unit == "").then(fallback).otherwise(unit).alias("_unit"),
        start.alias("_start"),
        end.alias("_end"),
        numeric.alias("_numeric"),
    )
    for f, n in df.group_by("Flag").len().iter_rows():
        rep.flags[f] = rep.flags.get(f, 0) + int(n)
    return df.select(
        "line",
        "_indicator",
        pl.col("Area Code").alias("_geo"),
        pl.col("Area Code (M49)").str.strip_chars().str.strip_chars_start("'").alias("_m49"),
        "_start",
        "_end",
        "Value",
        "_numeric",
        "_unit",
        "Flag",
        "_note",
    )


def fao_area_iso3(m49: str | None) -> str | None:
    """Crosswalk key of a FAO area from its M49 code (``'004``/``004``/``4`` alike); aggregates -> None."""
    code = (m49 or "").strip().lstrip("'")
    if not code.isdigit():  # FAO groupings use non-numeric codes such as 159.03 / 097.1203
        return None
    return country_iso3(str(int(code)), GEO_SCHEME)


def parse_domain(domain: Domain, body: bytes, raw_sha256: str) -> tuple[pl.DataFrame, DomainReport]:
    """One FAOSTAT bulk zip -> (atlas frame, report). Raises :class:`FaoParseError` on any layout surprise."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise FaoParseError(f"{domain.file}: not a zip archive ({e})") from e
    members = [n for n in zf.namelist() if n.endswith("_All_Data_(Normalized).csv")]
    if len(members) != 1:
        raise FaoParseError(f"{domain.file}: expected one *_All_Data_(Normalized).csv member, found {members}")
    member = members[0]
    rep = DomainReport(domain=domain.code, member=member)
    meanings = flag_meanings(zf)
    defs = area_codes(zf)
    parts: list[pl.DataFrame] = []
    line = 2  # file line of the first data row (header = line 1)
    try:
        for header, block in iter_blocks(zf, member):
            got = _parse_block(domain, header, block, line, rep)
            line += block.count(b"\n")
            if got is not None:
                parts.append(got)
    except (zipfile.BadZipFile, EOFError) as e:  # CRC mismatch / truncated member
        raise FaoParseError(f"{domain.file}: corrupt member {member} ({e})") from e
    if not parts:
        raise FaoParseError(f"{domain.file}: no cell in scope (items/elements filter matched nothing)")
    df = pl.concat(parts, how="vertical")
    # FAO's own country definitions (AreaCodes.csv) must agree with the rows
    for code, a in rep.areas.items():
        if defs and code in defs and defs[code][0] != a["m49"]:
            raise FaoParseError(f"{domain.code}: area {code} M49 {a['m49']} != AreaCodes.csv {defs[code][0]}")
        if defs and code not in defs:
            a["not_in_area_codes_file"] = True
    geo_map = {code: fao_area_iso3(a["m49"]) for code, a in rep.areas.items()}
    for code, iso in geo_map.items():
        rep.areas[code]["iso3"] = iso
    flag_keys = df.select("Flag", "_note").unique()
    fmap = {f"{f}\x1f{n}": _flags_json(f, n, meanings) for f, n in flag_keys.iter_rows()}
    selector = pl.format(
        '{{"file":"{}","row":{},"zip":"{}"}}', pl.lit(member), pl.col("line"), pl.lit(domain.file)
    )
    out = df.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(domain.dataset).alias("dataset"),
        pl.col("_indicator").alias("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.col("_geo").replace_strict(geo_map, default=None, return_dtype=pl.String).alias("iso3"),
        pl.col("_geo").alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.col("_start").alias("period_start"),
        pl.col("_end").alias("period_end"),
        pl.when(pl.col("_numeric")).then(pl.col("Value")).otherwise(None).alias("value"),
        pl.when(pl.col("_numeric")).then(None).otherwise(pl.col("Value").str.slice(0, 4000)).alias("value_text"),
        pl.col("_unit").str.slice(0, 100).alias("unit"),
        (pl.col("Flag") + pl.lit("\x1f") + pl.col("_note"))
        .replace_strict(fmap, return_dtype=pl.String)
        .alias("flags"),
        pl.lit(raw_sha256).alias("raw_sha256"),
        selector.alias("selector"),
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
    dup = out.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
    if dup:
        raise FaoParseError(f"{domain.code}: {dup} duplicate (indicator, area, period) rows")
    rep.cells = out.height
    return out, rep


def coverage_note(domain: Domain, rep: DomainReport, listing: Mapping[str, Any] | None, etag: str | None) -> str:
    li = listing or {}
    unm = sorted(
        (f"{c} {a['name']} [{a['years'][0]}-{a['years'][1]}]" for c, a in rep.areas.items() if not a.get("iso3")),
    )
    hist = [u for u in unm if any(k in u for k in HISTORICAL_AREA_NAMES)]
    parts = [
        f"FAOSTAT {domain.code} '{domain.title}', bulk file {domain.file}"
        f" (FAOSTAT DateUpdate {li.get('date_update')}, ETag {etag}).",
        f"Scope: items {'all' if domain.items is None else len(domain.items)}, elements "
        f"{'all' if domain.elements is None else sorted(domain.elements)}.",
        f"{rep.cells} cells from {rep.rows_in_scope} rows in scope; no cell for {sum(rep.dropped_missing_flag.values())}"
        f" rows flagged missing {rep.dropped_missing_flag} and {rep.dropped_empty_value} empty values.",
        "Flags verbatim with FAO meaning in flags; E/I/F rows kept with flags.estimate_kind.",
    ]
    if domain.third_party:
        parts.append(f"Third-party series redistributed by FAO: {domain.third_party}.")
    if hist:
        parts.append("Historical areas (unmapped): " + "; ".join(hist) + ".")
    parts.append(f"Unmapped FAO areas: {len(unm)} (aggregates, historical areas, Taiwan).")
    parts.append(f"Licence CC BY 4.0, cite: FAO. FAOSTAT: {domain.title}. {TERMS_URL}")
    return " ".join(parts)[:4000]


#: FAOSTAT historical areas (kept unmapped; candidates for KG historical-state entities).
HISTORICAL_AREA_NAMES = (
    "USSR",
    "Yugoslav SFR",
    "Czechoslovakia",
    "Ethiopia PDR",
    "Sudan (former)",
    "Belgium-Luxembourg",
    "Serbia and Montenegro",
    "Netherlands Antilles (former)",
    "Yemen Ar Rp",
    "Yemen Dem",
    "Pacific Islands Trust Territory",
)


def rights(domain: Domain) -> dict[str, Any]:
    return {
        "licence": "CC-BY-4.0",
        "licence_url": TERMS_URL,
        "attribution": f"FAO. FAOSTAT: {domain.title}. Licence: CC-BY-4.0.",
        "provider": "FAO (FAOSTAT)",
        "third_party_series": domain.third_party or None,
        "licence_checked": "2026-10-15",
        "source_key": SOURCE_KEY,
    }


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the domain listing and every wanted domain zip; archive; parse; register (one table each)."""
    wanted = [d for d in DOMAINS if run.wants(d.dataset)]
    raw_listing = await run.get(LISTING_URL, key="listing:datasets_E", media_types=("application/json",))
    listing_sha = run.archive(raw_listing)
    listing = parse_listing(raw_listing.body)
    out: dict[str, Any] = {"listing_sha256": listing_sha, "tables": {}}
    for d in wanted:
        li = listing.get(d.code)
        if li is None:
            raise FaoParseError(f"domain {d.code} is not in datasets_E.json any more")
        raw = await run.get(
            d.url,
            key=f"{d.code}:{li.get('date_update')}",
            media_types=ZIP_MEDIA,
            max_bytes=d.max_bytes,
            params={"domain": d.code, "date_update": li.get("date_update")},
        )
        sha = run.archive(raw)
        frame, rep = parse_domain(d, raw.body, sha)
        etag = (raw.response_metadata.get("headers") or {}).get("etag")
        del raw
        reg = run.register(
            dataset=d.dataset,
            frame=frame,
            raw_sha256s=[sha, listing_sha],
            coverage_note=coverage_note(d, rep, li, etag),
            rights=rights(d),
        )
        out["tables"][d.dataset] = {
            "dataset_key": reg["dataset_key"],
            "rows": reg["row_count"],
            "countries": reg["coverage"]["countries"],
            "created": reg["created"],
            "report": {k: v for k, v in rep.as_dict().items() if k not in ("indicators", "areas")},
            "unmapped": sorted(f"{c} {a['name']}" for c, a in rep.areas.items() if not a.get("iso3")),
        }
    return out


__all__ = [
    "CONNECTOR_NAME",
    "DOMAINS",
    "DOMAINS_BY_DATASET",
    "Domain",
    "DomainReport",
    "FaoParseError",
    "atlas_ingest",
    "atlas_source",
    "parse_domain",
    "parse_listing",
]
