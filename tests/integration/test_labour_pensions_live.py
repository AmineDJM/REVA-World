"""LIVE: eurostat.labour_pensions (+ POP demography for FR) -> dataset inputs -> live release ->
social.retirement_age run executed by the real Worker, in a throwaway database. Deselected by
default (``-m live``).

Egress is reserved and metered against ``registry/policies/budgets.yaml`` through the budget
ledger of the throwaway database (invariant 30), like the ingest job. ``REVA_PEN_LIVE_OUT``
(optional): a directory where the ingest report and run summary are written for the docs.
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
from reva.adapters.connectors import eurostat_labour as lab
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import make_live_client, run_connector
from reva.kernel.context import Origin
from reva.kernel.time import utc_now
from reva.models.social import retirement_age as ra
from reva.operations.budgets import budget_source_key, metered, reserve_ingest, settle_metered
from reva.ports.connectors import FetchBudget
from reva.settings import Settings
from reva.simulation.spec import validate_scenario_spec

pytestmark = [pytest.mark.live, pytest.mark.db]


def _live_ingest(
    connector: Any, settings: Settings, store: Any, worker: Any, post_publish: Any, planned: int
) -> tuple[Any, Any]:
    spec_ = connector.describe()
    reservation = reserve_ingest(
        worker, job_id=uuid.uuid4(), attempt=1, source_key=budget_source_key(spec_), planned_requests=planned
    )
    http = metered(make_live_client(spec_, settings), worker, reservation)

    async def go() -> Any:
        try:
            return await run_connector(
                connector,
                http,
                store,
                worker,
                persist_cursor=False,
                budget=FetchBudget(max_requests=planned),
                post_publish=post_publish,
            )
        finally:
            await http.aclose()

    report = asyncio.run(go())
    return report, settle_metered(worker, http)


def test_live_labour_pensions_ingest_and_worker_run(tmp_path: Path) -> None:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import seed
    from reva.operations.worker import Worker

    out_dir = Path(os.environ["REVA_PEN_LIVE_OUT"]) if os.environ.get("REVA_PEN_LIVE_OUT") else None
    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    with create_engine(base.migration_url().set(database="postgres")).connect() as c:
        c.execute(text("SELECT 1"))
    root = tmp_path / "objects"
    with throwaway_database(base, prefix="reva_live_pen") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        try:
            seed(engines["migration"], store)
            with engines["migration"].begin() as c:
                c.execute(text("UPDATE ops.schedule SET enabled = false"))
            labc = lab.EurostatLabourPensionsConnector()
            report, budget = _live_ingest(
                labc, settings, store, engines["worker"], lab.post_publish_datasets, len(lab.TABLES)
            )
            assert report.errors == [], report.errors
            assert {o.status for o in report.outcomes} == {"published"}, [o.model_dump() for o in report.outcomes]
            assert budget is not None and budget["consumed_requests"] == len(lab.TABLES)
            demo = ed.EurostatDemographyConnector(countries=("FR",), include_europop=False)
            dreport, _ = _live_ingest(
                demo, settings, store, engines["worker"], ed.post_publish_datasets, len(demo.all_items())
            )
            assert dreport.errors == [], dreport.errors
            wb = builtin_connectors()["worldbank.wdi.indicators"]  # one selected estimate; same (live) origin
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
                "title": "Live: France minimum pension age, law in force vs 2010 law",
                "baseline_release_id": str(rel.release_id),
                "knowledge_cutoff": rel.knowledge_cutoff.isoformat(),
                "model_key": ra.MODEL_KEY,
                "model_version": ra.MODEL_VERSION,
                "countries": ["FRA"],
                "reference_year": 2024,
                "horizon": {"periods": 16, "unit": "calendar_year"},
                "intervention": {
                    "kind": "retirement_age_rule_path",
                    "scenario_path": "law_in_force_2026",
                    "reference_path": "law_2010_as_amended_2011",
                },
                "assumptions": {"observed_path": "law_in_force_2026"},
                "fidelity": "L2",
                "missing_input_policy": "fail",
                "outputs": list(ra.DEFAULT_OUTPUTS),
                "seed": 0,
            }
            s = validate_scenario_spec(doc)
            with engines["api"].begin() as conn:
                sid = conn.execute(
                    text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:test')"),
                    {"spec": json.dumps(s.as_json()), "b": s.canonical_bytes(), "d": s.digest()},
                ).scalar_one()
            with engines["migration"].begin() as conn:
                jid = conn.execute(
                    text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                                           VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
                    {"p": json.dumps({"scenario_id": str(sid), "fidelity": "L2"}), "k": f"live-pen-{sid}"},
                ).scalar_one()
            w = Worker(
                settings,
                engines["worker"],
                store,
                engines["sim"],
                lease_seconds=600,
                heartbeat_seconds=5,
                poll_interval=0.05,
            )
            job = w.claim_one()
            assert job is not None and str(job.id) == str(jid)
            outcome = w.run_job(job)  # foreground execution with background heartbeats; closes the worker
            with engines["migration"].connect() as c:
                jrow = dict(
                    c.execute(text("SELECT status, result_ref, last_error FROM ops.job WHERE id=:j"), {"j": jid})
                    .mappings()
                    .one()
                )
                rows = [
                    dict(r)
                    for r in c.execute(
                        text(
                            """SELECT output_key, variant, value, period_start FROM sim.result
                       WHERE run_id = CAST(:r AS uuid)"""
                        ),
                        {"r": (jrow["result_ref"] or {}).get("run_id")},
                    ).mappings()
                ]
            assert jrow["status"] == "succeeded", (outcome, jrow)
            assert rows
            chg = {
                r["period_start"].year: Decimal(r["value"]["value"])
                for r in rows
                if r["output_key"] == "pension_recipients_change"
                and r["variant"].startswith("L2:")
                and r["value"]["kind"] == "decimal"
            }
            assert chg and all(v <= 0 for v in chg.values())
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                summary = {
                    "release_id": str(rel.release_id),
                    "job": {k: jrow[k] for k in ("status", "result_ref")},
                    "ingest": report.as_dict()["outcomes"],
                    "budget": budget,
                    "rows": len(rows),
                    "pension_recipients_change_by_year": {str(y): str(v) for y, v in sorted(chg.items())},
                }
                (out_dir / "pen_live_summary.json").write_text(json.dumps(summary, indent=1, default=str))
        finally:
            for e in engines.values():
                e.dispose()
