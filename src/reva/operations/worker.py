"""Durable worker process: claims leased jobs, heartbeats, runs the scheduler and
outbox invalidation, and drains gracefully on SIGTERM.

Start-up contract (binding spec §12 ``start_worker``):

* never accepts the migration-owner URL: if ``REVA_MIGRATION_DATABASE_URL`` is
  present (process environment or settings) the worker refuses to start in
  production; in local/test it logs a warning, removes it from its environment
  and ignores it;
* waits with bounded exponential retry for the expected schema
  (``alembic_version >= 0007_security``) and runtime roles;
* uses the ``reva_worker`` engine for queue/ingestion/publication and a
  separate ``reva_sim`` engine handed only to simulation-type handlers;
* thread budget from ``reva.settings.threads_budget()`` (explicit budget and
  cgroup quota, never ``os.cpu_count()``), exported to OMP/OpenBLAS/MKL/Polars;
* concurrency ``REVA_WORKER_CONCURRENCY`` (default 1: one active job);
* a heartbeat thread extends leases every ``REVA_JOB_HEARTBEAT_SECONDS`` over a
  dedicated small connection pool whose sessions use a short ``lock_timeout``
  and ``statement_timeout`` (``HEARTBEAT_LOCK_TIMEOUT_MS``/
  ``HEARTBEAT_STATEMENT_TIMEOUT_MS``) and never take advisory locks, so a
  heartbeat never queues behind a long publication; a lock timeout counts as
  "busy, retry next beat" (R2-14);
* SIGTERM/SIGINT: stop claiming, signal running handlers
  (``ctx.shutdown_requested()`` and the generic ``ctx.is_cancelled()`` turn
  true), drain within ``REVA_WORKER_DRAIN_SECONDS`` (default 100 s, below
  Render's 120 s grace). A shutdown is never a cancellation (R2-01): whatever
  the handler raises/returns to stop (``JobCancelled``, ``{"status":
  "cancelled"}``, ``{"status": "interrupted"}``) the job is released back to the
  queue without consuming an attempt, unless the database says a user asked to
  cancel it (status ``cancel_requested``), in which case it ends ``cancelled``.
  Jobs that do not stop in time are recovered by lease expiry.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from reva.adapters.postgres import queue
from reva.adapters.postgres.queue import ClaimedJob
from reva.kernel.errors import ConfigurationError, RevaError, StaleLease
from reva.operations import telemetry as tm
from reva.operations import tracing
from reva.operations.context import JobContext
from reva.operations.jobs import SIM_ENGINE_JOB_TYPES, HandlerUnavailable, JobCancelled, classify_exception
from reva.ports.storage import ArtifactStore
from reva.settings import Settings, threads_budget

log = logging.getLogger("reva.worker")

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "POLARS_MAX_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "RAYON_NUM_THREADS",
)
MIN_SCHEMA_REVISION = 7  # 0007_security
DEFAULT_DRAIN_SECONDS = 100.0
HEARTBEAT_MIN_INTERVAL = 1.0  # rate limit for handler-initiated status checks
HEARTBEAT_LOCK_TIMEOUT_MS = 2000
HEARTBEAT_STATEMENT_TIMEOUT_MS = 2000
# CHAOS-1: presume the lease lost once this fraction of it has elapsed without a successful
# heartbeat, i.e. stop the handler before the DB-clock lease can have expired.
LEASE_SAFETY_FRACTION = 0.1


class SchemaNotReady(RevaError):
    code = "schema_not_ready"
    http_status = 503


# --------------------------------------------------------------------------- start-up checks
def check_environment(settings: Settings, environ: MutableMapping[str, str] | None = None) -> Settings:
    """Refuse (production) or strip (local/test) any migration-owner credential."""
    env = os.environ if environ is None else environ
    present = bool(env.get("REVA_MIGRATION_DATABASE_URL")) or settings.reva_migration_database_url is not None
    if not present:
        return settings
    if settings.reva_env == "production":
        raise ConfigurationError(
            "worker refuses to start: REVA_MIGRATION_DATABASE_URL must not be present in the worker environment"
        )
    log.warning("REVA_MIGRATION_DATABASE_URL is present in a %s worker environment; ignoring it", settings.reva_env)
    env.pop("REVA_MIGRATION_DATABASE_URL", None)
    return settings.model_copy(update={"reva_migration_database_url": None})


def configure_threads(environ: MutableMapping[str, str] | None = None) -> int:
    """Export the explicit CPU budget to numerical libraries (never os.cpu_count())."""
    env = os.environ if environ is None else environ
    n = threads_budget()
    for k in THREAD_ENV_VARS:
        env[k] = str(n)
    return n


def _revision_number(version_num: str) -> int:
    m = re.match(r"(\d+)", version_num)
    return int(m.group(1)) if m else -1


def verify_runtime(conn: Connection, *, expected_role: str = "reva_worker") -> dict[str, Any]:
    role = conn.execute(
        text("SELECT current_user, r.rolsuper, r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user")
    ).one()
    if role[0] != expected_role or role[1] or role[2]:
        raise ConfigurationError(f"worker must connect as {expected_role} without superuser/bypassrls (got {role[0]})")
    revision: str | None = None
    try:
        with conn.begin_nested():
            revision = conn.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one_or_none()
    except DBAPIError:
        revision = None
    if revision is None:  # no read access to alembic_version: use a 0007_security catalogue marker
        marker = conn.execute(
            text("SELECT 1 FROM pg_policies WHERE schemaname = 'meta' AND policyname = 'location_worker_insert'")
        ).scalar_one_or_none()
        revision = "0007_security(marker)" if marker else None
    if revision is None or _revision_number(revision) < MIN_SCHEMA_REVISION:
        raise SchemaNotReady(f"schema revision {revision!r} is older than 0007_security")
    roles: dict[str, bool] = dict(
        conn.execute(
            text("SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname IN ('reva_worker','reva_sim','reva_api')")
        ).all()
    )
    missing = [r for r in ("reva_worker", "reva_sim") if not roles.get(r)]
    if missing:
        raise SchemaNotReady(f"runtime roles not provisioned for login: {missing}")
    fn = conn.execute(
        text("SELECT to_regprocedure('ops.complete_job(uuid,text,bigint,text,jsonb,jsonb)') IS NOT NULL")
    ).scalar_one()
    if not fn:
        raise SchemaNotReady("queue functions are missing")
    return {"schema_revision": revision, "role": role[0]}


def wait_for_schema(
    engine: Engine,
    *,
    attempts: int = 30,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    last: Exception | None = None
    for i in range(attempts):
        try:
            with engine.connect() as conn:
                info = verify_runtime(conn)
            log.info("schema ready: %s", info["schema_revision"])
            return info
        except (OperationalError, SchemaNotReady) as e:
            last = e
            delay = min(max_delay, base_delay * (2**i))
            log.warning(
                "waiting for schema/roles (attempt %d/%d): %s", i + 1, attempts, getattr(e, "message", type(e).__name__)
            )
            sleep(delay)
    raise ConfigurationError(f"schema/runtime roles not ready after {attempts} attempts: {last}")


# --------------------------------------------------------------------------- heartbeat pool
def heartbeat_engine_for(worker_engine: Engine, concurrency: int = 1) -> Engine:
    """A dedicated small pool for heartbeats (R2-14), same credentials as the worker.

    Its sessions carry short lock/statement timeouts so a heartbeat can never queue
    for long behind a transaction holding the job row, and it is never used for
    advisory locks or publication work. Falls back to ``worker_engine`` when the
    URL cannot be reused (e.g. a test double).
    """
    try:
        url = worker_engine.url
        if url.get_backend_name() != "postgresql":
            return worker_engine
        return create_engine(
            url,
            pool_size=1,
            max_overflow=max(1, concurrency),
            pool_timeout=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={
                "options": f"-c lock_timeout={HEARTBEAT_LOCK_TIMEOUT_MS} "
                f"-c statement_timeout={HEARTBEAT_STATEMENT_TIMEOUT_MS}",
                "application_name": "reva-worker-heartbeat",
                "connect_timeout": 5,
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        log.warning("dedicated heartbeat pool unavailable (%s); using the worker pool", type(e).__name__)
        return worker_engine


# --------------------------------------------------------------------------- worker
@dataclass
class _Active:
    job: ClaimedJob
    thread: threading.Thread | None = None
    status: str = "running"
    lost: bool = False
    last_hb: float = field(default_factory=time.monotonic)
    # Monotonic time at which the last *successful* lease extension was sent (the claim, then
    # each heartbeat): the DB-clock lease cannot expire before lease_basis + lease_seconds.
    lease_basis: float | None = None
    # Monotonic time of the last heartbeat attempt, successful or not (rate limit, CHAOS-2).
    last_attempt: float = 0.0

    def __post_init__(self) -> None:
        if self.lease_basis is None:
            self.lease_basis = getattr(self.job, "claimed_monotonic", None) or time.monotonic()


@dataclass
class JobOutcome:
    job_id: UUID
    job_type: str
    status: str  # succeeded | failed | dead | queued | cancelled | released | lost_lease | completed_in_handler
    ack_seconds: float
    duration_seconds: float
    error: dict[str, Any] | None = None


class Worker:
    def __init__(
        self,
        settings: Settings,
        worker_engine: Engine,
        store: ArtifactStore,
        sim_engine: Engine | None = None,
        *,
        worker_id: str | None = None,
        concurrency: int | None = None,
        lease_seconds: int | None = None,
        heartbeat_seconds: float | None = None,
        poll_interval: float = 0.25,
        drain_seconds: float | None = None,
        scheduler: bool = True,
        scheduler_tick_seconds: float | None = None,
        invalidation: bool = True,
        invalidation_tick_seconds: float = 5.0,
        job_types: list[str] | None = None,
        backoff_base_seconds: float = queue.DEFAULT_BACKOFF_BASE_SECONDS,
        heartbeat_engine: Engine | None = None,
    ) -> None:
        self.settings = settings
        self.engine = worker_engine
        self.sim_engine = sim_engine
        self.store = store
        self.worker_id = worker_id or f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.concurrency = concurrency or settings.reva_worker_concurrency
        self._own_hb_engine = heartbeat_engine is None
        self.heartbeat_engine = heartbeat_engine or heartbeat_engine_for(worker_engine, self.concurrency)
        self.lease_seconds = lease_seconds or settings.reva_job_lease_seconds
        self.heartbeat_seconds = heartbeat_seconds or settings.reva_job_heartbeat_seconds
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ConfigurationError("heartbeat interval must be shorter than the lease")
        self.poll_interval = poll_interval
        env_drain = os.environ.get("REVA_WORKER_DRAIN_SECONDS")
        self.drain_seconds = (
            drain_seconds if drain_seconds is not None else (float(env_drain) if env_drain else DEFAULT_DRAIN_SECONDS)
        )
        self.scheduler = scheduler
        self.scheduler_tick = scheduler_tick_seconds or float(settings.reva_scheduler_tick_seconds)
        self.invalidation = invalidation
        self.invalidation_tick = invalidation_tick_seconds
        self.job_types = job_types
        self.backoff_base = backoff_base_seconds
        self._stop = threading.Event()  # no more claiming
        self._shutdown = threading.Event()  # handlers asked to stop/checkpoint
        self._hb_stop = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[UUID, _Active] = {}
        self._hb_thread: threading.Thread | None = None
        self.outcomes: list[JobOutcome] = []
        self.ack_latencies: list[float] = []
        self.stop_reason: str | None = None

    # ------------------------------------------------------------------ control
    def request_stop(self, reason: str = "requested") -> None:
        if not self._stop.is_set():
            log.info("worker stopping (%s): no new claims; signalling running handlers", reason)
        self.stop_reason = self.stop_reason or reason
        self._stop.set()
        self._shutdown.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def active_jobs(self) -> list[UUID]:
        with self._lock:
            return list(self._active)

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            self.request_stop(signal.Signals(signum).name)

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    # ------------------------------------------------------------------ heartbeats
    def _start_heartbeats(self) -> None:
        if self._hb_thread is None or not self._hb_thread.is_alive():
            self._hb_stop.clear()
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="reva-heartbeat", daemon=True)
            self._hb_thread.start()

    def _presence(self) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO ops.worker_presence(worker_id) VALUES (:w) ON CONFLICT (worker_id) "
                        "DO UPDATE SET last_seen = clock_timestamp()"
                    ),
                    {"w": self.worker_id},
                )
        except Exception as e:  # presence is advisory; never kill the worker over it
            log.warning("worker presence update failed: %s", type(e).__name__)

    def _heartbeat_loop(self) -> None:
        self._presence()
        while not self._hb_stop.wait(self.heartbeat_seconds):
            self._presence()
            with self._lock:
                states = [s for s in self._active.values() if not s.lost]
            for st in states:
                try:
                    self._beat(st, None)
                except StaleLease:
                    pass  # recorded on the state; the handler learns it at its next check

    def _beat(self, st: _Active, progress: dict[str, Any] | None) -> str:
        sent = time.monotonic()
        st.last_attempt = sent
        try:
            status = queue.heartbeat(
                self.heartbeat_engine,
                st.job,
                progress,
                self.lease_seconds,
                lock_timeout_ms=HEARTBEAT_LOCK_TIMEOUT_MS,
                statement_timeout_ms=HEARTBEAT_STATEMENT_TIMEOUT_MS,
            )
        except StaleLease:
            st.lost = True
            log.warning("lease lost for job %s", st.job.id, extra={"job_id": str(st.job.id)})
            raise
        except Exception as e:
            if queue.is_lock_busy(e):
                log.info("heartbeat for job %s busy (row locked); retrying next beat", st.job.id)
            else:
                log.warning("heartbeat failed for job %s: %s", st.job.id, type(e).__name__)
            if self._lease_presumed_lost(st):
                raise StaleLease(
                    f"lease presumed lost for job {st.job.id}: no successful heartbeat "
                    f"within the {self.lease_seconds}s lease"
                ) from e
            return st.status
        st.status = status
        st.last_hb = time.monotonic()
        st.lease_basis = sent
        return status

    def _lease_presumed_lost(self, st: _Active) -> bool:
        """Local, conservative lease deadline (CHAOS-1): a worker that cannot reach the
        database must stop working before its DB-clock lease can have expired, because another
        worker may already have reclaimed the job. The basis is the *send* time of the last
        successful extension, earlier than the DB's ``lease_until`` basis."""
        if st.lost:
            return True
        basis = st.lease_basis if st.lease_basis is not None else st.last_hb
        elapsed = time.monotonic() - basis
        if elapsed >= self.lease_seconds * (1 - LEASE_SAFETY_FRACTION):
            st.lost = True
            log.error(
                "lease for job %s presumed lost: no successful heartbeat for %.1fs (lease %ss)",
                st.job.id,
                elapsed,
                self.lease_seconds,
                extra={"job_id": str(st.job.id)},
            )
        return st.lost

    def _active_state(self, job_id: UUID) -> _Active:
        with self._lock:
            st = self._active.get(job_id)
        if st is None:
            raise StaleLease(f"job {job_id} is not active in this worker")
        if st.lost:
            raise StaleLease(f"lease lost for job {job_id}")
        return st

    def _check_local_lease(self, st: _Active) -> None:
        if self._lease_presumed_lost(st):
            raise StaleLease(f"lease presumed lost for job {st.job.id}")

    def _ctx_heartbeat(self, job_id: UUID, progress: dict[str, Any] | None = None) -> str:
        """Handler heartbeat. Returns ``cancel_requested`` as the generic stop signal for a
        user cancel *or* a worker shutdown (backwards compatible); handlers distinguish the
        two with ``ctx.shutdown_requested()`` / ``ctx.cancel_requested()``."""
        st = self._active_state(job_id)
        status = st.status
        if progress is not None or time.monotonic() - max(st.last_hb, st.last_attempt) >= HEARTBEAT_MIN_INTERVAL:
            status = self._beat(st, progress)  # keeps the lease and the true DB status current
        else:
            self._check_local_lease(st)  # rate-limited: still stop once the local deadline passed
        if self._shutdown.is_set():
            return "cancel_requested"
        return status

    def _ctx_user_cancel(self, job_id: UUID) -> bool:
        """True only if the job row's DB status is ``cancel_requested`` (never for a shutdown)."""
        st = self._active_state(job_id)
        if time.monotonic() - max(st.last_hb, st.last_attempt) >= 0.2:
            self._beat(st, None)
        else:
            self._check_local_lease(st)
        return st.status == "cancel_requested"

    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    def _release_on_shutdown(
        self, job: ClaimedJob, checkpoint: dict[str, Any] | None, done: Callable[..., JobOutcome], ack: float
    ) -> JobOutcome:
        """Shutdown is not a cancellation: hand the job back without consuming an attempt.

        ``queue.release`` re-reads the status under the row lock, so a real user cancel
        (``cancel_requested``) still ends as ``cancelled``."""
        status = queue.release(self.engine, job, checkpoint, "worker_shutdown")
        return done("released" if status == "queued" else status, None, ack)

    # ------------------------------------------------------------------ execution
    def claim_one(self) -> ClaimedJob | None:
        return queue.claim(self.engine, self.worker_id, self.lease_seconds, job_types=self.job_types)

    def execute(self, job: ClaimedJob) -> JobOutcome:
        """Run one claimed job to a fenced terminal/requeued state (synchronously)."""
        st = _Active(job)
        with self._lock:
            self._active[job.id] = st
        try:
            with tm.log_context(job_id=str(job.id)):
                return self._execute(job, st)
        finally:
            with self._lock:
                self._active.pop(job.id, None)

    def _execute(self, job: ClaimedJob, st: _Active) -> JobOutcome:
        t0 = time.monotonic()
        tm.JOBS_CLAIMED.inc(job_type=job.job_type)

        def done(status: str, error: dict[str, Any] | None = None, ack: float = 0.0) -> JobOutcome:
            dur = time.monotonic() - t0
            tm.JOBS_COMPLETED.inc(job_type=job.job_type, status=status)
            tm.JOB_DURATION.observe(dur, job_type=job.job_type, status=status)
            if job.job_type == "simulation":
                tm.SIM_DURATION.observe(dur, status=status)
                tm.SIM_RSS.set(tm.max_rss_bytes())
            out = JobOutcome(job.id, job.job_type, status, ack, dur, error)
            self.outcomes.append(out)
            log.info("job %s (%s) -> %s in %.3fs", job.id, job.job_type, status, dur)
            return out

        def failed(
            exc: BaseException, ack: float, code: str | None = None, retryable: bool | None = None
        ) -> JobOutcome:
            c, r = classify_exception(exc)
            code, retryable = code or c, r if retryable is None else retryable
            details = getattr(exc, "details", None)
            err = queue.error_document(
                code,
                getattr(exc, "message", None) or str(exc) or type(exc).__name__,
                retryable=retryable,
                attempt=job.attempts,
                exception=type(exc).__name__,
                **({"details": details} if isinstance(details, dict) and details else {}),
            )
            tm.JOBS_FAILED.inc(job_type=job.job_type, status="retry" if retryable else "failed")
            try:
                status = queue.fail(self.engine, job, err, retryable=retryable, backoff_base_seconds=self.backoff_base)
            except StaleLease:
                return done("lost_lease", err, ack)
            return done(status, err, ack)

        try:
            from reva.operations.jobs import resolve_handler

            handler = resolve_handler(job.job_type)
        except HandlerUnavailable as e:
            return failed(e, 0.0, "handler_unavailable", False)
        if job.job_type in SIM_ENGINE_JOB_TYPES and job.job_type == "simulation" and self.sim_engine is None:
            return failed(
                ConfigurationError("simulation credentials (reva_sim) are not configured"),
                0.0,
                "sim_engine_unavailable",
                False,
            )

        def ctx_heartbeat(p: dict[str, Any] | None = None) -> str:
            return self._ctx_heartbeat(job.id, p)

        def ctx_user_cancel() -> bool:
            return self._ctx_user_cancel(job.id)

        ctx = JobContext(
            job_id=job.id,
            job_type=job.job_type,
            worker_id=job.worker_id,
            fencing_token=job.fencing_token,
            attempt=job.attempts,
            settings=self.settings,
            worker_engine=self.engine,
            sim_engine=self.sim_engine if job.job_type in SIM_ENGINE_JOB_TYPES else None,
            store=self.store,
            heartbeat=ctx_heartbeat,
            checkpoint=job.checkpoint,
            shutdown_signal=self._shutdown.is_set,
            cancel_probe=ctx_user_cancel,
        )
        ack = time.monotonic() - job.claimed_monotonic
        self.ack_latencies.append(ack)
        tm.QUEUE_ACK.observe(ack, job_type=job.job_type)
        try:
            if self._shutdown.is_set():
                status = queue.release(self.engine, job, None, "worker_shutdown_before_start")
                return done("released" if status == "queued" else status, None, ack)
            with tracing.span(
                "job.execute", job_type=job.job_type, job_id=str(job.id), attempt=job.attempts, worker_id=self.worker_id
            ):
                result = handler(dict(job.payload), ctx)
        except JobCancelled as e:
            try:
                if self._shutdown.is_set():
                    return self._release_on_shutdown(job, ctx.checkpoint, done, ack)
                queue.finish_cancelled(self.engine, job, {"message": e.message[:500]})
            except StaleLease:
                return done("lost_lease", None, ack)
            return done("cancelled", None, ack)
        except StaleLease:
            return done("lost_lease", None, ack)
        except Exception as e:
            log.warning("job %s failed: %s", job.id, type(e).__name__, exc_info=not isinstance(e, RevaError))
            return failed(e, ack)
        try:
            if ctx.completed_in_handler:
                return done("completed_in_handler", None, ack)
            res = dict(result or {})
            if res.get("status") == "cancelled":
                if self._shutdown.is_set():  # R2-01: a shutdown never ends a job as cancelled
                    return self._release_on_shutdown(job, res.get("checkpoint") or ctx.checkpoint, done, ack)
                queue.finish_cancelled(self.engine, job, {"message": "handler honoured cancellation"})
                return done("cancelled", None, ack)
            if res.get("status") == "interrupted":
                return self._release_on_shutdown(job, res.get("checkpoint"), done, ack)
            queue.complete(self.engine, job, res)
            return done("succeeded", None, ack)
        except StaleLease:
            return done("lost_lease", None, ack)

    def run_job(self, job: ClaimedJob) -> JobOutcome:
        """Execute one already-claimed job in the foreground with background heartbeats
        (used by ``reva demo`` when no worker is running). Honours user cancellation and
        ``request_stop`` exactly like the main loop."""
        self._start_heartbeats()
        try:
            return self.execute(job)
        finally:
            self._hb_stop.set()
            if self._hb_thread is not None:
                self._hb_thread.join(timeout=self.heartbeat_seconds + 5)
            self.close()

    def _spawn(self, job: ClaimedJob) -> None:
        st = _Active(job)

        def run() -> None:
            try:
                with tm.log_context(job_id=str(job.id)):
                    self._execute(job, st)
            except Exception:
                log.exception("unexpected worker error for job %s", job.id)
            finally:
                with self._lock:
                    self._active.pop(job.id, None)

        with self._lock:
            self._active[job.id] = st
        st.thread = threading.Thread(target=run, name=f"reva-job-{str(job.id)[:8]}", daemon=True)
        st.thread.start()

    # ------------------------------------------------------------------ loop
    def run(self, *, stop_when: Callable[[Worker], bool] | None = None) -> bool:
        """Main loop. Returns True if all running jobs drained before the deadline."""
        from reva.operations import invalidation, schedules

        self._start_heartbeats()
        next_sched = next_inv = next_metrics = time.monotonic()
        log.info("worker %s started (concurrency=%d, lease=%ss)", self.worker_id, self.concurrency, self.lease_seconds)
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if self.scheduler and now >= next_sched:
                    try:
                        schedules.tick(self.engine, actor=f"scheduler:{self.worker_id}")
                    except Exception as e:
                        log.warning("scheduler tick failed: %s", type(e).__name__)
                    next_sched = now + self.scheduler_tick
                if self.invalidation and now >= next_inv:
                    try:
                        invalidation.process_outbox(self.engine, environment=self.settings.reva_env)
                    except Exception as e:
                        log.warning("outbox processing failed: %s", type(e).__name__)
                    next_inv = now + self.invalidation_tick
                if now >= next_metrics:
                    try:
                        tm.collect_queue_metrics(self.engine)
                    except Exception as e:
                        log.debug("queue metrics refresh failed: %s", type(e).__name__)
                    next_metrics = now + 60
                claimed = False
                while not self._stop.is_set() and len(self.active_jobs()) < self.concurrency:
                    try:
                        job = self.claim_one()
                    except OperationalError as e:
                        log.warning("claim failed (database unavailable?): %s", type(e).__name__)
                        job = None
                    if job is None:
                        break
                    claimed = True
                    self._spawn(job)
                if stop_when is not None and stop_when(self):
                    self.request_stop("stop condition")
                    break
                if not claimed:
                    self._stop.wait(self.poll_interval)
        finally:
            self.request_stop(self.stop_reason or "loop exit")
            drained = self.drain()
            self._hb_stop.set()
            if self._hb_thread is not None:
                self._hb_thread.join(timeout=self.heartbeat_seconds + 5)
            self.close()
        return drained

    def close(self) -> None:
        """Dispose of the dedicated heartbeat pool (if this worker created it)."""
        if self._own_hb_engine and self.heartbeat_engine is not self.engine:
            self.heartbeat_engine.dispose()

    def drain(self, deadline_seconds: float | None = None) -> bool:
        deadline = time.monotonic() + (self.drain_seconds if deadline_seconds is None else deadline_seconds)
        while True:
            with self._lock:
                threads = [s.thread for s in self._active.values() if s.thread is not None]
            if not threads:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "drain deadline reached with %d running job(s); leases will expire and be re-queued", len(threads)
                )
                return False
            threads[0].join(timeout=min(remaining, 0.5))


# --------------------------------------------------------------------------- entrypoint
def main(argv: list[str] | None = None) -> int:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import load_settings

    tm.configure_logging(os.environ.get("REVA_LOG_LEVEL", "INFO"))
    try:
        settings = check_environment(load_settings(), os.environ)
    except ConfigurationError as e:
        log.error("worker start refused: %s", e.message)
        return 2
    threads = configure_threads()
    log.info("thread budget: %d", threads)
    pool = settings.reva_worker_concurrency + 3  # claim loop + heartbeat + per-job + lock connection
    engine = make_engine("worker", settings.model_copy(update={"reva_db_pool_size": pool, "reva_db_max_overflow": 0}))
    sim_engine = make_engine("sim", settings) if settings.reva_sim_db_password is not None else None
    store = build_store(settings)
    try:
        wait_for_schema(engine)
        from reva.adapters.postgres.schema_contract import assert_schema_contract

        assert_schema_contract(engine, reva_env=settings.reva_env)
    except ConfigurationError as e:
        log.error("worker start refused: %s", e.message)
        return 3
    worker = Worker(settings, engine, store, sim_engine)
    worker.install_signal_handlers()
    drained = worker.run()
    engine.dispose()
    if sim_engine is not None:
        sim_engine.dispose()
    log.info("worker exited (%s); drained=%s", worker.stop_reason, drained)
    return 0 if drained else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
