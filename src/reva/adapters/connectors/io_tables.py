"""Eurostat FIGARO inter-country input-output table connector (``io_tables``; V2 step 3b).

Source: Eurostat FIGARO "EU inter-country input-output table at basic prices, industry
by industry", dissemination datasets ``NAIO_10_FCP_II1..II4`` (II4 = 2022 onwards),
read through the Eurostat SDMX 2.1 dissemination API as gzip-compressed SDMX-CSV.
The 2026 edition (released July 2026) covers 2010-2024; 2024 is the latest year.

Table layout (one reference year, current prices, million EUR, basic prices):

* 50 economies (27 EU, NO, CH, UK, ME, MK, AL, RS, TR, RU, ZA, CA, US, MX, AR, BR, CN, JP,
  KR, IN, ID, SA, AU and ``WRL_REST`` = rest of the world) x 64 NACE Rev. 2 industries
  (``A01`` .. ``U``): N = 3200 industries;
* ``c_orig`` x ``ind_ava`` (rows): supplying economy and industry, or ``c_orig = DOM`` with a
  primary-input row (``D1`` compensation of employees, ``B2A3G`` gross operating surplus and
  mixed income, ``D29X39`` other taxes less subsidies on production, ``D21X31`` taxes less
  subsidies on products, ``OP_RES`` / ``OP_NRES`` direct purchases abroad by residents /
  purchases on the domestic territory by non-residents);
* ``c_dest`` x ``ind_use`` (columns): using economy and industry, or a final-demand category
  (``P3_S13`` government, ``P3_S14`` households, ``P3_S15`` NPISH, ``P51G`` gross fixed
  capital formation, ``P5M`` changes in inventories and valuables).

Fetch plan (one acquisition per request, raw bytes archived content-addressed by the
evidence ingest service): the dataset's SDMX content constraint (code lists and years;
used to find the latest year and to detect code-list drift), then one data request per
pair of supplying economies (``A.....FR+DE``) plus one for the ``DOM`` primary-input rows,
for one year. Requests above roughly half a million cells are answered asynchronously by
Eurostat (an XML "queued" envelope instead of data); two economies per request stay
synchronous (checked live 2026-10-15). A queued envelope is a typed, retryable source
error, never data.

The connector publishes no scalar observations: the table is a *dataset input*. After all
data items of one year are archived, :func:`build_dataset` (pure) parses the archived
bytes, checks completeness and the accounting identities and writes one Parquet table;
:func:`reva.adapters.connectors.io_tables_publish.publish_io_dataset` registers it. A
table that fails an identity check is refused (:class:`IdentityCheckFailed`) and nothing
is registered.

Missing values: every cell of the dense table must be present with a numeric value; an
absent cell, an empty value or a confidentiality status makes the table incomplete and
it is refused — a missing cell is never treated as zero. A published ``0.000`` is a
reported zero.
"""

from __future__ import annotations

import gzip
import io
import re
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from reva.adapters.connectors.base import module_digest, to_raw
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    QualityReport,
    RawAcquisition,
)

NAME = "eurostat.figaro.iciot_ii"
VERSION = "1.0.0"
PARSER_VERSION = "figaro-sdmx-csv-1"
TEMPLATE = "eurostat_sdmx_csv"
SOURCE_KEY = "eurostat.dissemination"
HOSTS: tuple[str, ...] = ("ec.europa.eu",)
BASE = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1"
UNIT = "MIO_EUR"
LICENCE = "CC-BY-4.0 (Eurostat reuse policy, acknowledgement required)"
DATASET_SCHEMA = "reva.io_table.figaro_ii/1"
DATASET_CONCEPT = "economics.io.figaro_ii_table"
#: Absolute tolerance of the row-total = column-total identity, in million EUR: one unit
#: in the last published decimal (EUR 1,000). The 2026 edition balances exactly (0.000).
IDENTITY_ABS_TOLERANCE_MIO_EUR = Decimal("0.001")

#: Reference-year ranges of the FIGARO industry-by-industry datasets (2026 edition).
DATASETS: tuple[tuple[str, int, int], ...] = (
    ("NAIO_10_FCP_II1", 2010, 2013),
    ("NAIO_10_FCP_II2", 2014, 2017),
    ("NAIO_10_FCP_II3", 2018, 2021),
    ("NAIO_10_FCP_II4", 2022, 9999),
)

#: FIGARO economy code -> ISO 3166 alpha-3 (reviewed 2026-10-15). Eurostat uses ``EL`` for
#: Greece and ``UK`` for the United Kingdom. ``WRL_REST`` (rest of the world) is a region,
#: not a country: it has no ISO3 code and is addressed as ``FIGARO_ROW``.
ECONOMIES: dict[str, str] = {
    "BE": "BEL", "BG": "BGR", "CZ": "CZE", "DK": "DNK", "DE": "DEU", "EE": "EST", "IE": "IRL", "EL": "GRC",
    "ES": "ESP", "FR": "FRA", "HR": "HRV", "IT": "ITA", "CY": "CYP", "LV": "LVA", "LT": "LTU", "LU": "LUX",
    "HU": "HUN", "MT": "MLT", "NL": "NLD", "AT": "AUT", "PL": "POL", "PT": "PRT", "RO": "ROU", "SI": "SVN",
    "SK": "SVK", "FI": "FIN", "SE": "SWE", "NO": "NOR", "CH": "CHE", "UK": "GBR", "ME": "MNE", "MK": "MKD",
    "AL": "ALB", "RS": "SRB", "TR": "TUR", "RU": "RUS", "ZA": "ZAF", "CA": "CAN", "US": "USA", "MX": "MEX",
    "AR": "ARG", "BR": "BRA", "CN": "CHN", "JP": "JPN", "KR": "KOR", "IN": "IND", "ID": "IDN", "SA": "SAU",
    "AU": "AUS", "WRL_REST": "FIGARO_ROW",
}
REST_OF_WORLD = "WRL_REST"
REST_OF_WORLD_KEY = "FIGARO_ROW"
DOM = "DOM"

#: The 64 FIGARO industries (NACE Rev. 2, A*64) with Eurostat's labels (codelist IND_USE).
INDUSTRIES: dict[str, str] = {
    "A01": "Crop and animal production, hunting and related service activities",
    "A02": "Forestry and logging",
    "A03": "Fishing and aquaculture",
    "B": "Mining and quarrying",
    "C10-12": "Manufacture of food products; beverages and tobacco products",
    "C13-15": "Manufacture of textiles, wearing apparel, leather and related products",
    "C16": "Manufacture of wood and of products of wood and cork, except furniture; manufacture of articles of "
           "straw and plaiting materials",
    "C17": "Manufacture of paper and paper products",
    "C18": "Printing and reproduction of recorded media",
    "C19": "Manufacture of coke and refined petroleum products",
    "C20": "Manufacture of chemicals and chemical products",
    "C21": "Manufacture of basic pharmaceutical products and pharmaceutical preparations",
    "C22": "Manufacture of rubber and plastic products",
    "C23": "Manufacture of other non-metallic mineral products",
    "C24": "Manufacture of basic metals",
    "C25": "Manufacture of fabricated metal products, except machinery and equipment",
    "C26": "Manufacture of computer, electronic and optical products",
    "C27": "Manufacture of electrical equipment",
    "C28": "Manufacture of machinery and equipment n.e.c.",
    "C29": "Manufacture of motor vehicles, trailers and semi-trailers",
    "C30": "Manufacture of other transport equipment",
    "C31_32": "Manufacture of furniture; other manufacturing",
    "C33": "Repair and installation of machinery and equipment",
    "D35": "Electricity, gas, steam and air conditioning supply",
    "E36": "Water collection, treatment and supply",
    "E37-39": "Sewerage, waste management, remediation activities",
    "F": "Construction",
    "G45": "Wholesale and retail trade and repair of motor vehicles and motorcycles",
    "G46": "Wholesale trade, except of motor vehicles and motorcycles",
    "G47": "Retail trade, except of motor vehicles and motorcycles",
    "H49": "Land transport and transport via pipelines",
    "H50": "Water transport",
    "H51": "Air transport",
    "H52": "Warehousing and support activities for transportation",
    "H53": "Postal and courier activities",
    "I": "Accommodation and food service activities",
    "J58": "Publishing activities",
    "J59_60": "Motion picture, video, television programme production; programming and broadcasting activities",
    "J61": "Telecommunications",
    "J62_63": "Computer programming, consultancy, and information service activities",
    "K64": "Financial service activities, except insurance and pension funding",
    "K65": "Insurance, reinsurance and pension funding, except compulsory social security",
    "K66": "Activities auxiliary to financial services and insurance activities",
    "L": "Real estate activities",
    "M69_70": "Legal and accounting activities; activities of head offices; management consultancy activities",
    "M71": "Architectural and engineering activities; technical testing and analysis",
    "M72": "Scientific research and development",
    "M73": "Advertising and market research",
    "M74_75": "Other professional, scientific and technical activities; veterinary activities",
    "N77": "Rental and leasing activities",
    "N78": "Employment activities",
    "N79": "Travel agency, tour operator reservation service and related activities",
    "N80-82": "Security and investigation, service and landscape, office administrative and support activities",
    "O84": "Public administration and defence; compulsory social security",
    "P85": "Education",
    "Q86": "Human health activities",
    "Q87_88": "Residential care activities and social work activities without accommodation",
    "R90-92": "Creative, arts and entertainment activities; libraries, archives, museums and other cultural "
              "activities; gambling and betting activities",
    "R93": "Sports activities and amusement and recreation activities",
    "S94": "Activities of membership organisations",
    "S95": "Repair of computers and personal and household goods",
    "S96": "Other personal service activities",
    "T": "Activities of households as employers; undifferentiated goods- and services-producing activities of "
         "households for own use",
    "U": "Activities of extraterritorial organisations and bodies",
}
FINAL_DEMAND: dict[str, str] = {
    "P3_S13": "Final consumption expenditure by government",
    "P3_S14": "Final consumption expenditure by households",
    "P3_S15": "Final consumption expenditure by non-profit organisations serving households (NPISH)",
    "P51G": "Gross fixed capital formation",
    "P5M": "Changes in inventories and acquisition less disposals of valuables",
}
PRIMARY_INPUTS: dict[str, str] = {
    "B2A3G": "Operating surplus and mixed income, gross",
    "D1": "Compensation of employees",
    "D21X31": "Taxes less subsidies on products",
    "D29X39": "Other taxes less other subsidies on production",
    "OP_RES": "Direct purchases abroad by residents",
    "OP_NRES": "Purchases on the domestic territory by non-residents",
}
#: Primary inputs that enter an industry's column total (gross output at basic prices).
#: OP_RES/OP_NRES are final-use adjustments; they must be zero in industry columns (checked).
COLUMN_TOTAL_PRIMARY_INPUTS = ("B2A3G", "D1", "D21X31", "D29X39")
#: Primary inputs that are structurally zero in final-demand columns (checked).
FINAL_DEMAND_ZERO_PRIMARY_INPUTS = ("B2A3G", "D1", "D29X39")

COUNTRY_CODES: tuple[str, ...] = tuple(ECONOMIES)
INDUSTRY_CODES: tuple[str, ...] = tuple(INDUSTRIES)
USE_CODES: tuple[str, ...] = (*INDUSTRY_CODES, *FINAL_DEMAND)
AVA_CODES: tuple[str, ...] = (*INDUSTRY_CODES, *PRIMARY_INPUTS)
ORIGIN_CODES: tuple[str, ...] = (*COUNTRY_CODES, DOM)
ORIGINS_PER_REQUEST = 2
GROSS_OUTPUT = "GROSS_OUTPUT"


@dataclass(frozen=True)
class TableDims:
    """Code lists of one inter-country table (FIGARO by default; tiny synthetic tables in tests)."""

    countries: tuple[str, ...] = COUNTRY_CODES
    industries: tuple[str, ...] = INDUSTRY_CODES
    final_demand: tuple[str, ...] = tuple(FINAL_DEMAND)
    primary_inputs: tuple[str, ...] = tuple(PRIMARY_INPUTS)

    @property
    def uses(self) -> tuple[str, ...]:
        return (*self.industries, *self.final_demand)

    @property
    def avas(self) -> tuple[str, ...]:
        return (*self.industries, *self.primary_inputs)

    @property
    def origins(self) -> tuple[str, ...]:
        return (*self.countries, DOM)

    def expected_rows(self, origins: Iterable[str]) -> int:
        n = 0
        for o in origins:
            ava = len(self.primary_inputs) if o == DOM else len(self.industries)
            n += ava * len(self.countries) * len(self.uses)
        return n

    def dense_cells(self) -> int:
        return self.expected_rows(self.origins)


FIGARO_DIMS = TableDims()

CSV_HEADER = ("DATAFLOW", "LAST UPDATE", "freq", "ind_use", "ind_ava", "c_dest", "unit", "c_orig", "TIME_PERIOD",
              "OBS_VALUE", "OBS_FLAG", "CONF_STATUS")
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_DATA_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CONSTRAINT_BYTES = 1_000_000
_VALUE_RE = r"^-?[0-9]+(\.[0-9]{1,3})?$"
_ASYNC_MARKERS = (b"<queued>", b"syncResponse")
_KEYVALUE_RE = re.compile(r'<c:KeyValue id="([A-Za-z_]{1,32})">(.*?)</c:KeyValue>', re.S)
_VALUE_TAG_RE = re.compile(r"<c:Value>([A-Za-z0-9_\-]{1,32})</c:Value>")


class IdentityCheckFailed(ValidationProblem):
    """The table violates completeness or an accounting identity; it is not published."""

    code = "io_identity_check_failed"


def dataset_for_year(year: int) -> str:
    for code, lo, hi in DATASETS:
        if lo <= year <= hi:
            return code
    raise ValidationProblem(f"FIGARO has no industry-by-industry table for {year} (2010 onwards)", year=year)


def origin_groups(per_request: int = ORIGINS_PER_REQUEST, dims: TableDims | None = None) -> list[tuple[str, ...]]:
    """Request groups: pairs of supplying economies, then the ``DOM`` primary-input rows alone."""
    eco = list((dims or FIGARO_DIMS).countries)
    groups = [tuple(eco[i:i + per_request]) for i in range(0, len(eco), per_request)]
    return [*groups, (DOM,)]


def expected_rows(origins: Iterable[str]) -> int:
    return FIGARO_DIMS.expected_rows(origins)


def data_url(dataset: str, origins: Sequence[str], year: int) -> str:
    return (f"{BASE}/data/{dataset}/A.....{'+'.join(origins)}?startPeriod={year}&endPeriod={year}"
            "&format=SDMX-CSV&compressed=true")


def constraint_url(dataset: str) -> str:
    return f"{BASE}/contentconstraint/ESTAT/{dataset}"


# --------------------------------------------------------------------------- byte handling
def _gunzip_bounded(body: bytes, limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise SourceResponseError(f"decompressed FIGARO response exceeds {limit} bytes", retryable=False,
                                  kind="oversized")
    if not d.eof:
        raise SourceResponseError("truncated gzip FIGARO response", retryable=True, kind="decoding_error")
    if d.unused_data.strip(b"\x00"):
        raise SourceResponseError("trailing data after the gzip member", retryable=False, kind="decoding_error")
    return out


def _check_not_queued(body: bytes) -> None:
    head = body[:4096]
    if head.lstrip().startswith(b"<") and all(m in head for m in _ASYNC_MARKERS):
        raise SourceResponseError(
            "Eurostat queued the request for asynchronous extraction instead of returning data; retry later "
            "or split the request", retryable=True, kind="provider_async_queued")


def csv_bytes(body: bytes) -> bytes:
    """Plain SDMX-CSV bytes from a response body (gzip or identity)."""
    _check_not_queued(body)
    data = _gunzip_bounded(body) if body[:2] == b"\x1f\x8b" else body
    if len(data) > MAX_DECOMPRESSED_BYTES:
        raise SourceResponseError("FIGARO response too large", retryable=False, kind="oversized")
    return data


# --------------------------------------------------------------------------- parsing (pure)
@dataclass(frozen=True)
class ParsedItem:
    """One parsed data response: a long frame plus the checks that were run on it."""

    frame: Any  # polars.DataFrame: c_orig, ind_ava, c_dest, ind_use (str), value (Decimal(18,3))
    findings: tuple[Finding, ...]
    metadata: dict[str, Any]

    @property
    def blocking(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)


def parse_sdmx_csv(body: bytes, *, dataset: str, year: int, origins: Sequence[str],
                   dims: TableDims = FIGARO_DIMS) -> ParsedItem:
    """Parse one SDMX-CSV response into a long frame and record every check as a finding.

    Blockers: unexpected header, dataflow, frequency, unit or period; a code outside the
    pinned code lists or an origin that was not requested; an empty or non-numeric value; a
    confidentiality status; duplicate keys; a row count different from the dense count.
    Observation flags (none in the 2026 edition) are counted and reported, never dropped.
    """
    import polars as pl

    data = csv_bytes(body)
    first = data.split(b"\n", 1)[0].rstrip(b"\r").decode("utf-8", "replace")
    findings: list[Finding] = []
    if tuple(first.split(",")) != CSV_HEADER:
        raise SourceResponseError(f"unexpected SDMX-CSV header {first[:200]!r}", retryable=False,
                                  kind="schema_drift")
    df = pl.read_csv(io.BytesIO(data), infer_schema_length=0, has_header=True)
    del data

    def blocker(code: str, msg: str) -> None:
        findings.append(Finding(code=code, severity="blocker", message=msg))

    want_flow = f"ESTAT:{dataset}(1.0)"
    for col, allowed, code in (("DATAFLOW", {want_flow}, "dataflow_mismatch"), ("freq", {"A"}, "freq_mismatch"),
                               ("unit", {UNIT}, "unit_mismatch"), ("TIME_PERIOD", {str(year)}, "period_mismatch"),
                               ("c_orig", set(origins), "origin_mismatch"), ("c_dest", set(dims.countries),
                                                                             "code_list_drift"),
                               ("ind_use", set(dims.uses), "code_list_drift")):
        seen = set(df[col].unique().to_list())
        extra = sorted(str(v) for v in seen - allowed)
        if extra:
            blocker(code, f"{col}: unexpected values {extra[:10]}")
    ava_allowed = set(dims.primary_inputs) if tuple(origins) == (DOM,) else set(dims.industries)
    if DOM in origins and tuple(origins) != (DOM,):
        raise ValidationProblem("DOM primary-input rows are requested on their own")
    extra_ava = sorted(set(df["ind_ava"].unique().to_list()) - ava_allowed)
    if extra_ava:
        blocker("code_list_drift", f"ind_ava: unexpected values {extra_ava[:10]}")
    bad_values = df.filter(pl.col("OBS_VALUE").is_null() | ~pl.col("OBS_VALUE").str.contains(_VALUE_RE)).height
    if bad_values:
        blocker("missing_or_non_numeric_value",
                f"{bad_values} cells have an empty or non-numeric OBS_VALUE; a missing cell is never zero")
    conf = df.filter(pl.col("CONF_STATUS").is_not_null()).height
    if conf:
        blocker("confidential_cells", f"{conf} cells carry a confidentiality status")
    flags = df.filter(pl.col("OBS_FLAG").is_not_null()).group_by("OBS_FLAG").len().sort("OBS_FLAG")
    flag_counts = {str(r[0]): int(r[1]) for r in flags.iter_rows()}
    if flag_counts:
        findings.append(Finding(code="observation_flags", severity="warning",
                                message=f"observation flags present: {flag_counts}"))
    keys = ["c_orig", "ind_ava", "c_dest", "ind_use"]
    dupes = df.height - df.select(keys).unique().height
    if dupes:
        blocker("duplicate_keys", f"{dupes} duplicate (c_orig, ind_ava, c_dest, ind_use) keys")
    exp = dims.expected_rows(origins)
    if df.height != exp:
        blocker("incomplete_table", f"{df.height} rows, expected the dense count {exp}")
    last_update = sorted(str(v) for v in df["LAST UPDATE"].unique().to_list())
    if any(f.severity == "blocker" for f in findings):
        frame = df.select(*keys, pl.col("OBS_VALUE").alias("value"))  # kept as text for diagnosis only
    else:  # compact: pinned-order enums (1-2 bytes) and exact decimal(18,3) values
        frame = df.select(pl.col("c_orig").cast(pl.Enum(list(dims.origins))),
                          pl.col("ind_ava").cast(pl.Enum(list(dims.avas))),
                          pl.col("c_dest").cast(pl.Enum(list(dims.countries))),
                          pl.col("ind_use").cast(pl.Enum(list(dims.uses))),
                          pl.col("OBS_VALUE").cast(pl.Decimal(18, 3)).alias("value"))
    del df
    meta = {"dataset": dataset, "year": year, "origins": list(origins), "rows": frame.height,
            "expected_rows": exp, "observation_flags": flag_counts, "provider_last_update": last_update,
            "note": "provider LAST UPDATE is dataset-level metadata, not a per-value publication time"}
    return ParsedItem(frame, tuple(findings), meta)


def parse_constraint(body: bytes) -> dict[str, list[str]]:
    """Dimension values of an SDMX 2.1 content constraint (small XML; no DTD/entities accepted)."""
    if len(body) > MAX_CONSTRAINT_BYTES:
        raise SourceResponseError("content constraint too large", retryable=False, kind="oversized")
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise SourceResponseError("DTD/entity declarations are not accepted", retryable=False, kind="unsafe_xml")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SourceResponseError("content constraint is not UTF-8", retryable=False, kind="schema_drift") from e
    # Flat pattern extraction (no XML parser on untrusted bytes): <c:KeyValue id="dim"> <c:Value>v</c:Value>...
    out: dict[str, list[str]] = {}
    for m in _KEYVALUE_RE.finditer(text):
        out[m.group(1)] = _VALUE_TAG_RE.findall(m.group(2))
    if not out:
        raise SourceResponseError("content constraint lists no dimension values", retryable=False,
                                  kind="schema_drift")
    return out


def constraint_findings(dims: Mapping[str, list[str]], year: int | None,
                        table: TableDims | None = None) -> tuple[list[Finding], int | None]:
    """Compare the published code lists with the pinned ones; returns findings and the target year."""
    findings: list[Finding] = []
    t = table or FIGARO_DIMS
    pinned = {"freq": {"A"}, "unit": {UNIT}, "c_orig": set(t.origins), "c_dest": set(t.countries),
              "ind_use": set(t.uses), "ind_ava": set(t.avas)}
    for dim, want in pinned.items():
        got = set(dims.get(dim, []))
        if got != want:
            findings.append(Finding(code="code_list_drift", severity="blocker",
                                    message=f"{dim}: published {sorted(got ^ want)[:12]} differ from the pinned list"))
    years = sorted(int(y) for y in dims.get("TIME_PERIOD", []) if re.fullmatch(r"\d{4}", y))
    if not years:
        findings.append(Finding(code="no_periods", severity="blocker", message="no reference years published"))
        return findings, None
    target = year if year is not None else years[-1]
    if target not in years:
        findings.append(Finding(code="year_unavailable", severity="blocker",
                                message=f"{target} is not published (available: {years})"))
        return findings, None
    return findings, target


# --------------------------------------------------------------------------- connector
class FigaroConnector:
    """Reviewed built-in connector for the FIGARO industry-by-industry table of one year."""

    def __init__(self, year: int | None = None, *, dataset: str = "NAIO_10_FCP_II4",
                 per_request: int = ORIGINS_PER_REQUEST, dims: TableDims = FIGARO_DIMS) -> None:
        if year is not None:
            dataset = dataset_for_year(year)
        self.year = year
        self.dataset = dataset
        self.per_request = per_request
        self.dims = dims  # FIGARO's pinned code lists (a smaller table only in tests)

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name=NAME,
            version=VERSION,
            template=TEMPLATE,  # type: ignore[arg-type]
            source_key=SOURCE_KEY,
            provider="Eurostat (FIGARO)",
            licence=LICENCE,
            allowed_hosts=HOSTS,
            dataset_ids=tuple(d for d, _, _ in DATASETS),
            field_mappings={"OBS_VALUE": f"dataset:{DATASET_CONCEPT}"},
            units={"OBS_VALUE": "million EUR, current prices, basic prices"},
            geography=tuple(ECONOMIES.values()),
            time_semantics="calendar-year flow totals for one reference year; current prices",
            lineage_family="eurostat_figaro",
            cadence_seconds=30 * 86400,
            release_aware=True,
            revision_strategy="annual edition revises the whole series; a new edition is a new acquisition",
            max_response_bytes=MAX_DATA_RESPONSE_BYTES,
            max_pages=64,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(FigaroConnector),
        )

    def _data_items(self, year: int, dataset: str) -> tuple[FetchItem, ...]:
        dep = f"constraint:{self.dataset}"
        return tuple(
            FetchItem(key=f"data:{dataset}:{year}:{'+'.join(g)}", url=data_url(dataset, g, year),
                      expected_media_types=("application/vnd.sdmx.data+csv", "application/octet-stream",
                                            "application/gzip"),
                      max_bytes=MAX_DATA_RESPONSE_BYTES,
                      params={"kind": "data", "dataset": dataset, "year": year, "origins": list(g),
                              "depends_on": dep})
            for g in origin_groups(self.per_request, self.dims)
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        items = (FetchItem(key=f"constraint:{self.dataset}", url=constraint_url(self.dataset),
                           expected_media_types=("application/vnd.sdmx.structure+xml", "application/xml"),
                           max_bytes=MAX_CONSTRAINT_BYTES,
                           params={"kind": "constraint", "dataset": self.dataset, "year": self.year}),)
        return FetchPlan(connector=NAME, items=items,
                         pagination="data requests planned after the content constraint (one per origin group)",
                         availability_expectation="annual edition, usually July; year T-2",
                         budget_exhausted=budget.max_requests < 1 + len(origin_groups(self.per_request, self.dims)))

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        raw = to_raw(item, result, getattr(http, "origin", "live"))
        if item.params.get("kind") == "data":
            _check_not_queued(raw.body)
        return raw

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        kind = raw.item.params.get("kind")
        if kind == "constraint":
            dims = parse_constraint(raw.body)
            findings, target = constraint_findings(dims, self.year, self.dims)
            follow = () if target is None or any(f.severity == "blocker" for f in findings) else \
                self._data_items(target, dataset_for_year(target))
            yield ObservationBatch(
                connector=NAME, connector_version=VERSION, item_key=raw.item.key, observations=(),
                metadata={"kind": kind, "dataset": self.dataset, "target_year": target,
                          "years": dims.get("TIME_PERIOD", []),
                          "findings": [f.model_dump() for f in findings]},
                follow_up=follow)
            return
        if kind != "data":
            raise SourceResponseError(f"unknown FIGARO item kind {kind!r}", retryable=False)
        p = raw.item.params
        parsed = parse_sdmx_csv(raw.body, dataset=p["dataset"], year=int(p["year"]), origins=p["origins"],
                                dims=self.dims)
        yield ObservationBatch(connector=NAME, connector_version=VERSION, item_key=raw.item.key, observations=(),
                               metadata={"kind": kind, **parsed.metadata,
                                         "findings": [f.model_dump() for f in parsed.findings]})

    def validate(self, batch: ObservationBatch) -> QualityReport:
        findings = tuple(Finding(**f) for f in batch.metadata.get("findings", []))
        blocking = any(f.severity == "blocker" for f in findings)
        n = int(batch.metadata.get("rows", 0))
        return QualityReport(outcome="quarantine" if blocking else "pass", findings=findings,
                             expected_count=int(batch.metadata.get("expected_rows", n)), received_count=n)

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        data = [o for o in outcomes if o.item_key.startswith("data:")]
        ok = bool(data) and len(data) == len(origin_groups(self.per_request, self.dims)) and all(
            o.status in ("published", "unchanged") for o in outcomes)
        return Cursor(state={"complete": ok, "dataset": self.dataset, "year": self.year,
                             "raw_sha256": {o.item_key: o.raw_sha256 for o in data}})


# --------------------------------------------------------------------------- dataset build (pure)
@dataclass(frozen=True)
class BuiltDataset:
    """A verified table. ``manifest`` becomes the registered dataset's ``coverage`` block.

    ``parquet`` is this build's serialisation (bounded memory); the registered artifact is
    re-serialised by :func:`reva.simulation.datasets.register_dataset`, so its sha256 is the
    registry's, not ``build_sha256``.
    """

    parquet: bytes
    manifest: dict[str, Any]
    identity_report: dict[str, Any]
    row_count: int
    build_sha256: str = ""

    def table(self) -> Any:
        import pyarrow.parquet as pq

        return pq.read_table(io.BytesIO(self.parquet))


def _block_enum() -> Any:
    import polars as pl

    return pl.Enum(["Z", "F", "V", "X"])


def _index_frame(name: str, codes: Sequence[str]) -> Any:
    import polars as pl

    return pl.DataFrame({name: list(codes), f"{name}_ord": list(range(len(codes)))},
                        schema={name: pl.Utf8, f"{name}_ord": pl.Int32})


def to_long(frame: Any, dims: TableDims = FIGARO_DIMS) -> Any:
    """One parsed item as rows of the long table, ordered by the pinned code lists.

    Blocks: ``Z`` industry -> industry, ``F`` industry -> final demand, ``V`` primary-input
    rows (``row_country = DOM``); ``X`` (gross output) is appended by :func:`build_dataset`.
    """
    import polars as pl

    df = frame.sort("c_orig", "ind_ava", "c_dest", "ind_use")  # enum order = pinned order
    block = (pl.when(pl.col("c_orig") == DOM).then(pl.lit("V"))
             .when(pl.col("ind_use").cast(pl.Utf8).is_in(list(dims.final_demand))).then(pl.lit("F"))
             .otherwise(pl.lit("Z")).cast(_block_enum()))
    return df.select(block.alias("block"), pl.col("c_orig").alias("row_country"),
                     pl.col("ind_ava").alias("row_code"), pl.col("c_dest").alias("col_country"),
                     pl.col("ind_use").cast(pl.Utf8).cast(pl.Enum([*dims.uses, GROSS_OUTPUT])).alias("col_code"),
                     pl.col("value"))


def _partials(long: Any, dims: TableDims) -> dict[str, Any]:
    """Additive per-item aggregates for the identity checks (exact decimal sums)."""
    import polars as pl

    s = [pl.col(c).cast(pl.Utf8) for c in ("row_country", "row_code", "col_country", "col_code")]
    return {
        "x_row": long.filter(pl.col("block") != "V").group_by("row_country", "row_code").agg(
            pl.col("value").sum().alias("x_row")).with_columns(s[:2]),
        "z_col": long.filter(pl.col("block") == "Z").group_by("col_country", "col_code").agg(
            pl.col("value").sum().alias("z_col")).with_columns(s[2:]),
        "v_col": long.filter(pl.col("block") == "V", pl.col("col_code").cast(pl.Utf8).is_in(list(dims.industries)),
                             pl.col("row_code").cast(pl.Utf8).is_in(list(COLUMN_TOTAL_PRIMARY_INPUTS))).group_by(
            "col_country", "col_code").agg(pl.col("value").sum().alias("v_col")).with_columns(s[2:]),
        "op_nonzero": long.filter(pl.col("block") == "V", pl.col("row_code").cast(pl.Utf8).is_in(["OP_RES", "OP_NRES"]),
                                  pl.col("col_code").cast(pl.Utf8).is_in(list(dims.industries)),
                                  pl.col("value") != 0).height,
        "va_fd_nonzero": long.filter(
            pl.col("block") == "V", pl.col("row_code").cast(pl.Utf8).is_in(list(FINAL_DEMAND_ZERO_PRIMARY_INPUTS)),
            pl.col("col_code").cast(pl.Utf8).is_in(list(dims.final_demand)), pl.col("value") != 0).height,
        "rows": long.height,
        "nulls": int(long["value"].null_count()),
        "z_sum": long.filter(pl.col("block") == "Z").select(pl.col("value").sum()).item(),
        "f_sum": long.filter(pl.col("block") == "F").select(pl.col("value").sum()).item(),
    }


def verify_identities(parts: Sequence[Mapping[str, Any]], *, dims: TableDims = FIGARO_DIMS,
                      tolerance: Decimal = IDENTITY_ABS_TOLERANCE_MIO_EUR) -> tuple[dict[str, Any], Any]:
    """Completeness and accounting identities in exact decimal arithmetic.

    For every economy c and industry i (FIGARO: N = 50 x 64 = 3200):

    * row total      x_row(c,i) = Σ intermediate use by every (c',j) + Σ final demand by every (c',f);
    * column total   x_col(c,i) = Σ intermediate inputs from every (c',j) + D1 + B2A3G + D29X39 + D21X31;
    * identity       |x_row − x_col| <= tolerance (million EUR) and x_row >= 0;
    * structural zeros: OP_RES/OP_NRES are 0 in every industry column; D1, B2A3G, D29X39 are 0 in
      every final-demand column;
    * completeness: exactly the dense number of cells, no empty value (each item was also checked
      for duplicate keys and its own dense count when parsed).

    ``parts`` are the per-item aggregates of :func:`_partials`. Returns ``(report, gross_output)``;
    ``report["passed"]`` is False on any failure.
    """
    import polars as pl

    n_cells = dims.dense_cells()
    n_ind = len(dims.countries) * len(dims.industries)
    rows = sum(int(p["rows"]) for p in parts)
    nulls = sum(int(p["nulls"]) for p in parts)
    checks: list[dict[str, Any]] = [{"check": "dense_completeness", "expected_cells": n_cells, "rows": rows,
                                     "null_values": nulls, "passed": rows == n_cells and nulls == 0}]

    def total(key: str, by: list[str]) -> Any:
        frames = [p[key] for p in parts if p[key].height]
        if not frames:
            return pl.DataFrame(schema={by[0]: pl.Utf8, by[1]: pl.Utf8, key: pl.Decimal(38, 3)})
        return pl.concat(frames).group_by(*by).agg(pl.col(key).sum())

    x_row = total("x_row", ["row_country", "row_code"]).rename({"row_country": "country", "row_code": "industry"})
    z_col = total("z_col", ["col_country", "col_code"]).rename({"col_country": "country", "col_code": "industry"})
    v_col = total("v_col", ["col_country", "col_code"]).rename({"col_country": "country", "col_code": "industry"})
    tot = (x_row.join(z_col, on=["country", "industry"], how="full", coalesce=True)
           .join(v_col, on=["country", "industry"], how="full", coalesce=True))
    tot = tot.with_columns((pl.col("z_col") + pl.col("v_col")).alias("x_col"))
    tot = tot.with_columns((pl.col("x_row") - pl.col("x_col")).alias("diff"))
    tol = pl.lit(tolerance).cast(pl.Decimal(18, 3))
    bad = tot.filter(pl.col("diff").is_null() | (pl.col("diff").abs() > tol))
    max_abs = tot.select(pl.col("diff").abs().max()).item()
    neg = tot.filter(pl.col("x_row") < 0)
    zero = tot.filter(pl.col("x_row") == 0)
    checks.append({
        "check": "row_total_equals_column_total", "industries": tot.height, "expected_industries": n_ind,
        "tolerance_mio_eur": str(tolerance), "max_abs_diff_mio_eur": str(max_abs) if max_abs is not None else None,
        "violations": bad.height,
        "examples": [{"country": r[0], "industry": r[1], "diff": None if r[2] is None else str(r[2])}
                     for r in bad.select("country", "industry", "diff").head(10).iter_rows()],
        "passed": bad.height == 0 and tot.height == n_ind})
    checks.append({"check": "gross_output_non_negative", "negative": neg.height,
                   "zero_output_industries": zero.height, "passed": neg.height == 0})
    op = sum(int(p["op_nonzero"]) for p in parts)
    vafd = sum(int(p["va_fd_nonzero"]) for p in parts)
    checks.append({"check": "structural_zeros", "op_rows_nonzero_in_industry_columns": op,
                   "value_added_nonzero_in_final_demand_columns": vafd, "passed": op == 0 and vafd == 0})
    totals = {
        "gross_output_mio_eur": str(tot.select(pl.col("x_row").sum()).item()),
        "intermediate_mio_eur": str(sum((p["z_sum"] for p in parts if p["z_sum"] is not None), Decimal(0))),
        "final_demand_mio_eur": str(sum((p["f_sum"] for p in parts if p["f_sum"] is not None), Decimal(0))),
    }
    report = {"schema": "reva.io_identity_report/1", "checks": checks, "totals": totals,
              "arithmetic": "exact decimal (published 3-decimal million-EUR values, 128-bit integer sums)",
              "passed": all(c["passed"] for c in checks)}
    return report, tot.select("country", "industry", pl.col("x_row").alias("value"))


def build_dataset(parsed: Sequence[ParsedItem], *, dataset: str, year: int,
                  acquisitions: Sequence[Mapping[str, Any]] = (), origin: str = "live",
                  dims: TableDims = FIGARO_DIMS) -> BuiltDataset:
    """Verify and serialise one year item by item (bounded memory). Refuses on any failure.

    Items are written to Parquet in pinned origin order while additive aggregates are
    collected; the identity checks then run on the aggregates. On a failure the buffer is
    discarded and :class:`IdentityCheckFailed` is raised, so nothing can be registered.
    ``acquisitions`` lists ``{"acquisition_id", "raw_sha256", "item_key"}`` of the archived raw
    bytes the items were parsed from (provenance in the manifest).
    """
    import hashlib

    import polars as pl
    import pyarrow.parquet as pq

    blockers = [f.model_dump() | {"item": p.metadata.get("origins")} for p in parsed for f in p.findings
                if f.severity == "blocker"]
    covered = [o for p in parsed for o in p.metadata.get("origins", [])]
    if blockers or sorted(covered) != sorted(dims.origins):
        raise IdentityCheckFailed(
            "input-output table is incomplete or failed item checks; nothing is published",
            blockers=blockers[:20], missing_origins=sorted(set(dims.origins) - set(covered)),
            duplicate_origins=sorted({o for o in covered if covered.count(o) > 1}))
    order = {o: i for i, o in enumerate(dims.origins)}
    items = sorted(parsed, key=lambda p: order[p.metadata["origins"][0]])
    buf = io.BytesIO()
    writer: Any = None
    parts: list[dict[str, Any]] = []
    schema: Any = None
    try:
        for p in items:
            long = to_long(p.frame, dims)
            parts.append(_partials(long, dims))
            table = long.to_arrow()
            if writer is None:
                schema = long.schema
                writer = pq.ParquetWriter(buf, table.schema, compression="zstd", compression_level=6,
                                          use_dictionary=True, write_statistics=False)
            writer.write_table(table, row_group_size=1_048_576)
            del long, table
        report, gross = verify_identities(parts, dims=dims)
        report.update({"dataset": dataset, "year": year})
        if not report["passed"]:
            raise IdentityCheckFailed("input-output accounting identities do not hold; the table is not published",
                                      report=report)
        x_rows = gross.join(_index_frame("country", dims.countries), on="country").join(
            _index_frame("industry", dims.industries), on="industry").sort("country_ord", "industry_ord").select(
            pl.lit("X").cast(schema["block"]).alias("block"),
            pl.col("country").cast(schema["row_country"]).alias("row_country"),
            pl.col("industry").cast(schema["row_code"]).alias("row_code"),
            pl.col("country").cast(schema["col_country"]).alias("col_country"),
            pl.lit(GROSS_OUTPUT).cast(schema["col_code"]).alias("col_code"),
            pl.col("value").cast(schema["value"]))
        writer.write_table(x_rows.to_arrow())
        n_rows = sum(int(p["rows"]) for p in parts) + x_rows.height
    finally:
        if writer is not None:
            writer.close()
    data = buf.getvalue()
    manifest = {
        "schema": DATASET_SCHEMA,
        "dataset_id": f"figaro.iciot_ii.{year}",
        "concept": DATASET_CONCEPT,
        "source": {"provider": "Eurostat", "programme": "FIGARO", "dataset": dataset, "edition_note":
                   "2026 edition (released July 2026) covers 2010-2024", "licence": LICENCE,
                   "source_key": SOURCE_KEY, "connector": f"{NAME}@{VERSION}"},
        "parser_version": PARSER_VERSION,
        "reference_year": year,
        "origin": origin,
        "acquisitions": [dict(a) for a in acquisitions],
        "unit": "million EUR, current prices, basic prices",
        "value_type": "decimal128(18,3): the provider's published 3-decimal values, unchanged",
        "columns": {
            "block": "Z intermediate (industry->industry), F final demand (industry->category), "
                     "V primary inputs (row_country=DOM), X gross output (derived: row total, verified equal to "
                     "column total)",
            "row_country": "supplying economy (c_orig) or DOM", "row_code": "ind_ava",
            "col_country": "using economy (c_dest)", "col_code": "ind_use or GROSS_OUTPUT"},
        "code_lists": {"economies": list(dims.countries), "industries": list(dims.industries),
                       "final_demand": list(dims.final_demand), "primary_inputs": list(dims.primary_inputs)},
        "economy_iso3": {c: ECONOMIES.get(c) for c in dims.countries},
        "row_count": n_rows,
        "identity_report": report,
    }
    return BuiltDataset(parquet=data, manifest=manifest, identity_report=report, row_count=n_rows,
                        build_sha256="sha256:" + hashlib.sha256(data).hexdigest())


def gzip_csv(rows: Iterable[Sequence[Any]]) -> bytes:
    """Encode rows in the provider's SDMX-CSV layout (used for synthetic test tables only)."""
    out = io.StringIO()
    out.write(",".join(CSV_HEADER) + "\n")
    for r in rows:
        out.write(",".join("" if v is None else str(v) for v in r) + "\n")
    return gzip.compress(out.getvalue().encode(), mtime=0)


# --------------------------------------------------------------------------- dataset publication (DB)
DATASET_SUBJECT = "WORLD"


def dataset_key(year: int) -> str:
    return f"figaro.iciot_ii.{year}"


def latest_data_acquisitions(conn: Any, year: int, origin: str = "live") -> list[dict[str, Any]]:
    """Latest passing acquisition per data item of ``year`` (quarantined bytes never used)."""
    from sqlalchemy import text

    rows = conn.execute(text(
        """SELECT DISTINCT ON (a.response_metadata->>'item_key')
                  a.id::text AS acquisition_id, a.raw_sha256, a.response_metadata->>'item_key' AS item_key,
                  a.response_metadata->'quality' AS quality, a.recorded_at
           FROM evidence.acquisition a JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
           WHERE cv.name = :n AND a.origin = :o AND a.response_metadata->>'item_key' LIKE :pat
           ORDER BY a.response_metadata->>'item_key', a.recorded_at DESC, a.id DESC"""),
        {"n": NAME, "o": origin, "pat": f"data:%:{year}:%"}).mappings().all()
    out = []
    for r in rows:
        quality = r["quality"] or []
        if any(q.get("outcome") != "pass" for q in quality):
            continue
        out.append({"acquisition_id": r["acquisition_id"], "raw_sha256": r["raw_sha256"],
                    "item_key": r["item_key"]})
    return out


def publish_io_dataset(engine: Any, store: Any, *, year: int, origin: str = "live",
                       actor: str = "worker:io_tables", dims: TableDims = FIGARO_DIMS) -> dict[str, Any]:
    """Parse the archived raw bytes of one year, verify, and register the dataset input.

    Refuses (``IdentityCheckFailed``, nothing registered) when an item is missing, failed
    its checks, or an accounting identity does not hold. Returns the registration summary
    with the identity report.
    """
    import hashlib

    from reva.simulation.datasets import register_dataset

    dataset = dataset_for_year(year)
    with engine.connect() as conn:
        acqs = latest_data_acquisitions(conn, year, origin)
    parsed: list[ParsedItem] = []
    for a in acqs:
        body = store.get("raw", a["raw_sha256"])
        if hashlib.sha256(body).hexdigest() != a["raw_sha256"]:
            raise IdentityCheckFailed("archived raw bytes do not match their sha256", **a)
        origins = a["item_key"].rsplit(":", 1)[1].split("+")
        parsed.append(parse_sdmx_csv(body, dataset=dataset, year=year, origins=origins, dims=dims))
        del body
    built = build_dataset(parsed, dataset=dataset, year=year, acquisitions=acqs, origin=origin, dims=dims)
    del parsed
    table = built.table()
    with engine.begin() as conn:
        rec = register_dataset(conn, store, dataset_key=dataset_key(year), concept_id=DATASET_CONCEPT,
                               subject_key=DATASET_SUBJECT, table=table,
                               source_acquisition_ids=[a["acquisition_id"] for a in acqs],
                               parser_version=PARSER_VERSION, origin=origin, coverage=built.manifest, actor=actor,
                               rights={"licence": LICENCE, "attribution": "Source: Eurostat (FIGARO)",
                                       "source_key": SOURCE_KEY})
    return {"dataset_id": str(rec.id), "dataset_key": rec.dataset_key, "artifact_sha256": rec.artifact_sha256,
            "manifest_sha256": rec.manifest_sha256, "row_count": rec.row_count, "created": rec.created,
            "identity_report": built.identity_report, "acquisitions": len(acqs)}
