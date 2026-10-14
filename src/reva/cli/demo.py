"""`reva demo`: the crude border-price stress scenario against a REAL published baseline.

Resolves the latest published belief release (never the fixture UUID), creates an
immutable scenario through the API role's narrow function, submits a simulation
job through ops.submit_job, executes it through the same fenced job path the
worker uses, and prints the assumption-conditioned results.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from reva.adapters.objects.factory import get_store
from reva.adapters.postgres.engine import get_engine
from reva.belief.snapshots import release_as_of
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.operations.context import JobContext
from reva.settings import get_settings
from reva.simulation.spec import validate_scenario_spec

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCENARIOS = {"crude-border-price-25": {"import_multiplier": 1.25, "export_multiplier": 1.25}}


def build_spec(release: dict[str, Any], countries: list[str], fidelity: str, scenario: str) -> dict[str, Any]:
    spec = json.loads((CONTRACTS / "scenario.example.json").read_text())
    spec["title"] = f"{scenario}: +25% crude border-price stress ({', '.join(countries)})"
    spec["baseline_release_id"] = str(release["id"])
    spec["knowledge_cutoff"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    spec["countries"] = countries
    spec["intervention"].update(SCENARIOS[scenario])
    spec["fidelity"] = fidelity
    if fidelity == "L2":
        spec["assumptions"] = {
            "quantity_response": "elasticity_sensitivity",
            "no_general_equilibrium": True,
            "elasticity_grid": [
                {"import_elasticity": ei, "export_elasticity": ex, "basis": "scenario_assumption"}
                for ei in (-0.5, -0.25, 0.0) for ex in (0.0, 0.25)
            ],
        }
    return spec


def _claim(job_id: str, worker_id: str) -> int:
    with get_engine("worker").begin() as conn:
        tok = conn.execute(
            text(
                """UPDATE ops.job SET status='running', worker_id=:w, attempts=attempts+1,
                     lease_until=clock_timestamp() + interval '120 seconds',
                     fencing_token=nextval('ops.fencing_seq'), updated_at=clock_timestamp()
                   WHERE id=:j AND status='queued' RETURNING fencing_token"""
            ),
            {"j": job_id, "w": worker_id},
        ).scalar_one_or_none()
    if tok is None:
        raise ValidationProblem("job is not queued (a running worker may already have claimed it)")
    return int(tok)


def run_demo(*, scenario: str, countries: list[str], fidelity: str, origin: str = "live") -> dict[str, Any]:
    settings = get_settings()
    api, sim = get_engine("api"), get_engine("sim")
    with api.connect() as conn:
        release = release_as_of(conn, None, origin)
    if release is None:
        raise ValidationProblem(f"no published {origin} belief release; run ingest + beliefs publish first")
    spec = validate_scenario_spec(build_spec(release, countries, fidelity, scenario))
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
    with api.connect() as conn:
        status = conn.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": job_id}).scalar_one()
    if status == "queued":
        worker_id = "cli-demo"
        token = _claim(str(job_id), worker_id)
        from reva.simulation.jobs import handle_simulation

        ctx = JobContext(job_id=job_id, job_type="simulation", worker_id=worker_id, fencing_token=token, attempt=1,
                         settings=settings, worker_engine=get_engine("worker"), sim_engine=sim, store=get_store())
        handle_simulation(payload, ctx)
    with api.connect() as conn:
        job = dict(conn.execute(text("SELECT status, result_ref, last_error FROM ops.job WHERE id=:j"),
                                {"j": job_id}).mappings().one())
        rows = conn.execute(
            text(
                """SELECT e.label AS country, r.output_key, r.variant, r.value->>'value' AS value,
                          r.value->>'unit_ref' AS unit, r.uncertainty->>'kind' AS uncertainty_kind,
                          r.uncertainty->>'lower' AS lower, r.uncertainty->>'upper' AS upper
                   FROM sim.result r JOIN sim.run run ON run.id = r.run_id
                   LEFT JOIN world.entity e ON e.id = r.subject_id
                   WHERE run.scenario_id = :s ORDER BY e.label, r.output_key, r.variant"""
            ),
            {"s": scenario_id},
        ).mappings().all()
    return {
        "context": "SIMULATION — assumption-conditioned accounting stress test (not a forecast; not GDP loss)",
        "scenario_id": str(scenario_id),
        "baseline_release": {"id": str(release["id"]), "sequence": release["sequence"],
                             "knowledge_cutoff": release["knowledge_cutoff"], "origin": release["origin"]},
        "job": {"id": str(job_id), **job},
        "results": [dict(r) for r in rows if fidelity != "L2" or r["variant"] == "L1" or True],
    }
