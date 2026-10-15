"""Helpers shared by the DB-backed invariant tests (scenario creation, manual job lease)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text


def create_scenario(db, spec_dict: dict[str, Any]) -> UUID:
    """Create a scenario through the API role's narrow SECURITY DEFINER function."""
    from reva.simulation.spec import validate_scenario_spec

    spec = validate_scenario_spec(spec_dict)
    with db.api.begin() as conn:
        sid = conn.execute(
            text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :bytes, :digest, 'human:test')"),
            {"spec": json.dumps(spec.as_json()), "bytes": spec.canonical_bytes(), "digest": spec.digest()},
        ).scalar_one()
    return UUID(str(sid))


def claim_simulation_job(db, payload: dict[str, Any], worker: str = "invariant-worker"):
    """Insert and lease one simulation job by hand; returns the handler's JobContext."""
    from reva.kernel.time import utc_now
    from reva.operations.context import JobContext

    with db.owner.begin() as conn:
        jid = conn.execute(
            text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                    VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
            {"p": json.dumps(payload), "k": f"inv-sim-{utc_now().timestamp()}-{id(payload)}"},
        ).scalar_one()
        token = conn.execute(
            text("""UPDATE ops.job SET status='running', worker_id=:w, lease_until=now() + interval '10 minutes',
                        fencing_token=nextval('ops.fencing_seq'), attempts=attempts+1
                    WHERE id=:j RETURNING fencing_token"""),
            {"w": worker, "j": jid},
        ).scalar_one()

    def heartbeat(_progress: dict[str, Any] | None = None) -> str:
        with db.owner.connect() as c:
            return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

    return JobContext(
        job_id=jid,
        job_type="simulation",
        worker_id=worker,
        fencing_token=int(token),
        attempt=1,
        settings=db.settings,
        worker_engine=db.worker,
        sim_engine=db.sim,
        store=db.store,
        heartbeat=heartbeat,
    )


def scenario_spec(release: dict[str, Any], **over: Any) -> dict[str, Any]:
    from reva.simulation.spec import CONTRACTS

    spec = json.loads((CONTRACTS / "scenario.example.json").read_text())
    spec["baseline_release_id"] = str(release["release_id"])
    spec["knowledge_cutoff"] = release["cutoff"].isoformat()
    spec.update(over)
    return spec
