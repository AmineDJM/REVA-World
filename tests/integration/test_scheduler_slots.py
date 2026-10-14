"""Scheduler slot keys are TimeZone-independent (R2-17) and a slot key held by a
foreign job never suppresses the scheduled job (R2-02)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from test_queue_reliability import FreshDB, fresh_database

from reva.adapters.postgres import queue
from reva.operations import schedules

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def sdb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FreshDB]:
    yield from fresh_database(tmp_path_factory, "sched")


@pytest.fixture(autouse=True)
def _quiet(sdb: FreshDB) -> None:
    with sdb.owner.begin() as c:
        c.execute(text("UPDATE ops.schedule SET enabled = false"))


def _schedule(d: FreshDB, key: str, job_type: str = "health", params: str = "{}") -> datetime:
    with d.owner.begin() as c:
        return c.execute(text(
            """INSERT INTO ops.schedule(schedule_key, job_type, parameters, cadence_spec, next_due_at)
               VALUES (:k, :t, CAST(:p AS jsonb), '{"interval_seconds": 3600}',
                       date_trunc('second', clock_timestamp()) - interval '10 seconds')
               RETURNING next_due_at"""), {"k": key, "t": job_type, "p": params}).scalar_one()


def test_slot_key_is_utc_whatever_the_offset() -> None:
    t = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    kolkata = t.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert schedules.slot_key("k", kolkata) == schedules.slot_key("k", t) == "schedule:k:2026-01-01T12:00:00+00:00"
    with pytest.raises(ValueError):
        schedules.slot_key("k", datetime(2026, 1, 1))
    long_key = schedules.slot_key("x" * 300, t)
    assert len(long_key) <= 200 and long_key.endswith("2026-01-01T12:00:00+00:00")


def test_tick_in_non_utc_session_produces_utc_key(sdb: FreshDB) -> None:
    key = f"tz:{uuid.uuid4().hex[:8]}"
    due = _schedule(sdb, key)
    eng = create_engine(sdb.worker.url, connect_args={"options": "-c TimeZone=Asia/Kolkata"})
    try:
        out = [s for s in schedules.tick(eng) if s.schedule_key == key]
    finally:
        eng.dispose()
    assert len(out) == 1 and out[0].created and out[0].slot.utcoffset() == timedelta(0)
    expected = f"schedule:{key}:{due.astimezone(UTC).isoformat()}"
    assert queue.job_row(sdb.owner, out[0].job_id)["idempotency_key"] == expected


def test_foreign_job_holding_the_slot_key_does_not_suppress_the_schedule(sdb: FreshDB) -> None:
    key = f"conflict:{uuid.uuid4().hex[:8]}"
    due = _schedule(sdb, key, "ingest", '{"connector": "worldbank.wdi.indicators"}')
    slot_key = schedules.slot_key(key, due)
    with sdb.owner.begin() as c:  # e.g. an API-submitted job that pre-claimed the slot key
        foreign = c.execute(text("INSERT INTO ops.job(job_type, payload, idempotency_key, created_by) "
                                 "VALUES ('replay', '{}', :k, 'api:attacker') RETURNING id"),
                            {"k": slot_key}).scalar_one()
    out = [s for s in schedules.tick(sdb.worker, actor="scheduler:test") if s.schedule_key == key]
    assert len(out) == 1
    s = out[0]
    assert s.created and s.job_id != foreign and s.conflict is not None
    row = queue.job_row(sdb.owner, s.job_id)
    assert row["job_type"] == "ingest" and row["payload"] == {"connector": "worldbank.wdi.indicators",
                                                               "mode": "live"}
    assert row["idempotency_key"] == f"{slot_key}#alt1"
    with sdb.owner.connect() as c:
        audit = c.execute(text("SELECT actor, details FROM ops.audit WHERE action = 'schedule.slot_key_conflict' "
                               "AND target->>'schedule_key' = :k"), {"k": key}).all()
        nxt = c.execute(text("SELECT next_due_at FROM ops.schedule WHERE schedule_key = :k"), {"k": key}).scalar()
    assert len(audit) == 1 and audit[0].actor == "scheduler:test"
    assert audit[0].details["foreign_job_id"] == str(foreign) and audit[0].details["foreign_job_type"] == "replay"
    assert nxt == due + timedelta(hours=1)
    # Re-processing the slot converges on the same disambiguated job, without a second audit row.
    with sdb.owner.begin() as c:
        c.execute(text("UPDATE ops.schedule SET next_due_at = last_slot WHERE schedule_key = :k"), {"k": key})
    again = [s for s in schedules.tick(sdb.worker, actor="scheduler:test") if s.schedule_key == key]
    assert again[0].job_id == s.job_id and not again[0].created
    with sdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ops.audit WHERE action = 'schedule.slot_key_conflict' "
                              "AND target->>'schedule_key' = :k"), {"k": key}).scalar() == 1


def test_one_broken_schedule_does_not_block_the_others(sdb: FreshDB, monkeypatch) -> None:
    good, bad = f"ok:{uuid.uuid4().hex[:8]}", f"bad:{uuid.uuid4().hex[:8]}"
    _schedule(sdb, good)
    bad_due = _schedule(sdb, bad, "health", '{"probe": "boom"}')
    real = schedules.schedule_payload

    def payload(job_type, params):
        if params.get("probe") == "boom":
            raise RuntimeError("broken schedule parameters")
        return real(job_type, params)

    monkeypatch.setattr(schedules, "schedule_payload", payload)
    out = {s.schedule_key: s for s in schedules.tick(sdb.worker)}
    assert out[good].created and out[good].job_id is not None
    assert out[bad].job_id is None and (out[bad].skipped_reason or "").startswith("error")
    with sdb.owner.connect() as c:  # the broken one is retried next tick (not advanced)
        assert c.execute(text("SELECT next_due_at FROM ops.schedule WHERE schedule_key = :k"),
                         {"k": bad}).scalar() == bad_due
