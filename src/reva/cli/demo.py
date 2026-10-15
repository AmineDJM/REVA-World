"""`reva demo`: the crude border-price stress scenario against a REAL published baseline.

Resolves the latest published belief release of the requested origin (``live``
by default, ``test_fixture`` for a fixture-only local database; never the
fixture UUID), creates an immutable scenario through the API role's narrow
function, and submits a simulation job through ``ops.submit_job``.

Execution (R2-16):

* if a worker is running (fresh ``ops.worker_presence`` row) the job is left
  queued for it and the demo polls the job with a timeout;
* otherwise the demo executes the job itself through the real worker path
  (``queue.claim_job`` + ``Worker.run_job``: fenced lease, heartbeats, user
  cancellation via ``POST /v1/jobs/{id}/cancel`` and Ctrl-C/SIGTERM release the
  job instead of completing it).

The scenario ``knowledge_cutoff`` is the *database* clock (``clock_timestamp()``,
never earlier than the baseline release cutoff), and results are those of the
job's own run (``result_ref.run_id``), never other runs of the same scenario.
"""

from __future__ import annotations

import json
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.adapters.objects.factory import get_store
from reva.adapters.postgres import queue
from reva.adapters.postgres.engine import get_engine
from reva.belief.snapshots import release_as_of
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.settings import get_settings
from reva.simulation.spec import validate_scenario_spec

ORIGINS = ("live", "test_fixture")
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead"})
WORKER_FRESH_SECONDS = 120
LOCAL_WORKER_ID = "cli-demo"  # the demo's in-process executor
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCENARIOS = {"crude-border-price-25": {"import_multiplier": 1.25, "export_multiplier": 1.25}}


def _iso_z(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def build_spec(
    release: dict[str, Any], countries: list[str], fidelity: str, scenario: str, knowledge_cutoff: datetime
) -> dict[str, Any]:
    """``knowledge_cutoff`` must come from the database clock (see :func:`db_cutoff`)."""
    if knowledge_cutoff.tzinfo is None:
        raise ValidationProblem("knowledge_cutoff must be timezone-aware")
    spec: dict[str, Any] = json.loads((CONTRACTS / "scenario.example.json").read_text())
    spec["title"] = f"{scenario}: +25% crude border-price stress ({', '.join(countries)})"
    spec["baseline_release_id"] = str(release["id"])
    spec["knowledge_cutoff"] = _iso_z(knowledge_cutoff)
    spec["countries"] = countries
    spec["intervention"].update(SCENARIOS[scenario])
    spec["fidelity"] = fidelity
    if fidelity == "L2":
        spec["assumptions"] = {
            "quantity_response": "elasticity_sensitivity",
            "no_general_equilibrium": True,
            "elasticity_grid": [
                {"import_elasticity": ei, "export_elasticity": ex, "basis": "scenario_assumption"}
                for ei in (-0.5, -0.25, 0.0)
                for ex in (0.0, 0.25)
            ],
        }
    return spec


def db_cutoff(engine: Engine, release: dict[str, Any]) -> datetime:
    """Database clock in UTC (whole seconds, rounded *up*), never before the release cutoff."""
    with engine.connect() as conn:
        now: datetime = conn.execute(
            text(
                "SELECT date_trunc('second', clock_timestamp() AT TIME ZONE 'UTC' + interval '1 second') "
                "AT TIME ZONE 'UTC'"
            )
        ).scalar_one()
    now = now.astimezone(UTC)
    rc = release.get("knowledge_cutoff")
    if isinstance(rc, datetime) and rc.tzinfo is not None and rc.astimezone(UTC) > now:
        rc = rc.astimezone(UTC)
        now = rc.replace(microsecond=0) + timedelta(seconds=1) if rc.microsecond else rc
    return now


def worker_running(engine: Engine, fresh_seconds: int = WORKER_FRESH_SECONDS) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    # the demo's own in-process executor is not a running worker: a previous local demo
                    # must not make the next one wait for a worker that does not exist
                    "SELECT EXISTS (SELECT 1 FROM ops.worker_presence "
                    "WHERE last_seen > clock_timestamp() - make_interval(secs => :s) AND worker_id <> :local)"
                ),
                {"s": fresh_seconds, "local": LOCAL_WORKER_ID},
            ).scalar_one()
        )


def _job_row(engine: Engine, job_id: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(text("SELECT status, result_ref, last_error FROM ops.job WHERE id=:j"), {"j": job_id})
            .mappings()
            .one()
        )


def _wait(engine: Engine, job_id: Any, timeout: float, poll: float = 1.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    row = _job_row(engine, job_id)
    while row["status"] not in TERMINAL and time.monotonic() < deadline:
        time.sleep(poll)
        row = _job_row(engine, job_id)
    return row


def _run_locally(job_id: Any, settings: Any) -> str:
    """Execute this job through the real worker path; returns the outcome status."""
    from reva.operations.worker import Worker

    worker_engine = get_engine("worker")
    w = Worker(
        settings,
        worker_engine,
        get_store(),
        get_engine("sim"),
        worker_id=LOCAL_WORKER_ID,
        concurrency=1,
        scheduler=False,
        invalidation=False,
        job_types=["simulation"],
    )
    job = queue.claim_job(worker_engine, UUID(str(job_id)), w.worker_id, w.lease_seconds)
    if job is None:
        return "not_claimed"  # a worker picked it up meanwhile
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        w.install_signal_handlers()  # Ctrl-C releases the job back to the queue (not "cancelled")
    except ValueError:  # not in the main thread
        old = {}
    try:
        return w.run_job(job).status
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


def run_demo(
    *,
    scenario: str,
    countries: list[str],
    fidelity: str,
    origin: str = "live",
    wait_seconds: float = 600.0,
    execute_locally: bool | None = None,
) -> dict[str, Any]:
    """Create and run the demo scenario.

    ``execute_locally``: ``None`` (default) runs the job here only when no worker is
    running; ``False`` always leaves it to a worker (poll with ``wait_seconds``);
    ``True`` always tries to claim it here.
    """
    if origin not in ORIGINS:
        raise ValidationProblem(f"origin must be one of {', '.join(ORIGINS)}", origin=str(origin)[:32])
    if scenario not in SCENARIOS:
        raise ValidationProblem("unknown demo scenario", scenario=scenario[:64], known=sorted(SCENARIOS))
    settings = get_settings()
    api = get_engine("api")
    with api.connect() as conn:
        release = release_as_of(conn, None, origin)
    if release is None:
        raise ValidationProblem(f"no published {origin} belief release; run ingest + beliefs publish first")
    spec = validate_scenario_spec(build_spec(release, countries, fidelity, scenario, db_cutoff(api, release)))
    with api.begin() as conn:
        scenario_id = conn.execute(
            text("SELECT sim.create_scenario(CAST(:s AS jsonb), :b, :d, 'human:cli-demo')"),
            {"s": json.dumps(spec.as_json()), "b": spec.canonical_bytes(), "d": spec.digest()},
        ).scalar_one()
        payload = {"scenario_id": str(scenario_id), "fidelity": fidelity}
        job_id = conn.execute(
            text("SELECT ops.submit_job('simulation', CAST(:p AS jsonb), :k, 0, 'human:cli-demo')"),
            {"p": json.dumps(payload), "k": "demo:" + logical_hash(payload)[:32]},
        ).scalar_one()
    execution = "existing"
    row = _job_row(api, job_id)
    if row["status"] not in TERMINAL:
        local = execute_locally if execute_locally is not None else not worker_running(get_engine("worker"))
        if local and row["status"] == "queued":
            execution = f"local:{_run_locally(job_id, settings)}"
        else:
            execution = "worker"
        row = _wait(api, job_id, wait_seconds if execution != "local:released" else 0)
    warnings: list[str] = []
    if row["status"] not in TERMINAL:
        warnings.append(f"job still {row['status']} after {wait_seconds:.0f}s; poll GET /v1/jobs/{job_id}")
    run_id = (row.get("result_ref") or {}).get("run_id") if row["status"] == "succeeded" else None
    rows: list[Any] = []
    if run_id:
        with api.connect() as conn:
            rows = list(
                conn.execute(
                    text(
                        """SELECT e.label AS country, r.output_key, r.variant, r.value->>'value' AS value,
                              r.value->>'unit_ref' AS unit, r.uncertainty->>'kind' AS uncertainty_kind,
                              CASE WHEN r.value->>'kind' = 'sensitivity_range' THEN r.value->>'lower'
                                   ELSE r.uncertainty->>'lower' END AS lower,
                              CASE WHEN r.value->>'kind' = 'sensitivity_range' THEN r.value->>'upper'
                                   ELSE r.uncertainty->>'upper' END AS upper,
                              r.value->>'kind' AS value_kind
                       FROM sim.result r
                       LEFT JOIN world.entity e ON e.id = r.subject_id
                       WHERE r.run_id = :r ORDER BY e.label, r.output_key, r.variant"""
                    ),
                    {"r": run_id},
                )
                .mappings()
                .all()
            )
    return {
        "context": "SIMULATION — assumption-conditioned accounting stress test (not a forecast; not GDP loss)",
        "scenario_id": str(scenario_id),
        "baseline_release": {
            "id": str(release["id"]),
            "sequence": release["sequence"],
            "knowledge_cutoff": release["knowledge_cutoff"],
            "origin": release["origin"],
        },
        "knowledge_cutoff": spec.as_json().get("knowledge_cutoff"),
        "execution": execution,
        "job": {"id": str(job_id), **row},
        "run_id": run_id,
        "results": [dict(r) for r in rows],
        "warnings": warnings,
    }
