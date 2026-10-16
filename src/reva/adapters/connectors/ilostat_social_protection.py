"""ILOSTAT SDG indicator 1.3.1, old-age pension coverage by sex (``ilostat.social_protection``).

Reviewed built-in connector (V3 agent SIMFIX) feeding ``social.retirement_age@1.2.0``: one ILOSTAT
bulk indicator file (licence CC BY 4.0, ``registry/sources/ilostat.yaml``; service ``rplumber.ilo.org``,
one gzip CSV for every area and year):

* ``SDG_0131_SEX_SOC_RT_A`` - "SDG indicator 1.3.1: Proportion of population covered by social
  protection floors/systems by sex and function (%)", annual, ILOSTAT ``database`` ILOSDG, source
  "ILO - Social Security Inquiry Database" (``XA:<n>`` source ids, one per area).

Only the function ``SOC_CONTIG_PENSION`` is kept - ILOSTAT ``classif1`` label (dictionary retrieved
2026-10-16): "Function: Persons above retirement age receiving a pension" - for sexes F, M and T.
The other functions (children, unemployed, disability, ...) are counted and dropped. The series
counts **contributory and non-contributory** old-age pensions alike and is measured among persons
*above* the statutory pensionable age (the ILO World Social Protection Report definition): it is not
a share of contributors among workers. ILOSTAT publishes no contributor series (checked 2026-10-16:
the indicator catalogue's only social-protection indicator is this one; the World Social Protection
Database pages answer HTTP 403 to this environment).

Values are kept verbatim (exact decimal text, percent; ILOSTAT caps them at 100) with ILOSTAT's
observation status and the classification note code (``note_classif``, e.g. ``C15:6326`` "Estimated
based on legal coverage") and its reviewed label. The connector publishes no scalar observations: the
file is archived as one acquisition and turned into one Parquet *dataset input* per country by
:func:`post_publish_datasets` (schema ``reva-ilostat-sdg131-pension-table/1``). Areas outside the
country crosswalk (ILO regional aggregates ``X..``) are counted and listed, never mapped.

A separate connector (not a new version of ``ilostat.labour_age5``): different file layout (nine
columns, no indicator/source notes) and a different concept; ``ilostat.labour_age5@1.0.1`` is
unchanged.
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

NAME = "ilostat.social_protection"
VERSION = "1.0.0"
PARSER_VERSION = "ilostat-bulk-csv-sdg131-pension-1"
TEMPLATE = "eurostat_sdmx_csv"  # V2 built-in template value (see un_wpp_demography.TEMPLATE)
SOURCE_KEY = "ilostat.rplumber"
HOSTS: tuple[str, ...] = ("rplumber.ilo.org",)
BASE = "https://rplumber.ilo.org/data/indicator/"
LICENCE = "CC-BY-4.0 (ILO, ILOSTAT)"
ATTRIBUTION = "ILOSTAT, International Labour Organization (https://ilostat.ilo.org/data/)"
MAX_RESPONSE_BYTES = 5_000_000  # 184 757 bytes on 2026-10-16
DATASET_SCHEMA = "reva-ilostat-sdg131-pension-table/1"
INDICATOR_ID = "SDG_0131_SEX_SOC_RT_A"
INDICATOR_LABEL = (
    "SDG indicator 1.3.1: Proportion of population covered by social protection floors/systems by sex and function (%)"
)
FUNCTION = "SOC_CONTIG_PENSION"
FUNCTION_LABEL = "Function: Persons above retirement age receiving a pension"
CONCEPT_ID = "social_protection.ilostat.old_age_pension_coverage.sex"
CONCEPT_VERSION = "1.0.0"
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
)
SEXES = {"SEX_F": "F", "SEX_M": "M", "SEX_T": "T"}
#: Reviewed ILOSTAT observation status codes (same reviewed list as ilostat.labour_age5).
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
#: ILOSTAT ``note_classif`` codes seen on social-security coverage rows, labels verbatim from the ILOSTAT
#: dictionary https://rplumber.ilo.org/metadata/dic/?var=note_classif&format=.csv (retrieved 2026-10-16).
#: An unknown code is kept with the label ``None`` (never guessed).
NOTE_CLASSIF = {
    "C15:6302": "Social security coverage: Disaggregation by sex available partially",
    "C15:6326": "Social security coverage: Estimated based on legal coverage",
    "C15:6327": "Social security coverage: Based on available data mainly from central government schemes",
    "C15:6328": "Social security coverage: Including school feeding programmes",
    "C15:6329": (
        "Social security coverage: Including school feeding programme(s) and other food benefits for children"
    ),
    "C15:6330": "Social security coverage: Data not available for BOCW Fund",
    "C15:6331": "Social security coverage: National estimate",
    "C15:6332": (
        "Social security coverage: Estimated based on available data for Public Sector Pension and Social "
        "Security Allowance Programme (SSA) only"
    ),
    "C15:6565": "Social security coverage: Estimated based on national programmes only",
}
DATASET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dataset", "string"),
    ("iso3", "string"),
    ("ref_area", "string"),
    ("source", "string"),
    ("sex", "string"),
    ("function", "string"),
    ("year", "int32"),
    ("value", "string"),
    ("unit", "string"),
    ("status", "string"),
    ("status_label", "string"),
    ("note_classif", "string"),
    ("note_classif_label", "string"),
)
#: ILOSTAT area codes that are not crosswalk keys (reviewed for ilostat.labour_age5 against the ILOSTAT
#: ref_area dictionary, 2026-10-15: ``"KOS","Kosovo"``).
AREA_ALIASES = {"KOS": "XKX"}
_DEC = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _fail(msg: str, kind: str = "schema_drift") -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind=kind)


def data_url(indicator_id: str = INDICATOR_ID) -> str:
    return f"{BASE}?id={indicator_id}&format=.csv.gz"


@dataclass
class ParseReport:
    rows_read: int = 0
    rows_kept: int = 0
    other_function_rows: int = 0
    other_sex_rows: int = 0
    unmapped_areas: dict[str, int] | None = None
    unknown_note_codes: dict[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "rows_kept": self.rows_kept,
            "other_function_rows": self.other_function_rows,
            "other_sex_rows": self.other_sex_rows,
            "unmapped_areas": dict(sorted((self.unmapped_areas or {}).items())),
            "unknown_note_codes": dict(sorted((self.unknown_note_codes or {}).items())),
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
    except (OSError, EOFError, UnicodeDecodeError, StopIteration) as e:
        raise _fail(f"ILOSTAT file could not be decoded: {type(e).__name__}", kind="integrity") from e


def parse_indicator(
    body: bytes, countries: Iterable[str] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], ParseReport]:
    """Rows per country (crosswalk key) of the SDG 1.3.1 file: old-age pension function, F/M/T."""
    rep = ParseReport()
    xw = crosswalk_iso3()
    wanted = set(countries) if countries is not None else None
    out: dict[str, list[dict[str, Any]]] = {}
    for r in iter_csv(body):
        rep.rows_read += 1
        area, src, ind, sex, cl1, time, val, st, nc = r
        if ind != INDICATOR_ID.removesuffix("_A"):
            raise _fail(f"indicator {ind!r} in the file of {INDICATOR_ID}")
        if not cl1.startswith("SOC_CONTIG_"):
            raise _fail(f"unreviewed ILOSTAT social protection function {cl1!r}")
        if cl1 != FUNCTION:
            rep.other_function_rows += 1
            continue
        if sex not in SEXES:
            rep.other_sex_rows += 1
            continue
        mapped = AREA_ALIASES.get(area, area)
        if mapped not in xw:
            rep.unmapped_areas = {**(rep.unmapped_areas or {}), area: (rep.unmapped_areas or {}).get(area, 0) + 1}
            continue
        if wanted is not None and mapped not in wanted:
            continue
        if st not in OBS_STATUS:
            raise _fail(f"unreviewed ILOSTAT observation status {st!r}")
        if val != "" and not _DEC.match(val):
            raise _fail(f"non-numeric or negative ILOSTAT value {val[:30]!r}")
        if not re.fullmatch(r"\d{4}", time):
            raise _fail(f"non-annual ILOSTAT period {time!r}")
        if nc and nc not in NOTE_CLASSIF:
            rep.unknown_note_codes = {
                **(rep.unknown_note_codes or {}),
                nc: (rep.unknown_note_codes or {}).get(nc, 0) + 1,
            }
        rep.rows_kept += 1
        out.setdefault(mapped, []).append(
            {
                "dataset": INDICATOR_ID,
                "iso3": mapped,
                "ref_area": area,
                "source": src,
                "sex": SEXES[sex],
                "function": cl1,
                "year": int(time),
                "value": val if val != "" else None,
                "unit": "percent",
                "status": st,
                "status_label": OBS_STATUS[st],
                "note_classif": nc,
                "note_classif_label": NOTE_CLASSIF.get(nc) if nc else None,
            }
        )
    return out, rep


def rows_to_table(rows: Sequence[Mapping[str, Any]]) -> Any:
    import pyarrow as pa

    schema = pa.schema([(n, getattr(pa, t)()) for n, t in DATASET_COLUMNS])
    ordered = sorted(rows, key=lambda r: (r["year"], r["sex"], r["source"]))
    return pa.table({n: [r.get(n) for r in ordered] for n, _ in DATASET_COLUMNS}, schema=schema)


def build_tables(body: bytes, countries: Iterable[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    per, rep = parse_indicator(body, countries)
    tables: dict[str, Any] = {}
    dupes: dict[str, int] = {}
    over: dict[str, list[str]] = {}
    for iso3, rows in sorted(per.items()):
        keys = [(r["year"], r["sex"]) for r in rows]
        if len(keys) != len(set(keys)):  # the bulk file holds one source per area-year
            dupes[iso3] = len(keys) - len(set(keys))
            continue
        bad = [
            f"{r['year']} {r['sex']}: {r['value']}" for r in rows if r["value"] is not None and float(r["value"]) > 100
        ]
        if bad:  # a share above 100 % is never data
            over[iso3] = bad[:5]
            continue
        tables[iso3] = rows_to_table(rows)
    return tables, {
        "countries": len(tables),
        "duplicate_cells_refused": dupes,
        "values_above_100_refused": over,
        "parse": rep.as_dict(),
    }


class IlostatSocialProtectionConnector:
    """Reviewed built-in connector: ILOSTAT SDG 1.3.1 old-age pension coverage by sex."""

    def __init__(self, countries: Sequence[str] | None = None) -> None:
        self.countries = tuple(sorted(countries)) if countries is not None else None

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name=NAME,
            version=VERSION,
            template=TEMPLATE,  # type: ignore[arg-type]
            source_key=SOURCE_KEY,
            provider="International Labour Organization (ILOSTAT)",
            licence=LICENCE,
            allowed_hosts=HOSTS,
            dataset_ids=(INDICATOR_ID,),
            field_mappings={INDICATOR_ID: f"{CONCEPT_ID}@{CONCEPT_VERSION}"},
            units={INDICATOR_ID: "unit.percent"},
            geography=self.countries or tuple(sorted(crosswalk_iso3())),
            time_semantics=(
                "reference year of the ILO Social Security Inquiry estimate (persons above the statutory "
                "pensionable age receiving an old-age pension, as a share of that population)"
            ),
            lineage_family="ilostat",
            cadence_seconds=30 * 86400,
            release_aware=True,
            revision_strategy="new acquisition when the file bytes change (ILOSTAT revises series continuously)",
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(IlostatSocialProtectionConnector),
        )

    def all_items(self) -> list[FetchItem]:
        return [
            FetchItem(
                key=INDICATOR_ID,
                url=data_url(),
                expected_media_types=("application/octet-stream", "application/gzip", "application/x-gzip"),
                max_bytes=MAX_RESPONSE_BYTES,
                params={"kind": "ilostat_bulk_csv_gz", "indicator": INDICATOR_ID},
            )
        ]

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        pending = self.all_items()
        items = tuple(pending[: max(0, budget.max_requests)])
        return FetchPlan(
            connector=NAME,
            items=items,
            pagination="none: one bulk gzip CSV",
            availability_expectation="ILOSTAT updates series continuously",
            budget_exhausted=len(items) < len(pending),
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        ok = any(o.status in ("published", "unchanged") for o in outcomes)
        return Cursor(state={"complete": ok})

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        _tables, rep = build_tables(raw.body, self.countries)
        yield ObservationBatch(
            connector=NAME,
            connector_version=VERSION,
            item_key=raw.item.key,
            observations=(),
            metadata={
                "kind": "ilostat_bulk_csv_gz",
                "indicator": INDICATOR_ID,
                "function": FUNCTION,
                "countries": rep["countries"],
                "duplicate_cells_refused": rep["duplicate_cells_refused"],
                "values_above_100_refused": rep["values_above_100_refused"],
                "parse": rep["parse"],
                "dataset_input": {"schema": DATASET_SCHEMA, "concept": CONCEPT_ID},
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
        for key, code in (
            ("duplicate_cells_refused", "duplicate_cells"),
            ("values_above_100_refused", "share_above_100"),
        ):
            if md.get(key):
                f.append(
                    Finding(
                        code=code,
                        severity="warning",
                        message=f"countries left out ({key}): {sorted(md[key])}",
                    )
                )
        unknown = (md.get("parse") or {}).get("unknown_note_codes")
        if unknown:
            f.append(
                Finding(
                    code="unreviewed_note_codes",
                    severity="warning",
                    message=f"note codes kept without a reviewed label: {sorted(unknown)}",
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

    tables, rep = build_tables(raw.body, countries)
    out = []
    for iso3, table in sorted(tables.items()):
        rows = table.to_pylist()
        years = sorted({r["year"] for r in rows})
        out.append(
            register_dataset(
                conn,
                store,
                dataset_key=f"ilostat.{INDICATOR_ID.lower()}.pension",
                concept_id=CONCEPT_ID,
                subject_key=iso3,
                table=table,
                source_acquisition_ids=[acquisition_id],
                parser_version=PARSER_VERSION,
                origin=raw.origin,
                coverage={
                    "indicator": INDICATOR_ID,
                    "function": FUNCTION,
                    "years": [years[0], years[-1]] if years else [],
                    "sexes": sorted({r["sex"] for r in rows}),
                    "sources": sorted({r["source"] for r in rows}),
                    "status_codes": sorted({r["status"] for r in rows if r["status"]}),
                    "note_codes": sorted({r["note_classif"] for r in rows if r["note_classif"]}),
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
    """Register the per-country dataset inputs of the SDG 1.3.1 file (acquisition transaction)."""
    if summary.acquisition_id is None:
        raise _fail("post-publish hook without an acquisition id", kind="integrity")
    register_tables(conn, store, raw, str(summary.acquisition_id))


IlostatSocialProtectionConnector.post_publish = staticmethod(post_publish_datasets)  # type: ignore[attr-defined]
