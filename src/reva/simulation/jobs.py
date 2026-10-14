"""Simulation job handler: ``handle_simulation(payload, ctx)``.

payload: ``{"scenario_id": uuid, "fidelity": "L0"|"L1"|"L2"}`` (optional
``idempotency_key``). Reads and writes go through the reva_sim role only; the sim
role cannot write evidence or beliefs (database grants, not a WHERE clause).

Request problems (missing inputs, unsupported fidelity/horizon, cache miss) are
recorded as immutable run outcomes and the job completes with that outcome as a
failed job — never as a fake success. Results, outcome and job completion commit
in one transaction guarded by the fencing token.

Run reuse (R2-06): the idempotency key always includes the logical digest of the
model manifest (which carries the model + engine implementation digest), so a
change to model or engine code never silently reuses a run computed by older code.

Cancellation (R2-01): a user cancel (job status ``cancel_requested``) completes the
job as ``cancelled`` before anything is published. A worker shutdown is *not* a
cancel: the handler raises ``JobCancelled`` so the worker releases the job for a
retry instead of ending it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from reva.kernel.errors import ConfigurationError, StaleLease, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.operations.context import JobContext
from reva.simulation import results as res
from reva.simulation.engine import ENGINE_VERSION, RunPlan, model_manifest, plan_run

ALL_FIDELITY = ("L0", "L1", "L2", "L3", "L4")


def load_scenario(conn: Connection, scenario_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        text("""SELECT id, context_id, baseline_release_id, spec, spec_digest, spec_sha256, created_by
                FROM sim.scenario WHERE id = :i"""),
        {"i": scenario_id},
    ).mappings().one_or_none()
    if row is None:
        raise ValidationProblem(f"scenario {scenario_id} not found")
    return dict(row)


def default_idempotency_key(spec_digest: str, fidelity: str, baseline_release_id: Any,
                            model_manifest_digest: str) -> str:
    return f"sim:{spec_digest}:{fidelity}:{baseline_release_id}:{model_manifest_digest}"


def _shutdown_requested(ctx: JobContext) -> bool:
    fn = getattr(ctx, "shutdown_requested", None)
    return bool(fn()) if callable(fn) else False


def _is_stale(e: DBAPIError) -> bool:
    return getattr(e.orig, "sqlstate", None) == "RV001" or "stale lease" in str(e.orig)


def _job_status(plan_status: str) -> str:
    return "succeeded" if plan_status == "succeeded" else "failed"


def _error_for(ref: dict[str, Any]) -> dict[str, Any] | None:
    if ref["status"] == "succeeded":
        return None
    return {"code": ref["status"], "detail": f"simulation run recorded with status {ref['status']}",
            "run_id": ref["run_id"], "problems": ref.get("problems", [])}


def handle_simulation(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    engine = ctx.sim_engine
    if engine is None:
        raise ConfigurationError("simulation jobs require the reva_sim database role (ctx.sim_engine)")
    try:
        scenario_id = UUID(str(payload["scenario_id"]))
    except (KeyError, ValueError) as e:
        raise ValidationProblem("payload.scenario_id must be a scenario UUID") from e

    # ---- read / compute phase (no writes) ----
    with engine.connect() as conn:
        scenario = load_scenario(conn, scenario_id)
        fidelity = str(payload.get("fidelity") or scenario["spec"].get("fidelity"))
        if fidelity not in ALL_FIDELITY:
            raise ValidationProblem(f"unknown fidelity {fidelity!r}")
        manifest = model_manifest(conn)
        plan: RunPlan = plan_run(conn, scenario, fidelity, manifest)
        conn.rollback()

    mm_digest = logical_hash(manifest)
    if payload.get("idempotency_key"):
        key = f"{payload['idempotency_key']}:mm:{mm_digest}"
    else:
        key = default_idempotency_key(scenario["spec_digest"], fidelity, scenario["baseline_release_id"], mm_digest)
    if fidelity == "L0" and not payload.get("idempotency_key"):
        # A lookup outcome depends on what has been computed so far: a miss must not
        # be replayed forever, so L0 keys name the hit (or the job, for a miss).
        key += f":hit:{plan.cache_hit_of}" if plan.cache_hit_of else f":miss:job:{ctx.job_id}"
    execution_profile = {
        "engine": ENGINE_VERSION,
        "fidelity": fidelity,
        "cache_key": plan.cache_key,
        "model_manifest_logical_sha256": mm_digest,
        "scheduling": "deterministic: countries sorted by ISO3; acyclic one-way coupling",
        "decimal_precision": manifest["models"][0]["decimal_precision"],
        "job_id": str(ctx.job_id),
        "attempt": ctx.attempt,
        "worker_id": ctx.worker_id,
    }

    # ---- cancellation is honoured before anything becomes visible ----
    if ctx.is_cancelled():
        if _shutdown_requested(ctx):
            from reva.operations.jobs import JobCancelled

            # Worker shutdown, not a user cancel: nothing is published and the worker
            # releases/requeues the job (it handles JobCancelled); never "cancelled".
            raise JobCancelled(f"worker shutting down; simulation job {ctx.job_id} released unpublished",
                               scenario_id=str(scenario_id), published=False)
        try:
            with engine.begin() as conn:
                ctx.complete_in(conn, "cancelled", {"scenario_id": str(scenario_id), "published": False})
        except DBAPIError as e:
            if _is_stale(e):
                raise StaleLease(f"job {ctx.job_id} lost its lease before cancellation") from e
            raise
        return {"status": "cancelled", "scenario_id": str(scenario_id), "published": False}

    # ---- publication: results + outcome + job completion in ONE fenced transaction ----
    try:
        with engine.begin() as conn:
            ctx.assert_lease(conn)
            prior = res.existing_run(conn, key)
            if prior is not None and prior.get("status") is not None:
                ref = {**res.result_ref(prior), "idempotent_replay": True}
            else:
                ref = res.publish_run(
                    conn, ctx.store, plan, scenario=scenario, manifest=manifest, idempotency_key=key,
                    execution_profile=execution_profile, job_id=ctx.job_id, actor=f"worker:{ctx.worker_id}",
                )
            ctx.complete_in(conn, _job_status(ref["status"]), ref, _error_for(ref))
    except DBAPIError as e:
        if _is_stale(e):
            raise StaleLease(f"job {ctx.job_id} lost its lease/fencing token; results not published") from e
        raise
    return ref
