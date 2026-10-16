"""U.S. EIA International Energy Data (bulk file) -> atlas tables (V3, agent A-RESOURCES).

Production, consumption, imports, exports, reserves (coal), capacity, generation and energy CO2 of every
country and year, in the physical units EIA publishes (thousand barrels per day, billion cubic feet / metres,
1000 metric tons and thousand short tons of coal, billion kWh, million kW, quadrillion Btu…).

Source (researched 2026-10-15): the EIA open-data bulk file ``https://www.eia.gov/opendata/bulk/INTL.zip``
(one member ``INTL.txt``: one JSON object per line, a series or a category; ~24 MB zipped, 129 MB inflated;
no API key — the EIA API v2 needs one, the bulk files do not). The bulk manifest
``https://www.eia.gov/opendata/bulk/manifest.txt`` (archived) gives the INTL ``last_updated`` and its access
URL, which must be the URL fetched. EIA's own notation for non-numeric cells is read from the archived
international data browser footnotes page ``https://www.eia.gov/international/content/info/footnotes.html``:
``--`` = Not applicable, ``NA`` = Not available, ``W`` = Data withheld to avoid disclosure, ``ie`` = Data are
included elsewhere for this time period.

Licence: U.S. public domain ("U.S. government publications are in the public domain and are not subject to
copyright protection", https://www.eia.gov/about/copyrights_reuse.php); every INTL series carries
``"copyright": "None"``; a series with any other copyright value is flagged on each of its cells.

Atlas tables (annual series only; EIA's few monthly/quarterly OECD stock and trade series are not taken):

* ``intl_petroleum`` (crude oil, NGPL, other liquids, refined products, LPG/ethane, stocks),
* ``intl_gas`` (gross, dry, vented/flared and reinjected natural gas),
* ``intl_coal`` (coal and its ranks, metallurgical coke, coal reserves),
* ``intl_electricity`` (generation by source, capacity by source, consumption, trade, losses),
* ``intl_energy`` (total primary energy, by fuel; biofuels; energy CO2 emissions; intensity per capita / GDP).

Not taken: GDP at PPP (4701) and population (4702) — inputs EIA takes from other producers (World Bank /
UN / IMF are ingested at their source by other agents); the ``TJ`` and ``MTOE`` versions of series also
published in quadrillion Btu (EIA's own unit conversions of the same quantity).

Cells: ``indicator`` = EIA's ``geoset_id`` (the series id without its geography, e.g.
``INTL.57-1-TBPD.A`` = crude oil incl. lease condensate production, thousand barrels per day);
``flags.series_last_updated`` = the series' own ``last_updated`` stamp (EIA revises series individually);
``provider_geo`` = EIA's geography code (ISO 3166 alpha-3 plus EIA aggregates and historical areas; scheme
``eia`` = ISO3 + ``XKS`` Kosovo); ``value`` = the JSON number text verbatim (``json`` parsed with
``parse_float=str``), zeros reported by EIA are kept; ``--`` / ``NA`` / JSON null produce **no cell** (counted
in the report); ``ie`` and ``W`` produce a value-less cell with ``flags.missing`` and the provider code and
meaning (never a zero). Aggregate rows carry ``flags.eia_members`` (EIA's member list of the aggregate).
"""

from __future__ import annotations

import hashlib
import html
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

CONNECTOR_NAME = "atlas.eia_intl"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_eia/1"
SOURCE_KEY = "eia.opendata_bulk"
SOURCE = "eia"
HOST = "www.eia.gov"
GEO_SCHEME = "eia"
ZIP_URL = f"https://{HOST}/opendata/bulk/INTL.zip"
MANIFEST_URL = f"https://{HOST}/opendata/bulk/manifest.txt"
FOOTNOTES_URL = f"https://{HOST}/international/content/info/footnotes.html"
TERMS_URL = "https://www.eia.gov/about/copyrights_reuse.php"
MEMBER = "INTL.txt"
LICENCE = "U.S. public domain (EIA: 'U.S. government publications are in the public domain')"
ZIP_MEDIA = ("application/x-zip-compressed", "application/zip", "application/octet-stream")
MAX_ZIP_BYTES = 80_000_000
MAX_MEMBER_BYTES = 1_500_000_000
SERIES_RE = re.compile(r"^INTL\.([0-9]+)-([0-9]+)-([A-Z0-9]+)-([A-Z0-9]+)\.A$")
YEAR_RE = re.compile(r"^[0-9]{4}$")
DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
#: EIA notation (international data browser footnotes); the archived page is the authority, these are the
#: codes the connector knows how to treat. Codes -> no cell; codes -> value-less flagged cell.
DROP_CODES = {"--": "not_applicable", "NA": "not_available"}
FLAG_CODES = {"ie": "not_reported", "w": "confidential", "W": "confidential"}
EXCLUDED_PRODUCTS = {"4701": "GDP at purchasing power parities (input from other producers)", "4702": "population"}
EXCLUDED_UNITS = {"TJ": "terajoules", "MTOE": "million metric tons of oil equivalent"}

PETROLEUM = {"5", "13", "53", "54", "55", "56", "57", "58", "59", "62", "63", "64", "65", "66", "67", "68"}
PETROLEUM |= {"6798", "6799", "6899", "8399", "8499"}
GAS = {"3", "26", "43", "48"}
COAL = {"7", "11", "12", "14", "21", "119", "130"}
ELECTRICITY = {"2", "27", "28", "29", "30", "31", "32", "33", "34", "35", "36", "37", "38", "82", "116", "117", "2890"}
ENERGY = {"44", "47", "79", "80", "81", "4411", "4413", "4415", "4417", "4418", "4419"}
EMISSIONS_ACTIVITY = "8"


@dataclass(frozen=True)
class Table:
    dataset: str
    title: str
    products: frozenset[str]


TABLES: tuple[Table, ...] = (
    Table("intl_petroleum", "Petroleum and other liquids", frozenset(PETROLEUM)),
    Table("intl_gas", "Natural gas", frozenset(GAS)),
    Table("intl_coal", "Coal and coke", frozenset(COAL)),
    Table("intl_electricity", "Electricity", frozenset(ELECTRICITY)),
    Table("intl_energy", "Total energy, biofuels, energy CO2 emissions, intensity", frozenset(ENERGY)),
)
TABLES_BY_DATASET = {t.dataset: t for t in TABLES}


class EiaParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def table_of(product: str, activity: str) -> str:
    """Atlas dataset of an EIA (product, activity); unknown products go to ``intl_energy`` (reported)."""
    if activity == EMISSIONS_ACTIVITY:
        return "intl_energy"
    for t in TABLES:
        if product in t.products:
            return t.dataset
    return "intl_energy"


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
        provider="U.S. Energy Information Administration (EIA), International Energy Data (bulk download)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(t.dataset for t in TABLES),
        dataset_ids=("INTL",),
        cadence_seconds=30 * 86_400,
        planned_requests=3,
        max_response_bytes=MAX_ZIP_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{t.dataset}" for t in TABLES], "geo_scheme": GEO_SCHEME},
        notes="EIA bulk INTL.zip (one JSON series per line), manifest.txt, international footnotes page",
    )


# --------------------------------------------------------------------------- small documents
def parse_manifest(body: bytes) -> dict[str, Any]:
    try:
        d = json.loads(body)["dataset"]["INTL"]
    except (ValueError, KeyError, TypeError) as e:
        raise EiaParseError(f"bulk manifest: no dataset INTL ({e})") from e
    if d.get("accessURL") != ZIP_URL:
        raise EiaParseError(f"bulk manifest: INTL accessURL {d.get('accessURL')!r} != {ZIP_URL}")
    return {k: d.get(k) for k in ("last_updated", "modified", "title", "accessURL", "publisher", "accessLevel")}


def parse_footnotes(body: bytes) -> dict[str, str]:
    """``<code> = <meaning>`` lines of EIA's international footnotes page (HTML)."""
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body.decode("utf-8", errors="replace")))
    out: dict[str, str] = {}
    for ln in text.splitlines():
        m = re.match(r"^\s*(--|NA|W|ie|\(s\)|NM)\s*=\s*(.+?)\s*$", ln)
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


def code_meaning(code: str, notes: dict[str, str]) -> str:
    return notes.get(code) or notes.get(code.upper()) or "meaning not found on the archived EIA footnotes page"


# --------------------------------------------------------------------------- bulk parsing
@dataclass
class TableReport:
    dataset: str
    series: int = 0
    cells: int = 0
    flagged_cells: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    zeros: int = 0
    indicators: dict[str, dict[str, str]] = field(default_factory=dict)
    geos: set[str] = field(default_factory=set)
    years: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("indicators", "geos")}
        d["indicators"] = len(self.indicators)
        d["geos"] = len(self.geos)
        return d


@dataclass
class BulkReport:
    lines: int = 0
    categories: int = 0
    series: int = 0
    non_annual: int = 0
    excluded_product: dict[str, int] = field(default_factory=dict)
    excluded_unit: dict[str, int] = field(default_factory=dict)
    unclassified_products: dict[str, int] = field(default_factory=dict)
    unknown_codes: dict[str, int] = field(default_factory=dict)
    copyright_other: dict[str, int] = field(default_factory=dict)
    last_updated: set[str] = field(default_factory=set)
    tables: dict[str, TableReport] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "tables"}
        lu = sorted(self.last_updated)
        d["last_updated"] = {"series": len(lu), "min": lu[0] if lu else None, "max": lu[-1] if lu else None}
        d["tables"] = {k: t.as_dict() for k, t in self.tables.items()}
        return d


@dataclass
class _Cols:
    indicator: list[str] = field(default_factory=list)
    geo: list[str] = field(default_factory=list)
    year: list[int] = field(default_factory=list)
    value: list[str | None] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    unit: list[str] = field(default_factory=list)
    line: list[int] = field(default_factory=list)
    series_id: list[str] = field(default_factory=list)


def _member_lines(body: bytes):  # type: ignore[no-untyped-def]
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise EiaParseError(f"INTL.zip is not a zip archive ({e})") from e
    if MEMBER not in zf.namelist():
        raise EiaParseError(f"INTL.zip has no member {MEMBER} ({zf.namelist()[:5]})")
    if zf.getinfo(MEMBER).file_size > MAX_MEMBER_BYTES:
        raise EiaParseError(f"{MEMBER}: inflated size exceeds {MAX_MEMBER_BYTES}")
    try:
        with zf.open(MEMBER) as fh:
            yield from enumerate(fh, start=1)
    except (zipfile.BadZipFile, EOFError) as e:
        raise EiaParseError(f"INTL.zip: corrupt member ({e})") from e


def _flags(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_bulk(
    body: bytes, raw_sha: str, notes: dict[str, str], datasets: set[str] | None = None
) -> tuple[dict[str, pl.DataFrame], BulkReport]:
    """INTL.zip -> {dataset: atlas frame} for the wanted datasets (all when None) and the report."""
    want = set(datasets) if datasets else {t.dataset for t in TABLES}
    rep = BulkReport()
    cols = {d: _Cols() for d in want}
    for d in want:
        rep.tables[d] = TableReport(d)
    for line_no, line in _member_lines(body):
        rep.lines += 1
        try:
            d = json.loads(line, parse_float=str, parse_int=str)
        except ValueError as e:
            raise EiaParseError(f"{MEMBER} line {line_no}: not JSON ({e})") from e
        if "series_id" not in d:
            rep.categories += 1
            continue
        rep.series += 1
        sid = d["series_id"]
        m = SERIES_RE.match(sid)
        if not m or d.get("f") != "A":
            rep.non_annual += 1
            continue
        prod, act, geo, unit_code = m.groups()
        if prod in EXCLUDED_PRODUCTS:
            rep.excluded_product[prod] = rep.excluded_product.get(prod, 0) + 1
            continue
        if unit_code in EXCLUDED_UNITS:
            rep.excluded_unit[unit_code] = rep.excluded_unit.get(unit_code, 0) + 1
            continue
        ds = table_of(prod, act)
        if ds == "intl_energy" and prod not in ENERGY and act != EMISSIONS_ACTIVITY:
            rep.unclassified_products[prod] = rep.unclassified_products.get(prod, 0) + 1
        if ds not in want:
            continue
        ind = f"INTL.{prod}-{act}-{unit_code}.A"
        if d.get("geoset_id") not in (None, ind):
            raise EiaParseError(f"line {line_no}: geoset_id {d.get('geoset_id')!r} does not match {sid}")
        unit = str(d.get("units") or "").strip()
        if not unit:
            raise EiaParseError(f"line {line_no}: series {sid} has no units")
        lu = str(d.get("last_updated") or "")
        rep.last_updated.add(lu)
        base: dict[str, Any] = {"series_last_updated": lu} if lu else {}
        cr = str(d.get("copyright", "None"))
        if cr != "None":
            base["copyright"] = cr
            rep.copyright_other[cr] = rep.copyright_other.get(cr, 0) + 1
        members = str(d.get("geography") or "")
        if "+" in members:
            base["eia_members"] = members
        trep = rep.tables[ds]
        trep.series += 1
        trep.geos.add(geo)
        name = str(d.get("name") or "")
        if ind not in trep.indicators or geo == "WORL":
            trep.indicators[ind] = {"name": name, "units": unit, "product": prod, "activity": act}
        base_flags = _flags(base)
        c = cols[ds]
        seen: set[str] = set()
        for pair in d.get("data") or []:
            period, v = pair[0], pair[1]
            if not YEAR_RE.match(str(period)):
                raise EiaParseError(f"line {line_no}: series {sid} has period {period!r}")
            if period in seen:
                raise EiaParseError(f"line {line_no}: series {sid} repeats period {period}")
            seen.add(period)
            if v is None:
                trep.dropped["null"] = trep.dropped.get("null", 0) + 1
                continue
            if DECIMAL_RE.match(v):
                value, fl = v, base_flags
                if float(v) == 0:
                    trep.zeros += 1
            elif v in DROP_CODES:
                trep.dropped[v] = trep.dropped.get(v, 0) + 1
                continue
            else:
                reason = FLAG_CODES.get(v, "provider_missing_code")
                if v not in FLAG_CODES:
                    rep.unknown_codes[v] = rep.unknown_codes.get(v, 0) + 1
                value = None
                fl = _flags({**base, "missing": reason, "provider_code": v, "provider_meaning": code_meaning(v, notes)})
                trep.flagged_cells[v] = trep.flagged_cells.get(v, 0) + 1
            c.indicator.append(ind)
            c.geo.append(geo)
            c.year.append(int(period))
            c.value.append(value)
            c.flags.append(fl)
            c.unit.append(unit)
            c.line.append(line_no)
            c.series_id.append(sid)
    if rep.series == 0:
        raise EiaParseError(f"{MEMBER}: no series")
    frames: dict[str, pl.DataFrame] = {}
    for ds in sorted(want):
        c = cols[ds]
        if not c.indicator:
            raise EiaParseError(f"{ds}: no cell")
        geo_map = {g: country_iso3(g, GEO_SCHEME) for g in set(c.geo)}
        df = pl.DataFrame(
            {
                "indicator": c.indicator,
                "provider_geo": c.geo,
                "year": c.year,
                "value": c.value,
                "flags": c.flags,
                "unit": c.unit,
                "line": c.line,
                "series_id": c.series_id,
            },
            schema={
                "indicator": pl.String,
                "provider_geo": pl.String,
                "year": pl.Int32,
                "value": pl.String,
                "flags": pl.String,
                "unit": pl.String,
                "line": pl.Int64,
                "series_id": pl.String,
            },
        )
        out = df.select(
            pl.lit(SOURCE).alias("source"),
            pl.lit(ds).alias("dataset"),
            "indicator",
            pl.lit(None, dtype=pl.String).alias("concept_id"),
            pl.col("provider_geo").replace_strict(geo_map, default=None, return_dtype=pl.String).alias("iso3"),
            "provider_geo",
            pl.lit(None, dtype=pl.String).alias("entity_qid"),
            pl.date(pl.col("year"), 1, 1).alias("period_start"),
            pl.date(pl.col("year") + 1, 1, 1).alias("period_end"),
            "value",
            pl.lit(None, dtype=pl.String).alias("value_text"),
            pl.col("unit").str.slice(0, 100),
            "flags",
            pl.lit(raw_sha).alias("raw_sha256"),
            pl.format(
                '{{"file":"{}","line":{},"period":"{}","series_id":"{}"}}',
                pl.lit(MEMBER),
                pl.col("line"),
                pl.col("year"),
                pl.col("series_id"),
            ).alias("selector"),
        ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
        dup = out.select(pl.struct("indicator", "provider_geo", "period_start").is_duplicated().sum()).item()
        if dup:
            raise EiaParseError(f"{ds}: {dup} duplicate (series, geography, year) cells")
        t = rep.tables[ds]
        t.cells = out.height
        t.years = [int(df["year"].min()), int(df["year"].max())]  # type: ignore[arg-type]
        frames[ds] = out
    return frames, rep


def coverage_note(t: Table, trep: TableReport, rep: BulkReport, manifest: dict[str, Any], etag: str | None) -> str:
    unm = sorted(g for g in trep.geos if country_iso3(g, GEO_SCHEME) is None)
    parts = [
        f"EIA International Energy Data bulk {ZIP_URL} (manifest last_updated {manifest.get('last_updated')},"
        f" ETag {etag}), member {MEMBER}; table '{t.title}': {trep.series} annual series, {trep.cells} cells,"
        f" {len(trep.indicators)} indicators (EIA geoset ids), years {trep.years[0]}-{trep.years[1]}.",
        f"No cell for {trep.dropped} ('--' not applicable, 'NA' not available, JSON null);"
        f" value-less flagged cells {trep.flagged_cells} ('ie' included elsewhere, 'W' withheld); zeros reported by"
        f" EIA kept ({trep.zeros}).",
        f"Not taken: TJ/MTOE conversions {rep.excluded_unit}, products {sorted(EXCLUDED_PRODUCTS)}"
        f" (GDP PPP, population), {rep.non_annual} monthly/quarterly series.",
        f"Unmapped EIA geographies (aggregates/historical, kept): {unm}.",
        f"Licence: U.S. public domain ({TERMS_URL}). Cite: U.S. Energy Information Administration, International"
        " Energy Data.",
    ]
    return " ".join(parts)[:4000]


def rights() -> dict[str, Any]:
    return {
        "licence": "U.S. public domain",
        "licence_url": TERMS_URL,
        "terms_quote": "U.S. government publications are in the public domain and are not subject to copyright"
        " protection.",
        "attribution": "Source: U.S. Energy Information Administration (EIA), International Energy Data.",
        "provider": "U.S. Energy Information Administration",
        "licence_checked": "2026-10-15",
        "source_key": SOURCE_KEY,
    }


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = {t.dataset for t in TABLES if run.wants(t.dataset)}
    raw_man = await run.get(MANIFEST_URL, key="bulk:manifest", media_types=("text/plain", "application/json"))
    man_sha = run.archive(raw_man)
    manifest = parse_manifest(raw_man.body)
    raw_fn = await run.get(
        FOOTNOTES_URL, key="international:footnotes", media_types=("text/html",), max_bytes=2_000_000
    )
    fn_sha = run.archive(raw_fn)
    notes = parse_footnotes(raw_fn.body)
    raw = await run.get(
        ZIP_URL,
        key=f"bulk:INTL:{manifest.get('last_updated')}",
        media_types=ZIP_MEDIA,
        max_bytes=MAX_ZIP_BYTES,
        params={"last_updated": manifest.get("last_updated")},
    )
    sha = run.archive(raw)
    etag = (raw.response_metadata.get("headers") or {}).get("etag")
    frames, rep = parse_bulk(raw.body, sha, notes, wanted)
    del raw
    out: dict[str, Any] = {"manifest": manifest, "footnotes": notes, "tables": {}}
    for t in TABLES:
        if t.dataset not in frames:
            continue
        trep = rep.tables[t.dataset]
        reg = run.register(
            dataset=t.dataset,
            frame=frames.pop(t.dataset),
            raw_sha256s=[sha, man_sha, fn_sha],
            geo_scheme=GEO_SCHEME,
            coverage_note=coverage_note(t, trep, rep, manifest, etag),
            rights=rights(),
        )
        out["tables"][t.dataset] = {
            "dataset_key": reg["dataset_key"],
            "rows": reg["row_count"],
            "countries": reg["coverage"]["countries"],
            "created": reg["created"],
            "report": trep.as_dict(),
            "unmapped": [u["provider_geo"] for u in reg.get("unmapped_provider_geo", [])],
        }
    out["bulk"] = {k: v for k, v in rep.as_dict().items() if k != "tables"}
    return out


__all__ = [
    "CONNECTOR_NAME",
    "TABLES",
    "TABLES_BY_DATASET",
    "EiaParseError",
    "atlas_ingest",
    "atlas_source",
    "parse_bulk",
    "parse_footnotes",
    "parse_manifest",
    "table_of",
]
