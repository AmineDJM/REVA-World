"""ILOSTAT employment and labour-force rates by sex and five-year age band (``ilostat.labour_age5``).

Reviewed built-in connector (V3 agent SIMWORLD) feeding ``social.retirement_age@1.1.0`` for
countries outside the Eurostat LFS: two ILOSTAT bulk indicator files (licence CC BY 4.0,
``registry/sources/ilostat.yaml``; service ``rplumber.ilo.org``, one gzip CSV per indicator,
every area and year, the ILO-selected best source per area-year):

* ``EMP_DWAP_SEX_AGE_RT_A`` - employment-to-population ratio by sex and age (%), annual;
* ``EAP_DWAP_SEX_AGE_RT_A`` - labour force participation rate by sex and age (%), annual.

Only the five-year age bands are kept (``AGE_5YRBANDS_*``: 15-19 .. 60-64 and 65+), sexes
F, M and T. ILOSTAT publishes **no 65-69 band** (the five-year classification ends with
``YGE65``), which is why the 1.1.0 retirement model works with ages 55-64 on this path.

Values are kept verbatim (exact decimal text, percent) with ILOSTAT's observation status
(e.g. ``B`` break in series, ``U`` unreliable) and notes; the source column (survey/series id,
e.g. ``BA:829``) names the national series the value comes from. ILOSTAT national values are
compiled from national labour force surveys: for EU countries they are not independent of
Eurostat LFS values (lineage ``ilostat``, see the source file).

The connector publishes no scalar observations: each indicator file is archived as one
acquisition and turned into one Parquet *dataset input* per (indicator, country) by
:func:`post_publish_datasets` (schema ``reva-ilostat-age5-table/1``). Areas outside the
country crosswalk (ILO regional aggregates ``X..``) are counted and listed, never mapped.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from reva.adapters.connectors.base import module_digest, to_raw
from reva.adapters.connectors.un_wpp_demography import crosswalk_iso3
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

NAME = "ilostat.labour_age5"
VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB while the code was still in flux (never reuse)
PARSER_VERSION = "ilostat-bulk-csv-age5-1"
TEMPLATE = "eurostat_sdmx_csv"  # V2 built-in template value (see un_wpp_demography.TEMPLATE)
SOURCE_KEY = "ilostat.rplumber"
HOSTS: tuple[str, ...] = ("rplumber.ilo.org",)
BASE = "https://rplumber.ilo.org/data/indicator/"
LICENCE = "CC-BY-4.0 (ILO, ILOSTAT)"
ATTRIBUTION = "ILOSTAT, International Labour Organization (https://ilostat.ilo.org/data/)"
MAX_RESPONSE_BYTES = 20_000_000
DATASET_SCHEMA = "reva-ilostat-age5-table/1"
HEADER = (
    "ref_area",
    "source",
    "indicator",
    "sex",
    "classif1",
    "time",
    "obs_value",
    "obs_status",
    "note_classif",
    "note_indicator",
    "note_source",
)
SEXES = {"SEX_F": "F", "SEX_M": "M", "SEX_T": "T"}
AGE_PREFIX = "AGE_5YRBANDS_"
#: Reviewed ILOSTAT observation status codes (ILOSTAT metadata, checked 2026-10-15).
OBS_STATUS = {
    "": "none",
    "B": "break in series",
    "U": "unreliable",
    "E": "estimate",
    "P": "provisional",
    "F": "forecast",
    "I": "imputation",
    "S": "suppressed",
    "N": "not significant",
    "R": "real value",
    "C": "confidential",
    "X": "not applicable",
}


@dataclass(frozen=True)
class Indicator:
    id: str
    concept_id: str
    label: str


INDICATORS: tuple[Indicator, ...] = (
    Indicator(
        "EMP_DWAP_SEX_AGE_RT_A",
        "labour.ilostat.employment_to_population_ratio.age5_sex",
        "Employment-to-population ratio by sex and age (%), annual",
    ),
    Indicator(
        "EAP_DWAP_SEX_AGE_RT_A",
        "labour.ilostat.labour_force_participation_rate.age5_sex",
        "Labour force participation rate by sex and age (%), annual",
    ),
)
CONCEPT_VERSION = "1.0.0"
DATASET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dataset", "string"),
    ("iso3", "string"),
    ("ref_area", "string"),
    ("source", "string"),
    ("sex", "string"),
    ("age", "string"),
    ("age_kind", "string"),
    ("age_start", "int32"),
    ("age_end", "int32"),
    ("year", "int32"),
    ("value", "string"),
    ("unit", "string"),
    ("status", "string"),
    ("status_label", "string"),
    ("note_classif", "string"),
    ("note_indicator", "string"),
    ("note_source", "string"),
)
#: ILOSTAT area codes that are not crosswalk keys, reviewed against the ILOSTAT ``ref_area`` dictionary
#: (https://rplumber.ilo.org/metadata/dic/?var=ref_area&format=.csv, retrieved 2026-10-15:
#: ``"KOS","Kosovo"``). ``ANT`` ("Netherlands Antilles", dissolved 2010) has no successor key: kept unmapped.
AREA_ALIASES = {"KOS": "XKX"}
_DEC = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_BAND = re.compile(r"^Y(\d+)-(\d+)$")


def _fail(msg: str, kind: str = "schema_drift") -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind=kind)


def data_url(indicator_id: str) -> str:
    return f"{BASE}?id={indicator_id}&format=.csv.gz"


def age_band(classif1: str) -> tuple[str, str, int, int | None]:
    """(code, kind, start, end exclusive) of an ``AGE_5YRBANDS_*`` code."""
    tail = classif1[len(AGE_PREFIX) :]
    if tail == "TOTAL":
        return "TOTAL", "total", 0, None
    if m := _BAND.match(tail):
        a, b = int(m.group(1)), int(m.group(2))
        if b - a == 4:
            return f"Y{a}-{b}", "group", a, b + 1
    if m := re.match(r"^YGE(\d+)$", tail):
        n = int(m.group(1))
        return f"Y_GE{n}", "open_upper", n, None
    raise _fail(f"unreviewed ILOSTAT age band {classif1!r}")


@dataclass
class ParseReport:
    rows_read: int = 0
    rows_kept: int = 0
    other_classif_rows: int = 0
    other_sex_rows: int = 0
    unmapped_areas: dict[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "rows_kept": self.rows_kept,
            "other_classif_rows": self.other_classif_rows,
            "other_sex_rows": self.other_sex_rows,
            "unmapped_areas": dict(sorted((self.unmapped_areas or {}).items())),
        }


def iter_csv(body: bytes) -> Iterator[list[str]]:
    if body[:2] != b"\x1f\x8b":
        raise _fail("ILOSTAT response is not gzip (an empty or HTML body is never data)", kind="not_gzip")
    try:
        text = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(body)), encoding="utf-8-sig", newline="")
        reader = csv.reader(text)
        head = next(reader)
        if tuple(head) != HEADER:
            raise _fail(f"ILOSTAT header drift: {head} != {list(HEADER)}")
        for row in reader:
            if len(row) != len(HEADER):
                raise _fail(f"ILOSTAT row with {len(row)} fields")
            yield row
    except (OSError, EOFError, UnicodeDecodeError) as e:
        raise _fail(f"ILOSTAT file could not be decoded: {type(e).__name__}", kind="integrity") from e


def parse_indicator(
    indicator_id: str, body: bytes, countries: Iterable[str] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], ParseReport]:
    """Rows per country (crosswalk key) of one indicator file: five-year bands, F/M/T."""
    rep = ParseReport()
    xw = crosswalk_iso3()
    wanted = set(countries) if countries is not None else None
    out: dict[str, list[dict[str, Any]]] = {}
    for r in iter_csv(body):
        rep.rows_read += 1
        area, src, ind, sex, cl1, time, val, st, nc, ni, ns = r
        if ind != indicator_id.removesuffix("_A"):
            raise _fail(f"indicator {ind!r} in the file of {indicator_id}")
        if not cl1.startswith(AGE_PREFIX):
            rep.other_classif_rows += 1
            continue
        if sex not in SEXES:
            rep.other_sex_rows += 1
            continue
        area = AREA_ALIASES.get(area, area)
        if area not in xw:
            rep.unmapped_areas = {**(rep.unmapped_areas or {}), area: (rep.unmapped_areas or {}).get(area, 0) + 1}
            continue
        if wanted is not None and area not in wanted:
            continue
        if st not in OBS_STATUS:
            raise _fail(f"unreviewed ILOSTAT observation status {st!r}")
        if val != "" and not _DEC.match(val):
            raise _fail(f"non-numeric ILOSTAT value {val[:30]!r}")
        if not re.fullmatch(r"\d{4}", time):
            raise _fail(f"non-annual ILOSTAT period {time!r}")
        code, kind, a, b = age_band(cl1)
        rep.rows_kept += 1
        out.setdefault(area, []).append(
            {
                "dataset": indicator_id,
                "iso3": area,
                "ref_area": r[0],
                "source": src,
                "sex": SEXES[sex],
                "age": code,
                "age_kind": kind,
                "age_start": a,
                "age_end": b,
                "year": int(time),
                "value": val if val != "" else None,
                "unit": "percent",
                "status": st,
                "status_label": OBS_STATUS[st],
                "note_classif": nc,
                "note_indicator": ni,
                "note_source": ns,
            }
        )
    return out, rep


def rows_to_table(rows: Sequence[Mapping[str, Any]]) -> Any:
    import pyarrow as pa

    schema = pa.schema([(n, getattr(pa, t)()) for n, t in DATASET_COLUMNS])
    ordered = sorted(rows, key=lambda r: (r["year"], r["sex"], r["age_start"], r["age"], r["source"]))
    return pa.table({n: [r.get(n) for r in ordered] for n, _ in DATASET_COLUMNS}, schema=schema)


def build_tables(
    indicator_id: str, body: bytes, countries: Iterable[str] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    per, rep = parse_indicator(indicator_id, body, countries)
    tables: dict[str, Any] = {}
    dupes: dict[str, int] = {}
    for iso3, rows in sorted(per.items()):
        keys = [(r["year"], r["sex"], r["age"]) for r in rows]
        if len(keys) != len(set(keys)):  # the bulk file holds one selected source per area-year
            dupes[iso3] = len(keys) - len(set(keys))
            continue
        tables[iso3] = rows_to_table(rows)
    return tables, {"countries": len(tables), "duplicate_cells_refused": dupes, "parse": rep.as_dict()}


class IlostatLabourConnector:
    """Reviewed built-in connector: the two ILOSTAT rate files by sex and five-year age band."""

    def __init__(self, indicators: Sequence[Indicator] = INDICATORS, countries: Sequence[str] | None = None) -> None:
        self.indicators = tuple(indicators)
        self.countries = tuple(sorted(countries)) if countries is not None else None
        self._prior_done: set[str] = set()

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name=NAME,
            version=VERSION,
            template=TEMPLATE,  # type: ignore[arg-type]
            source_key=SOURCE_KEY,
            provider="International Labour Organization (ILOSTAT)",
            licence=LICENCE,
            allowed_hosts=HOSTS,
            dataset_ids=tuple(i.id for i in self.indicators),
            field_mappings={i.id: f"{i.concept_id}@{CONCEPT_VERSION}" for i in self.indicators},
            units={i.id: "unit.percent" for i in self.indicators},
            geography=self.countries or tuple(sorted(crosswalk_iso3())),
            time_semantics="annual averages of national labour force surveys (calendar year), as selected by ILOSTAT",
            lineage_family="ilostat",
            cadence_seconds=30 * 86400,
            release_aware=True,
            revision_strategy="new acquisition when the file bytes change (ILOSTAT revises series continuously)",
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_pages=10,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(IlostatLabourConnector),
        )

    def all_items(self) -> list[FetchItem]:
        return [
            FetchItem(
                key=i.id,
                url=data_url(i.id),
                expected_media_types=("application/octet-stream", "application/gzip", "application/x-gzip"),
                max_bytes=MAX_RESPONSE_BYTES,
                params={"kind": "ilostat_bulk_csv_gz", "indicator": i.id},
            )
            for i in self.indicators
        ]

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        state = dict(cursor.state) if cursor is not None else {}
        done = set(state.get("done") or []) if not state.get("complete") else set()
        self._prior_done = done
        pending = [i for i in self.all_items() if i.key not in done]
        items = tuple(pending[: max(0, budget.max_requests)])
        return FetchPlan(
            connector=NAME,
            items=items,
            pagination="none: one bulk gzip CSV per indicator",
            availability_expectation="ILOSTAT updates series continuously",
            budget_exhausted=len(items) < len(pending),
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        ok = {o.item_key for o in outcomes if o.status in ("published", "unchanged")}
        keys = [i.key for i in self.all_items()]
        done = sorted((set(self._prior_done) | ok) & set(keys))
        complete = len(done) == len(keys)
        return Cursor(state={"complete": complete, "done": [] if complete else done})

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        ind = str(raw.item.params["indicator"])
        _tables, rep = build_tables(ind, raw.body, self.countries)
        yield ObservationBatch(
            connector=NAME,
            connector_version=VERSION,
            item_key=raw.item.key,
            observations=(),
            metadata={
                "kind": "ilostat_bulk_csv_gz",
                "indicator": ind,
                "countries": rep["countries"],
                "duplicate_cells_refused": rep["duplicate_cells_refused"],
                "parse": rep["parse"],
                "dataset_input": {
                    "schema": DATASET_SCHEMA,
                    "concept": dict((i.id, i.concept_id) for i in INDICATORS)[ind],
                },
            },
            schema_fingerprint=",".join(HEADER),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        md = batch.metadata
        f: list[Finding] = []
        if not md.get("countries"):
            f.append(
                Finding(code="empty_response", severity="blocker", message="no country rows: never zero or complete")
            )
        if md.get("duplicate_cells_refused"):
            f.append(
                Finding(
                    code="duplicate_cells",
                    severity="warning",
                    message=f"countries with several values per (year, sex, age) left out: {sorted(md['duplicate_cells_refused'])}",
                )
            )
        return QualityReport(
            outcome="blocked" if any(x.severity == "blocker" for x in f) else "pass",
            findings=tuple(f),
        )


def register_tables(
    conn: Any, store: Any, raw: RawAcquisition, acquisition_id: str, countries: Iterable[str] | None = None
) -> list[Any]:
    from reva.simulation.datasets import register_dataset

    ind = str(raw.item.params["indicator"])
    concept = dict((i.id, i.concept_id) for i in INDICATORS)[ind]
    tables, rep = build_tables(ind, raw.body, countries)
    out = []
    for iso3, table in sorted(tables.items()):
        rows = table.to_pylist()
        years = sorted({r["year"] for r in rows})
        out.append(
            register_dataset(
                conn,
                store,
                dataset_key=f"ilostat.{ind.lower()}",
                concept_id=concept,
                subject_key=iso3,
                table=table,
                source_acquisition_ids=[acquisition_id],
                parser_version=PARSER_VERSION,
                origin=raw.origin,
                coverage={
                    "indicator": ind,
                    "years": [years[0], years[-1]] if years else [],
                    "age_bands": sorted({r["age"] for r in rows}),
                    "sources": sorted({r["source"] for r in rows}),
                    "status_codes": sorted({r["status"] for r in rows if r["status"]}),
                    "file_parse": rep["parse"],
                },
                actor=f"connector:{NAME}",
                rights={"licence": LICENCE, "attribution": ATTRIBUTION, "source_key": SOURCE_KEY},
            )
        )
    return out


def post_publish_datasets(
    conn: Any, store: Any, raw: RawAcquisition, batches: Sequence[ObservationBatch], summary: Any
) -> None:
    """Register the per-country dataset inputs of this indicator file (acquisition transaction)."""
    if summary.acquisition_id is None:
        raise _fail("post-publish hook without an acquisition id", kind="integrity")
    register_tables(conn, store, raw, str(summary.acquisition_id))


IlostatLabourConnector.post_publish = staticmethod(post_publish_datasets)  # type: ignore[attr-defined]
