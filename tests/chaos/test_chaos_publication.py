"""Real-process fault injection for belief publication (throwaway database).

* concurrent publish_beliefs jobs on two real ``reva worker`` processes produce exactly
  one release per dirty set;
* a worker SIGKILLed inside the publication transaction leaves no half-published
  release: the release is either absent or sealed (has its ``ops.publication`` row).
"""

from __future__ import annotations

import asyncio
import signal
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.operations.jobs import PUBLISH_JOB_LOCK

from ._harness import (
    TERMINAL,
    ChaosDB,
    WorkerProc,
    clear_queue,
    enqueue,
    events,
    fresh_db,
    job,
    start_worker,
    wait_until,
)

pytestmark = [pytest.mark.db, pytest.mark.chaos]


@pytest.fixture(scope="module")
def pdb(tmp_path_factory: pytest.TempPathFactory):
    yield from fresh_db(tmp_path_factory, "pub")


@pytest.fixture()
def procs():
    started: list[WorkerProc] = []
    yield started
    for p in started:
        p.stop()


def ingest_and_invalidate(d: ChaosDB, connector: str) -> int:
    """Fixture ingest in-process, then one outbox pass: marks belief nodes dirty and enqueues
    the outbox's own publish_beliefs job. Returns the latest observation outbox id."""
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.evidence.runner import FixtureHttp, run_connector
    from reva.operations.invalidation import process_outbox
    from reva.operations.jobs import latest_observation_event

    c = builtin_connectors()[connector]
    rep = asyncio.run(run_connector(c, FixtureHttp(c.describe().name), d.store, d.worker, persist_cursor=False))
    assert rep.inserted > 0
    process_outbox(d.worker, environment="test")
    with d.worker.connect() as conn:
        return latest_observation_event(conn)


def snapshot(d: ChaosDB) -> dict[str, Any]:
    with d.owner.connect() as c:
        return dict(c.execute(text(
            "SELECT (SELECT count(*) FROM belief.release) AS releases, "
            "(SELECT count(*) FROM ops.publication) AS publications, "
            "(SELECT count(*) FROM belief.release r WHERE NOT EXISTS "
            "   (SELECT 1 FROM ops.publication p WHERE p.release_id = r.id)) AS unsealed, "
            "(SELECT count(*) FROM belief.estimate) AS estimates, "
            "(SELECT count(*) FROM ops.dirty_node WHERE node_key LIKE 'belief:%') AS dirty")).mappings().one())


def publish_jobs(d: ChaosDB) -> list[dict[str, Any]]:
    with d.owner.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, status, attempts, result_ref, last_error FROM ops.job WHERE job_type = 'publish_beliefs' "
            "AND status <> 'cancelled' ORDER BY created_at")).mappings()]


def test_concurrent_publish_jobs_on_two_workers_make_one_release(
        pdb: ChaosDB, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(pdb)
    before = snapshot(pdb)
    through = ingest_and_invalidate(pdb, "worldbank.wdi.indicators")
    assert snapshot(pdb)["dirty"] > 0
    outbox_jobs = publish_jobs(pdb)
    assert len(outbox_jobs) == 1  # the invalidation pass enqueued one
    dup = [enqueue(pdb, "publish_beliefs", {"origin": "test_fixture", "through_outbox_id": through,
                                            "chaos_duplicate": i}, priority=5) for i in range(2)]
    ids = {str(j["id"]) for j in outbox_jobs} | {str(j) for j in dup}
    assert len(ids) == 3
    a = start_worker(pdb, tmp_path / "a", cli=True)
    b = start_worker(pdb, tmp_path / "b", cli=True)
    procs += [a, b]

    def all_done() -> bool:
        js = [j for j in publish_jobs(pdb) if str(j["id"]) in ids]
        return len(js) == 3 and all(j["status"] in TERMINAL for j in js)

    wait_until(all_done, 50, interval=0.2, what="all publish jobs terminal", procs=[a, b])
    js = [j for j in publish_jobs(pdb) if str(j["id"]) in ids]
    assert all(j["status"] == "succeeded" for j in js), js
    statuses = sorted(j["result_ref"]["status"] for j in js)
    assert statuses == ["published", "up_to_date", "up_to_date"], js
    after = snapshot(pdb)
    assert after["releases"] - before["releases"] == 1
    assert after["publications"] - before["publications"] == 1
    assert after["unsealed"] == 0 and after["dirty"] == 0
    # any extra job the workers' own outbox passes enqueued must not have published again
    wait_until(lambda: all(j["status"] in TERMINAL for j in publish_jobs(pdb)), 30, what="no pending publish")
    assert snapshot(pdb)["releases"] - before["releases"] == 1


def test_sigkill_inside_publication_transaction_leaves_no_half_published_release(
        pdb: ChaosDB, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(pdb)
    ingest_and_invalidate(pdb, "un.comtrade.public_exact")
    before = snapshot(pdb)
    assert before["dirty"] > 0
    jobs_ = [j for j in publish_jobs(pdb) if j["status"] == "queued"]
    assert len(jobs_) == 1
    jid = jobs_[0]["id"]

    a = start_worker(pdb, tmp_path / "a", CHAOS_PUBLISH_DELAY="60")
    procs.append(a)
    ev = wait_until(lambda: events(tmp_path / "a", "publish_in_txn"), 40, what="worker inside publication",
                    procs=[a])
    rid = uuid.UUID(ev[0]["release_id"])
    with pdb.owner.connect() as c:  # the release exists only inside A's open transaction
        assert c.execute(text("SELECT count(*) FROM belief.release WHERE id = :r"), {"r": rid}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = :k"),
                         {"k": PUBLISH_JOB_LOCK}).scalar() == 1
    a.signal(signal.SIGKILL)
    assert a.wait(10) == -signal.SIGKILL

    def lock_freed() -> bool:
        with pdb.owner.connect() as c:
            return c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = :k"),
                             {"k": PUBLISH_JOB_LOCK}).scalar() == 0

    wait_until(lock_freed, 15, what="the killed backend's locks are released")
    mid = snapshot(pdb)
    assert (mid["releases"], mid["publications"], mid["estimates"]) == \
        (before["releases"], before["publications"], before["estimates"])  # absent, not half-written
    assert mid["unsealed"] == 0 and mid["dirty"] == before["dirty"]  # nothing consumed
    with pdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM belief.estimate WHERE release_id = :r"), {"r": rid}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM ops.outbox WHERE event_type = 'belief.release_published' "
                              "AND payload->>'release_id' = :r"), {"r": str(rid)}).scalar() == 0

    b = start_worker(pdb, tmp_path / "b", cli=True)
    procs.append(b)
    wait_until(lambda: job(pdb, jid)["status"] in TERMINAL, 45, what="publication re-run after lease expiry",
               procs=[b])
    r = job(pdb, jid)
    assert r["status"] == "succeeded" and r["attempts"] == 2 and r["result_ref"]["status"] == "published"
    assert uuid.UUID(r["result_ref"]["release_id"]) != rid
    after = snapshot(pdb)
    assert after["releases"] - before["releases"] == 1 and after["publications"] - before["publications"] == 1
    assert after["unsealed"] == 0 and after["dirty"] == 0
    with pdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM belief.estimate WHERE release_id = :r"),
                         {"r": r["result_ref"]["release_id"]}).scalar() == r["result_ref"]["estimates"] > 0
