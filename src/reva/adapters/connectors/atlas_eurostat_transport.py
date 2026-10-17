"""Eurostat transport statistics (rail, road, inland waterways, air, maritime; country and NUTS 2) -> atlas (V6INFRA).

Aggregated official statistics only (no vehicle- or shipment-level data). Same API, budget key, parser and rules as
:mod:`atlas_eurostat_macro` (JSON-stat 2.0, values verbatim, Eurostat status flags kept with their labels, cells
without a value not stored, aggregates such as ``EU27_2020`` kept unmapped); this module only declares the transport
queries and registers them as ``eurostat.transport_*`` tables.

==========================  ===================  ================================================================
dataset                     Eurostat dataset     series
==========================  ===================  ================================================================
transport_rail_goods        rail_go_total        goods transported by rail: thousand tonnes, million tkm; annual
transport_rail_pass         rail_pa_total        rail passengers: thousand passengers, million pkm; annual
transport_road_goods        road_go_ta_tott      road freight, total (all operations, all transport types):
                                                 thousand tonnes, million tkm, million vehicle-km; annual
transport_iww_goods         iww_go_atygo         inland waterways, total goods, all packaging: thousand tonnes,
                                                 million tkm; annual
transport_air_pass_m        avia_paoc            air passengers carried, all schedules, total / national /
                                                 international transport; monthly
transport_nuts2_sea_goods   tran_r_mago_nm       maritime freight loaded and unloaded by NUTS 2 region; annual
transport_nuts2_air_pass    tran_r_avpa_nm       air passengers carried by NUTS 2 region; annual
==========================  ===================  ================================================================

NUTS regions keep their code in ``provider_geo`` (``flags.nuts_level``, ``flags.country_iso3``), as in V4ECON's
regional tables (whose ``nuts:<code>`` KG entities are the regions). Licence: Eurostat reuse policy (Commission
Decision 2011/833/EU; CC BY 4.0), acknowledgement required.

    uv run reva atlas ingest atlas.eurostat_transport [--dataset transport_rail_goods]
"""

from __future__ import annotations

import sys
from typing import Any

from reva.adapters.connectors import atlas_eurostat_macro as em
from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.eurostat_transport"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_eurostat_transport/1"
_NUTS_FIX = ("FR", "FR10", "FRL0", "NL", "NL33", "BE21", "EU27_2020")

QUERIES: tuple[em.EsQuery, ...] = (
    em.EsQuery("transport_rail_goods", "rail_go_total", "Goods transported (rail)", "unit=THS_T&unit=MIO_TKM", "A",
               reference="calendar-year flow"),  # fmt: skip
    em.EsQuery("transport_rail_pass", "rail_pa_total", "Passengers transported (rail)", "unit=THS_PAS&unit=MIO_PKM", "A",
               reference="calendar-year flow"),  # fmt: skip
    em.EsQuery(
        "transport_road_goods",
        "road_go_ta_tott",
        "Road freight transport by type of operation and type of transport - annual data",
        "tra_type=TOTAL&tra_oper=TOTAL&unit=THS_T&unit=MIO_TKM&unit=MIO_VKM",
        "A",
        reference="calendar-year flow, vehicles registered in the reporting country",
    ),
    em.EsQuery(
        "transport_iww_goods",
        "iww_go_atygo",
        "Inland waterways transport by type of good",
        "tra_cov=TOTAL&nst07=TOTAL&typpack=TOTAL&unit=THS_T&unit=MIO_TKM",
        "A",
        reference="calendar-year flow on the reporting country's territory",
    ),
    em.EsQuery(
        "transport_air_pass_m",
        "avia_paoc",
        "Air passenger transport by type of schedule, transport coverage and country",
        "freq=M&unit=PAS&tra_meas=PAS_CRD&schedule=TOTAL&tra_cov=TOTAL&tra_cov=NAT&tra_cov=INTL",
        "M",
        reference="monthly flow, passengers carried",
        fixture_since="2025-10",
    ),
    em.EsQuery(
        "transport_nuts2_sea_goods",
        "tran_r_mago_nm",
        "Maritime transport of freight by NUTS 2 region",
        "tra_meas=FR_LD_NLD&tra_meas=FR_LD&tra_meas=FR_NLD&unit=THS_T",
        "A",
        regional=True,
        reference="calendar-year flow, ports of the region",
        fixture_geos=_NUTS_FIX,
        fixture_since="2023",
    ),
    em.EsQuery(
        "transport_nuts2_air_pass",
        "tran_r_avpa_nm",
        "Air transport of passengers by NUTS 2 region",
        "tra_meas=PAS_CRD&tra_meas=PAS_CRD_ARR&tra_meas=PAS_CRD_DEP&unit=THS_PAS",
        "A",
        regional=True,
        reference="calendar-year flow, airports of the region",
        fixture_geos=_NUTS_FIX,
        fixture_since="2023",
    ),
)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=em.SOURCE_KEY,
        budget_key=em.SOURCE_KEY,
        atlas_source=em.SOURCE,
        provider="Eurostat (statistics API, JSON-stat 2.0): transport statistics (rail, road, inland waterways, air, "
        "maritime), country and NUTS 2",
        licence=em.LICENCE,
        allowed_hosts=(em.HOST,),
        datasets=tuple(q.dataset for q in QUERIES),
        dataset_ids=tuple(q.code for q in QUERIES),
        cadence_seconds=30 * 86_400,
        planned_requests=len(QUERIES),
        max_response_bytes=em.MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(em, sys.modules[__name__]),
        field_mappings={"geo_scheme": em.GEO_SCHEME, "tables": {q.code: f"{em.SOURCE}.{q.dataset}" for q in QUERIES}},
        notes="Seven JSON-stat requests per full run; aggregated official transport statistics only.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": [], "reports": {}}
    for q in QUERIES:
        if not run.wants(q.dataset):
            continue
        try:
            raw = await run.get(q.url(fixture), key=f"data:{q.code}", media_types=("application/json",),
                                max_bytes=em.MAX_BYTES)  # fmt: skip
            if not raw.body:
                raise SourceResponseError(f"Eurostat {q.code}: empty response body", retryable=True)
        except BudgetExhausted:
            raise
        except Exception as e:
            out["errors"].append({"dataset": q.dataset, "error": type(e).__name__, "message": str(e)[:300]})
            continue
        sha = run.archive(raw)
        out["fetched"].append({"item": f"data:{q.code}", "bytes": len(raw.body), "sha256": sha})
        try:
            frame, rep, _ = em.parse_jsonstat(q, raw.body, sha)
            reg = run.register(dataset=q.dataset, frame=frame, raw_sha256s=[sha], coverage_note=em.coverage_note(q, rep),
                               geo_scheme=None if q.regional else em.GEO_SCHEME, rights=dict(em.RIGHTS))  # fmt: skip
        except BudgetExhausted:
            raise
        except Exception as e:
            out["errors"].append({"dataset": q.dataset, "stage": "parse/register", "error": type(e).__name__,
                                  "message": str(e)[:300]})  # fmt: skip
            continue
        out["tables"].append({"table": reg["dataset_key"].removeprefix("atlas."), "rows": reg["row_count"],
                              "created": reg["created"], "artifact_sha256": reg["artifact_sha256"]})  # fmt: skip
        out["reports"][q.dataset] = rep.as_dict()
    out["ok"] = not out["errors"]
    return out
