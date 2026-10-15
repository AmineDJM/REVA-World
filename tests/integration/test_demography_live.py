"""LIVE: Eurostat demography ingest -> dataset inputs -> release -> cohort projection (job path)
-> stored back-test evaluation, in a throwaway database. Deselected by default (``-m live``).

Countries: ``REVA_POP_LIVE_COUNTRIES`` (Eurostat geo codes, default FR,DE,IT,ES,PL). The
egress is reserved and metered against ``registry/policies/budgets.yaml`` through the
budget ledger of the throwaway database (invariant 30), exactly as the ingest job does.
``REVA_POP_LIVE_OUT`` (optional): a directory where the evaluation manifest and the run
summaries are written for the documentation.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from reva.adapters.connectors import eurostat_demography as ed
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import make_live_client, run_connector
from reva.kernel.context import Origin
from reva.kernel.time import utc_now
from reva.models.population import cohort as C
from reva.operations.budgets import budget_source_key, metered, reserve_ingest, settle_metered
from reva.operations.context import JobContext
from reva.ports.connectors import FetchBudget
from reva.settings import Settings
from reva.simulation.jobs import handle_simulation
from reva.simulation.spec import validate_scenario_spec

pytestmark = [pytest.mark.live, pytest.mark.db]


def test_live_ingest_projection_and_backtest(tmp_path: Path) -> None:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import seed
    from reva.evaluation.population_backtest import run_population_evaluation

    geos = [g.strip() for g in os.environ.get("REVA_POP_LIVE_COUNTRIES", "FR,DE,IT,ES,PL").split(",") if g.strip()]
    iso3s = [ed.COUNTRIES[g] for g in geos]
    out_dir = Path(os.environ["REVA_POP_LIVE_OUT"]) if os.environ.get("REVA_POP_LIVE_OUT") else None
    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    with create_engine(base.migration_url().set(database="postgres")).connect() as c:
        c.execute(text("SELECT 1"))
    root = tmp_path / "objects"
    with throwaway_database(base, prefix="reva_live_demog") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        try:
            seed(engines["migration"], store)
            connector = ed.EurostatDemographyConnector(countries=geos, window_years=ed.WINDOW_YEARS)
            spec_ = connector.describe()
            planned = len(connector.all_items())
            reservation = reserve_ingest(
                engines["worker"],
                job_id=uuid.uuid4(),
                attempt=1,
                source_key=budget_source_key(spec_),
                planned_requests=planned,
            )
            http = metered(make_live_client(spec_, settings), engines["worker"], reservation)

            async def ingest() -> Any:
                try:
                    return await run_connector(
                        connector,
                        http,
                        store,
                        engines["worker"],
                        persist_cursor=False,
                        budget=FetchBudget(max_requests=planned),
                        post_publish=ed.post_publish_datasets,
                    )
                finally:
                    await http.aclose()

            report = asyncio.run(ingest())
            budget = settle_metered(engines["worker"], http)
            assert report.errors == [], report.errors
            assert budget is not None and budget["consumed_requests"] == planned
            wb = builtin_connectors()["worldbank.wdi.indicators"]  # a release needs one selected estimate;
            # the live release must not mix origins, so the WB values are fetched live too
            wb_http = make_live_client(wb.describe(), settings)

            async def ingest_wb() -> Any:
                try:
                    return await run_connector(wb, wb_http, store, engines["worker"], persist_cursor=False)
                finally:
                    await wb_http.aclose()

            asyncio.run(ingest_wb())
            rel = publish_release(engines["worker"], store, cutoff=utc_now(), origin=Origin.live, environment="test")
            doc = {
                "schema_version": "2.0.0",
                "title": "Live: cohort-component projection 2025-2050",
                "baseline_release_id": str(rel.release_id),
                "knowledge_cutoff": rel.knowledge_cutoff.isoformat(),
                "model_key": "demography.cohort_component",
                "model_version": "1.0.0",
                "countries": iso3s,
                "reference_year": 2025,
                "horizon": {"periods": 25, "unit": "calendar_year"},
                "intervention": {"kind": "baseline_projection"},
                "assumptions": {
                    "mortality": "constant_base_period_rates",
                    "fertility": "constant_base_period_rates",
                    "migration": "average_residual",
                    "base_period": {"first_year": 2019, "last_year": 2023},
                    "sensitivity": {
                        "migration": ["zero", "average_residual", "europop2023_bsl"],
                        "fertility": ["constant_base_period_rates", "europop2023_bsl"],
                    },
                },
                "fidelity": "L2",
                "missing_input_policy": "fail",
                "outputs": [
                    "population.total.all_ages",
                    "population.total.age_65_plus",
                    "old_age_dependency_ratio_65plus_per_15_64",
                    "old_age_dependency_ratio_67plus_per_20_66",
                    "births.total",
                    "deaths.total",
                    "net_migration.total",
                ],
                "seed": 0,
            }
            s = validate_scenario_spec(doc)
            with engines["api"].begin() as conn:
                sid = conn.execute(
                    text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:test')"),
                    {"spec": json.dumps(s.as_json()), "b": s.canonical_bytes(), "d": s.digest()},
                ).scalar_one()
            payload = {"scenario_id": str(sid), "fidelity": "L2"}
            with engines["migration"].begin() as conn:
                jid = conn.execute(
                    text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                                           VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
                    {"p": json.dumps(payload), "k": f"live-demog-{sid}"},
                ).scalar_one()
                token = conn.execute(
                    text("""UPDATE ops.job SET status='running', worker_id='t', attempts=1,
                                             lease_until=now()+interval '30 minutes',
                                             fencing_token=nextval('ops.fencing_seq') WHERE id=:j
                                             RETURNING fencing_token"""),
                    {"j": jid},
                ).scalar_one()

            def hb(_p: Any = None) -> str:
                with engines["migration"].connect() as c:
                    return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

            ctx = JobContext(
                job_id=jid,
                job_type="simulation",
                worker_id="t",
                fencing_token=int(token),
                attempt=1,
                settings=settings,
                worker_engine=engines["worker"],
                sim_engine=engines["sim"],
                store=store,
                heartbeat=hb,
            )
            ref = handle_simulation(payload, ctx)
            assert ref["status"] == "succeeded", ref
            with engines["migration"].connect() as c:
                rows = [
                    dict(r)
                    for r in c.execute(
                        text(
                            """SELECT r.output_key, r.variant, r.value, r.period_start, r.applicability->>'country' AS iso3
                       FROM sim.result r WHERE r.run_id=:r"""
                        ),
                        {"r": ref["run_id"]},
                    ).mappings()
                ]
            assert rows and all(r["period_start"] is not None for r in rows)
            summary = {r["iso3"]: {} for r in rows}
            for r in rows:
                if r["period_start"].year in (2025, 2050) and r["output_key"] in (
                    "population.total.all_ages",
                    "old_age_dependency_ratio_65plus_per_15_64",
                ):
                    v = r["value"]
                    summary[r["iso3"]][f"{r['output_key']}|{r['period_start'].year}|{r['variant']}"] = (
                        v.get("value") if v["kind"] == "decimal" else [v["lower"], v["upper"]]
                    )
            ev = run_population_evaluation(engines["worker"], store, rel.release_id, iso3s)
            for iso3 in iso3s:
                prim = ev["manifest"]["countries"][iso3]["backtests"]["primary_leak_free"]
                assert (
                    Decimal(prim["variants"]["mig=average_residual;fert=constant_base_period_rates"]["total_ape"]) >= 0
                )
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "evaluation_manifest.json").write_text(json.dumps(ev["manifest"], indent=1, default=str))
                (out_dir / "run_summary.json").write_text(
                    json.dumps(
                        {
                            "run": ref,
                            "release_id": str(rel.release_id),
                            "ingest": report.as_dict(),
                            "budget": budget,
                            "label": C.LABEL,
                            "results": summary,
                            "evaluation_manifest_sha256": ev["evaluation_manifest_sha256"],
                            "evaluation_ids": ev["evaluation_ids"],
                        },
                        indent=1,
                        default=str,
                    )
                )
        finally:
            for e in engines.values():
                e.dispose()
