"""GATE end to end: worker proposes → separate gate *process* (reva_gate) decides → worker promotes.

* the worker files a gate request and, with no gate running, gets ``gate_pending`` and promotes nothing;
* ``python -m reva.gate.service --once`` (a real subprocess, gate login only, test database) evaluates
  the persisted proposal, recomputes the reports and records the decision as ``reva_gate``;
* the worker then promotes only through ``evidence.approve_eligibility`` citing that gate result;
* the gate service fails closed (malformed request → recorded ``blocked``; kill switch → nothing
  recorded, request stays pending).

Offline: captured Eurostat fixtures; the sandbox's public probe is pointed at a closed local port in the
subprocess too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from test_eurostat_discovery import CONNECTOR, _pipeline

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
CONCEPT = "demography.population.jan1_age_sex@1.0.0"
GATE_ONCE = ("import reva.autonomy.sandbox as s; s.PUBLIC_PROBE_ADDR = ('127.0.0.1', 1); "
             "from reva.gate.service import main; raise SystemExit(main(['--once']))")


def _job_ctx(db: Any) -> Any:
    from reva.operations.context import JobContext

    jid = uuid.uuid4()
    with db.owner.begin() as conn:
        conn.execute(text("""INSERT INTO ops.job(id, job_type, payload, idempotency_key, status, worker_id,
                                 lease_until, fencing_token, attempts)
                             VALUES (:i, 'discovery', '{}'::jsonb, :k, 'running', 'test-worker',
                                     clock_timestamp() + interval '10 minutes', 7, 1)"""),
                     {"i": jid, "k": f"gate-{jid}"})
    return JobContext(job_id=jid, job_type="discovery", worker_id="test-worker", fencing_token=7, attempt=1,
                      settings=db.settings, worker_engine=db.worker, sim_engine=None, store=db.store)


def _gate_env(db: Any) -> dict[str, str]:
    """The gate process environment: test database, gate login, no owner URL, no worker/sim/API password."""
    s = db.settings
    env = {k: v for k, v in os.environ.items() if not k.startswith("REVA_")}
    env.update({
        "REVA_ENV": "test", "REVA_PROCESS_ROLE": "gate", "REVA_DB_HOST": s.reva_db_host,
        "REVA_DB_PORT": str(s.reva_db_port), "REVA_DB_NAME": db.name,
        "REVA_GATE_DB_PASSWORD": s.reva_gate_db_password.get_secret_value(),
        "REVA_OBJECT_BACKEND": "filesystem", "REVA_OBJECT_ROOT": str(s.reva_object_root),
        "REVA_S3_PREFIX": s.reva_s3_prefix,
        # neutralise the local .env (process env wins): the gate never sees other credentials
        "REVA_MIGRATION_DATABASE_URL": "", "REVA_WORKER_DB_PASSWORD": "", "REVA_SIM_DB_PASSWORD": "",
        "REVA_API_DB_PASSWORD": "", "REVA_SESSION_SECRET": "", "REVA_ADMIN_PASSWORD_HASH": "",
        "REVA_AUTONOMY_KILL_SWITCH": "false",
    })
    return env


async def _t1_proposal(db: Any) -> tuple[Any, Any, Any, dict[str, Any]]:
    """A persisted, in-scope T1 connector_promotion with fresh content (so a new version is promotable)."""
    from reva.adapters.connectors.eurostat_template import template_digest
    from reva.autonomy.common import ArtifactIO, default_budget
    from reva.autonomy.jobs import connector_change_proposal, connector_promotion_scope
    from reva.autonomy.policy import promotion_policy
    from reva.autonomy.promotion import resolve_connector_version
    from reva.autonomy.proposals import ProposalStore
    from reva.autonomy.sandbox import TrustedTemplateSandbox, trusted_template_policy
    from reva.autonomy.validator import JsonStatConnectorValidator
    from reva.ports.autonomy import MappingProposal, ValueEstimate

    io_ = ArtifactIO(db.store)
    bank, _d, _c, mappings, cp = await _pipeline(io_)
    m = io_.get_json(cp.manifest)
    m["cadence"] = {**m["cadence"], "minimum_interval_seconds": 9 * 86400 + int(uuid.uuid4().hex[:4], 16)}
    with db.worker.connect() as conn:
        m["version"] = resolve_connector_version(conn, io_, m, template_digest())[0]
    cp2 = type(cp)(cp.proposal_id, cp.candidate, cp.template_id, cp.template_digest, io_.put_json(m, "models"),
                   cp.mapping)
    v = await JsonStatConnectorValidator(io_).validate(cp2, bank, default_budget())
    val = ValueEstimate("unsupported", None, None, {}, io_.put_json({"t": 1}, "evaluation"), "t")
    p = connector_change_proposal(io_, cp2, m, mappings[0], val, promotion_policy())
    mp = MappingProposal(uuid.uuid4(), cp.candidate.metadata, CONCEPT, "exact", mappings[0].mapping_manifest, False)
    ProposalStore(db.worker, io_).persist(p, proposal_type="connector_promotion",
                                          scope=connector_promotion_scope(io_, m, mp), risk={})
    s = await TrustedTemplateSandbox(io_, fixtures=bank).run(p, trusted_template_policy(default_budget()))
    return io_, p, [v, s], m


async def test_worker_proposes_gate_process_decides_worker_promotes(db: Any) -> None:
    from reva.autonomy.jobs import promote_connector, promoted_connectors, rollback_promotion
    from reva.autonomy.policy import promotion_policy
    from reva.gate.client import PENDING_REASON, GateClient, recorded_decision

    if db.gate is None:
        pytest.skip("REVA_GATE_DB_PASSWORD not configured")
    before = promoted_connectors(db.worker, db.store).get(CONNECTOR)
    io_, p, reports, m = await _t1_proposal(db)
    policy = promotion_policy()

    # 1. no gate process running: the worker's wait ends in gate_pending, nothing recorded or promoted
    client = GateClient(db.worker, io_, wait_seconds=0.3, poll_seconds=0.05)
    pending = client.decide(p.proposal_id, reports, policy.ref())
    assert pending.outcome == "blocked" and pending.reasons[0].startswith(PENDING_REASON)
    with db.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ops.gate_result WHERE proposal_id = :p"),
                         {"p": p.proposal_id}).scalar_one() == 0
        assert c.execute(text("SELECT recorded_by FROM ops.gate_request WHERE id = :r"),
                         {"r": client.last_request_id}).scalar_one() == "reva_worker"
    with pytest.raises(Exception):  # an unrecorded decision can never be promoted
        promote_connector(db.worker, io_, _job_ctx(db), pending, policy)

    # 2. the gate as a separate OS process with only the gate login
    proc = subprocess.run([sys.executable, "-c", GATE_ONCE], cwd=ROOT, env=_gate_env(db), capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    handled = json.loads(proc.stdout.strip().splitlines()[-1])["handled"]
    mine = [h for h in handled if h["proposal_id"] == str(p.proposal_id)]
    assert mine and mine[0]["outcome"] == "promote" and mine[0]["recorded"], (mine, proc.stderr[-2000:])
    for secret in (db.settings.reva_worker_db_password.get_secret_value(),
                   db.settings.reva_gate_db_password.get_secret_value()):
        assert secret not in proc.stdout + proc.stderr

    # 3. the worker reads the recorded decision and promotes through the approval function
    d = recorded_decision(db.worker, io_, p.proposal_id)
    assert d is not None and d.outcome == "promote", d
    with db.owner.connect() as c:
        row = c.execute(text("SELECT recorded_by, evaluator, decision FROM ops.gate_result WHERE id = :g"),
                        {"g": d.decision_id}).one()
    assert tuple(row) == ("reva_gate", "gate-evaluator:promotion", "pass")
    out = promote_connector(db.worker, io_, _job_ctx(db), d, policy, expected_manifest=m)
    assert out["status"] == "promoted" and out["version"] == m["version"], out
    with db.owner.connect() as c:
        use = c.execute(text("SELECT u.approver, g.recorded_by FROM ops.approval_use u "
                             "JOIN ops.gate_result g ON g.id = u.approval_id "
                             "WHERE u.approval_id = :g AND u.kind = 'eligibility'"), {"g": d.decision_id}).one()
        assert tuple(use) == ("gate-evaluator:promotion", "reva_gate")
        elig = c.execute(text("SELECT decision, recorded_by FROM evidence.eligibility "
                              "WHERE connector_version_id = :cv ORDER BY recorded_at DESC LIMIT 1"),
                         {"cv": uuid.UUID(out["connector_version_id"])}).one()
        assert tuple(elig) == ("eligible", "reva_worker")
    assert promoted_connectors(db.worker, db.store)[CONNECTOR].manifest["version"] == m["version"]

    # 4. roll back: the superseded version is restored only because its approval was independent
    rollback_promotion(db.owner, CONNECTOR, m["version"], CONCEPT, "human:maintainer", "gate e2e drill")
    after = promoted_connectors(db.worker, db.store).get(CONNECTOR)
    assert (after.manifest["version"] if after else None) == (before.manifest["version"] if before else None)


async def test_gate_service_fails_closed(db: Any) -> None:
    from reva.autonomy.policy import AutonomyGuard, promotion_policy
    from reva.gate.client import recorded_decision, request_gate
    from reva.gate.service import GateService, check_gate_login

    if db.gate is None:
        pytest.skip("REVA_GATE_DB_PASSWORD not configured")
    assert check_gate_login(db.gate)["login"] == "reva_gate"
    with pytest.raises(Exception, match="reva_gate"):
        check_gate_login(db.worker)
    io_, p, reports, _m = await _t1_proposal(db)

    # kill switch: no decision is recorded, the request stays pending
    killed = AutonomyGuard.from_settings(db.settings.model_copy(update={"reva_autonomy_kill_switch": True}))
    svc_killed = GateService(db.gate, db.store, db.settings, guard=killed)
    rid, since = request_gate(db.worker, p.proposal_id, reports, policy_sha256=promotion_policy().sha256)
    res = [r for r in svc_killed.run_once() if r["request_id"] == str(rid)]
    assert res and res[0]["recorded"] is False
    assert svc_killed.pending(request_id=rid)

    # autonomy back on: the request is evaluated and recorded by reva_gate
    svc = GateService(db.gate, db.store, db.settings)
    out = {r["request_id"]: r for r in svc.run_once()}
    assert out[str(rid)]["outcome"] == "promote" and out[str(rid)]["recorded"] is True
    d = recorded_decision(db.worker, io_, p.proposal_id, since=since)
    assert d is not None and d.outcome == "promote" and not svc.pending(request_id=rid)

    # malformed reports in a request: a typed refusal is recorded as 'blocked' (no retry loop)
    with db.worker.begin() as c:
        q = c.execute(text("INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, author) "
                           "SELECT 'connector_promotion', manifest_sha256, 1, 'agent:test' FROM ops.proposal "
                           "WHERE id = :p RETURNING id"), {"p": p.proposal_id}).scalar_one()
        bad = c.execute(text("INSERT INTO ops.gate_request(proposal_id, reports) "
                             "VALUES (:q, '[{\"outcome\": \"pass\"}]') RETURNING id"), {"q": q}).scalar_one()
    out = {r["request_id"]: r for r in svc.run_once()}
    assert out[str(bad)]["outcome"] == "blocked" and out[str(bad)]["recorded"] is True
    bd = recorded_decision(db.worker, io_, q)
    assert bd is not None and bd.outcome == "blocked" and any("malformed report" in r for r in bd.reasons)
    assert not svc.pending(request_id=bad)
