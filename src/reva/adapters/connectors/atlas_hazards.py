"""INFORM Risk Index — natural hazard & exposure scores per country (V3, agent A-GEO).

Source: INFORM (European Commission Joint Research Centre with the INFORM partnership), JSON API
``https://drmkc.jrc.ec.europa.eu/inform-index/API/InformAPI``. INFORM states it is "open-source" and "open,
also for integration in other applications" and publishes this API for programmers; no dataset-specific
licence is shown, so the European Commission reuse policy (Decision 2011/833/EU, CC BY 4.0 by default for
Commission content) is recorded and flagged for maintainer confirmation.

Two requests per run:

1. ``workflows/GetByWorkflowGroup/<EDITION_GROUP>`` — the edition's workflows; the main one is the workflow
   whose ``Name`` is exactly ``"INFORM Risk <year>"`` (the ``"- <year>"`` variants are back-calculated
   trend series). Its id and ``FlagDataSaved`` date are the source revision.
2. ``countries/Scores/?WorkflowId=<id>&IndicatorId=<ids>`` — scores (0-10, higher = more exposed) of the
   natural-hazard dimension: ``HA.NAT`` (natural hazard & exposure, composite) and its components
   earthquake ``HA.NAT.EQ``, river flood ``HA.NAT.FL``, coastal flood ``HA.NAT.CFL``, tropical cyclone
   ``HA.NAT.TC``, drought ``HA.NAT.DR``, tsunami ``HA.NAT.TS`` and epidemic ``HA.NAT.EPI`` (INFORM places
   epidemics in the natural-hazard dimension).

These are *modelled exposure indices*, not observations: each row says so. Period: the edition year
(``[2026-01-01, 2027-01-01)`` for INFORM Risk 2026), flagged ``period_basis = edition year``; the
underlying input data are mostly of earlier years.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from reva.adapters.connectors.atlas_geo_common import atlas_spec, fetch_raw
from reva.adapters.connectors.base import dec_str, json_decimal, module_digest, schema_fingerprint
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

CONNECTOR_NAME = "atlas.inform_risk_hazards"
CONNECTOR_VERSION = "1.0.0"
SOURCE_KEY = "jrc.inform_risk"
PARSER_VERSION = "atlas-inform-hazards/1"
ATLAS_SOURCE = "inform"
HOST = "drmkc.jrc.ec.europa.eu"
API = f"https://{HOST}/inform-index/API/InformAPI/"
EDITION_YEAR = 2026
EDITION_GROUP = f"INFORM{EDITION_YEAR}"
EDITION_NAME = f"INFORM Risk {EDITION_YEAR}"
ATLAS_DATASET = f"risk_{EDITION_YEAR}_natural_hazards"
INDICATORS = {
    "HA.NAT": "natural hazard & exposure (composite)",
    "HA.NAT.EQ": "earthquake",
    "HA.NAT.FL": "river flood",
    "HA.NAT.CFL": "coastal flood",
    "HA.NAT.TC": "tropical cyclone",
    "HA.NAT.DR": "drought",
    "HA.NAT.TS": "tsunami",
    "HA.NAT.EPI": "epidemic",
}
MAX_BYTES = 3_000_000
MIN_COUNTRIES = 180

RIGHTS = {
    "licence": "European Commission reuse policy (Decision 2011/833/EU; CC BY 4.0) - to be confirmed by a maintainer",
    "licence_url": "https://eur-lex.europa.eu/eli/dec/2011/833/oj",
    "attribution": "INFORM Risk Index, European Commission Joint Research Centre / INFORM partnership",
    "provider_statement": "INFORM is open-source; open, also for integration in other applications (INFORM About page)",
}


class InformError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> InformError:
    return InformError(msg, retryable=False, kind=kind)


def workflows_item() -> FetchItem:
    return FetchItem(key="workflows", url=f"{API}workflows/GetByWorkflowGroup/{EDITION_GROUP}", max_bytes=200_000)


def scores_item(workflow_id: int) -> FetchItem:
    ids = ",".join(INDICATORS)
    return FetchItem(
        key="scores",
        url=f"{API}countries/Scores/?WorkflowId={int(workflow_id)}&IndicatorId={ids}",
        max_bytes=MAX_BYTES,
        params={"workflow_id": int(workflow_id)},
    )


def parse_workflows(body: bytes) -> dict[str, Any]:
    """The main workflow of the edition: ``{"id", "name", "date", "data_saved"}``."""
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _err("workflow list is not JSON") from e
    if not isinstance(doc, list):
        raise _err("workflow list is not an array")
    main = [w for w in doc if isinstance(w, dict) and w.get("Name") == EDITION_NAME]
    if len(main) != 1:
        raise _err(f"expected exactly one workflow named {EDITION_NAME!r}, found {len(main)}", kind="incomplete")
    w = main[0]
    if not isinstance(w.get("WorkflowId"), int):
        raise _err("WorkflowId is not an integer")
    return {
        "id": w["WorkflowId"],
        "name": w["Name"],
        "date": w.get("WorkflowDate"),
        "data_saved": w.get("FlagDataSaved"),
    }


def parse_scores(body: bytes) -> list[tuple[int, str, str, Decimal]]:
    """``[(record index, iso3, indicator, score)]``; unknown indicators/out-of-range scores fail."""
    try:
        doc = json_decimal(body)
    except ValueError as e:
        raise _err("scores are not JSON") from e
    if not isinstance(doc, list):
        raise _err("scores are not an array")
    out = []
    for i, r in enumerate(doc):
        if not isinstance(r, dict):
            raise _err(f"record {i} is not an object")
        iso, ind, sc = r.get("Iso3"), r.get("IndicatorId"), r.get("IndicatorScore")
        if ind not in INDICATORS:
            raise _err(f"record {i}: unexpected indicator {ind!r}")
        if not isinstance(iso, str) or len(iso) != 3:
            raise _err(f"record {i}: bad Iso3 {iso!r}")
        if sc is None:
            continue  # not scored: omitted, never filled
        if not isinstance(sc, Decimal) or not (Decimal(0) <= sc <= Decimal(10)):
            raise _err(f"record {i}: score {sc!r} outside 0..10")
        out.append((i, iso, ind, sc))
    return out


def atlas_rows(
    scores: Sequence[tuple[int, str, str, Decimal]], workflow: dict[str, Any], raw_sha256: str
) -> list[dict[str, Any]]:
    start, end = date(EDITION_YEAR, 1, 1), date(EDITION_YEAR + 1, 1, 1)
    rows = []
    for i, iso, ind, sc in scores:
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": ATLAS_DATASET,
                "indicator": ind,
                "concept_id": None,
                "iso3": None,
                "provider_geo": iso,
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": dec_str(sc),
                "value_text": None,
                "unit": "index (0-10)",
                "flags": json.dumps(
                    {
                        "edition": workflow["name"],
                        "workflow_id": workflow["id"],
                        "workflow_data_saved": workflow["data_saved"],
                        "hazard": INDICATORS[ind],
                        "nature": "modelled exposure index (higher = more exposed), not an observation",
                        "period_basis": "edition year; underlying inputs are mostly from earlier years",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "raw_sha256": raw_sha256,
                "selector": json.dumps(
                    {"item": "scores", "record": i, "Iso3": iso, "IndicatorId": ind},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    return rows


class InformHazardsConnector:
    def describe(self) -> ConnectorSpec:
        return atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="European Commission JRC - INFORM",
            licence="EC reuse policy (CC BY 4.0) - to confirm",
            allowed_hosts=(HOST,),
            dataset_ids=(ATLAS_DATASET,),
            field_mappings={"geo": "Iso3", "indicator": "IndicatorId", "value": "IndicatorScore"},
            units={"*": "index (0-10)"},
            geography=("world",),
            time_semantics="annual edition (INFORM Risk <year>)",
            lineage_family="inform",
            cadence_seconds=90 * 86400,
            revision_strategy="edition workflow id + FlagDataSaved is the revision",
            max_response_bytes=MAX_BYTES,
            max_pages=2,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(InformHazardsConnector),
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        return FetchPlan(
            connector=CONNECTOR_NAME,
            items=(workflows_item(),),
            pagination="workflow list, then one scores request (follow-up)",
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_raw(item, http)

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        if raw.item.key == "workflows":
            wf = parse_workflows(raw.body)
            yield ObservationBatch(
                connector=CONNECTOR_NAME,
                connector_version=CONNECTOR_VERSION,
                item_key=raw.item.key,
                observations=(),
                metadata={"workflow": wf},
                follow_up=(scores_item(wf["id"]),),
            )
            return
        sc = parse_scores(raw.body)
        per = {ind: len({s[1] for s in sc if s[2] == ind}) for ind in INDICATORS}
        yield ObservationBatch(
            connector=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION,
            item_key=raw.item.key,
            observations=(),
            metadata={"countries_per_indicator": per},
            schema_fingerprint=schema_fingerprint(sorted(INDICATORS)),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        per = batch.metadata.get("countries_per_indicator")
        if per is None:
            return QualityReport(outcome="pass")
        low = {k: n for k, n in per.items() if n < MIN_COUNTRIES}
        findings = tuple(
            Finding(code="incomplete", severity="blocker", message=f"{k}: {n} countries") for k, n in low.items()
        )
        return QualityReport(
            outcome="blocked" if findings else "pass", findings=findings, received_count=min(per.values(), default=0)
        )

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes)})


async def ingest(engine: Any, store: Any, http: Any) -> dict[str, Any]:
    import polars as pl

    from reva.adapters.connectors.atlas_geo_common import archive_raw
    from reva.atlas.tables import ATLAS_SCHEMA, register_atlas_table

    conn = InformHazardsConnector()
    spec = conn.describe()
    wraw = await conn.fetch(workflows_item(), http)
    wsha = archive_raw(engine, store, spec, wraw, actor="worker:atlas_a_geo")
    wf = parse_workflows(wraw.body)
    sraw = await conn.fetch(scores_item(wf["id"]), http)
    ssha = archive_raw(engine, store, spec, sraw, actor="worker:atlas_a_geo")
    for b in conn.parse(sraw):
        q = conn.validate(b)
        if q.blocking:
            raise _err("; ".join(f.message for f in q.findings), kind="incomplete")
    frame = pl.DataFrame(atlas_rows(parse_scores(sraw.body), wf, ssha), schema=ATLAS_SCHEMA)
    table = register_atlas_table(
        engine,
        store,
        source=ATLAS_SOURCE,
        dataset=ATLAS_DATASET,
        frame=frame,
        raw_sha256s=[ssha],
        parser_version=PARSER_VERSION,
        origin=sraw.origin,
        geo_scheme="iso3",
        rights=RIGHTS,
        actor="worker:atlas_a_geo",
        coverage_note=f"{wf['name']} (workflow {wf['id']}, data saved {wf['data_saved']}): natural hazard & "
        "exposure scores 0-10 (composite + 7 components); modelled indices, not observations",
    )
    return {
        "connector": CONNECTOR_NAME,
        "workflow": wf,
        "raw_sha256s": {"workflows": wsha, "scores": ssha},
        "table": table,
    }
