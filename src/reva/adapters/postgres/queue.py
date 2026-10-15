"""Durable PostgreSQL job queue: leases, fencing tokens, retries, dead letters.

Protocol
--------
* ``enqueue`` inserts idempotently: the key is ``job_type`` + a logical hash of
  the immutable inputs (see :func:`idempotency_key`). Re-using a key with a
  different request raises instead of silently returning another job.
* ``claim`` runs ONE short transaction that (1) re-queues expired leases
  (status running/cancel_requested with ``lease_until < clock_timestamp()``;
  jobs already at ``max_attempts`` become ``dead``, a cancel-requested job whose
  holder vanished becomes ``cancelled``), then (2) selects a candidate with
  ``FOR UPDATE SKIP LOCKED``, assigns ``fencing_token = nextval('ops.fencing_seq')``,
  increments ``attempts`` and sets the lease from the *database* clock. It
  commits before returning, so no row lock is held while the handler works.
* ``heartbeat``/``complete``/``fail``/``release``/``save_checkpoint`` go through
  ``ops.assert_lease``/``ops.heartbeat``/``ops.complete_job`` which lock the
  row and verify owner, current fencing token and unexpired lease with the
  database clock. A stale holder therefore cannot commit (``StaleLease``).

Priority and starvation protection (R2-08)
------------------------------------------
Two rules, applied in this order:

1. **Hard wait bound.** Any available job created more than ``max_wait_seconds``
   ago (default 1800 s = 30 min) is *overdue*. Overdue jobs are claimed before
   every non-overdue job, among themselves strictly FIFO (``created_at``, id),
   whatever their priority.
2. **Uncapped linear aging** for the rest: the effective priority is::

       priority + floor(seconds_since_created_at / aging_seconds)

   (one point per ``aging_seconds``, default 60 s, with *no* cap); ties break by
   ``created_at`` then id (FIFO).

Why this bounds waiting: with uncapped aging every job's score grows at the
same rate, so for two jobs the order depends only on ``priority`` and creation
time, never on how long a competing backlog has itself waited. And because
every job reaches the overdue class after ``max_wait_seconds`` and that class
is FIFO, a job is never overtaken by a job created after it once it is overdue:
its wait is bounded by ``max_wait_seconds`` plus the time to drain the overdue
jobs created before it (finite, independent of the arrival rate of new
high-priority work). The older "capped" aging did not have this property: a
backlog of high-priority jobs ages as well, so an old low-priority job could
starve indefinitely.

``created_at`` (not ``available_at``) is the reference, so time spent in retry
backoff counts as waiting.

Retry policy
------------
``fail(retryable=True)`` re-queues with exponential backoff
``min(cap, base * 2**(attempts-1))`` seconds via ``available_at``; once
``attempts >= max_attempts`` the job is dead-lettered (``dead``). Non-retryable
errors finish the job as ``failed`` immediately. Expired leases are re-queued
immediately (the attempt counter still bounds crash loops).

Cancellation: ``queued`` → ``cancelled`` at once; ``running`` →
``cancel_requested``, which the worker surfaces to the handler through
heartbeats (cooperative) and finishes as ``cancelled``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from reva.kernel.errors import StaleLease, ValidationProblem
from reva.kernel.ids import logical_hash

STALE_LEASE_SQLSTATE = "RV001"
DEFAULT_AGING_SECONDS = 60
DEFAULT_MAX_WAIT_SECONDS = 1800
RESERVED_KEY_PREFIXES = ("api:",)  # namespace of ops.submit_job (API-submitted jobs)
LOCK_BUSY_SQLSTATES = frozenset({"55P03", "57014"})  # lock_not_available, query_canceled (timeout)
DEFAULT_BACKOFF_BASE_SECONDS = 5.0
DEFAULT_BACKOFF_CAP_SECONDS = 3600.0
MAX_ERROR_MESSAGE = 2000


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    job_type: str
    payload: dict[str, Any]
    worker_id: str
    fencing_token: int
    attempts: int
    max_attempts: int
    lease_until: datetime
    claimed_at: datetime
    created_at: datetime
    checkpoint: dict[str, Any] | None
    lease_seconds: int
    claimed_monotonic: float
    expired_requeued: int = 0


def idempotency_key(job_type: str, payload: Mapping[str, Any]) -> str:
    """``{job_type}:{logical_hash}`` over the job type and its immutable inputs/spec digests."""
    if not job_type or not job_type.replace("_", "").isalnum():
        raise ValidationProblem("invalid job type", job_type=job_type)
    return f"{job_type}:{logical_hash({'job_type': job_type, 'payload': dict(payload)})}"


def to_json(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, sort_keys=True, default=str, allow_nan=False)


def is_lock_busy(exc: BaseException) -> bool:
    """A lock/statement timeout: the row is busy, not lost (retry later)."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) in LOCK_BUSY_SQLSTATES


def _stale(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == STALE_LEASE_SQLSTATE


def _raise_stale(job_id: UUID, token: int, exc: BaseException) -> None:
    raise StaleLease(f"lease lost for job {job_id} (fencing token {token})", job_id=str(job_id),
                     fencing_token=token) from exc


class KeyConflict(ValidationProblem):
    """An idempotency key is already held by a different job (type or payload differ)."""

    code = "idempotency_key_conflict"


@dataclass(frozen=True)
class EnqueueResult:
    job_id: UUID
    created: bool
    conflict: dict[str, Any] | None = None


def enqueue(
    conn: Connection,
    job_type: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    priority: int = 0,
    max_attempts: int | None = None,
    *,
    created_by: str = "system",
) -> UUID:
    """Insert a job in the caller's transaction; returns the existing id on replay.

    Internal producers may not use the API namespace (``api:``): API-submitted
    keys are namespaced by ``ops.submit_job`` and must never alias internal ones.
    Re-using a key for a different job type/payload raises :class:`KeyConflict`.
    """
    return _enqueue(conn, job_type, payload, idempotency_key, priority, max_attempts, created_by).job_id


def _enqueue(conn: Connection, job_type: str, payload: Mapping[str, Any], idempotency_key: str, priority: int,
             max_attempts: int | None, created_by: str) -> EnqueueResult:
    if not 8 <= len(idempotency_key) <= 200:
        raise ValidationProblem("idempotency key must be 8..200 characters")
    if idempotency_key.startswith(RESERVED_KEY_PREFIXES):
        raise ValidationProblem("idempotency key uses the reserved API namespace", idempotency_key=idempotency_key)
    if not -100 <= priority <= 100:
        raise ValidationProblem("priority must be within -100..100")
    if max_attempts is not None and max_attempts < 1:
        raise ValidationProblem("max_attempts must be >= 1")
    p = to_json(dict(payload))
    jid = conn.execute(
        text(
            """INSERT INTO ops.job(job_type, payload, idempotency_key, priority, max_attempts, created_by)
               VALUES (:t, CAST(:p AS jsonb), :k, :pr, COALESCE(:ma, 5), :cb)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"""
        ),
        {"t": job_type, "p": p, "k": idempotency_key, "pr": priority, "ma": max_attempts, "cb": created_by},
    ).scalar_one_or_none()
    if jid is not None:
        return EnqueueResult(UUID(str(jid)), True)
    row = conn.execute(
        text(
            "SELECT id, job_type = :t AND payload = CAST(:p AS jsonb) AS same FROM ops.job "
            "WHERE idempotency_key = :k"
        ),
        {"t": job_type, "p": p, "k": idempotency_key},
    ).one()
    if not row.same:
        raise KeyConflict("idempotency key reused with a different request", idempotency_key=idempotency_key)
    return EnqueueResult(UUID(str(row.id)), False)


def conflicting_job(conn: Connection, idempotency_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT id, job_type, created_by, status FROM ops.job WHERE idempotency_key = :k"),
        {"k": idempotency_key},
    ).mappings().one_or_none()
    return dict(row) if row else None


MAX_DISAMBIGUATION = 8


def enqueue_resolving_conflict(
    conn: Connection,
    job_type: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    priority: int = 0,
    max_attempts: int | None = None,
    *,
    created_by: str = "system",
) -> EnqueueResult:
    """Like :func:`enqueue`, but a key held by a *foreign* job never suppresses this one.

    Normally returns the job under ``idempotency_key`` (``conflict is None``).
    When the key is held by a job of another type/payload (a collision or a
    pre-claimed key) the job is created under a deterministic disambiguated key
    ``{key}#alt{n}`` (repeated attempts converge on the same job) and
    ``conflict`` describes the foreign holder so the caller can log/audit it.
    """
    try:
        with conn.begin_nested():
            return _enqueue(conn, job_type, payload, idempotency_key, priority, max_attempts, created_by)
    except KeyConflict:
        pass
    foreign = conflicting_job(conn, idempotency_key) or {}
    for n in range(1, MAX_DISAMBIGUATION + 1):
        alt = f"{idempotency_key[:190]}#alt{n}"
        try:
            with conn.begin_nested():
                res = _enqueue(conn, job_type, payload, alt, priority, max_attempts, created_by)
        except KeyConflict:
            continue
        conflict = {"idempotency_key": idempotency_key, "disambiguated_key": alt,
                    "foreign_job_id": str(foreign.get("id")), "foreign_job_type": foreign.get("job_type"),
                    "foreign_created_by": foreign.get("created_by"), "job_type": job_type}
        return EnqueueResult(res.job_id, res.created, conflict)
    raise KeyConflict("idempotency key and all disambiguated keys are held by different requests",
                      idempotency_key=idempotency_key)


REQUEUE_EXPIRED_SQL = """
WITH expired AS (
  SELECT id FROM ops.job
   WHERE status IN ('running','cancel_requested') AND lease_until < clock_timestamp()
   ORDER BY lease_until
   LIMIT 100
   FOR UPDATE SKIP LOCKED)
UPDATE ops.job j SET
  status = CASE WHEN j.status = 'cancel_requested' THEN 'cancelled'
                WHEN j.attempts >= j.max_attempts THEN 'dead'
                ELSE 'queued' END,
  finished_at = CASE WHEN j.status = 'cancel_requested' OR j.attempts >= j.max_attempts
                     THEN clock_timestamp() END,
  last_error = jsonb_build_object('code', 'lease_expired', 'retryable', true, 'attempt', j.attempts,
                                  'expired_worker_id', j.worker_id, 'expired_fencing_token', j.fencing_token,
                                  'message', 'worker lease expired before completion'),
  worker_id = NULL,
  lease_until = NULL,
  available_at = clock_timestamp(),
  updated_at = clock_timestamp()
FROM expired WHERE j.id = expired.id
RETURNING j.id, j.status
"""

CLAIM_SQL = """
WITH cand AS (
  SELECT id FROM ops.job
   WHERE status = 'queued' AND available_at <= clock_timestamp()
     AND (CAST(:types AS text[]) IS NULL OR job_type = ANY(CAST(:types AS text[])))
   ORDER BY (created_at <= clock_timestamp() - make_interval(secs => :max_wait)) DESC,
            CASE WHEN created_at <= clock_timestamp() - make_interval(secs => :max_wait)
                 THEN created_at END ASC,
            (priority + floor(EXTRACT(EPOCH FROM clock_timestamp() - created_at) / :aging)) DESC,
            created_at, id
   LIMIT 1
   FOR UPDATE SKIP LOCKED)
UPDATE ops.job j SET
  status = 'running',
  worker_id = :w,
  lease_until = clock_timestamp() + make_interval(secs => :lease),
  fencing_token = nextval('ops.fencing_seq'),
  attempts = j.attempts + 1,
  updated_at = clock_timestamp()
FROM cand WHERE j.id = cand.id
RETURNING j.id, j.job_type, j.payload, j.fencing_token, j.attempts, j.max_attempts, j.lease_until,
          j.checkpoint, j.created_at, clock_timestamp() AS claimed_at
"""


CLAIM_ONE_SQL = """
UPDATE ops.job j SET
  status = 'running',
  worker_id = :w,
  lease_until = clock_timestamp() + make_interval(secs => :lease),
  fencing_token = nextval('ops.fencing_seq'),
  attempts = j.attempts + 1,
  updated_at = clock_timestamp()
WHERE j.id = (SELECT id FROM ops.job WHERE id = :j AND status = 'queued' AND available_at <= clock_timestamp()
              FOR UPDATE SKIP LOCKED)
RETURNING j.id, j.job_type, j.payload, j.fencing_token, j.attempts, j.max_attempts, j.lease_until,
          j.checkpoint, j.created_at, clock_timestamp() AS claimed_at
"""


def requeue_expired(conn: Connection) -> list[tuple[UUID, str]]:
    rows = conn.execute(text(REQUEUE_EXPIRED_SQL)).all()
    return [(UUID(str(r.id)), r.status) for r in rows]


def claim(
    engine: Engine,
    worker_id: str,
    lease_seconds: int,
    *,
    job_types: Sequence[str] | None = None,
    aging_seconds: int = DEFAULT_AGING_SECONDS,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> ClaimedJob | None:
    """Claim one job in a short transaction that commits (releasing row locks) before returning.

    Ordering: overdue jobs (older than ``max_wait_seconds``) first in FIFO, then
    uncapped aged priority (see module docstring).
    """
    if lease_seconds < 1:
        raise ValidationProblem("lease_seconds must be >= 1")
    if aging_seconds < 1:
        raise ValidationProblem("aging_seconds must be >= 1")
    if max_wait_seconds < 1:
        raise ValidationProblem("max_wait_seconds must be >= 1")
    with engine.begin() as conn:
        expired = requeue_expired(conn)
        row = conn.execute(
            text(CLAIM_SQL),
            {"w": worker_id, "lease": lease_seconds, "types": list(job_types) if job_types else None,
             "aging": aging_seconds, "max_wait": max_wait_seconds},
        ).one_or_none()
    if expired:
        from reva.operations.telemetry import LEASE_EXPIRIES

        LEASE_EXPIRIES.inc(len(expired))
    if row is None:
        return None
    return _claimed(row, worker_id, lease_seconds, len(expired))


def claim_job(engine: Engine, job_id: UUID, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    """Claim one *specific* queued job (same lease/fencing protocol as :func:`claim`).

    Returns ``None`` when the job is not (or no longer) queued and available,
    e.g. because a running worker already claimed it.
    """
    if lease_seconds < 1:
        raise ValidationProblem("lease_seconds must be >= 1")
    with engine.begin() as conn:
        row = conn.execute(text(CLAIM_ONE_SQL), {"j": job_id, "w": worker_id, "lease": lease_seconds}).one_or_none()
    return None if row is None else _claimed(row, worker_id, lease_seconds, 0)


def _claimed(row: Any, worker_id: str, lease_seconds: int, expired: int) -> ClaimedJob:
    return ClaimedJob(
        id=UUID(str(row.id)),
        job_type=row.job_type,
        payload=dict(row.payload),
        worker_id=worker_id,
        fencing_token=int(row.fencing_token),
        attempts=int(row.attempts),
        max_attempts=int(row.max_attempts),
        lease_until=row.lease_until,
        claimed_at=row.claimed_at,
        created_at=row.created_at,
        checkpoint=dict(row.checkpoint) if row.checkpoint is not None else None,
        lease_seconds=lease_seconds,
        claimed_monotonic=time.monotonic(),
        expired_requeued=expired,
    )


def assert_lease(conn: Connection, job: ClaimedJob) -> None:
    try:
        with conn.begin_nested():
            conn.execute(text("SELECT ops.assert_lease(:j, :w, :t)"),
                         {"j": job.id, "w": job.worker_id, "t": job.fencing_token})
    except DBAPIError as e:
        if _stale(e):
            _raise_stale(job.id, job.fencing_token, e)
        raise


def heartbeat(engine: Engine, job: ClaimedJob, progress: Mapping[str, Any] | None = None,
              lease_seconds: int | None = None, *, lock_timeout_ms: int | None = None,
              statement_timeout_ms: int | None = None) -> str:
    """Extend the lease; returns the current status (``running`` or ``cancel_requested``).

    With ``lock_timeout_ms``/``statement_timeout_ms`` the transaction gives up
    quickly when the job row is locked by another transaction; the resulting
    DBAPIError satisfies :func:`is_lock_busy` (busy, retry), never ``StaleLease``.
    No advisory lock is ever taken here.
    """
    try:
        with engine.begin() as conn:
            if lock_timeout_ms is not None:
                conn.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{int(lock_timeout_ms)}ms"})
            if statement_timeout_ms is not None:
                conn.execute(text("SELECT set_config('statement_timeout', :v, true)"),
                             {"v": f"{int(statement_timeout_ms)}ms"})
            return str(conn.execute(
                text("SELECT ops.heartbeat(:j, :w, :t, :s, CAST(:p AS jsonb))"),
                {"j": job.id, "w": job.worker_id, "t": job.fencing_token,
                 "s": lease_seconds or job.lease_seconds, "p": to_json(dict(progress)) if progress else None},
            ).scalar_one())
    except DBAPIError as e:
        if _stale(e):
            _raise_stale(job.id, job.fencing_token, e)
        raise


def _complete(conn: Connection, job: ClaimedJob, status: str, result: Any, error: Any) -> None:
    try:
        with conn.begin_nested():
            conn.execute(
                text("SELECT ops.complete_job(:j, :w, :t, :s, CAST(:r AS jsonb), CAST(:e AS jsonb))"),
                {"j": job.id, "w": job.worker_id, "t": job.fencing_token, "s": status,
                 "r": to_json(result), "e": to_json(error)},
            )
    except DBAPIError as e:
        if _stale(e):
            _raise_stale(job.id, job.fencing_token, e)
        raise


def complete(engine: Engine, job: ClaimedJob, result: Mapping[str, Any] | None,
             conn: Connection | None = None) -> None:
    """Mark succeeded (fenced). Pass ``conn`` to commit together with result references."""
    if conn is not None:
        _complete(conn, job, "succeeded", dict(result) if result is not None else None, None)
        return
    with engine.begin() as c:
        _complete(c, job, "succeeded", dict(result) if result is not None else None, None)


def finish_cancelled(engine: Engine, job: ClaimedJob, details: Mapping[str, Any] | None = None) -> None:
    with engine.begin() as c:
        from reva.operations.telemetry import redact_obj

        _complete(c, job, "cancelled", None, {"code": "cancelled", **redact_obj(dict(details or {}))})


def error_document(code: str, message: str, *, retryable: bool, attempt: int, **extra: Any) -> dict[str, Any]:
    from reva.operations.telemetry import redact_obj, redact_text, secret_values

    return {"code": code[:64], "message": redact_text(message, secret_values())[:MAX_ERROR_MESSAGE],
            "retryable": retryable, "attempt": attempt, **redact_obj(dict(extra))}


def backoff_seconds(attempts: int, base: float = DEFAULT_BACKOFF_BASE_SECONDS,
                    cap: float = DEFAULT_BACKOFF_CAP_SECONDS) -> float:
    return float(min(cap, base * (2 ** max(0, attempts - 1))))


def fail(
    engine: Engine,
    job: ClaimedJob,
    error: Mapping[str, Any],
    *,
    retryable: bool,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
) -> str:
    """Record a failed attempt. Returns the new status: queued | failed | dead | cancelled."""
    err = dict(error)
    with engine.begin() as conn:
        assert_lease(conn, job)  # locks the row and verifies owner/token/lease
        row = conn.execute(
            text("SELECT status, attempts, max_attempts FROM ops.job WHERE id = :j"), {"j": job.id}
        ).one()
        if row.status == "cancel_requested":
            _complete(conn, job, "cancelled", None, {**err, "cancel_requested": True})
            return "cancelled"
        if not retryable:
            _complete(conn, job, "failed", None, err)
            return "failed"
        if row.attempts >= row.max_attempts:
            conn.execute(
                text(
                    "UPDATE ops.job SET status = 'dead', last_error = CAST(:e AS jsonb), lease_until = NULL, "
                    "finished_at = clock_timestamp(), updated_at = clock_timestamp() WHERE id = :j"
                ),
                {"j": job.id, "e": to_json({**err, "dead_letter": True})},
            )
            return "dead"
        delay = backoff_seconds(int(row.attempts), backoff_base_seconds, backoff_cap_seconds)
        conn.execute(
            text(
                "UPDATE ops.job SET status = 'queued', worker_id = NULL, lease_until = NULL, "
                "available_at = clock_timestamp() + make_interval(secs => :d), last_error = CAST(:e AS jsonb), "
                "updated_at = clock_timestamp() WHERE id = :j"
            ),
            {"j": job.id, "d": delay, "e": to_json({**err, "backoff_seconds": delay})},
        )
        return "queued"


def release(engine: Engine, job: ClaimedJob, checkpoint: Mapping[str, Any] | None = None,
            reason: str = "worker_shutdown") -> str:
    """Hand an interrupted job back (graceful shutdown); the attempt is not counted."""
    with engine.begin() as conn:
        assert_lease(conn, job)
        st = conn.execute(text("SELECT status FROM ops.job WHERE id = :j"), {"j": job.id}).scalar_one()
        if st == "cancel_requested":
            _complete(conn, job, "cancelled", None, {"code": "cancelled", "reason": reason})
            return "cancelled"
        conn.execute(
            text(
                "UPDATE ops.job SET status = 'queued', worker_id = NULL, lease_until = NULL, "
                "attempts = GREATEST(attempts - 1, 0), checkpoint = COALESCE(CAST(:c AS jsonb), checkpoint), "
                "available_at = clock_timestamp(), last_error = CAST(:e AS jsonb), updated_at = clock_timestamp() "
                "WHERE id = :j"
            ),
            {"j": job.id, "c": to_json(dict(checkpoint)) if checkpoint else None,
             "e": to_json({"code": "released", "reason": reason, "retryable": True})},
        )
        return "queued"


def save_checkpoint(engine: Engine, job: ClaimedJob, checkpoint: Mapping[str, Any]) -> None:
    with engine.begin() as conn:
        assert_lease(conn, job)
        conn.execute(
            text("UPDATE ops.job SET checkpoint = CAST(:c AS jsonb), updated_at = clock_timestamp() WHERE id = :j"),
            {"j": job.id, "c": to_json(dict(checkpoint))},
        )


def cancel(engine: Engine, job_id: UUID, actor: str) -> str:
    """Request cancellation through ops.request_cancel (granted to reva_api)."""
    with engine.begin() as conn:
        return str(conn.execute(text("SELECT ops.request_cancel(:j, :a)"), {"j": job_id, "a": actor}).scalar_one())


def job_row(engine: Engine, job_id: UUID) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM ops.job WHERE id = :j"), {"j": job_id}).mappings().one_or_none()
    return dict(row) if row else None
