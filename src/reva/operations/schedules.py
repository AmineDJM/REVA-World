"""Scheduler: turns due ops.schedule rows into jobs, exactly once per slot.

One transaction per tick: due schedule rows are locked with
``FOR UPDATE SKIP LOCKED`` (a concurrent scheduler skips them, and after our
commit their ``next_due_at`` is in the future, so it never re-selects them);
the job is inserted with idempotency key ``schedule:{schedule_key}:{due_slot_iso}``
(``ON CONFLICT`` is the second line of defence); ``next_due_at`` and
``last_slot`` advance in the same transaction. Each schedule row is processed in
its own savepoint, so one broken schedule never blocks the others (it is retried
on the next tick).

Slots are computed and rendered in UTC (R2-17): the transaction runs with
``TIME ZONE 'UTC'`` and the slot key uses ``slot.astimezone(UTC).isoformat()``,
so two workers whose sessions use different TimeZone settings derive the same
key for the same instant.

Key conflicts (R2-02): if the slot key is already held by a job of a different
type or payload (a foreign job that pre-claimed the key), the scheduler does not
silently treat it as the slot's job. It logs an error, records an
``schedule.slot_key_conflict`` audit row and the
``reva_idempotency_conflicts_total`` metric, and creates the job under a
deterministic disambiguated key (``…#alt{n}``).

Cadences come from ``cadence_spec.interval_seconds`` (>= 60 s, enforced by
the table). Missed slots are coalesced: after downtime only the latest due
slot is enqueued (no backlog flood, no minute-by-minute polling of sources).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.adapters.postgres import queue

log = logging.getLogger("reva.scheduler")

SCHEDULE_JOB_TYPES = frozenset(
    {
        "ingest",
        "health",
        "gaps",
        "gc",
        "gate_followup",  # GATE2
        "atlas_ingest",  # ATLASOPS (V3): budget-metered atlas source runs
        "atlas_snapshot",  # ATLASOPS (V3): daily atlas snapshot (reused when no table changed)
        "links_materialise",  # LINKWIRE (V3): daily link store after the snapshot (reused when inputs unchanged)
    }
)
SCHEDULE_PRIORITY = -5
MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class ScheduledJob:
    schedule_key: str
    slot: datetime
    job_id: UUID | None
    created: bool
    skipped_reason: str | None = None
    conflict: dict[str, Any] | None = None


def schedule_payload(job_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
    payload = dict(parameters)
    if job_type in ("ingest", "atlas_ingest"):
        payload.setdefault("mode", "live")
    return payload


def slot_key(schedule_key: str, slot: datetime) -> str:
    """``schedule:{schedule_key}:{UTC ISO slot}`` (TimeZone-independent; <= 200 chars)."""
    if slot.tzinfo is None:
        raise ValueError("schedule slots must be timezone-aware")
    iso = slot.astimezone(UTC).isoformat()
    key = f"schedule:{schedule_key}:{iso}"
    if len(key) > 200:  # very long schedule keys: stable digest instead of the raw key
        digest = hashlib.sha256(schedule_key.encode()).hexdigest()[:32]
        key = f"schedule:sha256-{digest}:{iso}"
    return key


def due_slot(next_due_at: datetime, now: datetime, interval: timedelta) -> datetime:
    """Latest slot ``next_due_at + k*interval`` that is <= now (k >= 0)."""
    if now < next_due_at:
        return next_due_at
    k = int((now - next_due_at) // interval)
    return next_due_at + k * interval


def _record_conflict(conn: Any, actor: str, schedule_key: str, slot: datetime, conflict: dict[str, Any]) -> None:
    """Surface a foreign holder of a slot key: error log, metric and an audit row."""
    from reva.operations.audit import record
    from reva.operations.telemetry import IDEMPOTENCY_CONFLICTS

    log.error(
        "schedule %s slot %s: idempotency key %s is held by foreign job %s (%s); created the job under %s",
        schedule_key,
        slot.isoformat(),
        conflict["idempotency_key"],
        conflict["foreign_job_id"],
        conflict["foreign_job_type"],
        conflict["disambiguated_key"],
    )
    IDEMPOTENCY_CONFLICTS.inc(job_type=str(conflict.get("job_type")))
    try:
        with conn.begin_nested():
            record(
                conn,
                actor,
                "schedule.slot_key_conflict",
                {"schedule_key": schedule_key, "slot": slot.isoformat()},
                conflict,
            )
    except Exception as e:  # the audit row is best effort; the job itself must still be created
        log.warning("could not audit schedule key conflict: %s", type(e).__name__)


def tick(engine: Engine, *, limit: int = 50, actor: str = "scheduler") -> list[ScheduledJob]:
    """Enqueue all due schedules once; safe to run concurrently from many workers."""
    out: list[ScheduledJob] = []
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        now = now.astimezone(UTC)
        rows = (
            conn.execute(
                text(
                    "SELECT schedule_key, job_type, parameters, cadence_spec, next_due_at FROM ops.schedule "
                    "WHERE enabled AND next_due_at <= clock_timestamp() ORDER BY next_due_at "
                    "LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": limit},
            )
            .mappings()
            .all()
        )
        for r in rows:
            interval = timedelta(seconds=max(MIN_INTERVAL_SECONDS, int(r["cadence_spec"]["interval_seconds"])))
            slot = due_slot(r["next_due_at"].astimezone(UTC), now, interval)
            job_id = None
            created = False
            reason = None
            conflict = None
            try:
                with conn.begin_nested():
                    if r["job_type"] in SCHEDULE_JOB_TYPES:
                        payload = schedule_payload(r["job_type"], dict(r["parameters"]))
                        key = slot_key(r["schedule_key"], slot)
                        res = queue.enqueue_resolving_conflict(
                            conn,
                            r["job_type"],
                            payload,
                            key,
                            SCHEDULE_PRIORITY,
                            created_by=f"{actor}:{r['schedule_key']}"[:200],
                        )
                        conflict = res.conflict
                        if conflict is not None and res.created:
                            _record_conflict(conn, actor, r["schedule_key"], slot, conflict)
                        job_id, created = res.job_id, res.created
                    else:
                        reason = f"job type {r['job_type']!r} is not schedulable"
                        log.warning("schedule %s skipped: %s", r["schedule_key"], reason)
                    conn.execute(
                        text("UPDATE ops.schedule SET next_due_at = :n, last_slot = :s WHERE schedule_key = :k"),
                        {"n": slot + interval, "s": slot, "k": r["schedule_key"]},
                    )
            except Exception as e:  # one broken schedule never blocks the others; retried next tick
                log.error(
                    "schedule %s slot %s failed: %s: %s",
                    r["schedule_key"],
                    slot.isoformat(),
                    type(e).__name__,
                    getattr(e, "message", None) or str(e)[:300],
                )
                out.append(ScheduledJob(r["schedule_key"], slot, None, False, f"error: {type(e).__name__}"))
                continue
            out.append(ScheduledJob(r["schedule_key"], slot, job_id, created, reason, conflict))
    for s in out:
        if s.created:
            log.info("scheduled %s for slot %s", s.schedule_key, s.slot.isoformat())
    return out


def next_due(engine: Engine) -> datetime | None:
    with engine.connect() as conn:
        sql = text("SELECT min(next_due_at) FROM ops.schedule WHERE enabled")
        due: datetime | None = conn.execute(sql).scalar_one()
        return due


def disable_ingest_on_auth_failure(
    conn: Any, connector: str, origin: str, streak: dict[str, Any], *, actor: str
) -> list[str]:
    """Disable the enabled ingest schedules of ``connector`` whose mode matches ``origin``
    (``live`` <-> mode ``live``, ``test_fixture`` <-> mode ``fixture``) after a persistent,
    connector-wide authentication failure. One ``schedule.disabled_auth_failure`` audit row
    per schedule commits with the update; re-enabling is a maintainer action after a
    credential is provisioned (never automatic, never a bypass).
    """
    from reva.operations.audit import record

    mode = "live" if origin == "live" else "fixture"
    keys = list(
        conn.execute(
            text(
                "UPDATE ops.schedule SET enabled = false WHERE enabled AND job_type = 'ingest' "
                "AND parameters->>'connector' = :c AND COALESCE(parameters->>'mode', 'live') = :m "
                "RETURNING schedule_key"
            ),
            {"c": connector, "m": mode},
        ).scalars()
    )
    for key in sorted(keys):
        log.error(
            "schedule %s disabled: %d consecutive runs of %s failed authentication (%s)",
            key,
            streak.get("runs", 0),
            connector,
            streak.get("statuses"),
        )
        record(
            conn,
            actor,
            "schedule.disabled_auth_failure",
            {"schedule_key": key, "connector": connector},
            {
                "origin": origin,
                "consecutive_auth_failure_runs": streak.get("runs"),
                "statuses": streak.get("statuses"),
                "since": str(streak.get("since")),
                "reenable": "maintainer action after credential provisioning (no bypass)",
            },
        )
    return sorted(keys)
