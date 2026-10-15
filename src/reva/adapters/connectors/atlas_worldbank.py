"""World Bank atlas layer (V3, agent A-WB): every economy, every indicator, from bulk files.

Sources (all public, no key; licences per series from the providers' own metadata, see
``registry/sources/worldbank_atlas.yaml``):

* DataBank bulk CSV ZIPs (``https://databank.worldbank.org/data/download/<X>_CSV.zip``, which
  redirects to ``databankfiles.worldbank.org``): WDI, Worldwide Governance Indicators, Gender
  Statistics, Health Nutrition and Population Statistics, Education Statistics, Jobs, Doing
  Business (archive; discontinued 2021) and International Debt Statistics. One request per
  database; each ZIP holds a wide data CSV plus Series/Country/footnote metadata CSVs.
* Poverty and Inequality Platform API (``api.worldbank.org/pip/v1/pip``): survey-year country
  estimates (``fill_gaps=false``: no interpolated/nowcast years) for a pinned PIP release and
  the 2021-PPP international poverty lines, one CSV request per line.
* Indicator metadata in French (``api.worldbank.org/v2/fr/source/<id>/indicator``), one request
  per source, used only for catalog labels (the English metadata comes from the ZIPs).

Every response is archived content-addressed as an ``evidence.acquisition`` **before** it is
parsed (``publish_acquisition`` with zero observation batches), then parsed member by member in
memory (never extracted to disk) into the atlas long schema (``reva.atlas.tables``) and
registered with ``register_atlas_table``: one atlas table per source database (WDI is split by
World Bank topic into ``wdi_econ`` / ``wdi_social`` to keep each table under ~5 M rows).

Parsing rules (honesty first):

* An empty cell is **absent** (no row), never zero. Values are kept as the provider's decimal
  text (all observed values are strict decimals; anything else would be kept as
  ``value_text`` with a flag, never coerced).
* Geography: the provider code (whitespace padding stripped, e.g. IDS ``"AFG       "``) maps to
  a crosswalk entity through the ``wb`` scheme, else through ``iso3`` (WGI reports a few
  territories without a World Bank economy, e.g. ``AIA``, ``REU``; flagged
  ``geo_via: iso3``). World Bank aggregates (regions, income and lending groups -- the
  crosswalk's ``excluded_worldbank_aggregates`` plus codes the database's own Country file
  marks as aggregates) are **excluded** and listed with their cell counts in the table's
  ``coverage_note``. Other codes that do not map (``TWN``, ``ANT``, Doing Business city
  codes such as ``CHN_BEI``) are **kept** with ``iso3 = null`` and listed in the manifest.
* Flags per cell: the provider footnote (``footnote``), the provider series-year note
  (``series_year_note``), ``period_after_vintage: true`` when the year is later than the
  year of the data file's own timestamp (projections, e.g. EdStats to 2100, IDS future debt
  service to 2032), and for PIP the survey descriptors.
* Selector: ``{"file": <zip member>, "row": <1-based data record>, "column": "<year>"}``.

The live run downloads ~680 MB; see ``docs/dev/v3_a_wb.md`` and the budget request
``registry/atlas/budget_requests/a_wb.yaml``.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import inspect
import io
import json
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.worldbank"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_wb/1"
SOURCE_KEY = "worldbank.atlas"
SOURCE = "wb"  # atlas `source` column
LICENCE = "CC-BY-4.0 (per-series licence in the provider metadata; exceptions listed in the catalog)"
ALLOWED_HOSTS = ("databank.worldbank.org", "databankfiles.worldbank.org", "api.worldbank.org")
DATABANK = "https://databank.worldbank.org/data/download/"
API = "https://api.worldbank.org/"

#: Strict decimal accepted by reva.atlas.tables (kept in sync; checked there again).
DECIMAL_RE = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$"
_DECIMAL = re.compile(DECIMAL_RE)
MAX_FLAG_TEXT = 1500  # characters of one provider note kept in a cell's flags (atlas cap 4000)

# PIP: pinned release, 2021 PPP; international poverty lines (US$ 2021 PPP per day).
PIP_VERSION = "20260922_2021_01_02_PROD"
PIP_POVERTY_LINES = ("3.00", "4.20", "8.30")


@dataclass(frozen=True)
class BulkDatabase:
    key: str  # atlas dataset key (or prefix for split databases)
    title: str
    zip_name: str
    api_source_id: int
    max_bytes: int
    #: WB topic (first segment) -> dataset key, for databases split into several atlas tables.
    split: Mapping[str, str] | None = None
    note: str = ""

    @property
    def url(self) -> str:
        return DATABANK + self.zip_name

    def datasets(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.split.values()))) if self.split else (self.key,)


_WDI_SPLIT = {
    "Economic Policy & Debt": "wdi_econ",
    "Financial Sector": "wdi_econ",
    "Private Sector & Trade": "wdi_econ",
    "Public Sector": "wdi_econ",
    "Trade": "wdi_econ",
    "Infrastructure": "wdi_econ",
    "Policy & institutions": "wdi_econ",
    "Health": "wdi_social",
    "Environment": "wdi_social",
    "Social Protection & Labor": "wdi_social",
    "Education": "wdi_social",
    "Poverty": "wdi_social",
    "Gender": "wdi_social",
}
WDI_OTHER = "wdi_other"  # any topic not listed above (none in the 2026-10 release)

DATABASES: tuple[BulkDatabase, ...] = (
    BulkDatabase("wdi", "World Development Indicators", "WDI_CSV.zip", 2, 450_000_000, _WDI_SPLIT),
    BulkDatabase("wgi", "Worldwide Governance Indicators", "WGI_CSV.zip", 3, 30_000_000),
    BulkDatabase("gender", "Gender Statistics", "Gender_Stats_CSV.zip", 14, 300_000_000),
    BulkDatabase("hnp", "Health Nutrition and Population Statistics", "HNP_Stats_CSV.zip", 16, 250_000_000),
    BulkDatabase(
        "edstats",
        "Education Statistics",
        "EdStats_CSV.zip",
        12,
        100_000_000,
        note="The bulk ZIP is the 2018 release (members dated 2018-01-22); the API source 12 lists "
        "more series (8,450 on 2026-10-15). Years after 2018 are projections (Wittgenstein Centre/IIASA).",
    ),
    BulkDatabase("jobs", "Jobs", "Jobs_CSV.zip", 25, 80_000_000),
    BulkDatabase(
        "doingbusiness",
        "Doing Business (archive)",
        "DB_CSV.zip",
        1,
        20_000_000,
        note="Doing Business was discontinued in September 2021 after data irregularities in the "
        "DB2018/DB2020 reports were reported; this archive is kept as published (DB2020 data, years "
        "2003-2019) and must not be read as a current measure. Includes city-level rows (unmapped).",
    ),
    BulkDatabase(
        "ids",
        "International Debt Statistics",
        "IDS_CSV.zip",
        6,
        60_000_000,
        note="Bulk file holds counterpart-area World (WLD) totals only; years after the release are "
        "projected contractual debt service (flagged period_after_vintage).",
    ),
)
DATABASES_BY_KEY = {d.key: d for d in DATABASES}


def _code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(inspect.getmodule(_code_digest)).encode()).hexdigest()  # type: ignore[arg-type]


def all_datasets() -> tuple[str, ...]:
    out: list[str] = []
    for d in DATABASES:
        out.extend(d.datasets())
        if d.split:
            out.append(WDI_OTHER)
    return (*out, "pip")


def atlas_source() -> Any:
    """The atlas source of this module (``reva.atlas.sources`` protocol)."""
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="World Bank",
        licence=LICENCE,
        allowed_hosts=ALLOWED_HOSTS,
        datasets=all_datasets(),
        dataset_ids=(*(f"{d.zip_name} (API source {d.api_source_id})" for d in DATABASES), f"PIP {PIP_VERSION}"),
        cadence_seconds=30 * 86400,
        planned_requests=len(fetch_items()),
        max_response_bytes=max(d.max_bytes for d in DATABASES),
        min_interval_seconds=1.0,
        budget_key=SOURCE_KEY,
        parser_version=PARSER_VERSION,
        code_digest=_code_digest(),
        field_mappings={
            "databases": {d.key: list(d.datasets()) for d in DATABASES},
            "bulk_item_max_bytes": {d.key: d.max_bytes for d in DATABASES},
            "pip_version": PIP_VERSION,
            "pip_poverty_lines": list(PIP_POVERTY_LINES),
        },
        notes="DataBank bulk CSV ZIPs + PIP API + French indicator metadata; aggregates excluded and listed.",
    )


# --------------------------------------------------------------------------- fetch plan
def bulk_item(db: BulkDatabase) -> FetchItem:
    return FetchItem(
        key=f"bulk:{db.key}",
        url=db.url,
        expected_media_types=("application/x-zip-compressed", "application/zip", "application/octet-stream"),
        max_bytes=db.max_bytes,
    )


def fr_item(db: BulkDatabase) -> FetchItem:
    return FetchItem(
        key=f"fr:{db.key}",
        url=f"{API}v2/fr/source/{db.api_source_id}/indicator?format=json&per_page=20000",
        max_bytes=52_428_800,
    )


def pip_item(line: str) -> FetchItem:
    return FetchItem(
        key=f"pip:{line}",
        url=(f"{API}pip/v1/pip?country=all&year=all&povline={line}&fill_gaps=false&version={PIP_VERSION}&format=csv"),
        expected_media_types=("text/csv",),
        max_bytes=52_428_800,
    )


def fetch_items(
    databases: Iterable[str] | None = None, *, include_pip: bool = True, include_fr: bool = True
) -> tuple[FetchItem, ...]:
    keys = list(databases) if databases is not None else [d.key for d in DATABASES]
    unknown = [k for k in keys if k not in DATABASES_BY_KEY]
    if unknown:
        raise ValueError(f"unknown World Bank databases {unknown}; known: {sorted(DATABASES_BY_KEY)}")
    items: list[FetchItem] = []
    for k in keys:
        items.append(bulk_item(DATABASES_BY_KEY[k]))
        if include_fr:
            items.append(fr_item(DATABASES_BY_KEY[k]))
    if include_pip:
        items.extend(pip_item(line) for line in PIP_POVERTY_LINES)
    return tuple(items)


def check_body(item: FetchItem, body: bytes) -> None:
    head = body[:256].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html")):
        raise SourceResponseError("HTML page received instead of data", retryable=True, kind="html_body")
    if item.key.startswith("bulk:") and not body.startswith(b"PK"):
        raise SourceResponseError("bulk download is not a ZIP archive", retryable=False, kind="not_zip")


# --------------------------------------------------------------------------- CSV helpers
def decode(data: bytes) -> tuple[str, str]:
    """Text of a CSV member: UTF-8 (BOM stripped) or, failing that, Windows-1252 (IDS)."""
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def _header(text: str) -> list[str]:
    return next(csv.reader(io.StringIO(text.split("\n", 1)[0])), [])


def _dict_rows(text: str) -> list[dict[str, str]]:
    return [{(k or "").strip(): (v or "") for k, v in r.items()} for r in csv.DictReader(io.StringIO(text))]


def _year_columns(cols: Sequence[str]) -> list[str]:
    return [c for c in cols if re.fullmatch(r"\d{4}", c.strip())]


@dataclass
class Members:
    data: str
    series: str | None = None
    country: str | None = None
    footnote: str | None = None
    series_time: str | None = None
    country_series: str | None = None
    texts: dict[str, str] = field(default_factory=dict)  # member -> decoded text
    encodings: dict[str, str] = field(default_factory=dict)
    timestamps: dict[str, str] = field(default_factory=dict)


def read_members(body: bytes) -> Members:
    """Classify the ZIP members by their header row (file names differ between databases)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise SourceResponseError("bulk download is not a readable ZIP", retryable=False) from e
    roles: dict[str, str] = {}
    texts: dict[str, str] = {}
    encs: dict[str, str] = {}
    stamps: dict[str, str] = {}
    for info in z.infolist():
        if info.is_dir():
            continue
        if info.file_size > 1_000_000_000:
            raise SourceResponseError(f"ZIP member {info.filename} declares {info.file_size} bytes", retryable=False)
        text, enc = decode(z.read(info))
        hdr = [h.strip() for h in _header(text)]
        name = info.filename
        if "Country Code" in hdr and ("Indicator Code" in hdr or "Series Code" in hdr) and _year_columns(hdr):
            role = "data"
        elif hdr[:1] in (["Series Code"], ["Code"]) and "Indicator Name" in hdr:
            role = "series"
        elif hdr[:1] in (["Country Code"], ["Code"]) and "Region" in hdr:
            role = "country"
        elif hdr[:4] == ["CountryCode", "SeriesCode", "Year", "DESCRIPTION"] or (
            hdr[:5] == ["Type", "Country Code", "Series Code", "Time Code", "Description"]
        ):
            role = "footnote"
        elif hdr[:3] == ["SeriesCode", "Year", "DESCRIPTION"]:
            role = "series_time"
        elif hdr[:3] in (["CountryCode", "SeriesCode", "DESCRIPTION"], ["Type", "Country Code", "Series Code"]):
            role = "country_series"
        else:
            role = f"other:{name}"
        if role in roles:
            raise SourceResponseError(
                f"two ZIP members look like the {role} file: {roles[role]}, {name}", retryable=False
            )
        roles[role] = name
        texts[name] = text
        encs[name] = enc
        y, mo, d, *_ = info.date_time
        stamps[name] = f"{y:04d}-{mo:02d}-{d:02d}"
    if "data" not in roles:
        raise SourceResponseError(
            "no data CSV (Country Code, Indicator/Series Code, years) in the ZIP", retryable=False
        )
    return Members(
        data=roles["data"],
        series=roles.get("series"),
        country=roles.get("country"),
        footnote=roles.get("footnote"),
        series_time=roles.get("series_time"),
        country_series=roles.get("country_series"),
        texts=texts,
        encodings=encs,
        timestamps=stamps,
    )


# --------------------------------------------------------------------------- metadata
@dataclass(frozen=True)
class SeriesMeta:
    code: str
    name: str
    topic: str
    unit: str  # provider "Unit of measure" ('' when blank)
    short_definition: str
    long_definition: str
    source_note: str  # provider "Source" field (upstream organisation)
    licence: str
    periodicity: str
    aggregation: str


def parse_series_meta(text: str | None) -> dict[str, SeriesMeta]:
    if not text:
        return {}
    out: dict[str, SeriesMeta] = {}
    for r in _dict_rows(text):
        code = (r.get("Series Code") or r.get("Code") or "").strip()
        if not code:
            continue
        out[code] = SeriesMeta(
            code=code,
            name=_clean(r.get("Indicator Name")),
            topic=_clean(r.get("Topic")),
            unit=_clean(r.get("Unit of measure")),
            short_definition=_clean(r.get("Short definition")),
            long_definition=_clean(r.get("Long definition")),
            source_note=_clean(r.get("Source")),
            licence=_clean(r.get("License Type")),
            periodicity=_clean(r.get("Periodicity")),
            aggregation=_clean(r.get("Aggregation method")),
        )
    return out


def parse_country_meta(text: str | None) -> dict[str, dict[str, str]]:
    if not text:
        return {}
    out = {}
    for r in _dict_rows(text):
        code = (r.get("Country Code") or r.get("Code") or "").strip()
        if code:
            out[code] = {
                "name": _clean(r.get("Table Name") or r.get("Long Name") or r.get("Short Name")),
                "region": _clean(r.get("Region")),
                "income_group": _clean(r.get("Income Group")),
            }
    return out


_PAREN_TAIL = re.compile(r"\(([^()]{1,98})\)\s*$")


def unit_for(code: str, meta: SeriesMeta | None, name: str) -> tuple[str, str]:
    """(unit, how): the provider's unit of measure, else the trailing parenthesis of the
    provider's indicator name (the World Bank naming convention, e.g. ``(% of GDP)``), else
    ``"unspecified"``. Never inferred from values."""
    if meta and meta.unit and len(meta.unit) <= 100:
        return meta.unit, "provider_unit_of_measure"
    m = _PAREN_TAIL.search(name or (meta.name if meta else ""))
    if m:
        return m.group(1).strip(), "indicator_name_parenthesis"
    _ = code
    return "unspecified", "none"


# --------------------------------------------------------------------------- geography
@dataclass
class GeoMap:
    mapped: dict[str, str]  # provider code -> crosswalk key
    via: dict[str, str]  # provider code -> 'wb' | 'iso3'
    aggregates: dict[str, str]  # provider code -> label (excluded)
    unmapped: dict[str, str]  # provider code -> label (kept, iso3 null)


def crosswalk_aggregates() -> set[str]:
    from reva.atlas.tables import CROSSWALK_PATH

    return set(json.loads(CROSSWALK_PATH.read_text())["excluded_worldbank_aggregates"])


def map_geographies(
    codes: Iterable[str], country_meta: Mapping[str, Mapping[str, str]], names: Mapping[str, str] | None = None
) -> GeoMap:
    """Map provider codes: ``wb`` scheme, else ``iso3``; aggregates excluded; others unmapped."""
    from reva.atlas.tables import country_iso3

    aggs = crosswalk_aggregates()
    gm = GeoMap({}, {}, {}, {})
    for code in sorted(set(codes)):
        label = (country_meta.get(code) or {}).get("name") or (names or {}).get(code, "")
        key = country_iso3(code, "wb")
        if key is not None:
            gm.mapped[code], gm.via[code] = key, "wb"
            continue
        if code in aggs:
            gm.aggregates[code] = label
            continue
        key = country_iso3(code, "iso3")
        if key is not None:
            gm.mapped[code], gm.via[code] = key, "iso3"
            continue
        # Not guessed further: a dissolved territory (ANT) has no region either, so "no region"
        # is not evidence of an aggregate. Kept with iso3 null and listed.
        gm.unmapped[code] = label
    return gm


# --------------------------------------------------------------------------- notes -> frames
def _norm_year(col: pl.Expr) -> pl.Expr:
    return col.str.extract(r"(\d{4})", 1).cast(pl.Int32, strict=False)


def _trunc_json(s: str) -> str:
    s = _clean(s)
    if len(s) > MAX_FLAG_TEXT:
        s = s[: MAX_FLAG_TEXT - 1] + "…"
    return json.dumps(s, ensure_ascii=False)


def _json_strings(col: pl.Series) -> pl.Series:
    uniq = col.drop_nulls().unique().to_list()
    mapping = {u: _trunc_json(u) for u in uniq}
    return col.replace_strict(mapping, default=None, return_dtype=pl.String)


def footnote_frame(text: str | None) -> pl.DataFrame | None:
    """(indicator, provider_geo, year, fn_json) from a footnote member (two layouts)."""
    if not text:
        return None
    df = pl.read_csv(io.StringIO(text).getvalue().encode(), infer_schema=False, truncate_ragged_lines=True)
    df = df.rename({c: c.strip() for c in df.columns})
    if "CountryCode" in df.columns:
        f = df.select(
            pl.col("SeriesCode").str.strip_chars().alias("indicator"),
            pl.col("CountryCode").str.strip_chars().alias("provider_geo"),
            _norm_year(pl.col("Year")).alias("year"),
            pl.col("DESCRIPTION").alias("fn"),
        )
    else:  # IDS: "Afghanistan (AFG)", "Name (CODE)", "2024 (YR2024)"
        f = df.select(
            pl.col("Series Code").str.extract(r"\(([^()]*)\)\s*$", 1).alias("indicator"),
            pl.col("Country Code").str.extract(r"\(([^()]*)\)\s*$", 1).alias("provider_geo"),
            _norm_year(pl.col("Time Code")).alias("year"),
            pl.col("Description").alias("fn"),
        )
    f = f.filter(pl.all_horizontal(pl.col("indicator", "provider_geo", "year", "fn").is_not_null()))
    f = f.filter(pl.col("fn").str.strip_chars() != "")
    # One note per cell: several notes for one cell are joined (provider order kept).
    f = f.group_by(["indicator", "provider_geo", "year"], maintain_order=True).agg(pl.col("fn").str.join(" | "))
    return f.with_columns(_json_strings(f["fn"]).alias("fn_json")).drop("fn")


def series_time_frame(text: str | None) -> pl.DataFrame | None:
    if not text:
        return None
    df = pl.read_csv(text.encode(), infer_schema=False, truncate_ragged_lines=True)
    df = df.rename({c: c.strip() for c in df.columns})
    f = df.select(
        pl.col("SeriesCode").str.strip_chars().alias("indicator"),
        _norm_year(pl.col("Year")).alias("year"),
        pl.col("DESCRIPTION").alias("st"),
    ).filter(pl.col("indicator").is_not_null() & pl.col("year").is_not_null() & pl.col("st").is_not_null())
    f = f.group_by(["indicator", "year"], maintain_order=True).agg(pl.col("st").str.join(" | "))
    return f.with_columns(_json_strings(f["st"]).alias("st_json")).drop("st")


# --------------------------------------------------------------------------- the parse
@dataclass
class ParsedTable:
    dataset: str
    frame: pl.DataFrame
    coverage_note: str


@dataclass
class ParsedDatabase:
    key: str
    raw_sha256: str
    tables: list[ParsedTable]
    series: dict[str, SeriesMeta]
    units: dict[str, tuple[str, str]]  # code -> (unit, how)
    datasets: dict[str, str]  # code -> atlas dataset (WDI split)
    geo: GeoMap
    excluded_aggregate_cells: dict[str, int]
    report: dict[str, Any]


def _json_flags(parts: Sequence[pl.Expr]) -> pl.Expr:
    """``{"k":v,...}`` from expressions yielding ``"k":v`` fragments or null (keys pre-sorted)."""
    return pl.concat_str([pl.lit("{"), pl.concat_list(parts).list.drop_nulls().list.join(","), pl.lit("}")])


def parse_bulk(db: BulkDatabase, body: bytes, raw_sha256: str) -> ParsedDatabase:
    """Parse one DataBank bulk ZIP into atlas frames (see the module docstring for the rules)."""
    from reva.atlas.tables import ATLAS_COLUMNS

    m = read_members(body)
    series = parse_series_meta(m.texts.get(m.series or ""))
    countries = parse_country_meta(m.texts.get(m.country or ""))
    # Release decoded member texts as soon as they are parsed (peak memory ~ ZIP + one long frame).
    fn = footnote_frame(m.texts.pop(m.footnote) if m.footnote else None)
    st = series_time_frame(m.texts.pop(m.series_time) if m.series_time else None)
    data_bytes = m.texts.pop(m.data).encode()
    m.texts.clear()
    raw = pl.read_csv(data_bytes, infer_schema=False, truncate_ragged_lines=True)
    del data_bytes
    raw = raw.select([c for c in raw.columns if c.strip()])
    raw = raw.rename({c: c.strip() for c in raw.columns})
    code_col = "Indicator Code" if "Indicator Code" in raw.columns else "Series Code"
    name_col = "Indicator Name" if "Indicator Name" in raw.columns else "Series Name"
    years = _year_columns(raw.columns)
    counterpart = "Counterpart-Area Code" in raw.columns
    raw = raw.with_row_index("row", offset=1).with_columns(
        pl.col("Country Code").str.strip_chars().alias("geo"),
        pl.col(code_col).str.strip_chars().alias("code"),
        (pl.col("Counterpart-Area Code").str.strip_chars() if counterpart else pl.lit(None, pl.String)).alias("cp"),
    )
    is_footer = pl.col("geo").is_null() | (pl.col("geo") == "") | pl.col("code").is_null()
    footer_rows = raw.filter(is_footer).height
    raw = raw.filter(~is_footer)
    data_rows = raw.height
    names = dict(raw.select("geo", "Country Name").unique("geo", keep="first").rows())
    geo = map_geographies(names, countries, names)
    aggs = sorted(geo.aggregates)
    # Indicator names (for units when the metadata gives none) of indicators with kept cells.
    inames = dict(raw.filter(~pl.col("geo").is_in(aggs)).select("code", name_col).unique("code", keep="first").rows())

    # Unpivot a few year columns at a time and drop empty cells at once (bounded peak memory).
    pieces = []
    for i in range(0, len(years), 8):
        cols = years[i : i + 8]
        pieces.append(
            raw.select(["row", "geo", "code", "cp", *cols])
            .unpivot(index=["row", "geo", "code", "cp"], on=cols, variable_name="column", value_name="v")
            .with_columns(pl.col("v").str.strip_chars())
            .filter(pl.col("v").is_not_null() & (pl.col("v") != ""))
        )
    long = pl.concat(pieces).with_columns(pl.col("column").cast(pl.Int32).alias("year"))
    del pieces
    del raw
    total_cells = long.height
    agg_cells = long.filter(pl.col("geo").is_in(aggs)).group_by("geo").len().sort("geo").rows() if aggs else []
    long = long.filter(~pl.col("geo").is_in(aggs))
    kept_codes = set(long["code"].unique().to_list())
    inames = {c: n for c, n in inames.items() if c in kept_codes}

    # Units and split assignment per indicator code (metadata first, else the data file's name).
    units = {c: unit_for(c, series.get(c), inames.get(c, "")) for c in sorted(inames)}
    split_of: dict[str, str] = {}
    for c in units:
        if db.split:
            sm = series.get(c)
            topic = (sm.topic if sm else "").split(":")[0].strip()
            split_of[c] = db.split.get(topic, WDI_OTHER)
        else:
            split_of[c] = db.key

    # Values: strict decimals stay as reported; anything else is kept as value_text, flagged.
    strict = pl.col("v").str.contains(DECIMAL_RE)
    nonstrict_n = long.filter(~strict).height
    lz = long.lazy()
    del long
    lz = lz.with_columns(
        pl.when(strict).then(pl.col("v")).otherwise(None).alias("value"),
        pl.when(strict).then(None).otherwise(pl.col("v")).alias("value_text"),
    )

    # Flags.
    vintage = int(m.timestamps[m.data][:4])
    if fn is not None:
        lz = lz.join(
            fn.lazy().rename({"indicator": "code", "provider_geo": "geo"}), on=["code", "geo", "year"], how="left"
        )
    else:
        lz = lz.with_columns(pl.lit(None, pl.String).alias("fn_json"))
    if st is not None:
        lz = lz.join(st.lazy().rename({"indicator": "code"}), on=["code", "year"], how="left")
    else:
        lz = lz.with_columns(pl.lit(None, pl.String).alias("st_json"))
    via_iso3 = [g for g, how in geo.via.items() if how == "iso3"]
    flag_parts = [
        pl.when(pl.col("fn_json").is_not_null()).then(pl.concat_str([pl.lit('"footnote":'), pl.col("fn_json")])),
        pl.when(pl.col("geo").is_in(via_iso3)).then(pl.lit('"geo_via":"iso3"')),
        pl.when(pl.col("year") > vintage).then(pl.lit('"period_after_vintage":true')),
        pl.when(pl.col("st_json").is_not_null()).then(
            pl.concat_str([pl.lit('"series_year_note":'), pl.col("st_json")])
        ),
        pl.when(pl.col("value_text").is_not_null()).then(pl.lit('"value_not_strict_decimal":true')),
    ]
    sel_parts = [pl.lit('{"column":"'), pl.col("column")]
    if counterpart:
        sel_parts += [pl.lit('","counterpart":"'), pl.col("cp")]
    sel_parts += [pl.lit(f'","file":{json.dumps(m.data)},"row":'), pl.col("row").cast(pl.String), pl.lit("}")]
    indicator = (
        pl.concat_str([pl.col("code"), pl.lit("|counterpart="), pl.col("cp")]) if counterpart else pl.col("code")
    )
    start = pl.date(pl.col("year"), 1, 1)
    end = pl.date(pl.col("year") + 1, 1, 1)
    unit_map = {c: u for c, (u, _) in units.items()}
    out_lz = lz.select(
        pl.lit(SOURCE).alias("source"),
        pl.col("code").replace_strict(split_of, return_dtype=pl.String).alias("dataset"),
        indicator.alias("indicator"),
        pl.concat_str([pl.lit("wb."), pl.col("code").str.to_lowercase()]).alias("concept_id"),
        pl.col("geo").replace_strict(geo.mapped, default=None, return_dtype=pl.String).alias("iso3"),
        pl.col("geo").alias("provider_geo"),
        pl.lit(None, pl.String).alias("entity_qid"),
        start.alias("period_start"),
        end.alias("period_end"),
        pl.col("value"),
        pl.col("value_text"),
        pl.col("code").replace_strict(unit_map, return_dtype=pl.String).alias("unit"),
        _json_flags(flag_parts).alias("flags"),
        pl.lit(raw_sha256).alias("raw_sha256"),
        pl.concat_str(sel_parts).alias("selector"),
    )
    # concept_id hints must match the catalog id pattern; codes that do not are left without a hint.
    out_lz = out_lz.with_columns(
        pl.when(pl.col("concept_id").str.contains(r"^[a-z0-9][a-z0-9_.-]{0,199}$"))
        .then(pl.col("concept_id"))
        .otherwise(None)
        .alias("concept_id")
    ).select(list(ATLAS_COLUMNS))
    out = out_lz.collect(engine="streaming")
    del lz, fn, st

    excluded = {g: int(n) for g, n in agg_cells}
    base_note = {
        "database": db.title,
        "file": m.data,
        "file_date": m.timestamps[m.data],
        "encoding": m.encodings[m.data],
        "excluded_aggregates": excluded,
        "geo_via_iso3": sorted(via_iso3),
        "provider_geo": "Country Code with whitespace padding stripped",
        "empty_cells": "absent (no row)",
    }
    if db.note:
        base_note["note"] = db.note
    tables = []
    kept_cells = out.height
    parts = {k[0]: v for k, v in out.partition_by("dataset", as_dict=True).items()}
    del out
    for ds in sorted(parts):
        f = parts.pop(ds)
        note = dict(base_note)
        if db.split:
            note["split"] = {
                "by": "World Bank topic (first segment)",
                "topics": sorted(t for t, d in db.split.items() if d == ds) or ["(topics not listed in the split)"],
            }
        tables.append(ParsedTable(ds, f, _note_json(note)))
    report = {
        "database": db.key,
        "raw_sha256": raw_sha256,
        "members": dict(sorted(m.timestamps.items())),
        "encodings": {k: v for k, v in sorted(m.encodings.items()) if v != "utf-8"},
        "data_rows": data_rows,
        "footer_rows_skipped": footer_rows,
        "indicators": len(units),
        "provider_cells_total": total_cells,
        "excluded_aggregate_cells": sum(excluded.values()),
        "kept_cells": kept_cells,
        "nonstrict_values": nonstrict_n,
        "vintage_year": vintage,
        "tables": {t.dataset: t.frame.height for t in tables},
        "geo": {
            "mapped": len(geo.mapped),
            "via_iso3": sorted(via_iso3),
            "aggregates_excluded": sorted(geo.aggregates),
            "unmapped_kept": {k: v for k, v in sorted(geo.unmapped.items())},
        },
    }
    return ParsedDatabase(db.key, raw_sha256, tables, series, units, split_of, geo, excluded, report)


def _note_json(note: Mapping[str, Any]) -> str:
    s = json.dumps(note, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(s) > 4000:  # atlas coverage_note cap: shorten the aggregate list to codes only
        note = {**note, "excluded_aggregates": sorted(note.get("excluded_aggregates", {}))}
        s = json.dumps(note, sort_keys=True, separators=(",", ":"), ensure_ascii=False)[:4000]
    return s


# --------------------------------------------------------------------------- PIP
PIP_LINE_MEASURES = ("headcount", "poverty_gap", "poverty_severity", "watts", "pop_in_poverty")
PIP_DIST_MEASURES = (
    "mean", "median", "mld", "gini", "polarization",
    *(f"decile{i}" for i in range(1, 11)),
    "spl", "spr", "pg", "cpi", "ppp", "reporting_pop", "reporting_gdp", "reporting_pce",
)  # fmt: skip
PIP_UNITS = {
    "headcount": "share of population (0-1)",
    "poverty_gap": "ratio (0-1)",
    "poverty_severity": "ratio (0-1)",
    "watts": "index",
    "pop_in_poverty": "persons",
    "mean": "2021 PPP US$ per day",
    "median": "2021 PPP US$ per day",
    "mld": "index",
    "gini": "index (0-1)",
    "polarization": "index (0-1)",
    "spl": "2021 PPP US$ per day",
    "spr": "share of population (0-1)",
    "pg": "index",
    "cpi": "index (provider base)",
    "ppp": "LCU per international $ (2021)",
    "reporting_pop": "persons",
    "reporting_gdp": "2021 PPP US$ per capita (provider)",
    "reporting_pce": "2021 PPP US$ per capita (provider)",
    **{f"decile{i}": "share of welfare (0-1)" for i in range(1, 11)},
}
PIP_FLAG_COLS = (
    "survey_acronym", "survey_coverage", "survey_year", "survey_comparability", "comparable_spell",
    "is_interpolated", "distribution_type", "estimation_type", "estimate_type",
)  # fmt: skip


def pip_concept_id(indicator: str) -> str:
    """Catalog concept of a folded PIP indicator: ``headcount|povline=3.00|level=national|welfare=income``
    -> ``wb.pip.headcount.pl3_00.national.income``."""
    measure, *dims = indicator.split("|")
    d = dict(x.split("=", 1) for x in dims)
    parts = [measure] + ([f"pl{d['povline'].replace('.', '_')}"] if "povline" in d else []) + [d["level"], d["welfare"]]
    return "wb.pip." + ".".join(re.sub(r"[^a-z0-9]+", "_", p.lower()).strip("_") for p in parts)


def parse_pip(bodies: Mapping[str, tuple[bytes, str]]) -> ParsedDatabase:
    """PIP survey-year estimates: ``{poverty_line: (csv_bytes, raw_sha256)}`` -> one atlas table.

    Indicator codes fold the dimensions: ``headcount|povline=3.00|level=national|welfare=consumption``
    for poverty-line measures, ``gini|level=national|welfare=income`` for distribution measures
    (taken from the first poverty-line file and checked identical in the others). Period:
    ``reporting_year``; the survey year and survey descriptors are cell flags.
    """
    from reva.atlas.tables import ATLAS_COLUMNS

    frames = []
    dist_ref: pl.DataFrame | None = None
    report: dict[str, Any] = {"database": "pip", "version": PIP_VERSION, "files": {}}
    for line, (body, sha) in sorted(bodies.items()):
        text, _ = decode(body)
        df = pl.read_csv(text.encode(), infer_schema=False)
        need = {"country_code", "reporting_year", "reporting_level", "welfare_type", "poverty_line"}
        if not need <= set(df.columns):
            raise SourceResponseError(f"PIP CSV lacks columns {sorted(need - set(df.columns))}", retryable=False)
        df = df.with_row_index("row", offset=1)
        bad_line = df.filter(pl.col("poverty_line").cast(pl.Float64) != float(line))
        if bad_line.height:
            raise SourceResponseError(f"PIP file for line {line} has other poverty lines", retryable=False)
        report["files"][line] = {"rows": df.height, "raw_sha256": sha}
        dims = pl.concat_str(
            [pl.lit("|level="), pl.col("reporting_level"), pl.lit("|welfare="), pl.col("welfare_type")]
        )
        flag_expr = pl.concat_str(
            [
                pl.lit("{"),
                pl.concat_list(
                    [
                        pl.when(pl.col(c).is_not_null() & (pl.col(c) != "")).then(
                            pl.concat_str([pl.lit(f'"{c}":"'), pl.col(c).str.replace_all('"', "'"), pl.lit('"')])
                        )
                        for c in PIP_FLAG_COLS
                        if c in df.columns
                    ]
                    + [pl.lit(f'"pip_version":"{PIP_VERSION}"')]
                )
                .list.drop_nulls()
                .list.join(","),
                pl.lit("}"),
            ]
        )
        measures = [c for c in PIP_LINE_MEASURES if c in df.columns]
        dist = [c for c in PIP_DIST_MEASURES if c in df.columns]
        base = df.with_columns(dims.alias("dims"), flag_expr.alias("flags"))

        def melt(
            cols: list[str], with_line: bool, f: pl.DataFrame = base, ln: str = line, s: str = sha
        ) -> pl.DataFrame:
            u = f.select(["row", "country_code", "reporting_year", "dims", "flags", *cols]).unpivot(
                index=["row", "country_code", "reporting_year", "dims", "flags"],
                on=cols,
                variable_name="measure",
                value_name="v",
            )
            u = u.with_columns(pl.col("v").str.strip_chars()).filter(pl.col("v").is_not_null() & (pl.col("v") != ""))
            mid = pl.lit(f"|povline={ln}") if with_line else pl.lit("")
            return u.with_columns(
                pl.concat_str([pl.col("measure"), mid, pl.col("dims")]).alias("indicator"),
                pl.lit(s).alias("raw_sha256"),
                pl.concat_str(
                    [
                        pl.lit('{"column":"'),
                        pl.col("measure"),
                        pl.lit(f'","povline":"{ln}","row":'),
                        pl.col("row").cast(pl.String),
                        pl.lit("}"),
                    ]
                ).alias("selector"),
            )

        frames.append(melt(measures, True))
        d = melt(dist, False)
        if dist_ref is None:
            dist_ref = d
            frames.append(d)
        else:
            a = dist_ref.select("indicator", "country_code", "reporting_year", "v").sort(
                "indicator", "country_code", "reporting_year"
            )
            b = d.select("indicator", "country_code", "reporting_year", "v").sort(
                "indicator", "country_code", "reporting_year"
            )
            if not a.equals(b):
                raise SourceResponseError(
                    "PIP distribution measures differ between poverty-line files (inconsistent release)",
                    retryable=False,
                )
    long = pl.concat(frames)
    # Numeric text: PIP writes plain decimals; anything else (TRUE/FALSE, NA) stays value_text.
    long = long.with_columns(pl.col("v").str.replace(r"^NA$", ""))
    long = long.filter(pl.col("v") != "")
    strict = pl.col("v").str.contains(DECIMAL_RE)
    codes = long["country_code"].unique().to_list()
    geo = map_geographies(codes, {}, {})
    year = pl.col("reporting_year").cast(pl.Int32)
    concept_of = {i: pip_concept_id(i) for i in long["indicator"].unique().to_list()}
    out = long.filter(~pl.col("country_code").is_in(list(geo.aggregates))).select(
        pl.lit(SOURCE).alias("source"),
        pl.lit("pip").alias("dataset"),
        pl.col("indicator"),
        pl.col("indicator").replace_strict(concept_of, return_dtype=pl.String).alias("concept_id"),
        pl.col("country_code").replace_strict(geo.mapped, default=None, return_dtype=pl.String).alias("iso3"),
        pl.col("country_code").alias("provider_geo"),
        pl.lit(None, pl.String).alias("entity_qid"),
        pl.date(year, 1, 1).alias("period_start"),
        pl.date(year + 1, 1, 1).alias("period_end"),
        pl.when(strict).then(pl.col("v")).alias("value"),
        pl.when(strict).then(None).otherwise(pl.col("v")).alias("value_text"),
        pl.col("measure").replace_strict(PIP_UNITS, default="unspecified", return_dtype=pl.String).alias("unit"),
        pl.col("flags"),
        pl.col("raw_sha256"),
        pl.col("selector"),
    )
    dup = out.select(pl.struct(["indicator", "provider_geo", "period_start"]).is_duplicated().sum()).item()
    if dup:
        # Two surveys reported for the same (level, welfare, reporting year): fold the survey year in.
        raise SourceResponseError(f"PIP: {dup} cells share (indicator, country, reporting_year)", retryable=False)
    out = out.select(list(ATLAS_COLUMNS))
    units = {i: (PIP_UNITS.get(i.split("|")[0], "unspecified"), "pip_column") for i in sorted(concept_of)}
    report.update(
        {
            "kept_cells": out.height,
            "indicators": out["indicator"].n_unique(),
            "geo": {"mapped": len(geo.mapped), "aggregates_excluded": sorted(geo.aggregates),
                    "unmapped_kept": geo.unmapped},
        }
    )  # fmt: skip
    note = _note_json(
        {
            "database": "Poverty and Inequality Platform (survey-year estimates, fill_gaps=false)",
            "pip_version": PIP_VERSION,
            "poverty_lines_2021ppp": list(PIP_POVERTY_LINES),
            "period": "reporting_year; survey_year in flags",
            "excluded_aggregates": {},
        }
    )
    return ParsedDatabase(
        "pip", ",".join(sorted(s for _, s in bodies.values())), [ParsedTable("pip", out, note)], {}, units,
        {i: "pip" for i in units}, geo, {}, report,
    )  # fmt: skip


# --------------------------------------------------------------------------- French labels
def parse_fr_labels(body: bytes) -> dict[str, dict[str, str]]:
    """``{code: {"name": .., "note": ..}}`` from ``/v2/fr/source/<id>/indicator`` (one page)."""
    doc = json.loads(body)
    if not (isinstance(doc, list) and len(doc) == 2 and isinstance(doc[1], list)):
        raise SourceResponseError("World Bank indicator list: unexpected payload shape", retryable=False)
    head = doc[0]
    if int(head.get("pages", 1)) != 1 or int(head.get("total", len(doc[1]))) != len(doc[1]):
        raise SourceResponseError("World Bank indicator list is paged or truncated", retryable=False)
    return {
        str(r["id"]): {"name": _clean(r.get("name")), "note": _clean(r.get("sourceNote"))}
        for r in doc[1]
        if isinstance(r, dict) and r.get("id")
    }


# --------------------------------------------------------------------------- ingest
def _wanted(run: Any, db: BulkDatabase) -> list[str]:
    ds = list(db.datasets()) + ([WDI_OTHER] if db.split else [])
    return [d for d in ds if run.wants(d)]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch (through ``run.get``), archive, parse and register the World Bank atlas tables.

    Databases are processed one at a time and released before the next download (peak memory
    ~ one ZIP + its long frame, ~5 GB for WDI). A failed item is reported under ``blocked`` with
    a typed reason and never registered; budget refusals propagate (typed run outcome)."""
    from reva.operations.budgets import BudgetExhausted

    out: dict[str, Any] = {"acquisitions": {}, "tables": {}, "reports": {}, "blocked": []}

    async def get(item: FetchItem) -> Any:
        try:
            raw = await run.get(item.url, key=item.key, media_types=item.expected_media_types, max_bytes=item.max_bytes)
            check_body(item, raw.body)
        except BudgetExhausted:
            raise
        except Exception as e:  # typed source errors are reported, never registered
            out["blocked"].append({"item": item.key, "error": type(e).__name__, "detail": str(e)[:300]})
            return None
        return raw

    def archive(item: FetchItem, raw: Any) -> str:
        sha = run.archive(raw)
        out["acquisitions"][item.key] = {
            "raw_sha256": sha,
            "bytes": len(raw.body),
            "retrieved_at": raw.retrieved_at.isoformat(),
            "last_modified": raw.response_metadata.get("last-modified"),
        }
        return str(sha)

    def register(p: ParsedDatabase, raw_shas: list[str]) -> None:
        for t in p.tables:
            if not run.wants(t.dataset):
                continue
            summary = run.register(
                dataset=t.dataset, frame=t.frame, raw_sha256s=raw_shas, coverage_note=t.coverage_note
            )
            cov = summary["coverage"]
            out["tables"][t.dataset] = {
                k: summary[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created")
            } | {
                "countries": cov["countries"],
                "indicators": cov["indicators"],
                "period_min": cov["period_min"],
                "period_max": cov["period_max"],
                "unmapped_provider_geo": summary["unmapped_provider_geo"],
            }

    for db in DATABASES:
        if not _wanted(run, db):
            continue
        item = bulk_item(db)
        raw = await get(item)
        if raw is None:
            continue
        sha = await asyncio.to_thread(archive, item, raw)
        try:
            parsed = await asyncio.to_thread(parse_bulk, db, raw.body, sha)
        except SourceResponseError as e:
            out["blocked"].append({"item": item.key, "error": "parse_failed", "detail": str(e)[:300]})
            continue
        finally:
            del raw
        out["reports"][db.key] = parsed.report
        await asyncio.to_thread(register, parsed, [sha])
        del parsed
        # French labels (catalog input only): archived, checked parseable, not an atlas table.
        fitem = fr_item(db)
        fraw = await get(fitem)
        if fraw is not None:
            await asyncio.to_thread(archive, fitem, fraw)
            try:
                out["reports"][db.key]["fr_labels"] = len(parse_fr_labels(fraw.body))
            except (SourceResponseError, ValueError) as e:
                out["blocked"].append({"item": fitem.key, "error": "parse_failed", "detail": str(e)[:300]})
    if run.wants("pip"):
        bodies: dict[str, tuple[bytes, str]] = {}
        for line in PIP_POVERTY_LINES:
            item = pip_item(line)
            raw = await get(item)
            if raw is None:
                continue
            bodies[line] = (raw.body, await asyncio.to_thread(archive, item, raw))
        if len(bodies) == len(PIP_POVERTY_LINES):
            try:
                parsed = await asyncio.to_thread(parse_pip, bodies)
            except SourceResponseError as e:
                out["blocked"].append({"item": "pip", "error": "parse_failed", "detail": str(e)[:300]})
            else:
                out["reports"]["pip"] = parsed.report
                await asyncio.to_thread(register, parsed, sorted(s for _, s in bodies.values()))
        elif bodies:
            out["blocked"].append(
                {"item": "pip", "error": "incomplete", "detail": "not every poverty line was fetched"}
            )
    return out


__all__ = [
    "CONNECTOR_NAME",
    "DATABASES",
    "PIP_VERSION",
    "SOURCE",
    "SOURCE_KEY",
    "BulkDatabase",
    "atlas_ingest",
    "atlas_source",
    "fetch_items",
    "map_geographies",
    "parse_bulk",
    "parse_fr_labels",
    "parse_pip",
]
