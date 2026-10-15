"""Built-in reviewed connector ``eurostat.labour_pensions`` (V2 step 4/5, agent PEN).

Fetches, from the Eurostat dissemination API (JSON-stat 2.0), the tables the
retirement-age module and the household extension need. One request per table,
all countries of the connector in one filtered cube:

=================  =========================================================  ==========================
table              content (exactly as published)                             unit (Eurostat code)
=================  =========================================================  ==========================
``lfsa_ergan``     LFS employment rate by sex, age group, citizenship=TOTAL   percent (PC)
``lfsa_argan``     LFS activity (participation) rate, same dimensions         percent (PC)
``lfsa_egan``      LFS employed persons, same dimensions                      thousand persons (THS_PER)
``spr_pns_ben``    ESSPROS pension beneficiaries, old-age total (OLD_TOT),    persons (PER)
                   all schemes, means-tested + non means-tested (TOTAL)
``spr_exp_pens``   ESSPROS expenditure on pensions by type (old-age OLD,      million euro (MIO_EUR)
                   anticipated AOLD, partial PART, TOTAL), means-testing TOTAL
``lfst_hhnhtych``  LFS private households by composition and number of       thousand households (THS_HH)
                   children (age of youngest child = TOTAL)
``ilc_lvph03``     EU-SILC distribution of households by household size       percent (PC)
``ilc_lvph01``     EU-SILC average household size                             persons per household (AVG)
=================  =========================================================  ==========================

Age groups: Eurostat publishes the LFS rates in five-year groups; no Eurostat
LFS table offers single years (or anything finer than five-year groups) for
ages 55-69 (checked against the dissemination catalogue on 2026-10-15), so the
leaves are ``Y55-59``, ``Y60-64``, ``Y65-69``. Pension beneficiaries have no age
dimension in ESSPROS.

Semantics preserved from the provider:

* every value keeps its Eurostat observation status verbatim (``b`` break,
  ``d`` definition differs, ``u`` low reliability, ``p`` provisional, ``e``
  estimated, ...; combined codes such as ``bd`` are split into their letters and
  each letter must belong to the reviewed vocabulary);
* a position absent from the JSON-stat ``value`` map is a typed Missing (never 0);
* values are stored verbatim in the provider's scale (thousands, millions) with an
  explicit ``scale_factor``; nothing is rescaled at ingest;
* total categories (sex ``T``, ``TOTAL`` codes) are flagged ``cell_type`` total
  or subtotal so totals are never added to leaves downstream;
* Eurostat returns only the periods it has; the returned periods must be a
  subset of the requested ones and are recorded (coverage), never padded.

Dataset inputs: :func:`derive_dataset_tables` turns the raw bytes of one
acquisition into per-country Arrow tables (deterministic, parser-versioned) that
the platform registers as content-addressed dataset inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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

CONNECTOR_NAME = "eurostat.labour_pensions"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "eurostat-labour-pensions-parse-1"
DATASET_DERIVATION_VERSION = "eurostat-labour-pensions-dataset-1"
SOURCE_KEY = "eurostat.dissemination"
DATA_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
HOST = "ec.europa.eu"
MAX_RESPONSE_BYTES = 5_000_000
MAX_POSITIONS = 200_000
LICENCE = "Eurostat copyright/licence policy (reuse authorised with acknowledgement, CC BY 4.0)"

#: Reviewed Eurostat ``geo`` code -> ISO 3166 alpha-3 (Eurostat uses EL for Greece and
#: UK for the United Kingdom; no such code is in the default set). Mirrors the
#: ``eurostat_geo`` aliases the entity registry must hold for publication.
EUROSTAT_GEO_TO_ISO3: dict[str, str] = {
    "FR": "FRA",
    "DE": "DEU",
    "IT": "ITA",
    "ES": "ESP",
    "NL": "NLD",
    "BE": "BEL",
    "SE": "SWE",
    "PL": "POL",
}
DEFAULT_GEOS: tuple[str, ...] = tuple(EUROSTAT_GEO_TO_ISO3)

#: Reviewed Eurostat observation-status letters (same meanings as the autonomy template).
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
_MALFORMED = (ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError, ArithmeticError)
_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


@dataclass(frozen=True)
class TableSpec:
    """Reviewed request + semantics of one Eurostat table."""

    code: str
    concept_id: str
    concept_version: str
    dimension_order: tuple[str, ...]
    filters: Mapping[str, tuple[str, ...]]  # every non-geo, non-time dimension is pinned
    resolution: tuple[str, ...]  # dimensions that vary inside the support (besides geo/time)
    totals: Mapping[str, tuple[str, ...]]  # total/subtotal categories per resolution dimension
    unit_code: str
    unit_ref: str
    scale_factor: str
    aggregation: str
    population_universe: str
    years: tuple[int, ...]
    currency: str | None = None
    price_basis: str | None = None
    note: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "concept": f"{self.concept_id}@{self.concept_version}",
            "dimension_order": list(self.dimension_order),
            "filters": {k: list(v) for k, v in self.filters.items()},
            "resolution": list(self.resolution),
            "totals": {k: list(v) for k, v in self.totals.items()},
            "unit_code": self.unit_code,
            "unit_ref": self.unit_ref,
            "scale_factor": self.scale_factor,
            "aggregation": self.aggregation,
            "population_universe": self.population_universe,
            "years": [self.years[0], self.years[-1]],
            "currency": self.currency,
            "note": self.note,
        }


_LFS_ORDER = ("freq", "unit", "sex", "age", "citizen", "geo", "time")
_LFS_AGES = ("Y55-59", "Y60-64", "Y65-69")
_LFS_YEARS = tuple(range(2000, 2026))
_LFS_UNIVERSE = "persons_living_in_private_households_lfs"

TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        code="lfsa_ergan",
        concept_id="labour.employment_rate.lfs_age_sex",
        concept_version="1.0.0",
        dimension_order=_LFS_ORDER,
        filters={"freq": ("A",), "unit": ("PC",), "sex": ("F", "M", "T"), "age": _LFS_AGES, "citizen": ("TOTAL",)},
        resolution=("sex", "age"),
        totals={"sex": ("T",)},
        unit_code="PC",
        unit_ref="unit.percent",
        scale_factor="1",
        aggregation="period_rate",
        population_universe=_LFS_UNIVERSE,
        years=_LFS_YEARS,
        note="employed persons as a percent of the population of the same sex and age group (annual average)",
    ),
    TableSpec(
        code="lfsa_argan",
        concept_id="labour.activity_rate.lfs_age_sex",
        concept_version="1.0.0",
        dimension_order=_LFS_ORDER,
        filters={"freq": ("A",), "unit": ("PC",), "sex": ("F", "M", "T"), "age": _LFS_AGES, "citizen": ("TOTAL",)},
        resolution=("sex", "age"),
        totals={"sex": ("T",)},
        unit_code="PC",
        unit_ref="unit.percent",
        scale_factor="1",
        aggregation="period_rate",
        population_universe=_LFS_UNIVERSE,
        years=_LFS_YEARS,
        note="labour force (employed + unemployed) as a percent of the population of the same sex and age group",
    ),
    TableSpec(
        code="lfsa_egan",
        concept_id="labour.employed_persons.lfs_age_sex",
        concept_version="1.0.0",
        dimension_order=_LFS_ORDER,
        filters={"freq": ("A",), "unit": ("THS_PER",), "sex": ("F", "M", "T"), "age": _LFS_AGES,
                 "citizen": ("TOTAL",)},
        resolution=("sex", "age"),
        totals={"sex": ("T",)},
        unit_code="THS_PER",
        unit_ref="unit.persons",
        scale_factor="1000",
        aggregation="period_average_stock",
        population_universe=_LFS_UNIVERSE,
        years=_LFS_YEARS,
        note="employed persons, annual average, thousands (value stored verbatim, scale_factor 1000)",
    ),
    TableSpec(
        code="spr_pns_ben",
        concept_id="social.pension_beneficiaries.esspros_old_age",
        concept_version="1.0.0",
        dimension_order=("freq", "spdepm", "spscheme", "spdepb", "sex", "unit", "geo", "time"),
        filters={"freq": ("A",), "spdepm": ("TOTAL",), "spscheme": ("TOTAL",), "spdepb": ("OLD_TOT",),
                 "sex": ("F", "M", "T"), "unit": ("PER",)},
        resolution=("sex",),
        totals={"sex": ("T",)},
        unit_code="PER",
        unit_ref="unit.persons",
        scale_factor="1",
        aggregation="period_average_stock",
        population_universe="esspros_pension_beneficiaries",
        years=tuple(range(2006, 2025)),
        note=("ESSPROS old-age pension beneficiaries (OLD_TOT: old-age, anticipated old-age and partial pensions), "
              "all schemes; counted without double counting of persons receiving several old-age pensions per "
              "the ESSPROS pension-beneficiaries module; reference date per country metadata (end of year or "
              "annual average); includes beneficiaries resident abroad where schemes pay them"),
    ),
    TableSpec(
        code="spr_exp_pens",
        concept_id="social.pension_expenditure.esspros",
        concept_version="1.0.0",
        dimension_order=("freq", "spdepb", "spdepm", "unit", "geo", "time"),
        filters={"freq": ("A",), "spdepb": ("TOTAL", "OLD", "AOLD", "PART"), "spdepm": ("TOTAL",),
                 "unit": ("MIO_EUR",)},
        resolution=("spdepb",),
        totals={"spdepb": ("TOTAL",)},
        unit_code="MIO_EUR",
        unit_ref="unit.eur",
        scale_factor="1000000",
        aggregation="period_flow_total",
        population_universe="esspros_social_protection_schemes",
        years=tuple(range(2000, 2025)),
        currency="EUR",
        price_basis="nominal",
        note=("ESSPROS expenditure on pensions, million euro at current prices; spdepb TOTAL also contains "
              "disability, survivors' and early-retirement benefits (not only old age)"),
    ),
    TableSpec(
        code="lfst_hhnhtych",
        concept_id="households.private_households.lfs_composition",
        concept_version="1.0.0",
        dimension_order=("freq", "agechild", "n_child", "phhcomp", "unit", "geo", "time"),
        filters={"freq": ("A",), "agechild": ("TOTAL",), "n_child": ("TOTAL", "0", "1", "2", "GE3"),
                 "phhcomp": ("TOTAL", "A1", "A1_NCH", "A1_CH", "CPL", "CPL_NCH", "CPL_CH", "OTH", "OTH_NCH",
                             "OTH_CH", "NRP"),
                 "unit": ("THS_HH",)},
        resolution=("phhcomp", "n_child"),
        totals={"phhcomp": ("TOTAL", "A1", "CPL", "OTH"), "n_child": ("TOTAL",)},
        unit_code="THS_HH",
        unit_ref="unit.households",
        scale_factor="1000",
        aggregation="period_average_stock",
        population_universe="private_households_lfs",
        years=tuple(range(2006, 2026)),
        note=("LFS private households (thousands) by household composition and number of resident children; "
              "'children' are resident sons/daughters per the LFS household module definitions; NRP = no response"),
    ),
    TableSpec(
        code="ilc_lvph03",
        concept_id="households.size_distribution.silc",
        concept_version="1.0.0",
        dimension_order=("freq", "n_person", "unit", "geo", "time"),
        filters={"freq": ("A",), "n_person": ("1", "2", "3", "4", "5", "GE6"), "unit": ("PC",)},
        resolution=("n_person",),
        totals={},
        unit_code="PC",
        unit_ref="unit.percent",
        scale_factor="1",
        aggregation="period_rate",
        population_universe="private_households_silc",
        years=tuple(range(2005, 2026)),
        note="EU-SILC distribution of private households by number of persons (percent of households)",
    ),
    TableSpec(
        code="ilc_lvph01",
        concept_id="households.average_size.silc",
        concept_version="1.0.0",
        dimension_order=("freq", "unit", "geo", "time"),
        filters={"freq": ("A",), "unit": ("AVG",)},
        resolution=(),
        totals={},
        unit_code="AVG",
        unit_ref="unit.persons_per_household",
        scale_factor="1",
        aggregation="period_rate",
        population_universe="private_households_silc",
        years=tuple(range(2005, 2026)),
        note="EU-SILC average number of persons per private household",
    ),
)
TABLES_BY_CODE: dict[str, TableSpec] = {t.code: t for t in TABLES}


def _positions(index: Any, size: int, dim: str) -> list[str]:
    if isinstance(index, list):
        codes = [str(c) for c in index]
    elif isinstance(index, dict):
        pairs = sorted(((int(str(v)), str(k)) for k, v in index.items()), key=lambda p: p[0])
        if [p for p, _ in pairs] != list(range(len(pairs))):
            raise SourceResponseError(f"dimension {dim} index positions are not 0..n-1", retryable=False,
                                      kind="schema_drift")
        codes = [k for _, k in pairs]
    else:
        raise SourceResponseError(f"dimension {dim} has no category index", retryable=False, kind="schema_drift")
    if len(codes) != size:
        raise SourceResponseError(f"dimension {dim}: {len(codes)} categories but size {size}", retryable=False,
                                  kind="schema_drift")
    return codes


def _sparse(obj: Any, n: int, what: str) -> dict[int, Any]:
    if obj is None:
        return {}
    if isinstance(obj, list):
        if len(obj) != n:
            raise SourceResponseError(f"{what} array length {len(obj)} != cube size {n}", retryable=False,
                                      kind="schema_drift")
        return {i: v for i, v in enumerate(obj) if v is not None}
    if isinstance(obj, dict):
        out: dict[int, Any] = {}
        for k, v in obj.items():
            i = int(str(k))
            if not 0 <= i < n:
                raise SourceResponseError(f"{what} position {i} outside cube of size {n}", retryable=False,
                                          kind="schema_drift")
            out[i] = v
        return out
    raise SourceResponseError(f"unexpected JSON-stat {what} type", retryable=False, kind="schema_drift")


def _decimal_text(v: Any) -> str:
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    if not d.is_finite():
        raise SourceResponseError("non-finite value", retryable=False, kind="schema_drift")
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


@dataclass(frozen=True)
class Cell:
    """One parsed cube position (provider semantics only; no REVA ids)."""

    table: str
    geo: str
    year: int
    coord: Mapping[str, str]
    value: str | None  # exact decimal text, None = absent from the value map
    status: str
    position: int


@dataclass(frozen=True)
class ParsedCube:
    table: TableSpec
    dimension_order: tuple[str, ...]
    categories: Mapping[str, tuple[str, ...]]
    cells: tuple[Cell, ...]
    status_labels: Mapping[str, str]
    updated: str | None
    value_count: int

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(sorted(int(t) for t in self.categories.get("time", ())))


def parse_cube(body: bytes, table: TableSpec) -> ParsedCube:
    """Parse JSON-stat bytes for ``table``. Structural problems raise ``SourceResponseError``."""
    try:
        doc = json_decimal(body)
        if not isinstance(doc, dict):
            raise SourceResponseError("JSON-stat document is not an object", retryable=False, kind="schema_drift")
        if "error" in doc and "value" not in doc:
            raise SourceResponseError(f"Eurostat API error: {str(plain(doc['error']))[:300]}", retryable=False,
                                      kind="api_error")
        if str(doc.get("version")) != "2.0" or doc.get("class") != "dataset":
            raise SourceResponseError("not a JSON-stat 2.0 dataset", retryable=False, kind="schema_drift")
        ids = [str(d) for d in doc["id"]]
        sizes = [int(str(s)) for s in doc["size"]]
        if len(ids) != len(sizes):
            raise SourceResponseError("JSON-stat id/size inconsistent", retryable=False, kind="schema_drift")
        n = math.prod(sizes)
        if n > MAX_POSITIONS:
            raise SourceResponseError(f"cube of {n} positions exceeds the connector bound", retryable=False,
                                      kind="oversized_cube")
        cats = {d: tuple(_positions(doc["dimension"][d]["category"].get("index"), s, d))
                for d, s in zip(ids, sizes, strict=True)}
        values = _sparse(doc.get("value"), n, "value")
        status = _sparse(doc.get("status"), n, "status")
        ext = doc.get("extension") if isinstance(doc.get("extension"), dict) else {}
        labels = {str(k): str(v) for k, v in dict(((ext.get("status") or {}).get("label")) or {}).items()}
        strides = [math.prod(sizes[i + 1:]) for i in range(len(sizes))]
        cells: list[Cell] = []
        for pos in range(n):
            coord = {d: cats[d][(pos // strides[i]) % sizes[i]] for i, d in enumerate(ids)}
            v = values.get(pos)
            cells.append(Cell(table.code, coord["geo"], int(coord["time"]), coord,
                              None if v is None else _decimal_text(v), str(status.get(pos) or ""), pos))
        return ParsedCube(table, tuple(ids), cats, tuple(cells), labels, doc.get("updated"), len(values))
    except SourceResponseError:
        raise
    except _MALFORMED as e:
        raise SourceResponseError(f"malformed JSON-stat document ({type(e).__name__})", retryable=False,
                                  kind="schema_drift") from None


def cell_type(table: TableSpec, coord: Mapping[str, str]) -> str:
    at_total = [d for d in table.resolution if coord.get(d) in set(table.totals.get(d, ()))]
    if not at_total:
        return "leaf"
    return "total" if len(at_total) == len(table.resolution) else "subtotal"


def logical_key(table: TableSpec, geo: str, coord: Mapping[str, str], year: int) -> str:
    parts = [f"{d}={coord[d]}" for d in table.resolution]
    return ":".join(["eurostat", table.code, geo, *parts, str(year)])


def quality_findings(cube: ParsedCube, geos: Sequence[str]) -> list[Finding]:
    """Blocking drift checks shared by ``validate`` and the dataset derivation."""
    t = cube.table
    f: list[Finding] = []
    if cube.dimension_order != t.dimension_order:
        f.append(Finding(code="dimension_order_changed", severity="blocker",
                         message=f"dimension order {list(cube.dimension_order)} differs from pinned "
                                 f"{list(t.dimension_order)}"))
    for dim, pinned in t.filters.items():
        got = cube.categories.get(dim)
        if got is None or sorted(got) != sorted(pinned):
            f.append(Finding(code="pinned_categories_changed", severity="blocker",
                             message=f"{t.code} dimension {dim}: categories {got} differ from pinned {list(pinned)}"))
    got_geo = cube.categories.get("geo", ())
    if sorted(got_geo) != sorted(geos):
        f.append(Finding(code="geo_mismatch", severity="blocker",
                         message=f"{t.code}: returned geo {list(got_geo)} != requested {list(geos)}"))
    years = cube.years
    if not years:
        f.append(Finding(code="empty_response", severity="blocker",
                         message="no periods returned: an empty response is never zero or complete"))
    elif not set(years) <= set(t.years):
        f.append(Finding(code="time_mismatch", severity="blocker",
                         message=f"{t.code}: periods {sorted(set(years) - set(t.years))} were not requested"))
    if cube.value_count == 0:
        f.append(Finding(code="empty_response", severity="blocker", message=f"{t.code}: no values in the cube"))
    seen: set[str] = set()
    for c in cube.cells:
        seen.update(ch for ch in c.status if ch.strip())
    for ch in sorted(seen):
        if ch not in EUROSTAT_FLAGS:
            f.append(Finding(code="unknown_status_flag", severity="blocker",
                             message=f"status flag {ch!r} is not in the reviewed vocabulary"))
    for code, label in cube.status_labels.items():
        # Eurostat appends qualifiers such as " (see metadata)"; the reviewed meaning must lead.
        if len(code) == 1 and code in EUROSTAT_FLAGS and not label.strip().lower().startswith(EUROSTAT_FLAGS[code]):
            f.append(Finding(code="status_label_changed", severity="warning",
                             message=f"flag {code!r} label {label!r} differs from the reviewed meaning"))
    neg = [c for c in cube.cells if c.value is not None and Decimal(c.value) < 0]
    if neg:
        f.append(Finding(code="negative_value", severity="blocker", message=f"{len(neg)} negative values"))
    if t.unit_ref == "unit.percent" and t.unit_code == "PC":
        over = [c for c in cube.cells if c.value is not None and Decimal(c.value) > 100]
        if over:
            f.append(Finding(code="percent_above_100", severity="blocker",
                             message=f"{len(over)} percentages above 100"))
    n_missing = sum(1 for c in cube.cells if c.value is None)
    if n_missing:
        f.append(Finding(code="missing_values", severity="warning",
                         message=f"{n_missing} positions absent from the value map (kept Missing, never zero)"))
    return f


class EurostatLabourPensionsConnector:
    """Reviewed connector for the labour-market, pension and household tables above."""

    def __init__(self, geos: Sequence[str] = DEFAULT_GEOS, tables: Sequence[str] | None = None) -> None:
        unknown = [g for g in geos if g not in EUROSTAT_GEO_TO_ISO3]
        if unknown:
            raise ValueError(f"geo codes without a reviewed ISO3 mapping: {unknown}")
        self.geos = tuple(geos)
        self.tables = tuple(TABLES_BY_CODE[c] for c in (tables or [t.code for t in TABLES]))

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            template="eurostat_jsonstat",
            source_key=SOURCE_KEY,
            provider="Eurostat",
            licence=LICENCE,
            allowed_hosts=(HOST,),
            dataset_ids=tuple(t.code for t in self.tables),
            field_mappings={t.code: t.describe() for t in self.tables},
            units={t.code: t.unit_ref for t in self.tables},
            geography=self.geos,
            time_semantics=("annual (calendar year); LFS rates and counts are annual averages; ESSPROS "
                            "beneficiaries per country reference date; ESSPROS expenditure annual flows"),
            lineage_family="eurostat",
            cadence_seconds=30 * 86400,
            revision_strategy="append revision when value or status flag changes (e.g. provisional -> final)",
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(EurostatLabourPensionsConnector),
        )

    def data_url(self, t: TableSpec) -> str:
        params: list[tuple[str, str]] = [("format", "JSON"), ("lang", "EN")]
        for g in self.geos:
            params.append(("geo", g))
        for dim in sorted(t.filters):
            for code in t.filters[dim]:
                if not _CODE_RE.match(code):
                    raise ValueError(f"invalid category code {code!r}")
                params.append((dim, code))
        for y in t.years:
            params.append(("time", str(y)))
        return DATA_BASE + t.code + "?" + urlencode(params)

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        items = tuple(
            FetchItem(key=f"jsonstat:{t.code}", url=self.data_url(t), expected_media_types=("application/json",),
                      max_bytes=MAX_RESPONSE_BYTES, params={"kind": "jsonstat", "table": t.code})
            for t in self.tables
        )
        kept = items[: budget.max_requests]
        return FetchPlan(connector=CONNECTOR_NAME, items=kept,
                         pagination="none: one filtered JSON-stat cube per table (all countries)",
                         availability_expectation="Eurostat dataset-level 'updated'; no per-value publication time",
                         budget_exhausted=len(kept) < len(items))

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    def _table_of(self, raw: RawAcquisition) -> TableSpec:
        code = str(raw.item.params.get("table", ""))
        if code not in TABLES_BY_CODE:
            raise SourceResponseError(f"unknown table {code!r}", retryable=False, kind="schema_drift")
        return TABLES_BY_CODE[code]

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        require_json(HttpResult(raw.item.url, raw.item.url, 200, {"content-type": raw.media_type}, raw.body,
                                raw.retrieved_at, 0.0, (), 1))
        t = self._table_of(raw)
        cube = parse_cube(raw.body, t)
        obs = tuple(self._observation(t, c, cube.status_labels) for c in cube.cells)
        meta = {
            "kind": "jsonstat",
            "table": t.code,
            "dataset_updated": cube.updated,  # dataset-level; NOT a value publication time
            "dimension_order": list(cube.dimension_order),
            "categories": {d: list(v) for d, v in cube.categories.items()},
            "status_labels": dict(cube.status_labels),
            "value_count": cube.value_count,
            "cube_positions": len(cube.cells),
            "years_returned": list(cube.years),
            "years_requested": [t.years[0], t.years[-1]],
            "parser_version": PARSER_VERSION,
        }
        fp = hashlib.sha256(json.dumps({"order": meta["dimension_order"],
                                        "cats": {d: sorted(v) for d, v in meta["categories"].items() if d != "time"},
                                        "top": schema_fingerprint({"labels": sorted(cube.status_labels)})},
                                       sort_keys=True).encode()).hexdigest()
        yield ObservationBatch(connector=CONNECTOR_NAME, connector_version=CONNECTOR_VERSION, item_key=raw.item.key,
                               observations=obs, expected_keys=tuple(o.logical_key for o in obs), metadata=meta,
                               schema_fingerprint=fp)

    def _observation(self, t: TableSpec, c: Cell, labels: Mapping[str, str]) -> ParsedObservation:
        flags = [ch for ch in c.status if ch.strip()]
        if c.value is None:
            reason = next((MISSING_REASON_BY_FLAG[ch] for ch in flags if ch in MISSING_REASON_BY_FLAG), "unavailable")
            value: dict[str, Any] = {"kind": "missing", "reason": reason}
        else:
            basis: dict[str, Any] = {"scale_factor": t.scale_factor}
            if t.currency:
                basis.update({"currency": t.currency, "price_basis": t.price_basis})
            value = {"kind": "decimal", "value": c.value, "unit_ref": t.unit_ref, "basis": basis}
        support = Support(
            temporal=TimeExtent.calendar_year(c.year),
            spatial=SpatialRef(scheme="eurostat_geo_code", key=f"ESTAT:{c.geo}", authority="Eurostat"),
            population=PopulationRef(universe=t.population_universe,
                                     definition_source=f"Eurostat {t.code} metadata"),
            aggregation=t.aggregation,  # type: ignore[arg-type]
            resolution={d: c.coord[d] for d in t.resolution},
        )
        source_flags: dict[str, Any] = {
            "dataset": t.code,
            "status": c.status,
            "status_labels": {ch: EUROSTAT_FLAGS.get(ch, labels.get(ch, "")) for ch in flags},
            "cell_type": cell_type(t, c.coord),
            "categories": {d: c.coord[d] for d in t.dimension_order if d != "time"},
            "unit_code": t.unit_code,
        }
        if c.value is None:
            source_flags["absent_in_value_map"] = True
        return ParsedObservation(
            subject_provider="eurostat_geo",
            subject_key=c.geo,
            concept_id=t.concept_id,
            concept_version=t.concept_version,
            support=support,
            logical_key=logical_key(t, c.geo, c.coord, c.year),
            value=value,
            uncertainty=UncertaintySpec.unknown(
                "Eurostat publishes no uncertainty for this value (LFS/SILC are sample surveys; sampling error "
                "is not published in this table)"),
            source_flags=source_flags,
            raw_selector={"jsonstat_position": c.position, "coordinates": dict(c.coord)},
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        md = batch.metadata
        t = TABLES_BY_CODE.get(str(md.get("table")))
        if t is None:
            return QualityReport(outcome="blocked", findings=(Finding(code="unknown_table", severity="blocker",
                                                                      message="batch has no known table"),))
        cats = {d: tuple(v) for d, v in (md.get("categories") or {}).items()}
        cells = tuple(Cell(t.code, o.subject_key, int(o.support.temporal.start.year), {},
                           o.value.get("value") if o.value.get("kind") == "decimal" else None,
                           str(o.source_flags.get("status", "")), int(o.raw_selector["jsonstat_position"]))
                      for o in batch.observations if o.support.temporal.start is not None)
        cube = ParsedCube(t, tuple(md.get("dimension_order") or ()), cats, cells,
                          dict(md.get("status_labels") or {}), md.get("dataset_updated"), int(md.get("value_count", 0)))
        f = quality_findings(cube, self.geos)
        n = int(md.get("cube_positions", 0))
        if len(batch.observations) != n:
            f.append(Finding(code="size_mismatch", severity="blocker",
                             message=f"{len(batch.observations)} cells parsed for a cube of {n}"))
        keys = [o.logical_key for o in batch.observations]
        if len(keys) != len(set(keys)):
            f.append(Finding(code="duplicate_keys", severity="blocker", message="duplicate logical keys"))
        outcome = "blocked" if any(x.severity == "blocker" for x in f) else "pass"
        return QualityReport(outcome=outcome, findings=tuple(f), expected_count=n, received_count=len(keys),
                             missing_keys=tuple(sorted(o.logical_key for o in batch.observations
                                                       if o.value.get("kind") == "missing")))

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        ok = bool(outcomes) and all(o.status in ("published", "unchanged") for o in outcomes)
        return Cursor(state={"complete": ok and not plan.budget_exhausted,
                             "tables": [t.code for t in self.tables], "geos": list(self.geos)})


# --------------------------------------------------------------------------- dataset inputs
def dataset_schema() -> Any:
    import pyarrow as pa

    return pa.schema([
        pa.field("table", pa.string(), nullable=False),
        pa.field("iso3", pa.string(), nullable=False),
        pa.field("geo", pa.string(), nullable=False),
        pa.field("year", pa.int16(), nullable=False),
        pa.field("dim1_name", pa.string(), nullable=True),
        pa.field("dim1", pa.string(), nullable=True),
        pa.field("dim2_name", pa.string(), nullable=True),
        pa.field("dim2", pa.string(), nullable=True),
        pa.field("cell_type", pa.string(), nullable=False),
        pa.field("value", pa.string(), nullable=True),  # exact decimal text; null = Missing
        pa.field("missing_reason", pa.string(), nullable=True),
        pa.field("status", pa.string(), nullable=False),
        pa.field("unit_ref", pa.string(), nullable=False),
        pa.field("scale_factor", pa.string(), nullable=False),
        pa.field("jsonstat_position", pa.int32(), nullable=False),
    ])


def derive_dataset_tables(body: bytes, table_code: str, geos: Sequence[str] = DEFAULT_GEOS) -> dict[str, Any]:
    """Raw JSON-stat bytes of one table -> {ISO3: Arrow table} (deterministic, sorted).

    Refuses (``SourceResponseError``) when the cube fails a blocking drift check: a
    dataset input is never derived from bytes the connector would quarantine.
    Values stay exact decimal text in the provider scale; missing stays null with a
    reason.
    """
    import pyarrow as pa

    t = TABLES_BY_CODE[table_code]
    cube = parse_cube(body, t)
    blockers = [f for f in quality_findings(cube, geos) if f.severity == "blocker"]
    if blockers:
        raise SourceResponseError(f"{table_code}: cube fails drift checks: {blockers[0].message}", retryable=False,
                                  kind="schema_drift", findings=[b.model_dump() for b in blockers])
    dims = list(t.resolution) + [None, None]
    out: dict[str, Any] = {}
    for geo in geos:
        rows = sorted((c for c in cube.cells if c.geo == geo),
                      key=lambda c: (c.year, *(c.coord[d] for d in t.resolution)))
        flags_of = [[ch for ch in c.status if ch.strip()] for c in rows]
        cols: dict[str, list[Any]] = {
            "table": [t.code] * len(rows),
            "iso3": [EUROSTAT_GEO_TO_ISO3[geo]] * len(rows),
            "geo": [geo] * len(rows),
            "year": [c.year for c in rows],
            "dim1_name": [dims[0]] * len(rows),
            "dim1": [c.coord[dims[0]] if dims[0] else None for c in rows],
            "dim2_name": [dims[1]] * len(rows),
            "dim2": [c.coord[dims[1]] if dims[1] else None for c in rows],
            "cell_type": [cell_type(t, c.coord) for c in rows],
            "value": [c.value for c in rows],
            "missing_reason": [None if c.value is not None else next(
                (MISSING_REASON_BY_FLAG[ch] for ch in fl if ch in MISSING_REASON_BY_FLAG), "unavailable")
                for c, fl in zip(rows, flags_of, strict=True)],
            "status": [c.status for c in rows],
            "unit_ref": [t.unit_ref] * len(rows),
            "scale_factor": [t.scale_factor] * len(rows),
            "jsonstat_position": [c.position for c in rows],
        }
        out[EUROSTAT_GEO_TO_ISO3[geo]] = pa.table(cols, schema=dataset_schema())
    return out


def dataset_manifest(table_code: str, iso3: str, table: Any, *, raw_sha256: str,
                     acquisition_id: str | None = None) -> dict[str, Any]:
    """Dataset manifest fields (the platform adds artifact sha256 and ids on registration)."""
    t = TABLES_BY_CODE[table_code]
    return {
        "schema": "reva-dataset-manifest/1",
        "concept": f"dataset.{t.concept_id}",
        "source_concept": f"{t.concept_id}@{t.concept_version}",
        "subject": iso3,
        "source_table": t.code,
        "connector": f"{CONNECTOR_NAME}@{CONNECTOR_VERSION}",
        "parser_version": PARSER_VERSION,
        "derivation_version": DATASET_DERIVATION_VERSION,
        "source_raw_sha256": [raw_sha256],
        "source_acquisition_ids": [acquisition_id] if acquisition_id else [],
        "row_count": int(table.num_rows),
        "columns": [f.name for f in table.schema],
        "years": sorted({int(y) for y in table.column("year").to_pylist()}),
        "semantics": t.describe(),
    }
