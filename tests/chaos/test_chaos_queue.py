"""Real-process fault injection for the durable queue/worker (throwaway database).

Workers are separate OS processes running the production ``worker.main()`` (via
``_chaos_worker.py``, which only adds a test-only ``chaos_slow`` job type and delay
hooks) or the real ``reva worker`` CLI. Leases are 10 s (the settings minimum),
heartbeats 1 s, drain grace 5 s.
"""

from __future__ import annotations

import json
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from reva.adapters.postgres import queue
from reva.adapters.postgres.queue import ClaimedJob
from reva.kernel.errors import StaleLease

from ._harness import (
    DRAIN_SECONDS,
    LEASE_SECONDS,
    TERMINAL,
    ChaosDB,
    WorkerProc,
    clear_queue,
    db_now,
    enqueue,
    events,
    fresh_db,
    job,
    start_worker,
    terminate_worker_backends,
    wait_until,
)

pytestmark = [pytest.mark.db, pytest.mark.chaos]


@pytest.fixture(scope="module")
def cdb(tmp_path_factory: pytest.TempPathFactory):
    yield from fresh_db(tmp_path_factory, "queue")


@pytest.fixture()
def procs():
    started: list[WorkerProc] = []
    yield started
    for p in started:
        p.stop()


@pytest.fixture(scope="module")
def sim_baseline(cdb: ChaosDB) -> dict[str, Any]:
    import asyncio

    from reva.adapters.connectors.registry import builtin_connectors
    from reva.belief.publication import publish_release
    from reva.evidence.runner import FixtureHttp, run_connector
    from reva.kernel.context import Origin

    async def ingest() -> None:
        for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
            c = builtin_connectors()[name]
            await run_connector(c, FixtureHttp(c.describe().name), cdb.store, cdb.worker, persist_cursor=False)

    asyncio.run(ingest())
    rel = publish_release(cdb.worker, cdb.store, cutoff=None, origin=Origin.test_fixture, environment="test")
    return {"release_id": rel.release_id, "cutoff": rel.knowledge_cutoff}


def create_scenario(d: ChaosDB, baseline: dict[str, Any], label: str) -> uuid.UUID:
    from reva.simulation.spec import CONTRACTS, validate_scenario_spec

    spec = json.loads((CONTRACTS / "scenario.example.json").read_text())
    spec.update(baseline_release_id=str(baseline["release_id"]), knowledge_cutoff=baseline["cutoff"].isoformat(),
                title=f"chaos {label} {uuid.uuid4().hex[:6]}")
    s = validate_scenario_spec(spec)
    with d.api.begin() as c:
        return c.execute(text("SELECT sim.create_scenario(CAST(:s AS jsonb), :b, :dg, 'human:chaos')"),
                         {"s": json.dumps(s.as_json()), "b": s.canonical_bytes(), "dg": s.digest()}).scalar_one()


def sim_runs(d: ChaosDB, jid: uuid.UUID) -> int:
    with d.owner.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM sim.run WHERE job_id = :j"), {"j": jid}).scalar_one())


def seen_since(d: ChaosDB, w: WorkerProc, t: Any) -> bool:
    with d.owner.connect() as c:
        return bool(c.execute(text("SELECT bool_or(last_seen > :t) FROM ops.worker_presence WHERE worker_id LIKE :w"),
                              {"t": t, "w": f"%:{w.pid}:%"}).scalar())


def terminal(d: ChaosDB, jid: uuid.UUID) -> bool:
    return job(d, jid)["status"] in TERMINAL


# =========================================================================== 1. SIGKILL mid-job
def test_sigkill_mid_simulation_reclaimed_after_lease_expiry_and_completed_once(
        cdb: ChaosDB, sim_baseline: dict, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(cdb)
    sid = create_scenario(cdb, sim_baseline, "sigkill")
    payload = {"scenario_id": str(sid), "fidelity": "L1"}
    jid = enqueue(cdb, "simulation", payload)
    a = start_worker(cdb, tmp_path / "a", CHAOS_PLAN_DELAY="60")
    procs.append(a)
    wait_until(lambda: events(tmp_path / "a", "plan_sleep_start"), 40, what="worker A mid-simulation", procs=[a])
    time.sleep(1.5)  # let a heartbeat or two extend the lease
    a.signal(signal.SIGKILL)
    assert a.wait(10) == -signal.SIGKILL
    killed = job(cdb, jid)
    assert killed["status"] == "running" and f":{a.pid}:" in killed["worker_id"] and killed["attempts"] == 1
    token_a, worker_a, lease_a = killed["fencing_token"], killed["worker_id"], killed["lease_until"]

    b = start_worker(cdb, tmp_path / "b", cli=True)  # the real `reva worker` CLI
    procs.append(b)
    first_new: list[Any] = []

    def done() -> bool:
        r = job(cdb, jid)
        if r["fencing_token"] != token_a and not first_new:
            first_new.append(db_now(cdb))
        return r["status"] in TERMINAL

    wait_until(done, 45, interval=0.05, what="reclaim + completion", procs=[b])
    r = job(cdb, jid)
    assert r["status"] == "succeeded", r
    assert r["attempts"] == 2 and r["fencing_token"] > token_a
    assert f":{b.pid}:" in r["worker_id"]
    assert first_new[0] >= lease_a, "reclaimed before the killed holder's DB-clock lease expired"
    assert sim_runs(cdb, jid) == 1  # exactly one published run
    result_before = r["result_ref"]

    # ---- the killed attempt's late writes are rejected by fencing (zombie with the stale token) ----
    from reva.operations.context import JobContext
    from reva.simulation.jobs import handle_simulation

    zombie = JobContext(job_id=jid, job_type="simulation", worker_id=worker_a, fencing_token=token_a, attempt=1,
                        settings=cdb.settings, worker_engine=cdb.worker, sim_engine=cdb.sim, store=cdb.store)
    with pytest.raises(StaleLease):
        handle_simulation(dict(payload), zombie)  # full handler: compute, then fenced publication
    stale = ClaimedJob(id=jid, job_type="simulation", payload=payload, worker_id=worker_a, fencing_token=token_a,
                       attempts=1, max_attempts=5, lease_until=lease_a, claimed_at=lease_a, created_at=lease_a,
                       checkpoint=None, lease_seconds=LEASE_SECONDS, claimed_monotonic=0.0)
    with pytest.raises(StaleLease):
        queue.complete(cdb.worker, stale, {"zombie": True})
    with pytest.raises(StaleLease):
        queue.heartbeat(cdb.worker, stale)
    with pytest.raises(StaleLease):
        queue.release(cdb.worker, stale, None, "zombie")
    with pytest.raises(StaleLease):
        queue.fail(cdb.worker, stale, {"code": "zombie"}, retryable=True)
    with pytest.raises(DBAPIError) as ei, cdb.sim.begin() as c:
        c.execute(text("SELECT ops.complete_job(:j, :w, :t, 'failed', NULL, NULL)"),
                  {"j": jid, "w": worker_a, "t": token_a})
    assert getattr(ei.value.orig, "sqlstate", None) == "RV001"
    after = job(cdb, jid)
    assert after["status"] == "succeeded" and after["result_ref"] == result_before
    assert after["fencing_token"] == r["fencing_token"]
    assert sim_runs(cdb, jid) == 1


def test_sigstop_zombie_wakes_after_reclaim_and_cannot_publish(
        cdb: ChaosDB, sim_baseline: dict, tmp_path: Path, procs: list[WorkerProc]) -> None:
    """A *live* zombie: worker A is frozen (SIGSTOP) mid-job, its lease expires, B completes
    the job, then A resumes and runs into the fence instead of publishing a second run."""
    clear_queue(cdb)
    sid = create_scenario(cdb, sim_baseline, "zombie")
    jid = enqueue(cdb, "simulation", {"scenario_id": str(sid), "fidelity": "L1"})
    a = start_worker(cdb, tmp_path / "a", CHAOS_PLAN_DELAY="3")
    procs.append(a)
    wait_until(lambda: events(tmp_path / "a", "plan_sleep_start"), 40, what="worker A mid-simulation", procs=[a])
    a.signal(signal.SIGSTOP)
    token_a = job(cdb, jid)["fencing_token"]
    b = start_worker(cdb, tmp_path / "b", cli=True)
    procs.append(b)
    try:
        wait_until(lambda: terminal(cdb, jid), 45, interval=0.1, what="B completes the job", procs=[b])
        r = job(cdb, jid)
        assert r["status"] == "succeeded" and r["fencing_token"] > token_a and f":{b.pid}:" in r["worker_id"]
    finally:
        a.signal(signal.SIGCONT)
    wait_until(lambda: events(tmp_path / "a", "plan_sleep_end"), 15, what="zombie resumed", procs=[a])
    wait_until(lambda: "lost_lease" in a.log(), 15, what="zombie learns it lost the lease", procs=[a])
    assert a.alive(), "the zombie worker must survive losing a lease"
    after = job(cdb, jid)
    assert after["status"] == "succeeded" and after["fencing_token"] == r["fencing_token"]
    assert after["result_ref"] == r["result_ref"]
    assert sim_runs(cdb, jid) == 1


# =========================================================================== 2. SIGTERM
def test_sigterm_releases_job_without_cancel_or_attempt_and_exits_within_grace(
        cdb: ChaosDB, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(cdb)
    jid = enqueue(cdb, "chaos_slow", {"steps": 120, "step_seconds": 0.05, "probe": uuid.uuid4().hex})
    w = start_worker(cdb, tmp_path / "w")
    procs.append(w)
    wait_until(lambda: len(events(tmp_path / "w", "step", job=str(jid))) >= 10, 40, what="job running",
               procs=[w])
    t0 = time.monotonic()
    w.signal(signal.SIGTERM)
    rc = w.wait(DRAIN_SECONDS + 10)
    elapsed = time.monotonic() - t0
    assert rc == 0, w.log()[-3000:]  # drained
    assert elapsed < DRAIN_SECONDS, elapsed  # a cooperative handler stops at its next safe point
    r = job(cdb, jid)
    assert r["status"] == "queued" and r["worker_id"] is None and r["lease_until"] is None
    assert r["attempts"] == 0 and r["finished_at"] is None  # not an attempt, not cancelled
    assert r["last_error"]["code"] == "released"
    step = r["checkpoint"]["step"]
    assert step >= 10
    stop = events(tmp_path / "w", "stop", job=str(jid))
    assert stop and stop[0]["shutdown"] is True

    w2 = start_worker(cdb, tmp_path / "w2", cli=False)
    procs.append(w2)
    wait_until(lambda: terminal(cdb, jid), 45, what="resumed job completes", procs=[w2])
    r2 = job(cdb, jid)
    assert r2["status"] == "succeeded" and r2["attempts"] == 1
    assert events(tmp_path / "w2", "start", job=str(jid))[0]["resume_from"] == step


def test_sigterm_with_uncooperative_handler_exits_at_grace_and_lease_recovers(
        cdb: ChaosDB, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(cdb)
    jid = enqueue(cdb, "chaos_slow", {"mode": "stubborn", "stubborn_seconds": 120, "steps": 3,
                                      "step_seconds": 0.05, "probe": uuid.uuid4().hex})
    w = start_worker(cdb, tmp_path / "w")
    procs.append(w)
    wait_until(lambda: events(tmp_path / "w", "start", job=str(jid)), 40, what="job running", procs=[w])
    t0 = time.monotonic()
    w.signal(signal.SIGTERM)
    rc = w.wait(DRAIN_SECONDS + 10)
    elapsed = time.monotonic() - t0
    assert rc == 1, w.log()[-3000:]  # not drained
    assert DRAIN_SECONDS - 0.5 <= elapsed < DRAIN_SECONDS + 3, elapsed
    r = job(cdb, jid)
    assert r["status"] == "running" and r["attempts"] == 1  # left to lease expiry, never cancelled
    w2 = start_worker(cdb, tmp_path / "w2")
    procs.append(w2)
    wait_until(lambda: terminal(cdb, jid), 45, what="recovered by lease expiry", procs=[w2])
    r2 = job(cdb, jid)
    assert r2["status"] == "succeeded" and r2["attempts"] == 2
    assert not events(tmp_path / "w", "finish", job=str(jid))  # the abandoned attempt never finished


# =========================================================================== 3. DB connection loss
def test_backend_terminated_mid_heartbeat_worker_reconnects_and_keeps_lease(
        cdb: ChaosDB, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(cdb)
    jid = enqueue(cdb, "chaos_slow", {"steps": 160, "step_seconds": 0.05, "probe": uuid.uuid4().hex})
    w = start_worker(cdb, tmp_path / "w")
    procs.append(w)
    wait_until(lambda: events(tmp_path / "w", "start", job=str(jid)), 40, what="job running", procs=[w])
    token = job(cdb, jid)["fencing_token"]

    # Freeze the job row so the next heartbeat blocks *inside* ops.heartbeat, then kill that backend.
    killed: list[int] = []
    with cdb.owner.connect() as locker:
        locker.execute(text("SELECT 1 FROM ops.job WHERE id = :j FOR UPDATE"), {"j": jid})

        def blocked() -> list[int]:
            with cdb.owner.connect() as c:
                return [int(p) for p in c.execute(text(
                    "SELECT pid FROM pg_stat_activity WHERE datname = :db AND usename = 'reva_worker' "
                    "AND wait_event_type = 'Lock' AND query LIKE '%ops.heartbeat%'"), {"db": cdb.name}).scalars()]

        pids = wait_until(blocked, 10, interval=0.02, what="a heartbeat blocked on the job row", procs=[w])
        with cdb.owner.connect() as c:
            for p in pids:
                killed.append(int(c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": p}).scalar_one()))
            c.commit()
        locker.rollback()
    assert killed and all(killed)
    # ...and every other connection of the worker (claim loop, presence, idle pool members).
    terminate_worker_backends(cdb)
    cut = db_now(cdb)
    lease_at_cut = job(cdb, jid)["lease_until"]
    wait_until(lambda: job(cdb, jid)["lease_until"] > lease_at_cut or terminal(cdb, jid), 8,
               what="lease extended again after reconnect", procs=[w])
    wait_until(lambda: seen_since(cdb, w, cut), 8, what="presence updated after reconnect", procs=[w])
    wait_until(lambda: terminal(cdb, jid), 30, what="job completes", procs=[w])
    r = job(cdb, jid)
    assert r["status"] == "succeeded" and r["attempts"] == 1 and r["fencing_token"] == token
    assert w.alive()


def test_sustained_db_outage_handler_stops_by_lease_expiry_then_worker_reconnects(
        cdb: ChaosDB, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(cdb)
    jid = enqueue(cdb, "chaos_slow", {"steps": 600, "step_seconds": 0.1, "retry_steps": 5,
                                      "probe": uuid.uuid4().hex})
    w = start_worker(cdb, tmp_path / "w")
    procs.append(w)
    wait_until(lambda: len(events(tmp_path / "w", "step", job=str(jid))) >= 3, 40, what="job running", procs=[w])
    token = job(cdb, jid)["fencing_token"]
    outage = time.time()
    try:
        with cdb.owner.connect() as c:
            c.execute(text(f'REVOKE CONNECT ON DATABASE "{cdb.name}" FROM reva_worker, reva_sim'))
            c.commit()
        terminate_worker_backends(cdb)
        time.sleep(LEASE_SECONDS + 6)
    finally:
        with cdb.owner.connect() as c:
            c.execute(text(f'GRANT CONNECT ON DATABASE "{cdb.name}" TO reva_worker, reva_sim'))
            c.commit()
    restored = db_now(cdb)
    wait_until(lambda: terminal(cdb, jid), 40, what="job recovers after the outage", procs=[w])
    r = job(cdb, jid)
    assert w.alive()
    # reconnected: presence is refreshed by the heartbeat thread once per beat, which can lag a
    # short resumed attempt that finishes within the first second after the re-grant
    wait_until(lambda: seen_since(cdb, w, restored), 8, what="presence updated after reconnect", procs=[w])
    assert r["status"] == "succeeded" and r["fencing_token"] > token
    # Safety: without a lease the old attempt must stop working (lease = last good beat + LEASE_SECONDS).
    late = [e for e in events(tmp_path / "w", "step", token=token) if e["t"] > outage + LEASE_SECONDS + 2]
    assert not late, (f"{len(late)} steps of the expired attempt ran after its lease; "
                      f"last at +{late[-1]['t'] - outage:.1f}s")


# =========================================================================== 4. cancellation
def test_request_cancel_during_simulation_cancels_and_publishes_nothing(
        cdb: ChaosDB, sim_baseline: dict, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(cdb)
    sid = create_scenario(cdb, sim_baseline, "cancel")
    jid = enqueue(cdb, "simulation", {"scenario_id": str(sid), "fidelity": "L1"})
    w = start_worker(cdb, tmp_path / "w", CHAOS_PLAN_DELAY="4")
    procs.append(w)
    wait_until(lambda: events(tmp_path / "w", "plan_sleep_start"), 40, what="simulation computing", procs=[w])
    with cdb.api.begin() as c:  # the API role, as POST /jobs/{id}/cancel does
        assert c.execute(text("SELECT ops.request_cancel(:j, 'human:chaos')"), {"j": jid}).scalar() == \
            "cancel_requested"
    wait_until(lambda: terminal(cdb, jid), 30, what="cancellation", procs=[w])
    r = job(cdb, jid)
    assert r["status"] == "cancelled" and r["finished_at"] is not None
    assert r["result_ref"] == {"scenario_id": str(sid), "published": False}
    assert sim_runs(cdb, jid) == 0
    with cdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM sim.run WHERE scenario_id = :s"), {"s": sid}).scalar() == 0
    # the worker stays healthy and takes the next job
    nxt = enqueue(cdb, "chaos_slow", {"steps": 2, "step_seconds": 0.01, "probe": uuid.uuid4().hex})
    wait_until(lambda: terminal(cdb, nxt), 20, what="next job", procs=[w])
    assert job(cdb, nxt)["status"] == "succeeded"


def test_request_cancel_stops_cooperative_handler_promptly(
        cdb: ChaosDB, tmp_path: Path, procs: list[WorkerProc]) -> None:
    clear_queue(cdb)
    jid = enqueue(cdb, "chaos_slow", {"steps": 400, "step_seconds": 0.05, "probe": uuid.uuid4().hex})
    w = start_worker(cdb, tmp_path / "w")
    procs.append(w)
    wait_until(lambda: len(events(tmp_path / "w", "step", job=str(jid))) >= 5, 40, what="job running", procs=[w])
    with cdb.api.begin() as c:
        c.execute(text("SELECT ops.request_cancel(:j, 'human:chaos')"), {"j": jid})
    t_cancel = time.time()
    wait_until(lambda: terminal(cdb, jid), 15, what="cancellation", procs=[w])
    r = job(cdb, jid)
    assert r["status"] == "cancelled" and r["attempts"] == 1 and r["result_ref"] is None
    late = [e for e in events(tmp_path / "w", "step", job=str(jid)) if e["t"] > t_cancel + 1.5]
    assert not late  # stopped within ~one heartbeat
    assert not events(tmp_path / "w", "finish", job=str(jid))


# =========================================================================== 5. idempotency
def _parallel(n: int, fn) -> list[Any]:
    barrier = threading.Barrier(n)
    out: list[Any] = [None] * n

    def run(i: int) -> None:
        barrier.wait()
        try:
            out[i] = fn(i)
        except Exception as e:  # collected for assertions
            out[i] = e

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


def test_twenty_parallel_submissions_with_one_idempotency_key_create_one_job(cdb: ChaosDB) -> None:
    clear_queue(cdb)
    api = create_engine(cdb.api.url, poolclass=NullPool)  # one real session per submitter
    wrk = create_engine(cdb.worker.url, poolclass=NullPool)
    key = f"chaos-{uuid.uuid4().hex}"
    payload = {"probe": key}
    try:
        def submit(_i: int) -> str:
            with api.begin() as c:
                return str(c.execute(text("SELECT ops.submit_job('health', CAST(:p AS jsonb), :k, 0, 'human:chaos')"),
                                     {"p": json.dumps(payload), "k": key}).scalar_one())

        ids = _parallel(20, submit)
        assert all(isinstance(i, str) for i in ids), [i for i in ids if not isinstance(i, str)]
        assert len(set(ids)) == 1

        # internal producer path (worker role, queue.enqueue) under the same concurrency
        ikey = queue.idempotency_key("health", payload)

        def internal(_i: int) -> str:
            with wrk.begin() as c:
                return str(queue.enqueue(c, "health", payload, ikey))

        iids = _parallel(20, internal)
        assert all(isinstance(i, str) for i in iids), [i for i in iids if not isinstance(i, str)]
        assert len(set(iids)) == 1 and iids[0] != ids[0]  # api: namespace never aliases internal keys

        # same key, different requests racing: exactly one wins, the rest are refused (never aliased)
        ckey = f"chaos-conflict-{uuid.uuid4().hex}"

        def conflicting(i: int) -> str:
            with api.begin() as c:
                return str(c.execute(text("SELECT ops.submit_job('health', CAST(:p AS jsonb), :k, 0, 'human:chaos')"),
                                     {"p": json.dumps({"probe": i}), "k": ckey}).scalar_one())

        cres = _parallel(20, conflicting)
        wins = [r for r in cres if isinstance(r, str)]
        assert len(wins) == 1 and all("reused with a different request" in str(r) for r in cres if r not in wins)
        with cdb.owner.connect() as c:
            rows = c.execute(text("SELECT idempotency_key, count(*) FROM ops.job WHERE idempotency_key IN "
                                  "(:a, :b, :c) GROUP BY 1"),
                             {"a": f"api:human:chaos:health:{key}", "b": ikey,
                              "c": f"api:human:chaos:health:{ckey}"}).all()
        assert sorted(n for _, n in rows) == [1, 1, 1]
    finally:
        api.dispose()
        wrk.dispose()
