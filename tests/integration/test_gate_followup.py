"""GATE2: ``gate_followup`` promotes a connector proposal whose reva_gate decision arrived late.

Discovery's gate wait ends in ``gate_pending`` when the gate process is down; the request stays
queued and the gate decides it later. The scheduled ``gate_followup`` job then promotes from the
stored proposal (no provider data is fetched): idempotently, fenced by the job lease, and blocked
by the kill switch. Offline: captured Eurostat fixtures.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from test_gate_separation import CONCEPT, _job_ctx, _t1_proposal

pytestmark = pytest.mark.db

CONNECTOR = "eurostat.demo_pjan.fr"


@pytest.fixture(autouse=True)
def _offline_sandbox_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.autonomy import sandbox as _sb

    monkeypatch.setattr(_sb, "PUBLIC_PROBE_ADDR", ("127.0.0.1", 1))


def _ctx(db: Any, **settings: Any) -> Any:
    ctx = _job_ctx(db)
    ctx.settings = db.settings.model_copy(update={"reva_gate_wait_seconds": 0.2, **settings})
    return ctx


def _mine(out: dict[str, Any], gid: Any) -> list[dict[str, Any]]:
    return [r for r in out.get("results", []) if r["gate_result_id"] == str(gid)]


def _count(db: Any, sql: str, **params: Any) -> int:
    with db.owner.connect() as c:
        return int(c.execute(text(sql), params).scalar_one())


def test_gate_followup_is_registered_and_scheduled(db: Any) -> None:
    from reva.cli.seed import DEFAULT_SCHEDULES
    from reva.operations.jobs import HANDLERS, resolve_handler
    from reva.operations.schedules import SCHEDULE_JOB_TYPES

    assert HANDLERS["gate_followup"] == "reva.autonomy.followup:handle_gate_followup"
    assert callable(resolve_handler("gate_followup"))
    assert "gate_followup" in SCHEDULE_JOB_TYPES
    sched = {k: (t, secs) for k, t, _p, secs in DEFAULT_SCHEDULES}
    assert sched["gate_followup:promotion"] == ("gate_followup", 300)
    with db.owner.connect() as c:
        row = c.execute(text("SELECT job_type, cadence_spec FROM ops.schedule WHERE schedule_key = :k"),
                        {"k": "gate_followup:promotion"}).one()
    assert row.job_type == "gate_followup" and row.cadence_spec["interval_seconds"] == 300


def test_gate_followup_payload_is_validated(db: Any) -> None:
    from reva.autonomy.followup import handle_gate_followup

    assert handle_gate_followup({"connector": "x"}, _ctx(db))["status"] == "rejected"
    assert handle_gate_followup({"limit": 0}, _ctx(db))["status"] == "rejected"
    assert handle_gate_followup({"max_age_hours": "1"}, _ctx(db))["status"] == "rejected"
    killed = handle_gate_followup({}, _ctx(db, reva_autonomy_kill_switch=True))
    assert killed["status"] == "blocked" and "KILL_SWITCH" in killed["reason"]


async def test_late_gate_decision_is_promoted_by_the_followup(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import reva.autonomy.jobs as jobs
    from reva.autonomy import followup
    from reva.autonomy.jobs import promoted_connectors, reapprove_promotion, rollback_promotion
    from reva.autonomy.policy import promotion_policy
    from reva.gate.client import PENDING_REASON, GateClient, recorded_decision
    from reva.gate.service import GateService
    from reva.kernel.errors import StaleLease
    from reva.operations.context import JobContext

    if db.gate is None:
        pytest.skip("REVA_GATE_DB_PASSWORD not configured")
    before = promoted_connectors(db.worker, db.store).get(CONNECTOR)
    io_, p, reports, m = await _t1_proposal(db)
    # shared session database: other tests may leave unspent gate passes; follow up only this proposal's
    real_pending = followup.pending_followups
    monkeypatch.setattr(followup, "pending_followups", lambda engine, **k: [
        c for c in real_pending(engine, **k) if c["proposal_id"] == p.proposal_id])

    # 1. discovery's wait ends in gate_pending (no gate running): nothing to follow up yet
    pending = GateClient(db.worker, io_, wait_seconds=0.2, poll_seconds=0.05).decide(
        p.proposal_id, reports, promotion_policy().ref())
    assert pending.outcome == "blocked" and pending.reasons[0].startswith(PENDING_REASON)
    out = followup.handle_gate_followup({}, _ctx(db))
    assert out["status"] == "ok" and not [r for r in out["results"] if r["proposal_id"] == str(p.proposal_id)]

    # 2. the gate process decides later (reva_gate login)
    res = {r["proposal_id"]: r for r in GateService(db.gate, db.store, db.settings).run_once()}
    assert res[str(p.proposal_id)]["outcome"] == "promote" and res[str(p.proposal_id)]["recorded"]
    d = recorded_decision(db.worker, io_, p.proposal_id)
    assert d is not None and d.outcome == "promote"
    # a decision younger than the worker's own gate wait (+ grace) is left to the waiting discovery step
    assert not _mine(followup.handle_gate_followup({}, _ctx(db)), d.decision_id)
    monkeypatch.setattr(followup, "GRACE_SECONDS", 0.0)
    assert [c for c in real_pending(db.worker) if c["gate_result_id"] == d.decision_id]
    time.sleep(0.3)  # older than the (test) worker gate wait of 0.2 s

    promoted_events = "SELECT count(*) FROM ops.outbox WHERE event_type = 'autonomy.connector_promoted'"
    followup_events = ("SELECT count(*) FROM ops.outbox WHERE event_type = 'autonomy.gate_followup' "
                       "AND payload->>'gate_result_id' = :g")
    n_promoted = _count(db, promoted_events)

    # 3. kill switch: nothing is promoted or recorded; the candidate stays
    killed = followup.handle_gate_followup({}, _ctx(db, reva_autonomy_kill_switch=True))
    assert killed["status"] == "blocked" and not killed.get("results")
    assert _count(db, followup_events, g=str(d.decision_id)) == 0

    # 4. fenced: a job context that lost its lease promotes and records nothing
    ok_ctx = _ctx(db)
    stale = JobContext(job_id=ok_ctx.job_id, job_type="gate_followup", worker_id="test-worker", fencing_token=8,
                       attempt=1, settings=ok_ctx.settings, worker_engine=db.worker, sim_engine=None, store=db.store)
    with pytest.raises(StaleLease):
        followup.handle_gate_followup({}, stale)
    assert _count(db, promoted_events) == n_promoted and _count(db, followup_events, g=str(d.decision_id)) == 0

    # 5. a transient database error is retried on the next run, not recorded as final
    real_promote = jobs.promote_connector

    def flaky(*a: Any, **k: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    monkeypatch.setattr(jobs, "promote_connector", flaky)
    out = followup.handle_gate_followup({}, _ctx(db))
    assert [r["status"] for r in _mine(out, d.decision_id)] == ["retry"]
    assert _count(db, followup_events, g=str(d.decision_id)) == 0
    monkeypatch.setattr(jobs, "promote_connector", real_promote)

    # 6. the follow-up promotes from the stored proposal through approve_eligibility
    out = followup.handle_gate_followup({}, _ctx(db))
    item = _mine(out, d.decision_id)
    assert [r["status"] for r in item] == ["promoted"] and item[0]["version"] == m["version"], out
    assert promoted_connectors(db.worker, db.store)[CONNECTOR].manifest["version"] == m["version"]
    with db.owner.connect() as c:
        use = c.execute(text("SELECT u.approver, g.recorded_by FROM ops.approval_use u JOIN ops.gate_result g "
                             "ON g.id = u.approval_id WHERE u.approval_id = :g AND u.kind = 'eligibility'"),
                        {"g": d.decision_id}).one()
    assert tuple(use) == ("gate-evaluator:promotion", "reva_gate")
    assert _count(db, promoted_events) == n_promoted + 1
    assert _count(db, followup_events, g=str(d.decision_id)) == 1

    # 7. idempotent: the spent pass is never promoted again
    again = followup.handle_gate_followup({}, _ctx(db))
    assert not _mine(again, d.decision_id) and _count(db, promoted_events) == n_promoted + 1

    # restore the shared session state (the version this test promoted is rolled back)
    rollback_promotion(db.owner, CONNECTOR, m["version"], CONCEPT, "human:maintainer", "gate followup drill")
    after = promoted_connectors(db.worker, db.store).get(CONNECTOR)
    if before is not None and (after is None or after.manifest["version"] != before.manifest["version"]):
        reapprove_promotion(db.owner, CONNECTOR, before.manifest["version"], CONCEPT, "human:maintainer",
                            "restore after gate followup drill")
        after = promoted_connectors(db.worker, db.store).get(CONNECTOR)
    assert (after.manifest["version"] if after else None) == (before.manifest["version"] if before else None)


def test_a_refused_followup_is_recorded_once(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate whose newest decision is not a recorded promote is a terminal, recorded outcome."""
    from reva.autonomy import followup

    gid, pid = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(followup, "pending_followups",
                        lambda engine, **k: [{"gate_result_id": gid, "proposal_id": pid, "recorded_at": None}])
    out = followup.handle_gate_followup({}, _ctx(db))
    assert [r["status"] for r in _mine(out, gid)] == ["not_promotable"] and out["promoted"] == 0
    with db.owner.connect() as c:
        rows = c.execute(text("SELECT payload FROM ops.outbox WHERE event_type = 'autonomy.gate_followup' "
                              "AND payload->>'gate_result_id' = :g"), {"g": str(gid)}).scalars().all()
    assert len(rows) == 1 and rows[0]["status"] == "not_promotable" and rows[0]["job_id"]
