"""UN World Population Prospects 2024 demography connector (``un.wpp``; V3 agent SIMWORLD).

Reviewed built-in connector feeding ``demography.cohort_component@1.1.0`` for every
country of WPP 2024 (237 countries/areas), the same way ``eurostat.demography`` feeds
the 1.0.0 EU path: raw bytes archived content-addressed by the evidence ingest
service, Parquet *dataset inputs* derived from those bytes and registered with
:func:`reva.simulation.datasets.register_dataset`, pinned by belief releases.

Source (UN DESA Population Division, licence CC BY 3.0 IGO; see
``registry/sources/un_wpp.yaml``): the official WPP 2024 CSV bulk files on
``population.un.org`` (files dated 2024-12-13):

* ``WPP2024_Population1JanuaryBySingleAgeSex_Medium_1950-2023.csv.gz`` (62.1 MB): population
  on **1 January** by single age 0..99 and 100+, by sex, in thousands (3 decimals = persons);
* ``WPP2024_DeathsBySingleAgeSex_Medium_1950-2023.csv.gz`` (45.2 MB): deaths during the
  calendar year by single age at death 0..99 and 100+, by sex (thousands);
* ``WPP2024_Fertility_by_Age5.csv.gz`` (82.9 MB): births by five-year age group of the mother
  (10-14 .. 50-54), all variants 1950-2100 (only the Medium variant and years <= 2023, the
  estimates, are kept);
* ``WPP2024_Demographic_Indicators_Medium.csv.gz`` (16.6 MB): total births, sex ratio at birth
  (SRB), deaths, net migration, TFR, life expectancy ... per year.

WPP *estimates* (1950-2023) are themselves model-based reconstructions (census, survey and
register inputs reconciled by the Population Division's own cohort-component model); they
are not observations. Every table says so and the back-test against them checks
consistency, not truth.

Fetching. The files exceed the per-response bound of the connector contract (50 MiB), so
each file is fetched in byte ranges of at most :data:`CHUNK_BYTES` (HTTP ``Range``; the
server answers ``206 Partial Content``, checked live 2026-10-15). Every range is one
request and one archived acquisition; the request URL carries the range as a query
parameter (``?bytes=a-b``, ignored by the server) so that each part has its own request
fingerprint. File size and ETag are pinned (reviewed 2026-10-15): a different size or
ETag means a revised file and blocks the part (a revised WPP needs a reviewed connector
version). The gzip CRC-32 and length trailer verify the reassembled file when it is
decompressed.

Dataset inputs (schema ``reva-wpp-demography-table/1``, one Parquet table per (dataset,
country), years :data:`FIRST_YEAR`..:data:`LAST_YEAR`):

* ``wpp2024_pop_jan1`` -> ``demography.wpp.population.jan1_age_sex``
* ``wpp2024_deaths`` -> ``demography.wpp.deaths.age_sex``
* ``wpp2024_births`` -> ``demography.wpp.live_births.mother_age5_newborn_sex`` (births by mother's
  five-year age group, sex "T"; and births by newborn sex **derived** as
  ``Births x SRB / (100 + SRB)`` from the Demographic Indicators file, SRB published to 0.1:
  marked in the ``derivation`` column)
* ``wpp2024_indicators`` -> ``demography.wpp.indicators_summary`` (totals and summary indicators,
  diagnostics only)

Values: WPP publishes thousands with three decimals; ``value`` is the exact person count
(the decimal point shifted by three places, no rounding) and ``value_thousands`` the
verbatim cell. Age codes follow the Eurostat-style codes the cohort model reads
(``Y0``..``Y99``, ``Y_GE100``, ``Y10-14``, ``TOTAL``). A country whose table lacks any
(year, sex, age) cell is not registered (listed in the coverage report); nothing is
imputed. Aggregates (regions, income groups, World) and locations outside the country
crosswalk (e.g. ``TWN``) are counted and listed, never mapped silently.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from reva.adapters.connectors.base import module_digest
from reva.kernel.errors import SourceResponseError
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

NAME = "un.wpp"
VERSION = "1.0.0"
PARSER_VERSION = "un-wpp2024-csv-1"
#: The connector contract's template literal (src/reva/ports/connectors.py, digest-pinned) has no
#: neutral "bulk file" value; reviewed V2 built-ins that are never autonomy-generated use
#: ``eurostat_sdmx_csv`` (tests/contracts/test_json_schemas.py V2_BUILTIN_TEMPLATES). The value
#: only selects the (absent) template post-publish hook; this connector brings its own.
TEMPLATE = "eurostat_sdmx_csv"
SOURCE_KEY = "un.wpp"
HOSTS: tuple[str, ...] = ("population.un.org",)
BASE = "https://population.un.org/wpp/assets/Excel%20Files/1_Indicator%20(Standard)/CSV_FILES/"
LICENCE = "CC BY 3.0 IGO (UN DESA Population Division, World Population Prospects 2024)"
ATTRIBUTION = (
    "United Nations, Department of Economic and Social Affairs, Population Division (2024). "
    "World Population Prospects 2024."
)
CHUNK_BYTES = 48 * 1024 * 1024  # < the 50 MiB per-response bound of the connector contract
MAX_RESPONSE_BYTES = CHUNK_BYTES
FIRST_YEAR = 1990
LAST_YEAR = 2023  # last estimate year of WPP 2024 (1 January 2023 population; 2023 events)
OPEN_AGE = 100
DATASET_SCHEMA = "reva-wpp-demography-table/1"
VARIANT = "Medium"
COUNTRY_LOC_TYPE = "Country/Area"
ESTIMATE_LABEL = (
    "WPP 2024 estimate (model-based reconstruction by the UN Population Division from censuses, surveys "
    "and registers; not an observation)"
)
CROSSWALK = Path(__file__).resolve().parents[4] / "registry" / "entities" / "country_crosswalk.json"
GZIP_MAGIC = b"\x1f\x8b"


@dataclass(frozen=True)
class WppFile:
    key: str
    file: str
    size: int
    etag: str | None  # pinned strong ETag (None: not checked, e.g. fixture files)
    last_modified: str
    header: tuple[str, ...]


_ID_COLS = (
    "SortOrder",
    "LocID",
    "Notes",
    "ISO3_code",
    "ISO2_code",
    "SDMX_code",
    "LocTypeID",
    "LocTypeName",
    "ParentID",
    "Location",
    "VarID",
    "Variant",
    "Time",
)
_AGE_COLS = ("MidPeriod", "AgeGrp", "AgeGrpStart", "AgeGrpSpan")
INDICATOR_HEADER = (
    *_ID_COLS,
    "TPopulation1Jan",
    "TPopulation1July",
    "TPopulationMale1July",
    "TPopulationFemale1July",
    "PopDensity",
    "PopSexRatio",
    "MedianAgePop",
    "NatChange",
    "NatChangeRT",
    "PopChange",
    "PopGrowthRate",
    "DoublingTime",
    "Births",
    "Births1519",
    "CBR",
    "TFR",
    "NRR",
    "MAC",
    "SRB",
    "Deaths",
    "DeathsMale",
    "DeathsFemale",
    "CDR",
    "LEx",
    "LExMale",
    "LExFemale",
    "LE15",
    "LE15Male",
    "LE15Female",
    "LE65",
    "LE65Male",
    "LE65Female",
    "LE80",
    "LE80Male",
    "LE80Female",
    "InfantDeaths",
    "IMR",
    "LBsurvivingAge1",
    "Under5Deaths",
    "Q5",
    "Q0040",
    "Q0040Male",
    "Q0040Female",
    "Q0060",
    "Q0060Male",
    "Q0060Female",
    "Q1550",
    "Q1550Male",
    "Q1550Female",
    "Q1560",
    "Q1560Male",
    "Q1560Female",
    "NetMigrations",
    "CNMR",
)

#: Reviewed 2026-10-15 (HEAD requests: size, ETag, Last-Modified; headers of the first bytes).
FILES: tuple[WppFile, ...] = (
    WppFile(
        "pop",
        "WPP2024_Population1JanuaryBySingleAgeSex_Medium_1950-2023.csv.gz",
        62107912,
        '"0x8DD1BA9F7BFACA3"',
        "Fri, 13 Dec 2024 19:11:37 GMT",
        (*_ID_COLS, *_AGE_COLS, "PopMale", "PopFemale", "PopTotal"),
    ),
    WppFile(
        "deaths",
        "WPP2024_DeathsBySingleAgeSex_Medium_1950-2023.csv.gz",
        45178348,
        '"0x8DD1BA9FE2AFD0C"',
        "Fri, 13 Dec 2024 19:11:48 GMT",
        (*_ID_COLS, *_AGE_COLS, "DeathMale", "DeathFemale", "DeathTotal"),
    ),
    WppFile(
        "fert5",
        "WPP2024_Fertility_by_Age5.csv.gz",
        82864610,
        '"0x8DD1BA9FE930DAD"',
        "Fri, 13 Dec 2024 19:11:49 GMT",
        (*_ID_COLS, *_AGE_COLS, "ASFR", "PASFR", "Births"),
    ),
    WppFile(
        "indicators",
        "WPP2024_Demographic_Indicators_Medium.csv.gz",
        16557272,
        '"0x8DD1BA9EBFD1565"',
        "Fri, 13 Dec 2024 19:11:17 GMT",
        INDICATOR_HEADER,
    ),
)


@dataclass(frozen=True)
class DatasetDef:
    key: str
    concept_id: str
    concept_version: str
    files: tuple[str, ...]
    label: str


DATASETS: dict[str, DatasetDef] = {
    "wpp2024_pop_jan1": DatasetDef(
        "wpp2024_pop_jan1",
        "demography.wpp.population.jan1_age_sex",
        "1.0.0",
        ("pop",),
        "population on 1 January by single age (0-99, 100+) and sex",
    ),
    "wpp2024_deaths": DatasetDef(
        "wpp2024_deaths",
        "demography.wpp.deaths.age_sex",
        "1.0.0",
        ("deaths",),
        "deaths during the calendar year by single age at death (0-99, 100+) and sex",
    ),
    "wpp2024_births": DatasetDef(
        "wpp2024_births",
        "demography.wpp.live_births.mother_age5_newborn_sex",
        "1.0.0",
        ("fert5", "indicators"),
        "births by mother's five-year age group (10-14 .. 50-54) and births by newborn sex (derived from SRB)",
    ),
    "wpp2024_indicators": DatasetDef(
        "wpp2024_indicators",
        "demography.wpp.indicators_summary",
        "1.0.0",
        ("indicators",),
        "total births, deaths, net migration, sex ratio at birth, TFR and life expectancy (diagnostics)",
    ),
}

DATASET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dataset", "string"),
    ("iso3", "string"),
    ("loc_id", "int32"),
    ("variant", "string"),
    ("year", "int32"),
    ("sex", "string"),
    ("age", "string"),
    ("age_kind", "string"),
    ("age_start", "int32"),
    ("age_end", "int32"),
    ("cell_type", "string"),
    ("value", "string"),
    ("value_thousands", "string"),
    ("unit", "string"),
    ("status", "string"),
    ("notes", "string"),
    ("derivation", "string"),
)

#: Demographic Indicators kept in the summary table: (column, unit, scale to persons?)
SUMMARY_INDICATORS: tuple[tuple[str, str, bool], ...] = (
    ("TPopulation1Jan", "persons", True),
    ("Births", "persons", True),
    ("Deaths", "persons", True),
    ("DeathsMale", "persons", True),
    ("DeathsFemale", "persons", True),
    ("NetMigrations", "persons", True),
    ("SRB", "males per 100 female births", False),
    ("TFR", "live births per woman", False),
    ("LEx", "years", False),
    ("LExMale", "years", False),
    ("LExFemale", "years", False),
    ("CNMR", "net migrants per 1000 population", False),
)
SEX_COLUMNS = {"pop": ("PopFemale", "PopMale"), "deaths": ("DeathFemale", "DeathMale")}
BIRTHS_BY_SEX_DERIVATION = (
    "derived: births by newborn sex = WPP Births x SRB / (100 + SRB) (male) and Births - male (female); "
    "WPP Demographic Indicators, SRB published to one decimal"
)
_CTX = Context(prec=50)
_DEC = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


# --------------------------------------------------------------------------- helpers
def _fail(msg: str, kind: str = "schema_drift", **details: Any) -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind=kind, **details)


def dec_text(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def thousands_to_persons(text: str) -> str:
    """Exact person count from a WPP 'thousands' cell (decimal point shifted, never rounded)."""
    if not _DEC.match(text):
        raise _fail(f"non-numeric WPP value {text[:40]!r}")
    return dec_text(Decimal(text).scaleb(3))


@lru_cache(maxsize=1)
def crosswalk_iso3() -> dict[str, str | None]:
    """ISO3 / registry country key -> UN M49 code (crosswalk entities of kind entity.country)."""
    doc = json.loads(CROSSWALK.read_text(encoding="utf-8"))
    return {
        str(e["key"]): (str(e["m49"]) if e.get("m49") else None)
        for e in doc["entities"]
        if e.get("kind") == "entity.country"
    }


def chunk_ranges(size: int, chunk: int = CHUNK_BYTES) -> list[tuple[int, int]]:
    """Inclusive byte ranges covering a file of ``size`` bytes."""
    if size <= 0 or chunk <= 0:
        raise ValueError("size and chunk must be positive")
    return [(a, min(a + chunk, size) - 1) for a in range(0, size, chunk)]


class _Chunks(io.RawIOBase):
    """Read-only stream over the concatenation of archived byte ranges (no copy)."""

    def __init__(self, parts: Sequence[bytes]) -> None:
        self._parts = list(parts)
        self._i = 0
        self._off = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        while self._i < len(self._parts):
            part = self._parts[self._i]
            if self._off < len(part):
                n = min(len(b), len(part) - self._off)
                b[:n] = part[self._off : self._off + n]
                self._off += n
                return n
            self._i += 1
            self._off = 0
        return 0


def iter_rows(parts: Sequence[bytes], header: Sequence[str]) -> Iterator[list[str]]:
    """CSV rows of the gzip file made of ``parts`` (header checked against the pinned one)."""
    if not parts or parts[0][:2] != GZIP_MAGIC:
        raise _fail("WPP file does not start with the gzip magic bytes")
    try:
        text = io.TextIOWrapper(
            gzip.GzipFile(fileobj=io.BufferedReader(_Chunks(parts), 1 << 20)), encoding="utf-8-sig", newline=""
        )
        reader = csv.reader(text)
        got = next(reader)
        if tuple(got) != tuple(header):
            raise _fail(f"WPP header drift: {got[:20]} != pinned {list(header)[:20]}")
        n = len(header)
        for row in reader:
            if len(row) != n:
                raise _fail(f"WPP row with {len(row)} fields (header has {n})")
            yield row
    except (OSError, EOFError, UnicodeDecodeError) as e:  # bad CRC, truncated stream, encoding
        raise _fail(f"WPP file could not be decompressed/decoded: {type(e).__name__}: {e}", kind="integrity") from e


@dataclass
class ParseReport:
    rows_read: int = 0
    rows_kept: int = 0
    aggregate_rows: int = 0
    other_variant_rows: int = 0
    out_of_range_year_rows: int = 0
    unmapped_locations: dict[str, str] | None = None  # ISO3 or LocID -> name
    m49_mismatches: dict[str, Any] | None = None
    additivity_mismatches: int = 0
    max_additivity_diff_thousands: str = "0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "rows_kept": self.rows_kept,
            "aggregate_rows": self.aggregate_rows,
            "other_variant_rows": self.other_variant_rows,
            "out_of_range_year_rows": self.out_of_range_year_rows,
            "unmapped_locations": dict(sorted((self.unmapped_locations or {}).items())),
            "m49_mismatches": dict(sorted((self.m49_mismatches or {}).items())),
            "additivity_mismatches": self.additivity_mismatches,
            "max_additivity_diff_thousands": self.max_additivity_diff_thousands,
        }


_IV, _ILT, _IISO, _ITIME = (_ID_COLS.index(c) for c in ("Variant", "LocTypeName", "ISO3_code", "Time"))


def _prefilter(r: Sequence[str], rep: ParseReport, first: int, last: int) -> bool:
    """Cheap row filter on the identifier columns (counted), before any per-row dict is built."""
    rep.rows_read += 1
    if r[_IV] != VARIANT:
        rep.other_variant_rows += 1
        return False
    if r[_ILT] != COUNTRY_LOC_TYPE or not r[_IISO]:
        rep.aggregate_rows += 1
        return False
    if not first <= int(r[_ITIME]) <= last:
        rep.out_of_range_year_rows += 1
        return False
    return True


def _keep(rec: Mapping[str, str], rep: ParseReport, countries: Iterable[str] | None) -> str | None:
    """The crosswalk key of a pre-filtered row, or None (unmapped locations are listed)."""
    iso3 = rec["ISO3_code"]
    xw = crosswalk_iso3()
    if iso3 not in xw:
        rep.unmapped_locations = {**(rep.unmapped_locations or {}), iso3: rec["Location"]}
        return None
    if xw[iso3] is not None and str(int(rec["LocID"])) != str(int(xw[iso3] or 0)):
        rep.m49_mismatches = {**(rep.m49_mismatches or {}), iso3: {"wpp_loc_id": rec["LocID"], "crosswalk_m49": xw[iso3]}}
    if countries is not None and iso3 not in countries:
        return None
    rep.rows_kept += 1
    return iso3


def _row(
    dataset: str,
    iso3: str,
    loc_id: int,
    year: int,
    sex: str,
    age: str,
    kind: str,
    start: int | None,
    end: int | None,
    cell_type: str,
    value: str | None,
    thousands: str | None,
    unit: str,
    notes: str,
    derivation: str = "",
) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "iso3": iso3,
        "loc_id": loc_id,
        "variant": VARIANT,
        "year": year,
        "sex": sex,
        "age": age,
        "age_kind": kind,
        "age_start": start,
        "age_end": end,
        "cell_type": cell_type,
        "value": value,
        "value_thousands": thousands,
        "unit": unit,
        "status": "",
        "notes": notes,
        "derivation": derivation,
    }


def age_cell(age_grp: str, start: str, span: str) -> tuple[str, str, int, int | None]:
    """(code, kind, start, end exclusive) of a WPP single-age row (0..99 single, 100+ open)."""
    a = int(start)
    if span == "1" and age_grp == str(a) and 0 <= a < OPEN_AGE:
        return f"Y{a}", "single", a, a + 1
    if span == "-1" and age_grp == f"{OPEN_AGE}+" and a == OPEN_AGE:
        return f"Y_GE{OPEN_AGE}", "open_upper", OPEN_AGE, None
    raise _fail(f"unreviewed WPP age group {age_grp!r} (start {start}, span {span})")


# --------------------------------------------------------------------------- pure parsers
def parse_single_age(
    file_key: str,
    parts: Sequence[bytes],
    *,
    first_year: int = FIRST_YEAR,
    last_year: int = LAST_YEAR,
    countries: Iterable[str] | None = None,
    header: Sequence[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], ParseReport]:
    """Population (``pop``) or deaths (``deaths``) rows per country, F and M (exact persons)."""
    wf = {f.key: f for f in FILES}[file_key]
    dataset = "wpp2024_pop_jan1" if file_key == "pop" else "wpp2024_deaths"
    cols = list(header or wf.header)
    ix = {c: i for i, c in enumerate(cols)}
    fcol, mcol = SEX_COLUMNS[file_key]
    tcol = "PopTotal" if file_key == "pop" else "DeathTotal"
    rep = ParseReport()
    out: dict[str, list[dict[str, Any]]] = {}
    max_diff = Decimal(0)
    wanted = set(countries) if countries is not None else None
    for r in iter_rows(parts, cols):
        if not _prefilter(r, rep, first_year, last_year):
            continue
        rec = {c: r[i] for c, i in ix.items()}
        iso3 = _keep(rec, rep, wanted)
        if iso3 is None:
            continue
        code, kind, a, b = age_cell(rec["AgeGrp"], rec["AgeGrpStart"], rec["AgeGrpSpan"])
        year, loc = int(rec["Time"]), int(rec["LocID"])
        f, m, t = rec[fcol], rec[mcol], rec[tcol]
        for v in (f, m, t):
            if not _DEC.match(v) or v.startswith("-"):
                raise _fail(f"{dataset} {iso3} {year} {code}: value {v[:30]!r} is not a non-negative decimal")
        diff = abs(Decimal(f) + Decimal(m) - Decimal(t))
        if diff > Decimal("0.0015"):  # one unit in the last published decimal of each part
            rep.additivity_mismatches += 1
            max_diff = max(max_diff, diff)
        rows = out.setdefault(iso3, [])
        for sex, v in (("F", f), ("M", m)):
            rows.append(
                _row(dataset, iso3, loc, year, sex, code, kind, a, b, "leaf", thousands_to_persons(v), v, "persons", rec["Notes"])
            )
    rep.max_additivity_diff_thousands = dec_text(max_diff)
    return out, rep


def parse_fertility5(
    parts: Sequence[bytes],
    *,
    first_year: int = FIRST_YEAR,
    last_year: int = LAST_YEAR,
    countries: Iterable[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], ParseReport]:
    """Births by mother's five-year age group (sex "T"), Medium variant estimates only."""
    wf = {f.key: f for f in FILES}["fert5"]
    ix = {c: i for i, c in enumerate(wf.header)}
    rep = ParseReport()
    out: dict[str, list[dict[str, Any]]] = {}
    wanted = set(countries) if countries is not None else None
    for r in iter_rows(parts, wf.header):
        if not _prefilter(r, rep, first_year, last_year):
            continue
        rec = {c: r[i] for c, i in ix.items()}
        iso3 = _keep(rec, rep, wanted)
        if iso3 is None:
            continue
        a, span = int(rec["AgeGrpStart"]), rec["AgeGrpSpan"]
        if span != "5" or rec["AgeGrp"] != f"{a}-{a + 4}" or not 10 <= a <= 50 or a % 5:
            raise _fail(f"unreviewed WPP mother's age group {rec['AgeGrp']!r}")
        v = rec["Births"]
        if not _DEC.match(v) or v.startswith("-"):
            raise _fail(f"births {iso3} {rec['Time']} {rec['AgeGrp']}: {v[:30]!r} is not a non-negative decimal")
        out.setdefault(iso3, []).append(
            _row(
                "wpp2024_births",
                iso3,
                int(rec["LocID"]),
                int(rec["Time"]),
                "T",
                f"Y{a}-{a + 4}",
                "group",
                a,
                a + 5,
                "leaf",
                thousands_to_persons(v),
                v,
                "persons",
                rec["Notes"],
            )
        )
    return out, rep


def parse_indicators(
    parts: Sequence[bytes],
    *,
    first_year: int = FIRST_YEAR,
    last_year: int = LAST_YEAR,
    countries: Iterable[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], ParseReport]:
    """(summary rows, births-by-sex rows) per country from the Demographic Indicators file."""
    ix = {c: i for i, c in enumerate(INDICATOR_HEADER)}
    rep = ParseReport()
    summary: dict[str, list[dict[str, Any]]] = {}
    by_sex: dict[str, list[dict[str, Any]]] = {}
    wanted = set(countries) if countries is not None else None
    for r in iter_rows(parts, INDICATOR_HEADER):
        if not _prefilter(r, rep, first_year, last_year):
            continue
        rec = {c: r[i] for c, i in ix.items()}
        iso3 = _keep(rec, rep, wanted)
        if iso3 is None:
            continue
        year, loc = int(rec["Time"]), int(rec["LocID"])
        rows = summary.setdefault(iso3, [])
        for col, unit, scale in SUMMARY_INDICATORS:
            v = rec[col]
            if v == "":
                rows.append(_row("wpp2024_indicators", iso3, loc, year, "T", col, "indicator", None, None, "total", None, None, unit, rec["Notes"]))
                continue
            if not _DEC.match(v):
                raise _fail(f"indicator {col} {iso3} {year}: {v[:30]!r} is not a decimal")
            rows.append(
                _row(
                    "wpp2024_indicators",
                    iso3,
                    loc,
                    year,
                    "T",
                    col,
                    "indicator",
                    None,
                    None,
                    "total",
                    thousands_to_persons(v) if scale else v,
                    v if scale else None,
                    unit,
                    rec["Notes"],
                )
            )
        births, srb = rec["Births"], rec["SRB"]
        if not (_DEC.match(births) and _DEC.match(srb)) or births.startswith("-") or Decimal(srb) <= 0:
            raise _fail(f"births/SRB {iso3} {year}: {births!r} / {srb!r}")
        b = Decimal(births).scaleb(3)
        male = _CTX.divide(_CTX.multiply(b, Decimal(srb)), _CTX.add(Decimal(100), Decimal(srb)))
        male = male.quantize(Decimal("1e-6"))
        female = b - male
        rows_s = by_sex.setdefault(iso3, [])
        rows_s.append(_row("wpp2024_births", iso3, loc, year, "T", "TOTAL", "total", None, None, "total", dec_text(b), births, "persons", rec["Notes"], "verbatim: WPP Demographic Indicators Births"))
        for sex, v in (("M", male), ("F", female)):
            rows_s.append(_row("wpp2024_births", iso3, loc, year, sex, "TOTAL", "total", None, None, "total", dec_text(v), None, "persons", rec["Notes"], BIRTHS_BY_SEX_DERIVATION))
    return summary, by_sex, rep


def rows_to_table(rows: Sequence[Mapping[str, Any]]) -> Any:
    """Arrow table with the pinned :data:`DATASET_COLUMNS` schema, deterministic row order."""
    import pyarrow as pa

    schema = pa.schema([(name, getattr(pa, typ)()) for name, typ in DATASET_COLUMNS])
    ordered = sorted(
        rows,
        key=lambda r: (r["dataset"], r["iso3"], r["year"], r["sex"], r["age_start"] if r["age_start"] is not None else -1, r["age"]),
    )
    return pa.table({name: [r.get(name) for r in ordered] for name, _ in DATASET_COLUMNS}, schema=schema)


def check_complete(dataset: str, rows: Sequence[Mapping[str, Any]], first_year: int, last_year: int) -> list[str]:
    """Problems that make a country's table incomplete (an incomplete table is never registered)."""
    years = range(first_year, last_year + 1)
    have: dict[tuple[int, str], set[str]] = {}
    for r in rows:
        if r["value"] is None and dataset != "wpp2024_indicators":
            return [f"{r['year']} {r['sex']} {r['age']}: empty value"]
        have.setdefault((r["year"], r["sex"]), set()).add(str(r["age"]))
    problems: list[str] = []
    if dataset in ("wpp2024_pop_jan1", "wpp2024_deaths"):
        ages = {f"Y{a}" for a in range(OPEN_AGE)} | {f"Y_GE{OPEN_AGE}"}
        for y in years:
            for s in ("F", "M"):
                missing = ages - have.get((y, s), set())
                if missing:
                    problems.append(f"{y} {s}: {len(missing)} age cells missing")
    elif dataset == "wpp2024_births":
        groups = {f"Y{a}-{a + 4}" for a in range(10, 55, 5)}
        for y in years:
            if groups - have.get((y, "T"), set()):
                problems.append(f"{y}: mother's age groups missing")
            for s in ("M", "F"):
                if "TOTAL" not in have.get((y, s), set()):
                    problems.append(f"{y} {s}: births by newborn sex missing")
    else:
        for y in years:
            if not have.get((y, "T")):
                problems.append(f"{y}: no indicators")
    return problems


def dataset_coverage(dataset: str, rows: Sequence[Mapping[str, Any]], report: ParseReport | None = None) -> dict[str, Any]:
    years = sorted({r["year"] for r in rows})
    return {
        "dataset": dataset,
        "label": DATASETS[dataset].label,
        "years": [years[0], years[-1]] if years else [],
        "sexes": sorted({r["sex"] for r in rows}),
        "open_age": f"{OPEN_AGE}+" if dataset in ("wpp2024_pop_jan1", "wpp2024_deaths") else None,
        "variant": VARIANT,
        "estimate_label": ESTIMATE_LABEL,
        "derived_rows": sum(1 for r in rows if str(r.get("derivation", "")).startswith("derived")),
        "wpp_notes": sorted({r["notes"] for r in rows if r.get("notes")}),
        "file_parse": report.as_dict() if report is not None else None,
    }


def build_tables(
    raws: Mapping[str, Sequence[bytes]],
    *,
    first_year: int = FIRST_YEAR,
    last_year: int = LAST_YEAR,
    countries: Iterable[str] | None = None,
    datasets: Iterable[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Pure: ``{file_key: [part bytes...]}`` -> ``({dataset: {iso3: arrow_table}}, report)``.

    Only datasets whose files are all given are built. A country whose table is incomplete
    is left out and listed in ``report[dataset]["incomplete"]`` (never imputed)."""
    wanted = set(datasets) if datasets is not None else set(DATASETS)
    out: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {}
    parsed: dict[str, tuple[dict[str, list[dict[str, Any]]], ParseReport]] = {}
    kw = {"first_year": first_year, "last_year": last_year, "countries": countries}
    for key in ("pop", "deaths"):
        ds = "wpp2024_pop_jan1" if key == "pop" else "wpp2024_deaths"
        if ds in wanted and key in raws:
            parsed[ds] = parse_single_age(key, raws[key], **kw)  # type: ignore[arg-type]
    if "indicators" in raws and ({"wpp2024_births", "wpp2024_indicators"} & wanted):
        summary, by_sex, rep_i = parse_indicators(raws["indicators"], **kw)  # type: ignore[arg-type]
        if "wpp2024_indicators" in wanted:
            parsed["wpp2024_indicators"] = (summary, rep_i)
        if "wpp2024_births" in wanted and "fert5" in raws:
            fert, rep_f = parse_fertility5(raws["fert5"], **kw)  # type: ignore[arg-type]
            merged = {k: [*fert.get(k, []), *by_sex.get(k, [])] for k in sorted(set(fert) | set(by_sex))}
            parsed["wpp2024_births"] = (merged, rep_f)
    for ds, (per_iso3, rep) in sorted(parsed.items()):
        tables: dict[str, Any] = {}
        incomplete: dict[str, list[str]] = {}
        for iso3, rows in sorted(per_iso3.items()):
            keys = [(r["year"], r["sex"], r["age"]) for r in rows]
            if len(keys) != len(set(keys)):
                raise _fail(f"{ds} {iso3}: duplicate (year, sex, age) cells")
            problems = check_complete(ds, rows, first_year, last_year)
            if problems:
                incomplete[iso3] = problems[:5]
                continue
            tables[iso3] = rows_to_table(rows)
        out[ds] = tables
        report[ds] = {"countries": len(tables), "incomplete": incomplete, "parse": rep.as_dict()}
    return out, report


# --------------------------------------------------------------------------- connector
class UnWppDemographyConnector:
    """Reviewed built-in connector for the WPP 2024 single-age demography files."""

    def __init__(
        self,
        files: Sequence[WppFile] = FILES,
        *,
        chunk_bytes: int = CHUNK_BYTES,
        first_year: int = FIRST_YEAR,
        last_year: int = LAST_YEAR,
        countries: Sequence[str] | None = None,
    ) -> None:
        if not 0 < chunk_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError(f"chunk_bytes must be in (0, {MAX_RESPONSE_BYTES}]")
        self.files = tuple(files)
        self.chunk_bytes = int(chunk_bytes)
        self.first_year, self.last_year = int(first_year), int(last_year)
        self.countries = tuple(sorted(countries)) if countries is not None else None
        self._prior_done: set[str] = set()

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name=NAME,
            version=VERSION,
            template=TEMPLATE,  # type: ignore[arg-type]
            source_key=SOURCE_KEY,
            provider="UN DESA Population Division",
            licence=LICENCE,
            allowed_hosts=HOSTS,
            dataset_ids=tuple(DATASETS),
            field_mappings={k: f"{d.concept_id}@{d.concept_version}" for k, d in DATASETS.items()},
            units={k: "unit.persons" for k in DATASETS},
            geography=tuple(sorted(crosswalk_iso3())) if self.countries is None else self.countries,
            time_semantics=(
                "population: 1 January point stock; deaths and births: calendar-year flow totals; WPP 2024 "
                "estimates 1990-2023 (model-based, not observations); no projection years are kept"
            ),
            lineage_family="un_wpp",
            cadence_seconds=30 * 86400,
            release_aware=True,
            revision_strategy="pinned file size and ETag; a revised WPP file blocks the parts (new connector version)",
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_pages=50,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(UnWppDemographyConnector),
        )

    # -------------------------------------------------------------- planning
    def all_items(self) -> list[FetchItem]:
        items: list[FetchItem] = []
        for f in self.files:
            ranges = chunk_ranges(f.size, self.chunk_bytes)
            for i, (a, b) in enumerate(ranges):
                items.append(
                    FetchItem(
                        key=f"{f.key}:{i}",
                        url=f"{BASE}{f.file}?bytes={a}-{b}",
                        expected_media_types=("application/x-gzip", "application/gzip", "application/octet-stream"),
                        max_bytes=self.chunk_bytes,
                        params={
                            "kind": "wpp_csv_gz_range",
                            "file_key": f.key,
                            "file": f.file,
                            "range": [a, b],
                            "total": f.size,
                            "part": i,
                            "parts": len(ranges),
                            "etag": f.etag,
                            "first_year": self.first_year,
                            "last_year": self.last_year,
                        },
                    )
                )
        return items

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        state = dict(cursor.state) if cursor is not None else {}
        done = set(state.get("done") or []) if not state.get("complete") else set()
        self._prior_done = done
        pending = [i for i in self.all_items() if i.key not in done]
        items = tuple(pending[: max(0, budget.max_requests)])
        return FetchPlan(
            connector=NAME,
            items=items,
            pagination="byte ranges of the pinned bulk files (HTTP Range, 206 Partial Content)",
            availability_expectation="WPP revisions are biennial (WPP 2024 files dated 2024-12-13)",
            budget_exhausted=len(items) < len(pending),
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        from reva.adapters.http.safe_client import request_fingerprint

        a, b = item.params["range"]
        result = await http.get(
            item.url, headers={"Range": f"bytes={a}-{b}"}, max_bytes=item.max_bytes, accept_status=(206,)
        )
        meta = result.response_metadata()
        meta["content_range"] = result.headers.get("content-range", "")
        meta["etag"] = result.headers.get("etag", "")
        meta["last_modified"] = result.headers.get("last-modified", "")
        return RawAcquisition(
            item=item,
            body=result.body,
            media_type=result.media_type or "application/octet-stream",
            retrieved_at=result.retrieved_at,
            response_metadata=meta,
            request_fingerprint=request_fingerprint("GET", item.url),
            origin=getattr(http, "origin", "live"),
        )

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        ok = {o.item_key for o in outcomes if o.status in ("published", "unchanged")}
        all_keys = [i.key for i in self.all_items()]
        done = sorted((set(self._prior_done) | ok) & set(all_keys))
        complete = len(done) == len(all_keys)
        return Cursor(state={"complete": complete, "done": [] if complete else done})

    # -------------------------------------------------------------- parsing / validation
    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        """No scalar observations: the parts are archived and assembled into dataset inputs."""
        p = raw.item.params
        a, b = p["range"]
        meta = {
            "kind": "wpp_csv_gz_range",
            "file_key": p["file_key"],
            "file": p["file"],
            "range": [a, b],
            "total": p["total"],
            "part": p["part"],
            "parts": p["parts"],
            "status": raw.response_metadata.get("status"),
            "content_range": raw.response_metadata.get("content_range", ""),
            "etag": raw.response_metadata.get("etag", ""),
            "pinned_etag": p.get("etag"),
            "byte_size": len(raw.body),
            "gzip_magic": raw.body[:2] == GZIP_MAGIC if p["part"] == 0 else None,
            "dataset_input": {
                "schema": DATASET_SCHEMA,
                "datasets": [k for k, d in DATASETS.items() if p["file_key"] in d.files],
            },
            "estimate_label": ESTIMATE_LABEL,
        }
        yield ObservationBatch(
            connector=NAME,
            connector_version=VERSION,
            item_key=raw.item.key,
            observations=(),
            metadata=meta,
            schema_fingerprint=f"{p['file']}:{p['total']}",
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        md = batch.metadata
        f: list[Finding] = []
        a, b = md["range"]
        if md.get("status") not in (206, None):
            f.append(Finding(code="not_partial_content", severity="blocker", message=f"HTTP {md.get('status')} != 206"))
        expected = f"bytes {a}-{b}/{md['total']}"
        if md.get("content_range") != expected:
            f.append(
                Finding(
                    code="content_range_mismatch",
                    severity="blocker",
                    message=f"Content-Range {md.get('content_range')!r} != {expected!r} (file revised or range ignored)",
                )
            )
        if md.get("byte_size") != b - a + 1:
            f.append(Finding(code="size_mismatch", severity="blocker", message=f"{md.get('byte_size')} bytes for range {a}-{b}"))
        if md.get("pinned_etag") and md.get("etag") and md["etag"] != md["pinned_etag"]:
            f.append(
                Finding(
                    code="file_revised",
                    severity="blocker",
                    message=f"ETag {md['etag']} != pinned {md['pinned_etag']}: a revised WPP file needs a reviewed connector version",
                )
            )
        if md.get("part") == 0 and not md.get("gzip_magic"):
            f.append(Finding(code="not_gzip", severity="blocker", message="first part lacks the gzip magic bytes"))
        return QualityReport(
            outcome="blocked" if any(x.severity == "blocker" for x in f) else "pass",
            findings=tuple(f),
            expected_count=0,
            received_count=0,
        )


# --------------------------------------------------------------------------- dataset registration
def _latest_parts(conn: Any, connector: UnWppDemographyConnector, file_key: str, origin: str) -> list[dict[str, Any]] | None:
    """Latest passing acquisition of every part of one file, or None while any is missing/blocked."""
    from sqlalchemy import text

    from reva.adapters.http.safe_client import request_fingerprint

    items = [i for i in connector.all_items() if i.params["file_key"] == file_key]
    fps = {request_fingerprint("GET", i.url): i for i in items}
    rows = (
        conn.execute(
            text(
                """SELECT DISTINCT ON (a.request_fingerprint) a.id, a.raw_sha256, a.request_fingerprint,
                          a.response_metadata
                   FROM evidence.acquisition a JOIN evidence.connector_version v ON v.id = a.connector_version_id
                   WHERE v.name = :n AND a.origin = :o AND a.request_fingerprint = ANY(:fps)
                   ORDER BY a.request_fingerprint, a.recorded_at DESC, a.id DESC"""
            ),
            {"n": NAME, "o": origin, "fps": list(fps)},
        )
        .mappings()
        .all()
    )
    latest = {r["request_fingerprint"]: dict(r) for r in rows}
    if set(latest) != set(fps):
        return None
    for r in latest.values():
        if any(q.get("outcome") != "pass" for q in (dict(r["response_metadata"] or {}).get("quality") or [])):
            return None
    return [dict(latest[fp], part=fps[fp].params["part"]) for fp in sorted(fps, key=lambda k: fps[k].params["part"])]


def assemble_datasets(
    conn: Any,
    store: Any,
    *,
    origin: str,
    connector: UnWppDemographyConnector | None = None,
    datasets: Iterable[str] | None = None,
) -> list[Any]:
    """Register every dataset input whose files are completely archived (worker role, caller's
    transaction). Idempotent: identical tables return the existing rows. ``[]`` while parts are missing."""
    from reva.kernel.ids import sha256_bytes
    from reva.simulation.datasets import register_dataset

    c = connector or UnWppDemographyConnector()
    wanted = [d for d in (datasets or DATASETS) if d in DATASETS]
    files_needed = sorted({f for d in wanted for f in DATASETS[d].files})
    parts: dict[str, list[dict[str, Any]]] = {}
    for fk in files_needed:
        got = _latest_parts(conn, c, fk, origin)
        if got is not None:
            parts[fk] = got
    ready = [d for d in wanted if all(f in parts for f in DATASETS[d].files)]
    if not ready:
        return []
    raws: dict[str, list[bytes]] = {}
    for fk in sorted({f for d in ready for f in DATASETS[d].files}):
        bodies = []
        for p in parts[fk]:
            body = store.get("raw", p["raw_sha256"])
            if sha256_bytes(body) != p["raw_sha256"]:
                raise _fail("archived raw bytes do not match their hash", kind="integrity")
            bodies.append(body)
        raws[fk] = bodies
    tables, report = build_tables(
        raws, first_year=c.first_year, last_year=c.last_year, countries=c.countries, datasets=ready
    )
    out = []
    for ds in ready:
        acq_ids = sorted(str(p["id"]) for f in DATASETS[ds].files for p in parts[f])
        for iso3, table in sorted(tables.get(ds, {}).items()):
            rows = table.to_pylist()
            out.append(
                register_dataset(
                    conn,
                    store,
                    dataset_key=f"un.wpp.{ds}",
                    concept_id=DATASETS[ds].concept_id,
                    subject_key=iso3,
                    table=table,
                    source_acquisition_ids=acq_ids,
                    parser_version=PARSER_VERSION,
                    origin=origin,
                    coverage={**dataset_coverage(ds, rows), "file_report": report.get(ds, {}).get("parse")},
                    actor=f"connector:{NAME}",
                    rights={"licence": LICENCE, "attribution": ATTRIBUTION, "source_key": SOURCE_KEY},
                )
            )
    return out


def post_publish_datasets(
    conn: Any, store: Any, raw: RawAcquisition, batches: Sequence[ObservationBatch], summary: Any
) -> None:
    """Register the dataset inputs this part completes (inside the acquisition transaction).

    A dataset is registered when the last part of the last file it needs is published, from the
    archived bytes of all parts (:func:`build_tables` is a pure function of those bytes)."""
    p = raw.item.params
    connector = UnWppDemographyConnector(
        first_year=int(p.get("first_year", FIRST_YEAR)), last_year=int(p.get("last_year", LAST_YEAR))
    )
    if (p["file"], p["total"]) not in {(f.file, f.size) for f in connector.files}:
        return  # a fixture/test file: registered by the caller with its own connector instance
    fk = str(p["file_key"])
    assemble_datasets(
        conn, store, origin=raw.origin, connector=connector, datasets=[d for d, dd in DATASETS.items() if fk in dd.files]
    )


UnWppDemographyConnector.post_publish = staticmethod(post_publish_datasets)  # type: ignore[attr-defined]


def post_publish_for_connector(connector: UnWppDemographyConnector) -> Any:
    """Post-publish hook bound to a connector instance (fixture files, country subsets)."""

    def hook(conn: Any, store: Any, raw: RawAcquisition, batches: Sequence[ObservationBatch], summary: Any) -> None:
        fk = str(raw.item.params["file_key"])
        assemble_datasets(
            conn,
            store,
            origin=raw.origin,
            connector=connector,
            datasets=[d for d, dd in DATASETS.items() if fk in dd.files],
        )

    return hook


def backfill_datasets(conn: Any, store: Any, *, origin: str = "live", connector: UnWppDemographyConnector | None = None) -> list[Any]:
    """Assemble and register every complete dataset input from archived acquisitions."""
    return assemble_datasets(conn, store, origin=origin, connector=connector)
