"""Job handler registry and the operations-owned handlers.

``HANDLERS`` maps a job type to a lazily imported ``module:function``. A handler
whose module/function cannot be imported produces a *failed* job with error
code ``handler_unavailable`` (never a success). Handler contract:
``handle(payload: dict, ctx: JobContext) -> dict`` (see ``operations.context``).

Cooperative cancellation/shutdown: handlers call ``ctx.is_cancelled()`` at safe
points and either raise :class:`JobCancelled` or return
``{"status": "cancelled"}``; during worker shutdown they may instead return
``{"status": "interrupted", "checkpoint": {...}}`` to be re-queued from the
checkpoint without consuming an attempt. A worker shutdown is never a
cancellation: the worker releases the job unless the database status says a
user cancelled it (``ctx.shutdown_requested()`` vs ``ctx.cancel_requested()``,
see ``operations.context``).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.kernel.errors import (
    CapabilityGap,
    ConfigurationError,
    ImmutableRecordViolation,
    IncompatibleSemantics,
    IntegrityFailure,
    LeakageBlocked,
    MissingInput,
    PolicyDenied,
    RevaError,
    SourceResponseError,
    StaleLease,
    UnsafeRequest,
    ValidationProblem,
)
from reva.operations.budgets import BudgetExhausted
from reva.operations.context import JobCancelled, JobContext

__all__ = ["HANDLERS", "JobCancelled", "JobContext", "resolve_handler"]

log = logging.getLogger("reva.jobs")

HANDLERS: dict[str, str] = {
    "ingest": "reva.operations.jobs:handle_ingest",
    "publish_beliefs": "reva.operations.jobs:handle_publish_beliefs",
    "simulation": "reva.simulation.jobs:handle_simulation",
    "replay": "reva.evaluation.replay:handle_replay",
    "discovery": "reva.autonomy.jobs:handle_discovery",
    "gaps": "reva.autonomy.jobs:handle_gaps",
    "health": "reva.operations.jobs:handle_health",
    "gc": "reva.operations.gc:handle_gc",
    "evaluation": "reva.evaluation.tournament_service:handle_evaluation",
    "population": "reva.population.service:handle_population",
    "connector_repair": "reva.autonomy.repair:handle_connector_repair",
    # --- GATE2: promote connector proposals whose reva_gate decision arrived after discovery stopped waiting
    "gate_followup": "reva.autonomy.followup:handle_gate_followup",
    # --- end GATE2
    # --- BTAPI: stored back-tests of V2 models from release-pinned datasets (worker credentials)
    "backtest": "reva.evaluation.backtests:handle_backtest",
    # --- end BTAPI
    # --- ATLASOPS (V3): one atlas source, budget-metered (reserve/meter/settle inside run_atlas_ingest)
    "atlas_ingest": "reva.atlas.ingest:handle_atlas_ingest",
    # --- ATLASOPS: record (or reuse when unchanged) an atlas snapshot of the registered atlas tables
    "atlas_snapshot": "reva.atlas.snapshot:handle_atlas_snapshot",
    # --- end ATLASOPS
}

# Job types that receive the separate reva_sim engine (numerical/simulation
# outputs only). Ingestion/publication/ops handlers get ctx.sim_engine = None.
SIM_ENGINE_JOB_TYPES = frozenset({"simulation", "replay", "evaluation", "population"})

PUBLISH_JOB_LOCK = 7262021
PUBLISH_PRIORITY = 5
REPAIR_PRIORITY = 0
# Marker namespace of reva.autonomy.repair.PAUSE_PREFIX ("publication_pause:" + "belief:<concept>").
PAUSE_NODE_PREFIX = "publication_pause:"


class HandlerUnavailable(RevaError):
    """The job type's handler is not installed/wired in this build."""

    code = "handler_unavailable"
    http_status = 501


class IngestFailed(RevaError):
    code = "ingest_failed"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool, **details: Any) -> None:
        super().__init__(message, **details)
        self.retryable = retryable


class PublicationBusy(RevaError):
    code = "publication_busy"
    http_status = 409


NON_RETRYABLE: tuple[type[RevaError], ...] = (
    ValidationProblem,
    CapabilityGap,
    MissingInput,
    IncompatibleSemantics,
    LeakageBlocked,
    ImmutableRecordViolation,
    PolicyDenied,
    ConfigurationError,
    UnsafeRequest,
    IntegrityFailure,
    HandlerUnavailable,
    BudgetExhausted,
)


def resolve_handler(job_type: str) -> Callable[[dict[str, Any], JobContext], dict[str, Any]]:
    path = HANDLERS.get(job_type)
    if path is None:
        raise HandlerUnavailable(f"no handler registered for job type {job_type!r}", job_type=job_type)
    module_name, _, func_name = path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise HandlerUnavailable(
            f"handler module {module_name} is unavailable: {e}", job_type=job_type, handler=path
        ) from e
    fn = getattr(module, func_name, None)
    if not callable(fn):
        raise HandlerUnavailable(f"handler {path} is not defined", job_type=job_type, handler=path)
    return fn  # type: ignore[no-any-return]


def classify_exception(exc: BaseException) -> tuple[str, bool]:
    """(error code, retryable). Unknown/infrastructure errors are retryable (bounded by max_attempts)."""
    if isinstance(exc, StaleLease):
        return exc.code, False
    if isinstance(exc, SourceResponseError | IngestFailed):
        return exc.code, bool(exc.retryable)
    if isinstance(exc, NON_RETRYABLE):
        return exc.code, False
    if isinstance(exc, RevaError):
        return exc.code, True
    if isinstance(exc, KeyError | TypeError | ValueError):
        return "handler_error", False  # deterministic programming/input errors do not improve on retry
    return "transient_error", True


# --------------------------------------------------------------------------- publish helpers
def latest_observation_event(conn: Connection) -> int:
    return int(
        conn.execute(
            text("SELECT COALESCE(max(id), 0) FROM ops.outbox WHERE event_type = 'evidence.observations_changed'")
        ).scalar_one()
    )


def enqueue_publish_beliefs(
    conn: Connection, origin: str, through_outbox_id: int, *, created_by: str, audit_actor: str | None = None
) -> Any:
    """Idempotent publish_beliefs job for (origin, through_outbox_id).

    A key held by a foreign job (R2-02) never blocks publication: the job is
    created under a disambiguated key and the conflict is logged, counted and
    audited (``jobs.publish_key_conflict``).
    """
    from reva.adapters.postgres.queue import enqueue_resolving_conflict, idempotency_key

    payload = {"origin": origin, "through_outbox_id": int(through_outbox_id)}
    res = enqueue_resolving_conflict(
        conn,
        "publish_beliefs",
        payload,
        idempotency_key("publish_beliefs", payload),
        priority=PUBLISH_PRIORITY,
        created_by=created_by,
    )
    if res.conflict is not None and res.created:  # surface once, when the disambiguated job is created
        from reva.operations.audit import record
        from reva.operations.telemetry import IDEMPOTENCY_CONFLICTS

        log.error(
            "publish_beliefs key %s is held by foreign job %s (%s); created job %s under %s",
            res.conflict["idempotency_key"],
            res.conflict["foreign_job_id"],
            res.conflict["foreign_job_type"],
            res.job_id,
            res.conflict["disambiguated_key"],
        )
        IDEMPOTENCY_CONFLICTS.inc(job_type="publish_beliefs")
        try:
            with conn.begin_nested():
                record(
                    conn,
                    audit_actor or "system",
                    "jobs.publish_key_conflict",
                    {"job_id": str(res.job_id)},
                    res.conflict,
                )
        except Exception as e:
            log.warning("could not audit publish key conflict: %s", type(e).__name__)
    return res.job_id


def dirty_belief_nodes(conn: Connection) -> int:
    """Dirty belief nodes that a publication can actually refresh.

    Nodes of concepts whose publication is paused (``publication_pause:belief:<concept>``,
    see :mod:`reva.autonomy.repair`) are excluded: a release withholds them, so they stay
    dirty until the pause is lifted (``connector.publication_resumed`` re-enqueues the
    publication) instead of triggering identical releases.
    """
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM ops.dirty_node d WHERE d.node_key LIKE 'belief:%' "
                "AND d.reason NOT LIKE 'materiality_skip%' "
                "AND NOT EXISTS (SELECT 1 FROM ops.dirty_node p WHERE p.node_key = :pp || d.node_key)"
            ),
            {"pp": PAUSE_NODE_PREFIX},
        ).scalar_one()
    )


def repair_idempotency_key(
    connector: str, *, acquisition_id: str | None = None, trigger: str | None = None, origin: str | None = None
) -> str:
    """Stable key per (connector, quarantined acquisition), or per (connector, origin, failure streak)."""
    from reva.adapters.postgres.queue import idempotency_key

    if acquisition_id is not None:
        return idempotency_key("connector_repair", {"connector": connector, "acquisition_id": str(acquisition_id)})
    return idempotency_key("connector_repair", {"connector": connector, "origin": origin, "trigger": trigger})


def enqueue_connector_repair(
    conn: Connection,
    connector: str,
    origin: str,
    *,
    acquisition_id: str | None = None,
    trigger: str | None = None,
    created_by: str,
    audit_actor: str | None = None,
) -> Any:
    """Idempotent ``connector_repair`` job for one quarantined acquisition or one failure streak.

    The payload names only what the handler reads (connector, origin) plus the
    triggering acquisition/streak for traceability; the handler re-derives the
    case from the database, so nothing in the payload selects hosts, fixtures or tiers.
    """
    from reva.adapters.postgres.queue import enqueue_resolving_conflict

    if (acquisition_id is None) == (trigger is None):
        raise ValidationProblem("a repair job is triggered by exactly one acquisition or failure streak")
    payload: dict[str, Any] = {"connector": connector, "origin": origin}
    if acquisition_id is not None:
        payload["acquisition_id"] = str(acquisition_id)
    else:
        payload["trigger"] = trigger
    key = repair_idempotency_key(connector, acquisition_id=acquisition_id, trigger=trigger, origin=origin)
    res = enqueue_resolving_conflict(
        conn, "connector_repair", payload, key, priority=REPAIR_PRIORITY, created_by=created_by
    )
    if res.conflict is not None and res.created:
        from reva.operations.audit import record
        from reva.operations.telemetry import IDEMPOTENCY_CONFLICTS

        log.error(
            "connector_repair key %s is held by foreign job %s; created job %s under %s",
            res.conflict["idempotency_key"],
            res.conflict["foreign_job_id"],
            res.job_id,
            res.conflict["disambiguated_key"],
        )
        IDEMPOTENCY_CONFLICTS.inc(job_type="connector_repair")
        try:
            with conn.begin_nested():
                record(
                    conn, audit_actor or "system", "jobs.repair_key_conflict", {"job_id": str(res.job_id)}, res.conflict
                )
        except Exception as e:
            log.warning("could not audit repair key conflict: %s", type(e).__name__)
    return res


def after_run_failures(
    engine: Engine, connector: str, origin: str, *, actor: str, environment: str = "local"
) -> dict[str, Any]:
    """Operations response to a persistent request-failure streak (§10.3).

    * ``PERSISTENT_RUNS`` consecutive runs with auth (401/403) or gone (404/410/DNS)
      signals enqueue one ``connector_repair`` job per streak (key: connector, origin,
      streak kind and first run).
    * A connector-wide auth streak (no item succeeded) disables the connector's
      ingest schedules for that mode, with an audit row
      (:func:`reva.operations.schedules.disable_ingest_on_auth_failure`).
    """
    from reva.autonomy.repair import AUTH, PERSISTENT_RUNS, failure_streak
    from reva.operations.schedules import disable_ingest_on_auth_failure

    out: dict[str, Any] = {"repair_job_id": None, "schedules_disabled": []}
    with engine.begin() as conn:
        streak = failure_streak(conn, connector, origin=origin)
        if streak is None or streak["runs"] < PERSISTENT_RUNS:
            return out
        out["streak"] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in streak.items()}
        if not (environment == "production" and origin != "live"):
            trigger = f"failure_streak:{streak['kind']}:{streak['since'].isoformat()}"
            res = enqueue_connector_repair(
                conn, connector, origin, trigger=trigger, created_by=actor, audit_actor=actor
            )
            out["repair_job_id"] = str(res.job_id)
        if streak["kind"] == AUTH and streak["connector_wide"]:
            out["schedules_disabled"] = disable_ingest_on_auth_failure(conn, connector, origin, streak, actor=actor)
    return out


# --------------------------------------------------------------------------- ingest
def _all_connectors(ctx: JobContext) -> dict[str, Any]:
    """Reviewed built-in connectors plus declarative connectors promoted by the gate.

    A promoted connector can never shadow a built-in (R4-01/R4-03): built-ins are
    applied last, and a promoted name that collides with a built-in is dropped,
    logged and audited (``connectors.promoted_name_collision``). Used by both the
    ingest and the health handlers.
    """
    from reva.adapters.connectors.registry import builtin_connectors

    builtins: dict[str, Any] = dict(builtin_connectors(ctx.settings.reva_http_contact_email))
    promoted: dict[str, Any] = {}
    try:
        from reva.autonomy.jobs import promoted_connectors

        promoted = dict(promoted_connectors(ctx.worker_engine, ctx.store))
    except Exception as e:
        log.warning("promoted connectors unavailable: %s", type(e).__name__)
    collisions = sorted(set(promoted) & set(builtins))
    if collisions:
        log.error("promoted connector(s) %s collide with built-in names; the built-ins are used", collisions)
        try:
            from reva.operations.audit import record

            with ctx.worker_engine.begin() as conn:
                record(
                    conn,
                    f"worker:{ctx.worker_id}"[:200],
                    "connectors.promoted_name_collision",
                    {"job_id": str(ctx.job_id)},
                    {"names": collisions[:50]},
                )
        except Exception as e:  # the anomaly record is best effort; the override is refused regardless
            log.warning("could not audit connector name collision: %s", type(e).__name__)
    return {**promoted, **builtins}


def handle_ingest(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    from reva.evidence.runner import FixtureHttp, RecordingHttp, make_live_client, post_publish_for, run_connector
    from reva.operations.telemetry import INGEST_BYTES, INGEST_REQUESTS

    name = payload.get("connector")
    mode = payload.get("mode", "live")
    capture = bool(payload.get("capture", False))
    if mode not in ("live", "fixture"):
        raise ValidationProblem("ingest mode must be 'live' or 'fixture'", mode=str(mode)[:32])
    connectors = _all_connectors(ctx)
    connector = connectors.get(str(name))
    if connector is None:
        from reva.autonomy.jobs import promoted_connector_availability

        # A template connector name (e.g. a scheduled eurostat.* ingest) that is not (or no longer)
        # active is a typed, non-failing result — never a crash and never a fake success.
        unavailable = promoted_connector_availability(ctx.worker_engine, str(name))
        if unavailable is not None:
            return unavailable
        raise ValidationProblem("unknown connector", connector=str(name)[:128], known=sorted(connectors))
    spec = connector.describe()
    production = ctx.settings.reva_env == "production"
    if mode == "fixture" and production:
        raise PolicyDenied("fixture ingestion is not allowed in production")
    if capture and (mode != "live" or production):
        raise PolicyDenied("fixture capture is only available for live runs outside production")
    if ctx.is_cancelled():
        raise JobCancelled("cancelled before acquisition started")

    # Invariant 30: live runs are charged against registry/policies/budgets.yaml before any
    # request; a run that would exceed a daily budget is not started (budget_exhausted).
    reservation = None
    fetch_budget = None
    if mode == "live":
        from reva.evidence.runner import load_cursor
        from reva.operations.budgets import budget_source_key, reserve_ingest
        from reva.ports.connectors import FetchBudget

        default = FetchBudget()
        planned = len(connector.plan(load_cursor(ctx.worker_engine, spec.name), default).items)
        reservation = reserve_ingest(
            ctx.worker_engine,
            job_id=ctx.job_id,
            attempt=ctx.attempt,
            source_key=budget_source_key(spec),
            planned_requests=planned,
        )
        fetch_budget = FetchBudget(
            max_requests=min(default.max_requests, reservation.max_requests),
            max_bytes=min(default.max_bytes, reservation.max_bytes),
        )

    http: Any
    if mode == "fixture":
        http = FixtureHttp(spec.name)
    else:
        client = make_live_client(spec, ctx.settings)
        http = RecordingHttp(client, spec.name, spec.licence) if capture else client
        from reva.operations.budgets import metered  # every request is covered by the ledger reservation

        http = metered(http, ctx.worker_engine, reservation)

    from reva.operations.tracing import span, traced_connector

    async def _run() -> Any:
        try:
            return await run_connector(
                traced_connector(connector),
                http,
                ctx.store,
                ctx.worker_engine,
                budget=fetch_budget,
                persist_cursor=(mode == "live"),
                post_publish=getattr(connector, "post_publish", None) or post_publish_for(spec.template),
                fence=ctx.assert_lease,
            )
        finally:
            await http.aclose()

    with span("ingest.run", connector=spec.name, mode=mode, job_id=str(ctx.job_id)):
        report = asyncio.run(_run())
    if capture:
        http.write_manifest()

    counts: dict[str, int] = {}
    for o in report.outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
        INGEST_REQUESTS.inc(connector=spec.name, status=o.status)
    shas = sorted({o.raw_sha256 for o in report.outcomes if o.raw_sha256})
    origin = "test_fixture" if mode == "fixture" else "live"
    dataset_info = None
    if spec.name == "eurostat.figaro.iciot_ii" and report.cursor.get("complete") and report.cursor.get("year"):
        # V2: a complete FIGARO year becomes a verified dataset input (identity checks; refuses otherwise)
        from reva.adapters.connectors.io_tables import publish_io_dataset

        dataset_info = publish_io_dataset(ctx.worker_engine, ctx.store, year=int(report.cursor["year"]), origin=origin)
    publish_job = None
    nbytes = 0
    with ctx.worker_engine.begin() as conn:
        if shas:
            nbytes = conn.execute(
                text("SELECT COALESCE(sum(byte_size), 0) FROM meta.artifact WHERE sha256 = ANY(:s)"), {"s": shas}
            ).scalar_one()
            INGEST_BYTES.inc(float(nbytes), connector=spec.name)
        if (report.inserted > 0 or dataset_info) and not (production and origin != "live"):
            try:
                with conn.begin_nested():
                    publish_job = enqueue_publish_beliefs(
                        conn,
                        origin,
                        latest_observation_event(conn),
                        created_by=f"job:{ctx.job_id}",
                        audit_actor=f"worker:{ctx.worker_id}"[:200],
                    )
            except Exception as e:  # data is already published; the outbox path enqueues it as a backstop
                log.error(
                    "publish_beliefs enqueue after ingest failed (%s); outbox processing will retry", type(e).__name__
                )
    budget_info = None
    if reservation is not None:
        from reva.operations.budgets import settle_metered

        budget_info = settle_metered(ctx.worker_engine, http)  # actual requests/bytes made
    result = {
        "connector": spec.name,
        "connector_version": spec.version,
        "mode": mode,
        "origin": origin,
        "inserted": report.inserted,
        "dataset": dataset_info,
        "unchanged": report.unchanged,
        "relations": report.relations,
        "outcomes": counts,
        "errors": report.errors[:10],
        "cursor_advanced": report.cursor_advanced,
        "raw_sha256": shas[:50],
        "publish_job_id": str(publish_job) if publish_job else None,
        "budget": budget_info,
        "status": "partial" if report.errors else "complete",
    }
    if report.errors:
        try:
            follow = after_run_failures(
                ctx.worker_engine,
                spec.name,
                origin,
                actor=f"worker:{ctx.worker_id}"[:200],
                environment=ctx.settings.reva_env,
            )
        except Exception as e:  # the ingest outcome stands; the streak is re-evaluated on the next run
            log.error("failure-streak follow-up for %s failed: %s", spec.name, type(e).__name__)
            follow = {"error": type(e).__name__}
        result["failure_followup"] = follow
    if report.outcomes and all(o.status == "failed" for o in report.outcomes):
        raise IngestFailed(
            f"all {len(report.outcomes)} acquisition items failed for {spec.name}",
            retryable=(mode == "live"),
            errors=report.errors[:5],
        )
    return result


# --------------------------------------------------------------------------- beliefs
def handle_publish_beliefs(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Publish one belief release at cutoff=now (DB clock) if belief nodes are dirty.

    Idempotent in effect: ``publish_release`` clears dirty belief nodes in its
    own transaction, so a retry after a crash between commit and ack finds no
    dirty nodes and returns ``up_to_date`` instead of a duplicate release.
    Publications are serialised with a session advisory lock.
    """
    from reva.belief.publication import publish_release
    from reva.kernel.context import Origin, assert_publishable

    try:
        origin = Origin(payload.get("origin", "live"))
    except ValueError:
        raise ValidationProblem("unknown origin", origin=str(payload.get("origin"))[:32]) from None
    env = ctx.settings.reva_env
    assert_publishable(origin, env)
    force = bool(payload.get("force", False))
    eng = ctx.worker_engine
    with eng.connect() as lock_conn:
        got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": PUBLISH_JOB_LOCK}).scalar_one()
        lock_conn.commit()
        if not got:
            raise PublicationBusy("another belief publication is in progress")
        try:
            with eng.connect() as conn:
                dirty = dirty_belief_nodes(conn)
            if dirty == 0 and not force:
                return {"status": "up_to_date", "origin": origin.value, "dirty_nodes": 0}
            if ctx.is_cancelled():  # also re-verifies the lease right before publishing
                raise JobCancelled("cancelled before publication")
            # R2-14: a non-locking lease check. ``ctx.assert_lease`` would hold the job row
            # lock for the whole (possibly long) publication transaction and block the
            # heartbeat that keeps the lease alive. Publication is idempotent (it clears
            # the dirty nodes it covers) and serialised by advisory locks, and the
            # worker's completion afterwards is fenced.
            rr = publish_release(
                eng,
                ctx.store,
                cutoff=None,
                origin=origin,
                environment=env,
                actor="worker:publish_beliefs",
                fence=ctx.check_lease,
            )
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PUBLISH_JOB_LOCK})
            lock_conn.commit()
    return {
        "status": "published",
        "release_id": str(rr.release_id),
        "context_id": str(rr.context_id),
        "sequence": rr.sequence,
        "estimates": rr.estimates,
        "manifest_sha256": rr.manifest_sha256,
        "knowledge_cutoff": rr.knowledge_cutoff.isoformat(),
        "origin": origin.value,
        "dirty_nodes_before": dirty,
    }


# --------------------------------------------------------------------------- health
def handle_health(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    from reva.operations.health import append_health, connector_health_vector

    connectors = _all_connectors(ctx)
    names = [payload["connector"]] if payload.get("connector") else sorted(connectors)
    origin = payload.get("origin") or ("live" if ctx.settings.reva_env == "production" else None)
    out: dict[str, Any] = {}
    for name in names:
        c = connectors.get(name)
        if c is None:
            raise ValidationProblem("unknown connector", connector=str(name)[:128])
        vec = connector_health_vector(ctx.worker_engine, name, c.describe().cadence_seconds, origin=origin)
        if vec is None:
            out[name] = {"status": "unregistered"}
            continue
        append_health(ctx.worker_engine, name, vec)
        out[name] = {
            "status": vec["status"],
            "lag_cadences": vec["freshness"]["lag_cadences"],
            "success_ratio": vec["success_ratio"],
            "schema_drift": vec["schema_drift"]["drift"],
        }
    return {"status": "recorded", "connectors": out}


# --------------------------------------------------------------------------- not yet wired
def handle_evaluation(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    raise HandlerUnavailable(
        "evaluation jobs are not wired in this build; use replay jobs for backtests",
        job_type="evaluation",
    )


def save_checkpoint(ctx: JobContext, checkpoint: dict[str, Any], engine: Engine | None = None) -> None:
    """Durable, fenced checkpoint for long handlers."""
    from reva.adapters.postgres.queue import to_json

    eng = engine or ctx.worker_engine
    with eng.begin() as conn:
        ctx.assert_lease(conn)
        conn.execute(
            text("UPDATE ops.job SET checkpoint = CAST(:c AS jsonb), updated_at = clock_timestamp() WHERE id = :j"),
            {"c": to_json(checkpoint), "j": ctx.job_id},
        )
    ctx.checkpoint = checkpoint
