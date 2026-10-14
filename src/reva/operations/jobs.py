"""Job handler registry and the operations-owned handlers.

``HANDLERS`` maps a job type to a lazily imported ``module:function``. A handler
whose module/function cannot be imported produces a *failed* job with error
code ``handler_unavailable`` (never a success). Handler contract:
``handle(payload: dict, ctx: JobContext) -> dict`` (see ``operations.context``).

Cooperative cancellation/shutdown: handlers call ``ctx.is_cancelled()`` at safe
points and either raise :class:`JobCancelled` or return
``{"status": "cancelled"}``; during worker shutdown they may instead return
``{"status": "interrupted", "checkpoint": {...}}`` to be re-queued from the
checkpoint without consuming an attempt.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable
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
from reva.operations.context import JobContext

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
}

# Job types that receive the separate reva_sim engine (numerical/simulation
# outputs only). Ingestion/publication/ops handlers get ctx.sim_engine = None.
SIM_ENGINE_JOB_TYPES = frozenset({"simulation", "replay", "evaluation", "population"})

PUBLISH_JOB_LOCK = 7262021
PUBLISH_PRIORITY = 5


class HandlerUnavailable(RevaError):
    """The job type's handler is not installed/wired in this build."""

    code = "handler_unavailable"
    http_status = 501


class JobCancelled(RevaError):
    code = "cancelled"
    http_status = 409


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
    ValidationProblem, CapabilityGap, MissingInput, IncompatibleSemantics, LeakageBlocked,
    ImmutableRecordViolation, PolicyDenied, ConfigurationError, UnsafeRequest, IntegrityFailure,
    HandlerUnavailable,
)


def resolve_handler(job_type: str) -> Callable[[dict[str, Any], JobContext], dict[str, Any]]:
    path = HANDLERS.get(job_type)
    if path is None:
        raise HandlerUnavailable(f"no handler registered for job type {job_type!r}", job_type=job_type)
    module_name, _, func_name = path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise HandlerUnavailable(f"handler module {module_name} is unavailable: {e}", job_type=job_type,
                                 handler=path) from e
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
    return int(conn.execute(
        text("SELECT COALESCE(max(id), 0) FROM ops.outbox WHERE event_type = 'evidence.observations_changed'")
    ).scalar_one())


def enqueue_publish_beliefs(conn: Connection, origin: str, through_outbox_id: int, *, created_by: str) -> Any:
    """Idempotent publish_beliefs job for (origin, through_outbox_id)."""
    from reva.adapters.postgres.queue import enqueue, idempotency_key

    payload = {"origin": origin, "through_outbox_id": int(through_outbox_id)}
    return enqueue(conn, "publish_beliefs", payload, idempotency_key("publish_beliefs", payload),
                   priority=PUBLISH_PRIORITY, created_by=created_by)


def dirty_belief_nodes(conn: Connection) -> int:
    return int(conn.execute(
        text("SELECT count(*) FROM ops.dirty_node WHERE node_key LIKE 'belief:%' "
             "AND reason NOT LIKE 'materiality_skip%'")
    ).scalar_one())


# --------------------------------------------------------------------------- ingest
def _all_connectors(ctx: JobContext) -> dict[str, Any]:
    """Reviewed built-in connectors plus declarative connectors promoted by the gate."""
    from reva.adapters.connectors.registry import builtin_connectors

    connectors: dict[str, Any] = dict(builtin_connectors(ctx.settings.reva_http_contact_email))
    try:
        from reva.autonomy.jobs import promoted_connectors

        connectors.update(promoted_connectors(ctx.worker_engine, ctx.store))
    except Exception as e:
        log.warning("promoted connectors unavailable: %s", type(e).__name__)
    return connectors


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
        raise ValidationProblem("unknown connector", connector=str(name)[:128], known=sorted(connectors))
    spec = connector.describe()
    production = ctx.settings.reva_env == "production"
    if mode == "fixture" and production:
        raise PolicyDenied("fixture ingestion is not allowed in production")
    if capture and (mode != "live" or production):
        raise PolicyDenied("fixture capture is only available for live runs outside production")
    if ctx.is_cancelled():
        raise JobCancelled("cancelled before acquisition started")

    http: Any
    if mode == "fixture":
        http = FixtureHttp(spec.name)
    else:
        client = make_live_client(spec, ctx.settings)
        http = RecordingHttp(client, spec.name, spec.licence) if capture else client

    async def _run() -> Any:
        try:
            return await run_connector(connector, http, ctx.store, ctx.worker_engine,
                                       persist_cursor=(mode == "live"), post_publish=post_publish_for(spec.template),
                                       fence=ctx.assert_lease)
        finally:
            await http.aclose()

    report = asyncio.run(_run())
    if capture:
        http.write_manifest()

    counts: dict[str, int] = {}
    for o in report.outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
        INGEST_REQUESTS.inc(connector=spec.name, status=o.status)
    shas = sorted({o.raw_sha256 for o in report.outcomes if o.raw_sha256})
    origin = "test_fixture" if mode == "fixture" else "live"
    publish_job = None
    with ctx.worker_engine.begin() as conn:
        if shas:
            nbytes = conn.execute(text("SELECT COALESCE(sum(byte_size), 0) FROM meta.artifact WHERE sha256 = ANY(:s)"),
                                  {"s": shas}).scalar_one()
            INGEST_BYTES.inc(float(nbytes), connector=spec.name)
        if report.inserted > 0 and not (production and origin != "live"):
            publish_job = enqueue_publish_beliefs(conn, origin, latest_observation_event(conn),
                                                  created_by=f"job:{ctx.job_id}")
    result = {
        "connector": spec.name,
        "connector_version": spec.version,
        "mode": mode,
        "origin": origin,
        "inserted": report.inserted,
        "unchanged": report.unchanged,
        "relations": report.relations,
        "outcomes": counts,
        "errors": report.errors[:10],
        "cursor_advanced": report.cursor_advanced,
        "raw_sha256": shas[:50],
        "publish_job_id": str(publish_job) if publish_job else None,
        "status": "partial" if report.errors else "complete",
    }
    if report.outcomes and all(o.status == "failed" for o in report.outcomes):
        raise IngestFailed(f"all {len(report.outcomes)} acquisition items failed for {spec.name}",
                           retryable=(mode == "live"), errors=report.errors[:5])
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
                cutoff = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            if dirty == 0 and not force:
                return {"status": "up_to_date", "origin": origin.value, "dirty_nodes": 0}
            if ctx.is_cancelled():  # also re-verifies the lease right before publishing
                raise JobCancelled("cancelled before publication")
            rr = publish_release(eng, ctx.store, cutoff=cutoff, origin=origin, environment=env,
                                 actor="worker:publish_beliefs", fence=ctx.assert_lease)
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
        out[name] = {"status": vec["status"], "lag_cadences": vec["freshness"]["lag_cadences"],
                     "success_ratio": vec["success_ratio"], "schema_drift": vec["schema_drift"]["drift"]}
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
        conn.execute(text("UPDATE ops.job SET checkpoint = CAST(:c AS jsonb), updated_at = clock_timestamp() "
                          "WHERE id = :j"), {"c": to_json(checkpoint), "j": ctx.job_id})
    ctx.checkpoint = checkpoint
