"""Built-in reviewed connector ``eurostat.demography`` (Eurostat JSON-stat 2.0, V2 step 3a).

Reviewed code (not an autonomy template instance: those are named
``eurostat.<dataset>.<geo>``; this name has no third dot-component and cannot
collide). It fetches, for the EU-27 and EFTA countries, from the Eurostat
dissemination API (``DATA_BASE``):

=============== ========================================================= ================
dataset         content                                                   becomes
=============== ========================================================= ================
demo_pjan       population on 1 January by single age and sex             observations +
demo_magec      deaths by age (completed years) and sex, calendar year     dataset inputs
demo_fasec      live births by mother's age and newborn's sex, cal. year
proj_23nanmig   EUROPOP2023 net-migration assumption by age and sex (BSL)  dataset inputs
proj_23naasfr   EUROPOP2023 fertility-rate assumption by age (BSL)          only (projection
proj_23np       EUROPOP2023 projected population by age and sex (BSL)      assumptions are
                                                                          not observations)
=============== ========================================================= ================

One request per (observed dataset, country) with ``sinceTimePeriod=2000`` (so
"latest" needs no code change) and one request per EUROPOP dataset for all
countries (the projections cover 2022+; Liechtenstein is not projected by
Eurostat and stays a typed gap). 31 countries -> 96 requests per full run.

Semantics preserved from the provider (same rules as the reviewed
``eurostat_jsonstat`` template):

* dimension order, unit and frequency are pinned; any other is schema drift;
* status flags (``p`` provisional, ``e`` estimated, ``b`` break, ...) are kept
  verbatim on every observation and every dataset row; an unknown flag blocks;
* a position absent from the JSON-stat value map is a typed Missing, never zero;
* open-ended and unknown age categories (``Y_OPEN``, ``Y_GE100``, ``Y_GE50``,
  ``Y_LT15``, ``UNK``) are kept as the categories they are and are never spread
  over single ages; every cell records its ``age_kind`` and ``cell_type``
  (leaf / subtotal / total) so totals are never added to disjoint leaves;
* the dataset-level ``updated`` timestamp is metadata, never a per-value
  publication time.

Dataset inputs: :func:`dataset_tables` derives, from the archived raw bytes only,
one Arrow table per (dataset, country) with the schema :data:`DATASET_SCHEMA`;
values are exact decimal *strings* (no float), status flags kept. The post-publish
step registers them through the platform's dataset registry when available.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from reva.adapters.connectors.base import json_decimal, module_digest, plain, schema_fingerprint, to_raw
from reva.adapters.http.safe_client import HttpResult, require_json
from reva.kernel.errors import SourceResponseError
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import TimeExtent
from reva.kernel.uncertainty import UncertaintySpec
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    ParsedObservation,
    QualityReport,
    RawAcquisition,
)

NAME = "eurostat.demography"
VERSION = "1.0.0"
SOURCE_KEY = "eurostat.demography"
PARSER_VERSION = "eurostat-demography-1"
DATA_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
HOSTS = ("ec.europa.eu",)
SINCE_YEAR = 2000
EUROPOP_YEARS = (2022, 2060)
EUROPOP_POP_YEARS = (2025, 2030, 2040, 2050, 2060)
MAX_POSITIONS = 600_000  # bounded parse (a 30-country EUROPOP cube is ~360k positions)
MAX_RESPONSE_BYTES = 20_000_000

# Eurostat geo code -> ISO 3166 alpha-3 (Eurostat uses EL for Greece). EU-27 + EFTA.
COUNTRIES: dict[str, str] = {
    "AT": "AUT", "BE": "BEL", "BG": "BGR", "HR": "HRV", "CY": "CYP", "CZ": "CZE", "DK": "DNK", "EE": "EST",
    "FI": "FIN", "FR": "FRA", "DE": "DEU", "EL": "GRC", "HU": "HUN", "IE": "IRL", "IT": "ITA", "LV": "LVA",
    "LT": "LTU", "LU": "LUX", "MT": "MLT", "NL": "NLD", "PL": "POL", "PT": "PRT", "RO": "ROU", "SK": "SVK",
    "SI": "SVN", "ES": "ESP", "SE": "SWE",
    "IS": "ISL", "LI": "LIE", "NO": "NOR", "CH": "CHE",
}
ISO3_TO_GEO = {v: k for k, v in COUNTRIES.items()}


@dataclass(frozen=True)
class DatasetDef:
    code: str
    kind: str  # "observed" | "europop"
    dimension_order: tuple[str, ...]
    unit: str
    concept_id: str
    concept_version: str
    aggregation: str
    temporal: str  # "point:01-01" | "calendar_year"
    population_universe: str
    unit_ref: str
    nonnegative: bool
    sexes: tuple[str, ...]
    label: str


DATASETS: dict[str, DatasetDef] = {
    "demo_pjan": DatasetDef(
        "demo_pjan", "observed", ("freq", "unit", "age", "sex", "geo", "time"), "NR",
        "demography.population.jan1_age_sex", "1.0.0", "point_in_time_stock", "point:01-01",
        "usually_resident_population_eurostat", "unit.persons", True, ("T", "M", "F"),
        "Population on 1 January by age and sex"),
    "demo_magec": DatasetDef(
        "demo_magec", "observed", ("freq", "unit", "sex", "age", "geo", "time"), "NR",
        "demography.deaths.age_sex", "1.0.0", "period_flow_total", "calendar_year",
        "usually_resident_population_eurostat", "unit.persons", True, ("T", "M", "F"),
        "Deaths by age and sex"),
    "demo_fasec": DatasetDef(
        "demo_fasec", "observed", ("freq", "unit", "age", "sex", "geo", "time"), "NR",
        "demography.live_births.mother_age_newborn_sex", "1.0.0", "period_flow_total", "calendar_year",
        "usually_resident_population_eurostat", "unit.persons", True, ("T", "M", "F"),
        "Live births by mother's age and newborn's sex"),
    "proj_23nanmig": DatasetDef(
        "proj_23nanmig", "europop", ("freq", "projection", "sex", "age", "unit", "geo", "time"), "PER",
        "demography.assumption.europop2023.net_migration_age_sex", "1.0.0", "period_flow_total", "calendar_year",
        "usually_resident_population_eurostat", "unit.persons", False, ("T", "M", "F"),
        "Assumptions for net migration by age, sex and type of projection"),
    "proj_23naasfr": DatasetDef(
        "proj_23naasfr", "europop", ("freq", "projection", "age", "unit", "geo", "time"), "NR",
        "demography.assumption.europop2023.fertility_rate_age", "1.0.0", "period_rate", "calendar_year",
        "usually_resident_population_eurostat", "unit.ratio", True, (),
        "Assumptions for fertility rates by age and type of projection"),
    "proj_23np": DatasetDef(
        "proj_23np", "europop", ("freq", "projection", "sex", "age", "unit", "geo", "time"), "PER",
        "demography.projection.europop2023.population_age_sex", "1.0.0", "point_in_time_stock", "point:01-01",
        "usually_resident_population_eurostat", "unit.persons", True, ("T", "M", "F"),
        "Population on 1st January by age, sex and type of projection"),
}
OBSERVED = tuple(k for k, d in DATASETS.items() if d.kind == "observed")
EUROPOP = tuple(k for k, d in DATASETS.items() if d.kind == "europop")
EUROPOP_PROJECTION = "BSL"  # baseline; other variants are not fetched in 1.0.0

# Eurostat observation status (OBS_FLAG) vocabulary (same reviewed meanings as the
# autonomy template; duplicated on purpose so this reviewed module's digest covers
# every rule it applies). Unknown codes block.
EUROSTAT_FLAGS: dict[str, str] = {
    "b": "break in time series",
    "c": "confidential",
    "d": "definition differs",
    "e": "estimated",
    "f": "forecast",
    "n": "not significant",
    "p": "provisional",
    "r": "revised",
    "s": "Eurostat estimate",
    "u": "low reliability",
    "z": "not applicable",
}
MISSING_REASON_BY_FLAG = {"c": "suppressed", "z": "not_applicable"}
# Eurostat now appends the SDMX confidentiality status after a ``|`` (e.g. ``|N`` = no
# observation flag, "not for publication"; seen live 2026-10-15 for demo_fasec DE/LT 2024).
# Reviewed CONF_STATUS codes; any other code after ``|`` blocks.
CONF_STATUS: dict[str, str] = {
    "N": "not for publication",
    "C": "confidential statistical information",
    "D": "secondary confidentiality",
}


def split_status(status: str) -> tuple[str, str]:
    """``"bep"`` -> (``"bep"``, ``""``); ``"|N"`` -> (``""``, ``"N"``); ``"p|N"`` -> (``"p"``, ``"N"``)."""
    if "|" in status:
        flag, conf = status.split("|", 1)
        return flag.strip(), conf.strip()
    return status.strip(), ""

# Reviewed age-code grammar. Anything else blocks (its meaning is not reviewed).
_AGE_SINGLE = re.compile(r"^Y(\d{1,3})$")
_AGE_GROUP = re.compile(r"^Y(\d{1,3})-(\d{1,3})$")
_AGE_GE = re.compile(r"^Y_GE(\d{1,3})$")
_AGE_LT = re.compile(r"^Y_LT(\d{1,3})$")

DATASET_SCHEMA_VERSION = "reva-eurostat-demography-table/1"
DATASET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dataset", "string"), ("geo", "string"), ("iso3", "string"), ("projection", "string"),
    ("year", "int32"), ("sex", "string"), ("age", "string"), ("age_kind", "string"),
    ("age_start", "int32"), ("age_end", "int32"), ("cell_type", "string"), ("value", "string"),
    ("missing_reason", "string"), ("status", "string"), ("unit", "string"),
)


def age_info(code: str, all_codes: Sequence[str] = ()) -> dict[str, Any]:
    """Classify an age category code. ``age_end`` is exclusive; None = open/unknown bound.

    ``kind``: single | open (``Y_OPEN``: starts after the last single age the country
    reports, start not encoded) | open_upper (``Y_GEn``) | open_lower (``Y_LTn``, n>1)
    | group (``Ya-b``) | unknown (``UNK``) | total (``TOTAL``). A group whose single
    ages are all present as categories is a ``subtotal`` cell; otherwise a ``leaf``.
    Returns kind ``unreviewed`` for any other code (the validator blocks it).
    """
    if code == "TOTAL":
        return {"kind": "total", "start": None, "end": None, "cell_type": "total"}
    if code == "UNK":
        return {"kind": "unknown", "start": None, "end": None, "cell_type": "leaf"}
    if code == "Y_OPEN":
        return {"kind": "open", "start": None, "end": None, "cell_type": "leaf"}
    if code == "Y_LT1":
        return {"kind": "single", "start": 0, "end": 1, "cell_type": "leaf"}
    if m := _AGE_SINGLE.match(code):
        a = int(m.group(1))
        return {"kind": "single", "start": a, "end": a + 1, "cell_type": "leaf"}
    if m := _AGE_GE.match(code):
        n = int(m.group(1))
        # a subtotal when a single age at or above n (or a higher open class) is also a category
        sub = any((i := age_info(o))["kind"] in ("single", "open_upper") and o != code
                  and (i["start"] or 0) >= n for o in all_codes)
        return {"kind": "open_upper", "start": n, "end": None, "cell_type": "subtotal" if sub else "leaf"}
    if m := _AGE_LT.match(code):
        n = int(m.group(1))
        singles = {f"Y{a}" for a in range(1, n)} | {"Y_LT1"}
        sub = bool(all_codes) and singles <= set(all_codes)
        return {"kind": "open_lower", "start": None, "end": n, "cell_type": "subtotal" if sub else "leaf"}
    if m := _AGE_GROUP.match(code):
        a, b = int(m.group(1)), int(m.group(2))
        if b < a:
            return {"kind": "unreviewed", "start": None, "end": None, "cell_type": "leaf"}
        singles = {f"Y{x}" if x else "Y_LT1" for x in range(a, b + 1)}
        sub = bool(all_codes) and singles <= set(all_codes)
        return {"kind": "group", "start": a, "end": b + 1, "cell_type": "subtotal" if sub else "leaf"}
    return {"kind": "unreviewed", "start": None, "end": None, "cell_type": "leaf"}


# --------------------------------------------------------------------------- cube decoding
@dataclass(frozen=True)
class Cell:
    position: int
    coord: dict[str, str]
    value: Decimal | None
    status: str


@dataclass(frozen=True)
class Cube:
    dataset: str
    ids: list[str]
    sizes: list[int]
    categories: dict[str, list[str]]
    labels: dict[str, dict[str, Any]]
    status_labels: dict[str, Any]
    status_present: bool
    updated: Any
    annotations: dict[str, Any]
    extension_id: Any
    value_count: int
    fingerprint: str
    cells: tuple[Cell, ...]


_MALFORMED = (ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError, ArithmeticError)


def _drift(msg: str) -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind="schema_drift")


def _positions(index: Any, size: int, dim: str) -> list[str]:
    """Category codes ordered by position (JSON-stat index may be an object or an array)."""
    if isinstance(index, list):
        codes = [str(c) for c in index]
    elif isinstance(index, dict):
        pairs = sorted(((int(str(v)), str(k)) for k, v in index.items()), key=lambda p: p[0])
        if [p for p, _ in pairs] != list(range(len(pairs))):
            raise _drift(f"dimension {dim} index positions are not 0..n-1")
        codes = [k for _, k in pairs]
    else:
        raise _drift(f"dimension {dim} has no category index")
    if len(codes) != size:
        raise _drift(f"dimension {dim}: {len(codes)} categories but size {size}")
    return codes


def _sparse(obj: Any, n: int, what: str) -> dict[int, Any]:
    """JSON-stat value/status as {position: item}; arrays with null are sparse too."""
    if obj is None:
        return {}
    if isinstance(obj, list):
        if len(obj) != n:
            raise _drift(f"{what} array length {len(obj)} != cube size {n}")
        return {i: v for i, v in enumerate(obj) if v is not None}
    if isinstance(obj, dict):
        out: dict[int, Any] = {}
        for k, v in obj.items():
            try:
                i = int(str(k))
            except ValueError:
                raise _drift(f"non-integer {what} key") from None
            if not 0 <= i < n:
                raise _drift(f"{what} position {i} outside cube of size {n}")
            out[i] = v
        return out
    if isinstance(obj, str) and what == "status":
        return dict.fromkeys(range(n), obj)
    raise _drift(f"unexpected JSON-stat {what} type")


def structure_fingerprint(doc: Mapping[str, Any]) -> str:
    """Order-preserving for dimensions (semantic in JSON-stat), order-free for keys; ignores
    time and geo categories (those vary with the request, not the structure)."""
    dims = doc.get("dimension") or {}
    shape = {
        "dimension_order": list(doc.get("id") or []),
        "categories": {d: sorted(dims.get(d, {}).get("category", {}).get("index") or {})
                       for d in (doc.get("id") or []) if d not in ("time", "geo")},
        "top_level": schema_fingerprint({k: v for k, v in doc.items() if k not in ("value", "status")}),
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


def decode_cube(body: bytes, media_type: str, url: str, dataset: str) -> Cube:
    """Decode a JSON-stat 2.0 dataset (Decimal values, bounded size). Typed errors only."""
    try:
        return _decode(body, media_type, url, dataset)
    except SourceResponseError:
        raise
    except _MALFORMED as e:
        raise SourceResponseError(f"malformed JSON-stat document: {type(e).__name__}", retryable=False,
                                  kind="schema_drift") from None


def _decode(body: bytes, media_type: str, url: str, dataset: str) -> Cube:
    from datetime import UTC, datetime

    require_json(HttpResult(url, url, 200, {"content-type": media_type}, body, datetime.now(UTC), 0.0, (), 1))
    doc = json_decimal(body)
    if not isinstance(doc, dict):
        raise SourceResponseError("JSON-stat document is not an object", retryable=False, kind="schema_drift")
    if "error" in doc and "value" not in doc:
        raise SourceResponseError(f"Eurostat API error: {str(plain(doc['error']))[:300]}", retryable=False,
                                  kind="api_error")
    if str(doc.get("version")) != "2.0" or doc.get("class") != "dataset":
        raise SourceResponseError("not a JSON-stat 2.0 dataset", retryable=False, kind="schema_drift")
    ids, sizes_raw = doc.get("id"), doc.get("size")
    if not (isinstance(ids, list) and isinstance(sizes_raw, list) and len(ids) == len(sizes_raw)):
        raise SourceResponseError("JSON-stat id/size missing or inconsistent", retryable=False, kind="schema_drift")
    if not isinstance(doc.get("dimension"), dict) or set(ids) - set(doc["dimension"]):
        raise SourceResponseError("JSON-stat dimension object incomplete", retryable=False, kind="schema_drift")
    ids = [str(d) for d in ids]
    sizes = [int(str(s)) for s in sizes_raw]
    n = math.prod(sizes)
    if n > MAX_POSITIONS:
        raise SourceResponseError(f"cube of {n} positions exceeds the connector bound", retryable=False,
                                  kind="oversized_cube")
    cats = {d: _positions(doc["dimension"][d].get("category", {}).get("index"), s, d)
            for d, s in zip(ids, sizes, strict=True)}
    labels = {d: dict(doc["dimension"][d].get("category", {}).get("label") or {}) for d in ids}
    values = _sparse(doc.get("value"), n, "value")
    status = _sparse(doc.get("status"), n, "status")
    ext = doc.get("extension") if isinstance(doc.get("extension"), dict) else {}
    status_labels = dict(((ext.get("status") or {}).get("label")) or {})
    annotations = {
        str(a.get("type")): a.get("date") or a.get("title")
        for a in (ext.get("annotation") or [])
        if isinstance(a, dict) and a.get("type") in (
            "UPDATE_DATA", "UPDATE_STRUCTURE", "OBS_COUNT", "OBS_PERIOD_OVERALL_LATEST",
            "OBS_PERIOD_OVERALL_OLDEST", "DISSEMINATION_TIMESTAMP_DATA")
    }
    strides = [math.prod(sizes[i + 1:]) for i in range(len(sizes))]
    cells: list[Cell] = []
    for pos in range(n):
        coord = {d: cats[d][(pos // strides[i]) % sizes[i]] for i, d in enumerate(ids)}
        v = values.get(pos)
        if v is not None:
            v = v if isinstance(v, Decimal) else Decimal(str(v))
            if not v.is_finite():
                raise SourceResponseError("non-finite value", retryable=False, kind="schema_drift")
        st = status.get(pos)
        cells.append(Cell(pos, coord, v, str(st) if st is not None else ""))
    return Cube(dataset, ids, sizes, cats, labels, status_labels, "status" in doc, doc.get("updated"),
                plain(annotations), ext.get("id"), len(values), structure_fingerprint(doc), tuple(cells))


def _dec_text(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _missing_reason(status: str) -> str:
    flag, conf = split_status(status)
    if conf in CONF_STATUS:
        return "suppressed"
    return next((MISSING_REASON_BY_FLAG[c] for c in flag if c in MISSING_REASON_BY_FLAG), "unavailable")


# --------------------------------------------------------------------------- dataset tables
def dataset_rows(cube: Cube) -> dict[str, list[dict[str, Any]]]:
    """Rows of the dataset-input table per ISO3, derived only from the decoded raw cube."""
    ddef = DATASETS[cube.dataset]
    age_codes = cube.categories.get("age", [])
    infos = {a: age_info(a, age_codes) for a in age_codes}
    out: dict[str, list[dict[str, Any]]] = {}
    for c in cube.cells:
        geo = c.coord["geo"]
        iso3 = COUNTRIES.get(geo)
        if iso3 is None:
            continue  # validation blocks unexpected geos; never silently renamed
        info = infos[c.coord["age"]]
        out.setdefault(iso3, []).append({
            "dataset": cube.dataset,
            "geo": geo,
            "iso3": iso3,
            "projection": c.coord.get("projection"),
            "year": int(c.coord["time"]),
            "sex": c.coord.get("sex", "T"),
            "age": c.coord["age"],
            "age_kind": info["kind"],
            "age_start": info["start"],
            "age_end": info["end"],
            "cell_type": info["cell_type"],
            "value": _dec_text(c.value) if c.value is not None else None,
            "missing_reason": None if c.value is not None else _missing_reason(c.status),
            "status": c.status,
            "unit": c.coord.get("unit", ddef.unit),
        })
    return out


def rows_to_table(rows: Sequence[Mapping[str, Any]]) -> Any:
    """Arrow table with the pinned :data:`DATASET_COLUMNS` schema, deterministic row order."""
    import pyarrow as pa

    schema = pa.schema([(name, getattr(pa, typ)()) for name, typ in DATASET_COLUMNS])
    ordered = sorted(rows, key=lambda r: (r["dataset"], r["iso3"], r["projection"] or "", r["year"], r["sex"],
                                          r["age_start"] if r["age_start"] is not None else -1, r["age"]))
    cols = {name: [r.get(name) for r in ordered] for name, _ in DATASET_COLUMNS}
    return pa.table(cols, schema=schema)


def dataset_tables(raw: RawAcquisition) -> dict[str, Any]:
    """``{iso3: arrow_table}`` for one acquisition (same bytes -> same tables)."""
    dataset = str(raw.item.params["dataset"])
    cube = decode_cube(raw.body, raw.media_type, raw.item.url, dataset)
    return {iso3: rows_to_table(rows) for iso3, rows in sorted(dataset_rows(cube).items())}


def dataset_concept(dataset: str) -> str:
    d = DATASETS[dataset]
    return f"{d.concept_id}@{d.concept_version}"


# --------------------------------------------------------------------------- connector
class EurostatDemographyConnector:
    """Reviewed built-in connector for Eurostat demography and EUROPOP2023 assumptions."""

    def __init__(self, countries: Sequence[str] | None = None, *, include_europop: bool = True,
                 since_year: int = SINCE_YEAR) -> None:
        geos = list(countries) if countries is not None else list(COUNTRIES)
        unknown = [g for g in geos if g not in COUNTRIES]
        if unknown:
            raise ValueError(f"not reviewed Eurostat geo codes: {unknown}")
        self.geos = tuple(sorted(set(geos)))
        self.include_europop = include_europop
        self.since_year = int(since_year)
        self._prior_done: set[str] = set()

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name=NAME,
            version=VERSION,
            template="eurostat_jsonstat",
            source_key=SOURCE_KEY,
            provider="Eurostat",
            licence="CC-BY-4.0 (Eurostat reuse policy, acknowledgement required)",
            allowed_hosts=HOSTS,
            dataset_ids=tuple(DATASETS),
            field_mappings={k: f"{d.concept_id}@{d.concept_version}" for k, d in DATASETS.items()},
            units={k: d.unit_ref for k, d in DATASETS.items()},
            geography=tuple(COUNTRIES[g] for g in self.geos),
            time_semantics=("demo_pjan: 1 January point stock; demo_magec/demo_fasec: calendar-year flow totals "
                            "(age in completed years); EUROPOP2023 BSL assumptions are projections, not "
                            "observations (dataset inputs only)"),
            lineage_family="eurostat",
            cadence_seconds=7 * 86400,
            release_aware=True,
            revision_strategy="append revision when value or status flag changes (e.g. provisional -> final)",
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_pages=200,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(EurostatDemographyConnector),
        )

    # -------------------------------------------------------------- planning
    def url_for(self, dataset: str, geos: Sequence[str]) -> str:
        d = DATASETS[dataset]
        params: list[tuple[str, str]] = [("format", "JSON"), ("lang", "EN")]
        params += [("geo", g) for g in geos]
        if d.kind == "europop":
            params.append(("projection", EUROPOP_PROJECTION))
            if dataset == "proj_23np":
                params += [("time", str(y)) for y in EUROPOP_POP_YEARS]
            else:
                params += [("sinceTimePeriod", str(EUROPOP_YEARS[0])), ("untilTimePeriod", str(EUROPOP_YEARS[1]))]
        else:
            params.append(("sinceTimePeriod", str(self.since_year)))
        return DATA_BASE + dataset + "?" + urlencode(params)

    def all_items(self) -> list[FetchItem]:
        items: list[FetchItem] = []
        for g in self.geos:
            for ds in OBSERVED:
                items.append(FetchItem(key=f"{ds}:{g}", url=self.url_for(ds, [g]),
                                       expected_media_types=("application/json",), max_bytes=MAX_RESPONSE_BYTES,
                                       params={"kind": "jsonstat", "dataset": ds, "geos": [g]}))
        if self.include_europop:
            geos = [g for g in self.geos]
            for ds in EUROPOP:
                items.append(FetchItem(key=f"{ds}:all", url=self.url_for(ds, geos),
                                       expected_media_types=("application/json",), max_bytes=MAX_RESPONSE_BYTES,
                                       params={"kind": "jsonstat", "dataset": ds, "geos": geos}))
        return items

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        """Resumable: items completed in an unfinished cycle (cursor ``done``) are skipped."""
        state = dict(cursor.state) if cursor is not None else {}
        done = set(state.get("done") or []) if not state.get("complete") else set()
        self._prior_done = done
        pending = [i for i in self.all_items() if i.key not in done]
        items = tuple(pending[: max(0, budget.max_requests)])
        return FetchPlan(
            connector=NAME,
            items=items,
            pagination="none: one filtered JSON-stat cube per request; unfinished cycles resume from the cursor",
            availability_expectation="Eurostat dataset-level UPDATE_DATA; no per-value publication time",
            budget_exhausted=len(items) < len(pending),
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        ok = {o.item_key for o in outcomes if o.status in ("published", "unchanged")}
        all_keys = [i.key for i in self.all_items()]
        prior_done = set(getattr(self, "_prior_done", set()))  # set by plan() of this run
        done = sorted((prior_done | ok) & set(all_keys))
        complete = len(done) == len(all_keys)
        return Cursor(state={"complete": complete, "done": [] if complete else done,
                             "since_year": self.since_year})

    # -------------------------------------------------------------- parsing
    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        dataset = str(raw.item.params["dataset"])
        if dataset not in DATASETS:
            raise SourceResponseError(f"unreviewed dataset {dataset!r}", retryable=False, kind="schema_drift")
        cube = decode_cube(raw.body, raw.media_type, raw.item.url, dataset)
        ddef = DATASETS[dataset]
        age_codes = cube.categories.get("age", [])
        infos = {a: age_info(a, age_codes) for a in age_codes}
        observations: list[ParsedObservation] = []
        if ddef.kind == "observed" and cube.ids == list(ddef.dimension_order):
            for c in cube.cells:
                if c.coord.get("geo") in COUNTRIES and infos[c.coord["age"]]["kind"] != "unreviewed":
                    observations.append(self._observation(ddef, cube, c, infos[c.coord["age"]]))
        open_starts = _open_age_starts(cube)
        meta = {
            "kind": "jsonstat",
            "dataset": dataset,
            "dataset_kind": ddef.kind,
            "label": ddef.label,
            "dataset_updated": cube.updated,  # dataset-level; NOT a value publication time
            "extension_id": cube.extension_id,
            "annotations": cube.annotations,
            "dimension_order": cube.ids,
            "size": cube.sizes,
            "cube_positions": len(cube.cells),
            "value_count": cube.value_count,
            "status_labels": cube.status_labels,
            "status_present": cube.status_present,
            "status_codes_seen": sorted({c.status for c in cube.cells if c.status}),
            "categories": {d: v for d, v in cube.categories.items() if d != "time"},
            "years": sorted(int(t) for t in cube.categories.get("time", [])),
            "requested_geos": list(raw.item.params.get("geos") or []),
            "age_kinds": {a: infos[a]["kind"] for a in age_codes},
            "open_age_starts": open_starts,
            "structure_fingerprint": cube.fingerprint,
            "pinned_dimension_order": list(ddef.dimension_order),
            "negative_values": sum(1 for c in cube.cells if c.value is not None and c.value < 0),
            "additivity": _additivity(cube, infos),
            "dataset_input": {"schema": DATASET_SCHEMA_VERSION, "concept": dataset_concept(dataset)},
        }
        yield ObservationBatch(
            connector=NAME,
            connector_version=VERSION,
            item_key=raw.item.key,
            observations=tuple(observations),
            expected_keys=tuple(o.logical_key for o in observations),
            metadata=meta,
            schema_fingerprint=cube.fingerprint,
        )

    def _observation(self, ddef: DatasetDef, cube: Cube, c: Cell, info: Mapping[str, Any]) -> ParsedObservation:
        year = int(c.coord["time"])
        geo = c.coord["geo"]
        if ddef.temporal == "point:01-01":
            temporal = TimeExtent.on_date(date(year, 1, 1), label=f"{year}-01-01")
        else:
            temporal = TimeExtent.calendar_year(year)
        resolution = {"age": c.coord["age"], "sex": c.coord["sex"]}
        support = Support(
            temporal=temporal,
            spatial=SpatialRef(scheme="eurostat_geo_code", key=f"ESTAT:{geo}", authority="Eurostat"),
            population=PopulationRef(universe=ddef.population_universe,
                                     definition_source=f"Eurostat {ddef.code} metadata"),
            aggregation=ddef.aggregation,  # type: ignore[arg-type]
            resolution=resolution,
        )
        flag_part, conf = split_status(c.status)
        flag_chars = [ch for ch in flag_part if ch.strip()]
        if c.value is None:
            value: dict[str, Any] = {"kind": "missing", "reason": _missing_reason(c.status)}
        else:
            value = {"kind": "decimal", "value": _dec_text(c.value), "unit_ref": ddef.unit_ref,
                     "basis": {"scale_factor": "1"}}
        sex_cell = "total" if c.coord["sex"] == "T" else "leaf"
        age_cell = info["cell_type"]
        cell_type = "total" if (sex_cell == "total" and age_cell == "total") else (
            "leaf" if (sex_cell == "leaf" and age_cell == "leaf") else "subtotal")
        flags: dict[str, Any] = {
            "dataset": ddef.code,
            "status": c.status,
            "status_labels": ({c.status: str(cube.status_labels[c.status])} if c.status in cube.status_labels
                              else {ch: str(cube.status_labels.get(ch, "")) for ch in flag_chars}),
            "confidentiality_status": conf,
            "cell_type": cell_type,
            "age_kind": info["kind"],
            "categories": {d: c.coord[d] for d in ddef.dimension_order if d != "time"},
            "eurostat_geo": geo,
            "unit_code": c.coord.get("unit", ""),
            "freq": c.coord.get("freq", ""),
        }
        if info["kind"] in ("open", "open_upper", "open_lower", "unknown"):
            flags["special_category"] = {
                "open": "open-ended top age class (starts after the last single age the country reports)",
                "open_upper": f"open-ended age class {info['start']} and over",
                "open_lower": f"open-ended age class under {info['end']}",
                "unknown": "age not stated",
            }[info["kind"]]
        if c.value is None:
            flags["absent_in_value_map"] = True
        return ParsedObservation(
            # Subject: this module's reviewed Eurostat-geo -> ISO 3166 table (EL -> GRC), resolved
            # through the iso3166_alpha3 aliases. Deliberately NOT a new eurostat_geo alias: those
            # feed the autonomy template's T1 eligibility scope, which this connector must not widen.
            subject_provider="iso3166_alpha3",
            subject_key=COUNTRIES[geo],
            concept_id=ddef.concept_id,
            concept_version=ddef.concept_version,
            support=support,
            logical_key=f"estat-demo:{ddef.code}:{geo}:{c.coord['sex']}:{c.coord['age']}:{year}",
            value=value,
            uncertainty=UncertaintySpec.unknown("Eurostat publishes no uncertainty for this value"),
            source_flags=flags,
            raw_selector={"jsonstat_position": c.position, "coordinates": c.coord,
                          "labels": {d: str(cube.labels.get(d, {}).get(c.coord[d], "")) for d in c.coord}},
        )

    # -------------------------------------------------------------- validation
    def validate(self, batch: ObservationBatch) -> QualityReport:
        f: list[Finding] = []
        md = batch.metadata
        dataset = str(md.get("dataset"))
        ddef = DATASETS.get(dataset)
        if ddef is None:
            return _report([Finding(code="unreviewed_dataset", severity="blocker", message=dataset)], 0, 0, ())
        order = list(md.get("dimension_order") or [])
        if order != list(ddef.dimension_order):
            f.append(Finding(code="dimension_order_changed", severity="blocker",
                             message=f"dimension order {order} differs from pinned {list(ddef.dimension_order)}"))
        cats: dict[str, list[str]] = md.get("categories") or {}
        if cats.get("unit") != [ddef.unit]:
            f.append(Finding(code="unit_changed", severity="blocker",
                             message=f"unit categories {cats.get('unit')} != pinned [{ddef.unit}]"))
        if cats.get("freq") != ["A"]:
            f.append(Finding(code="freq_changed", severity="blocker", message=f"freq {cats.get('freq')} != ['A']"))
        if ddef.kind == "europop" and cats.get("projection") != [EUROPOP_PROJECTION]:
            f.append(Finding(code="projection_changed", severity="blocker",
                             message=f"projection {cats.get('projection')} != [{EUROPOP_PROJECTION}]"))
        if ddef.sexes and sorted(cats.get("sex") or []) != sorted(ddef.sexes) and not (
                ddef.kind == "europop" and set(cats.get("sex") or []) <= set(ddef.sexes)):
            f.append(Finding(code="sex_categories_changed", severity="blocker",
                             message=f"sex categories {cats.get('sex')} != {list(ddef.sexes)}"))
        geos = list(cats.get("geo") or [])
        requested = list(md.get("requested_geos") or [])
        extra = sorted(set(geos) - set(requested))
        if extra:
            f.append(Finding(code="unexpected_geo", severity="blocker", message=f"unrequested geos {extra}"))
        absent = sorted(set(requested) - set(geos))
        if absent:
            sev = "warning" if ddef.kind == "europop" else "blocker"
            f.append(Finding(code="geo_absent", severity=sev,
                             message=f"requested geos absent from the response (typed gap): {absent}"))
        bad_ages = sorted(a for a, k in (md.get("age_kinds") or {}).items() if k == "unreviewed")
        if bad_ages:
            f.append(Finding(code="unreviewed_age_category", severity="blocker",
                             message=f"age categories with unreviewed meaning: {bad_ages[:10]}"))
        n = int(md.get("cube_positions", 0))
        if n == 0 or int(md.get("value_count", 0)) == 0:
            f.append(Finding(code="empty_response", severity="blocker",
                             message="empty cube: an empty response is never zero or complete"))
        if ddef.kind == "observed" and len(batch.observations) != n:
            f.append(Finding(code="size_mismatch", severity="blocker",
                             message=f"{len(batch.observations)} cells parsed for a cube of {n}"))
        if ddef.kind == "observed" and md.get("years") and min(md["years"]) > self.since_year + 10:
            f.append(Finding(code="time_coverage_short", severity="warning",
                             message=f"series starts in {min(md['years'])}"))
        labels = md.get("status_labels") or {}
        for code in sorted(set(md.get("status_codes_seen") or [])):
            flag_part, conf = split_status(code)
            if conf and conf not in CONF_STATUS:
                f.append(Finding(code="unknown_status_flag", severity="blocker",
                                 message=f"confidentiality status {conf!r} is not in the reviewed vocabulary"))
            for ch in flag_part:
                if ch not in EUROSTAT_FLAGS:
                    f.append(Finding(code="unknown_status_flag", severity="blocker",
                                     message=f"status flag {ch!r} is not in the reviewed vocabulary"))
                elif ch not in labels and code not in labels:  # labels may name the combined code ("bep")
                    f.append(Finding(code="undeclared_status_flag", severity="blocker",
                                     message=f"status flag {ch!r} used without a declared label"))
        for ch, label in labels.items():
            if ch in EUROSTAT_FLAGS and str(label).strip().lower() != EUROSTAT_FLAGS[ch]:
                f.append(Finding(code="status_label_changed", severity="warning",
                                 message=f"flag {ch!r} label {label!r} differs from reviewed meaning"))
        if ddef.nonnegative and int(md.get("negative_values", 0)):
            f.append(Finding(code="negative_count", severity="blocker",
                             message=f"{md['negative_values']} negative values in a non-negative dataset"))
        keys = [o.logical_key for o in batch.observations]
        if len(keys) != len(set(keys)):
            f.append(Finding(code="duplicate_keys", severity="blocker", message="duplicate logical keys"))
        n_missing = sum(1 for o in batch.observations if o.value.get("kind") == "missing")
        if n_missing:
            f.append(Finding(code="missing_values", severity="info",
                             message=f"{n_missing} positions absent from the value map (kept Missing)"))
        add = md.get("additivity") or {}
        if add.get("mismatched_groups"):
            f.append(Finding(code="additivity_mismatch", severity="warning",
                             message=f"{add['mismatched_groups']} (geo, sex, year) groups whose age leaves do not "
                                     f"sum to TOTAL (max abs diff {add.get('max_abs_diff')}); kept as published"))
        return _report(f, n, len(keys), tuple(sorted(o.logical_key for o in batch.observations
                                                      if o.value.get("kind") == "missing")))


def _report(findings: list[Finding], expected: int, received: int, missing: tuple[str, ...]) -> QualityReport:
    outcome = "blocked" if any(x.severity == "blocker" for x in findings) else "pass"
    return QualityReport(outcome=outcome, findings=tuple(findings), expected_count=expected,
                         received_count=received, missing_keys=missing)


def _group_key(c: Cell) -> tuple[str, ...]:
    return tuple(f"{d}={v}" for d, v in sorted(c.coord.items()) if d != "age")


def _open_age_starts(cube: Cube) -> dict[str, list[int]]:
    """Per geo: distinct implied starts of ``Y_OPEN`` (1 + last single age with a value)."""
    last: dict[tuple[str, ...], int] = {}
    has_open: set[tuple[str, ...]] = set()
    for c in cube.cells:
        if c.value is None:
            continue
        a = c.coord.get("age", "")
        k = _group_key(c)
        info = age_info(a)
        if info["kind"] == "single":
            last[k] = max(last.get(k, -1), int(info["start"]))
        elif info["kind"] == "open":
            has_open.add(k)
    out: dict[str, set[int]] = {}
    for k in has_open:
        geo = next(p.split("=", 1)[1] for p in k if p.startswith("geo="))
        out.setdefault(geo, set()).add(last.get(k, -1) + 1)
    return {g: sorted(v) for g, v in sorted(out.items())}


def _additivity(cube: Cube, infos: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Age leaves (single, open, non-overlapping groups, UNK) vs TOTAL per (geo, sex, year, ...)."""
    if "age" not in cube.categories:
        return {}
    leaves: dict[tuple[str, ...], Decimal] = {}
    totals: dict[tuple[str, ...], Decimal] = {}
    for c in cube.cells:
        if c.value is None:
            continue
        k = _group_key(c)
        info = infos[c.coord["age"]]
        if info["kind"] == "total":
            totals[k] = c.value
        elif info["cell_type"] == "leaf" and info["kind"] != "unreviewed":
            leaves[k] = leaves.get(k, Decimal(0)) + c.value
    diffs = [abs(leaves.get(k, Decimal(0)) - t) for k, t in totals.items()]
    bad = [d for d in diffs if d != 0]
    return {"checked_groups": len(totals), "mismatched_groups": len(bad),
            "max_abs_diff": _dec_text(max(bad)) if bad else "0"}


def dataset_key(dataset: str) -> str:
    return f"eurostat.demography.{dataset}"


def dataset_coverage(table: Any) -> dict[str, Any]:
    """Coverage summary recorded with a dataset (years, sexes, open age categories, flags)."""
    rows = table.to_pylist()
    years = sorted({r["year"] for r in rows})
    present = [r for r in rows if r["value"] is not None]
    return {
        "years": [years[0], years[-1]] if years else [],
        "years_with_values": sorted({r["year"] for r in present}),
        "sexes": sorted({r["sex"] for r in rows}),
        "special_age_categories": sorted({r["age"] for r in rows if r["age_kind"] in (
            "open", "open_upper", "open_lower", "unknown")}),
        "status_flags": sorted({r["status"] for r in rows if r["status"]}),
        "missing_cells": len(rows) - len(present),
    }


def post_publish_datasets(conn: Any, store: Any, raw: RawAcquisition, batches: Sequence[ObservationBatch],
                          summary: Any) -> None:
    """Register the dataset inputs derived from this acquisition's raw bytes (same transaction).

    Contract of ``reva.evidence.runner.post_publish_for`` hooks: runs inside the acquisition
    publication transaction, only for newly published (not quarantined) raw bytes, and
    must not commit. :func:`dataset_tables` is a pure function of the archived bytes, so
    a dataset can always be re-derived from the acquisition it names."""
    from reva.simulation.datasets import register_dataset

    dataset = str(raw.item.params["dataset"])
    for iso3, table in dataset_tables(raw).items():
        register_dataset(conn, store, dataset_key=dataset_key(dataset), concept_id=DATASETS[dataset].concept_id,
                         subject_key=iso3, table=table, source_acquisition_ids=[summary.acquisition_id],
                         parser_version=PARSER_VERSION, origin=raw.origin, coverage=dataset_coverage(table),
                         actor=f"connector:{NAME}",
                         rights={"licence": "CC-BY-4.0 (Eurostat reuse policy)", "attribution": "Source: Eurostat",
                                 "source_key": SOURCE_KEY})


# Optional hook read by runners that dispatch post-publish steps per connector.
EurostatDemographyConnector.post_publish = staticmethod(post_publish_datasets)  # type: ignore[attr-defined]


def backfill_datasets(conn: Any, store: Any, *, origin: str = "live") -> list[dict[str, Any]]:
    """Derive and register dataset inputs from this connector's archived acquisitions.

    For runners that do not dispatch :func:`post_publish_datasets` (the worker ``ingest``
    job dispatches post-publish steps by template only): the latest passing acquisition per
    request is re-read from the object store (namespace ``raw``, hash-checked), re-parsed
    and registered. Idempotent (``register_dataset`` returns the existing row for the same
    table bytes). Caller's transaction (worker role)."""
    from sqlalchemy import text

    from reva.kernel.ids import sha256_bytes

    rows = conn.execute(text(
        """SELECT DISTINCT ON (a.request_fingerprint) a.id, a.raw_sha256, a.origin, a.retrieved_at,
                  a.request_fingerprint, a.response_metadata
           FROM evidence.acquisition a JOIN evidence.connector_version v ON v.id = a.connector_version_id
           WHERE v.name = :n AND a.origin = :o
           ORDER BY a.request_fingerprint, a.recorded_at DESC, a.id DESC"""), {"n": NAME, "o": origin}).mappings().all()
    items = {i.key: i for i in EurostatDemographyConnector().all_items()}
    out: list[dict[str, Any]] = []
    for r in rows:
        md = dict(r["response_metadata"] or {})
        if any(q.get("outcome") != "pass" for q in md.get("quality") or []):
            continue  # quarantined/blocked bytes never become dataset inputs
        key = str(md.get("item_key") or "")
        dataset = key.split(":", 1)[0]
        if dataset not in DATASETS:
            continue
        body = store.get("raw", r["raw_sha256"])
        if sha256_bytes(body) != r["raw_sha256"]:
            raise SourceResponseError("archived raw bytes do not match their hash", retryable=False,
                                      kind="integrity")
        item = items.get(key) or FetchItem(key=key, url=DATA_BASE + dataset, max_bytes=MAX_RESPONSE_BYTES,
                                           params={"kind": "jsonstat", "dataset": dataset})
        raw = RawAcquisition(item=item, body=body, media_type="application/json", retrieved_at=r["retrieved_at"],
                             response_metadata={}, request_fingerprint=r["request_fingerprint"],
                             origin=r["origin"])

        class _Summary:
            acquisition_id = r["id"]
            raw_sha256 = r["raw_sha256"]

        post_publish_datasets(conn, store, raw, (), _Summary())
        out.append({"acquisition_id": str(r["id"]), "item_key": key})
    return out
