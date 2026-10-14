"""handle_replay end to end with a claimed ops.job: evaluation context, manifest
artifact in the evaluation namespace, fenced completion in the same transaction."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.belief.publication import publish_release
from reva.evaluation.replay import handle_replay
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import CapabilityGap
from reva.operations.context import JobContext

pytestmark = pytest.mark.db

POP = "demography.population.wdi_midyear_total"
VINTAGES = Path(__file__).parents[1] / "fixtures" / "vintages"
WORKER = "test-worker-h1"


def _loader():
    if "vintage_loader" not in sys.modules:
        spec = importlib.util.spec_from_file_location("vintage_loader", VINTAGES / "vintage_loader.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["vintage_loader"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["vintage_loader"]


vl = _loader()


def _now(engine) -> datetime:
    with engine.connect() as c:
        return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def _claimed_job(db, payload: dict, *, lease: str = "10 minutes") -> JobContext:
    with db.api.begin() as c:
        jid = c.execute(text("SELECT ops.submit_job('replay', CAST(:p AS jsonb), :k, 0, 'test:h1')"),
                        {"p": json.dumps(payload), "k": f"replay-test-{uuid.uuid4().hex}"}).scalar_one()
    with db.owner.begin() as c:  # claim manually, as the worker's claim would
        token = c.execute(text(
            """UPDATE ops.job SET status = 'running', worker_id = :w, attempts = attempts + 1,
                 lease_until = clock_timestamp() + CAST(:l AS interval),
                 fencing_token = nextval('ops.fencing_seq'), updated_at = clock_timestamp()
               WHERE id = :j RETURNING fencing_token"""), {"w": WORKER, "l": lease, "j": jid}).scalar_one()
    return JobContext(job_id=jid, job_type="replay", worker_id=WORKER, fencing_token=int(token), attempt=1,
                      settings=db.settings, worker_engine=db.worker, sim_engine=db.sim, store=db.store)


def _job(db, jid) -> dict:
    with db.owner.connect() as c:
        return dict(c.execute(text("SELECT * FROM ops.job WHERE id = :j"), {"j": jid}).mappings().one())


async def test_operational_replay_job_writes_evaluation_context_and_manifest(db):
    await vl.ingest_vintage(db.worker, db.store, "pop_fra_1960_1961_job", "v1")
    rel = publish_release(db.worker, db.store, cutoff=_now(db.worker), origin=Origin.test_fixture,
                          environment="test", concepts=[POP])
    cutoff = _now(db.worker)
    payload = {"mode": "operational_as_known", "cutoff": cutoff.isoformat(), "origin": "test_fixture",
               "concepts": [POP]}
    ctx = _claimed_job(db, payload)
    result = handle_replay(payload, ctx)

    assert ctx.completed_in_handler
    job = _job(db, ctx.job_id)
    assert job["status"] == "succeeded" and job["result_ref"] == json.loads(json.dumps(result))
    assert result["status"] == "ok" and result["label"] == "operational_as_known"
    with db.api.connect() as c:
        context = c.execute(text("SELECT * FROM meta.context WHERE id = :i"),
                            {"i": result["context_id"]}).mappings().one()
        release_ctx = c.execute(text("SELECT context_id FROM belief.release WHERE id = :r"),
                                {"r": rel.release_id}).scalar_one()
        loc = c.execute(text("SELECT namespace FROM meta.artifact_location WHERE artifact_sha256 = :s"),
                        {"s": result["manifest_sha256"]}).scalars().all()
        act = c.execute(text(
            """SELECT a.kind, a.origin FROM meta.activity a JOIN meta.activity_output o ON o.activity_id = a.id
               WHERE o.artifact_sha256 = :s"""), {"s": result["manifest_sha256"]}).mappings().one()
    assert context["mode"] == "evaluation" and context["origin"] == "test_fixture"
    assert context["knowledge_cutoff"] == cutoff and context["manifest_sha256"] == result["manifest_sha256"]
    assert context["parent_context_id"] == release_ctx
    assert loc == ["evaluation"] and dict(act) == {"kind": "evaluation.replay", "origin": "evaluation"}
    with db.worker.connect() as c:
        events = c.execute(text("SELECT count(*) FROM ops.outbox WHERE event_type = 'evaluation.replay_completed' "
                                "AND payload->>'job_id' = :j"), {"j": str(ctx.job_id)}).scalar_one()
    assert events == 1

    manifest = json.loads(db.store.get("evaluation", result["manifest_sha256"]))
    assert manifest["mode"] == "operational_as_known" and manifest["job_id"] == str(ctx.job_id)
    assert manifest["release"]["release_id"] == str(rel.release_id)
    assert manifest["manifest_verified"] is True
    keys = {i["logical_key"]: i for i in manifest["inputs"]}
    assert {"wb:SP.POP.TOTL:FRA:1960", "wb:SP.POP.TOTL:FRA:1961"} <= set(keys)
    for k in ("wb:SP.POP.TOTL:FRA:1960", "wb:SP.POP.TOTL:FRA:1961"):
        proof = keys[k]["availability_proof"]
        assert proof["kind"] == "reva_first_commit"  # source availability unknown: REVA's own commit clock
        assert proof["recorded_at"] <= cutoff.isoformat() and keys[k]["source_published_at"] is None


async def test_retrospective_job_result_carries_label_and_disclaimer(db):
    c = builtin_connectors()["worldbank.wdi.indicators"]
    await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)
    payload = {"mode": "revised_data_retrospective", "cutoff": _now(db.worker).isoformat(),
               "origin": "test_fixture", "concepts": [POP], "options": {"min_train": 5, "horizons": [1]}}
    ctx = _claimed_job(db, payload)
    result = handle_replay(payload, ctx)
    assert result["label"] == "revised_data_retrospective"
    assert result["disclaimer"] == "not a real-time backtest; forecasts were not issued at these origins"
    assert "not a real-time backtest" in result["honesty_statement"]
    job = _job(db, ctx.job_id)
    assert job["status"] == "succeeded" and job["result_ref"]["label"] == "revised_data_retrospective"
    manifest = json.loads(db.store.get("evaluation", result["manifest_sha256"]))
    assert manifest["label"] == "revised_data_retrospective" and manifest["real_time_backtest"] is False


def test_no_release_at_cutoff_is_a_typed_result(db):
    payload = {"mode": "operational_as_known", "cutoff": "2001-01-01T00:00:00+00:00", "origin": "test_fixture"}
    ctx = _claimed_job(db, payload)
    result = handle_replay(payload, ctx)
    assert result["status"] == "no_release_at_cutoff" and result["outputs"] == 0
    assert _job(db, ctx.job_id)["result_ref"]["status"] == "no_release_at_cutoff"


def test_unknown_mode_is_a_capability_gap_and_writes_nothing(db):
    payload = {"mode": "strict_then_retrospective", "cutoff": datetime(2020, 1, 1, tzinfo=UTC).isoformat()}
    ctx = _claimed_job(db, payload)
    with pytest.raises(CapabilityGap):
        handle_replay(payload, ctx)
    job = _job(db, ctx.job_id)
    assert job["status"] == "running" and job["result_ref"] is None and not ctx.completed_in_handler


def test_stale_lease_cannot_publish_a_replay(db):
    payload = {"mode": "operational_as_known", "cutoff": "2001-01-01T00:00:00+00:00", "origin": "test_fixture"}
    ctx = _claimed_job(db, payload, lease="-1 second")
    with db.api.connect() as c:
        before = c.execute(text("SELECT count(*) FROM meta.context WHERE mode = 'evaluation'")).scalar_one()
    with pytest.raises(sa_exc.DBAPIError):
        handle_replay(payload, ctx)
    with db.api.connect() as c:
        after = c.execute(text("SELECT count(*) FROM meta.context WHERE mode = 'evaluation'")).scalar_one()
    assert after == before and _job(db, ctx.job_id)["result_ref"] is None
