"""Scheduler: turns due ops.schedule rows into jobs, exactly once per slot.

One transaction per tick: due schedule rows are locked with
``FOR UPDATE SKIP LOCKED`` (a concurrent scheduler skips them, and after our
commit their ``next_due_at`` is in the future, so it never re-selects them);
the job is inserted with idempotency key ``schedule:{schedule_key}:{due_slot_iso}``
(``ON CONFLICT DO NOTHING`` is the second line of defence); ``next_due_at`` and
``last_slot`` advance in the same transaction.

Cadences come from ``cadence_spec.interval_seconds`` (>= 60 s, enforced by
the table). Missed slots are coalesced: after downtime only the latest due
slot is enqueued (no backlog flood, no minute-by-minute polling of sources).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.adapters.postgres.queue import to_json

log = logging.getLogger("reva.scheduler")

SCHEDULE_JOB_TYPES = frozenset({"ingest", "health", "gaps", "gc"})
SCHEDULE_PRIORITY = -5
MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class ScheduledJob:
    schedule_key: str
    slot: datetime
    job_id: UUID | None
    created: bool
    skipped_reason: str | None = None


def schedule_payload(job_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
    payload = dict(parameters)
    if job_type == "ingest":
        payload.setdefault("mode", "live")
    return payload


def slot_key(schedule_key: str, slot: datetime) -> str:
    return f"schedule:{schedule_key}:{slot.isoformat()}"


def due_slot(next_due_at: datetime, now: datetime, interval: timedelta) -> datetime:
    """Latest slot ``next_due_at + k*interval`` that is <= now (k >= 0)."""
    if now < next_due_at:
        return next_due_at
    k = int((now - next_due_at) // interval)
    return next_due_at + k * interval


def tick(engine: Engine, *, limit: int = 50, actor: str = "scheduler") -> list[ScheduledJob]:
    """Enqueue all due schedules once; safe to run concurrently from many workers."""
    out: list[ScheduledJob] = []
    with engine.begin() as conn:
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        rows = conn.execute(
            text(
                "SELECT schedule_key, job_type, parameters, cadence_spec, next_due_at FROM ops.schedule "
                "WHERE enabled AND next_due_at <= clock_timestamp() ORDER BY next_due_at "
                "LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"n": limit},
        ).mappings().all()
        for r in rows:
            interval = timedelta(seconds=max(MIN_INTERVAL_SECONDS, int(r["cadence_spec"]["interval_seconds"])))
            slot = due_slot(r["next_due_at"], now, interval)
            job_id = None
            created = False
            reason = None
            if r["job_type"] in SCHEDULE_JOB_TYPES:
                payload = schedule_payload(r["job_type"], dict(r["parameters"]))
                jid = conn.execute(
                    text(
                        """INSERT INTO ops.job(job_type, payload, idempotency_key, priority, created_by)
                           VALUES (:t, CAST(:p AS jsonb), :k, :pr, :cb)
                           ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"""
                    ),
                    {"t": r["job_type"], "p": to_json(payload), "k": slot_key(r["schedule_key"], slot),
                     "pr": SCHEDULE_PRIORITY, "cb": f"{actor}:{r['schedule_key']}"[:200]},
                ).scalar_one_or_none()
                created = jid is not None
                if jid is None:
                    jid = conn.execute(text("SELECT id FROM ops.job WHERE idempotency_key = :k"),
                                       {"k": slot_key(r["schedule_key"], slot)}).scalar_one()
                job_id = UUID(str(jid))
            else:
                reason = f"job type {r['job_type']!r} is not schedulable"
                log.warning("schedule %s skipped: %s", r["schedule_key"], reason)
            conn.execute(
                text("UPDATE ops.schedule SET next_due_at = :n, last_slot = :s WHERE schedule_key = :k"),
                {"n": slot + interval, "s": slot, "k": r["schedule_key"]},
            )
            out.append(ScheduledJob(r["schedule_key"], slot, job_id, created, reason))
    for s in out:
        if s.created:
            log.info("scheduled %s for slot %s", s.schedule_key, s.slot.isoformat())
    return out


def next_due(engine: Engine) -> datetime | None:
    with engine.connect() as conn:
        return conn.execute(text("SELECT min(next_due_at) FROM ops.schedule WHERE enabled")).scalar_one()
