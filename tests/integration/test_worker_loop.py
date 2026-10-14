"""The real Worker loop end to end on a fresh database: fixture ingest ->
publish_beliefs enqueued and executed -> exactly one belief release; then a
SIGTERM stops claiming, signals the running handler and drains in time."""

from __future__ import annotations

import os
import signal
import statistics
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres import queue
from reva.adapters.postgres.engine import make_engine
from reva.operations import jobs
from reva.operations.worker import Worker
from reva.settings import Settings

pytestmark = pytest.mark.db
REPO = Path(__file__).resolve().parents[2]


@dataclass
class FreshDB:
    settings: Settings
    owner: Engine
    worker: Engine
    sim: Engine
    store: FilesystemArtifactStore


def fresh_database(tmp_path_factory: pytest.TempPathFactory, label: str) -> Iterator[FreshDB]:
    base = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"reva_test_{label}_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp(f"objects_{label}")
    data = base.model_dump()
    data.update(reva_migration_database_url=base.migration_url().set(database=name).render_as_string(
        hide_password=False), reva_db_name=name, reva_env="test", reva_object_root=root)
    settings = Settings.model_validate(data)
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    for attempt in range(3):
        try:
            bootstrap(settings)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
    pooled = settings.model_copy(update={"reva_db_pool_size": 4, "reva_db_max_overflow": 0})
    d = FreshDB(settings, make_engine("migration", settings), make_engine("worker", pooled),
                make_engine("sim", settings), store)
    seed(d.worker, store)
    try:
        yield d
    finally:
        for e in (d.owner, d.worker, d.sim):
            e.dispose()
        if not os.environ.get("REVA_KEEP_TEST_DB"):
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def wdb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FreshDB]:
    yield from fresh_database(tmp_path_factory, "worker")


def _enqueue(d: FreshDB, job_type: str, payload: dict, **kw) -> uuid.UUID:
    with d.worker.begin() as c:
        return queue.enqueue(c, job_type, payload, queue.idempotency_key(job_type, payload), **kw)


def _run_in_thread(w: Worker, **kw) -> tuple[threading.Thread, dict]:
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("drained", w.run(**kw)), name="worker-loop", daemon=True)
    t.start()
    return t, out


def test_worker_loop_fixture_ingest_publishes_one_release(wdb: FreshDB) -> None:
    with wdb.owner.begin() as c:
        c.execute(text("UPDATE ops.schedule SET enabled = false"))
    jid = _enqueue(wdb, "ingest", {"connector": "worldbank.wdi.indicators", "mode": "fixture"})

    def finished(_w: Worker) -> bool:
        with wdb.owner.connect() as c:
            releases = c.execute(text("SELECT count(*) FROM belief.release")).scalar()
            pending = c.execute(text("SELECT count(*) FROM ops.job WHERE status IN "
                                     "('queued','running','cancel_requested')")).scalar()
            unprocessed = c.execute(text("SELECT count(*) FROM ops.outbox WHERE processed_at IS NULL")).scalar()
        return releases >= 1 and pending == 0 and unprocessed == 0

    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, lease_seconds=60, heartbeat_seconds=2,
               poll_interval=0.05, scheduler=True, invalidation=True, invalidation_tick_seconds=0.5)
    t, out = _run_in_thread(w, stop_when=finished)
    t.join(180)
    if t.is_alive():
        w.request_stop("test timeout")
        t.join(110)
    assert out.get("drained") is True
    with wdb.owner.connect() as c:
        rows = c.execute(text("SELECT job_type, status, result_ref FROM ops.job ORDER BY created_at")).all()
        releases = c.execute(text("SELECT id, origin FROM belief.release")).all()
        dirty = c.execute(text("SELECT count(*) FROM ops.dirty_node WHERE node_key LIKE 'belief:%'")).scalar()
    ingest = [r for r in rows if r.job_type == "ingest"]
    publish = [r for r in rows if r.job_type == "publish_beliefs"]
    assert [r.status for r in ingest] == ["succeeded"] and ingest[0].result_ref["inserted"] == 200
    assert ingest[0].result_ref["publish_job_id"] is not None
    assert publish and all(r.status == "succeeded" for r in publish)
    assert sum(1 for r in publish if r.result_ref["status"] == "published") == 1
    assert len(releases) == 1 and releases[0].origin == "test_fixture"
    assert dirty == 0
    assert queue.job_row(wdb.owner, jid)["status"] == "succeeded"
    acks_ms = sorted(a * 1000 for a in w.ack_latencies)
    assert len(acks_ms) >= 2 and max(acks_ms) < 1000  # claim -> handler start well under the 1 s target
    print(f"\nqueue ack latency (claim->handler start) ms: n={len(acks_ms)} min={acks_ms[0]:.2f} "
          f"median={statistics.median(acks_ms):.2f} max={acks_ms[-1]:.2f}")
    outcomes = {o.job_type: o for o in w.outcomes}
    print("job durations s: " + ", ".join(f"{k}={v.duration_seconds:.3f}" for k, v in outcomes.items()))


def test_sigterm_stops_claiming_signals_handler_and_drains(wdb: FreshDB, monkeypatch) -> None:
    started = threading.Event()
    real = jobs.resolve_handler

    def cooperative(payload, ctx):
        started.set()
        step = 0
        while not ctx.is_cancelled():
            step += 1
            time.sleep(0.02)
        return {"status": "interrupted", "checkpoint": {"step": step, "resume": "from-checkpoint"}}

    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: cooperative if jt == "health" else real(jt))
    with wdb.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET status = 'cancelled', worker_id = NULL, lease_until = NULL "
                       "WHERE status IN ('queued','running','cancel_requested')"))
    first = _enqueue(wdb, "health", {"probe": "sigterm-1"}, priority=10)
    second = _enqueue(wdb, "health", {"probe": "sigterm-2"})
    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, concurrency=1, lease_seconds=60,
               heartbeat_seconds=2, poll_interval=0.05, scheduler=False, invalidation=False, drain_seconds=10)
    old_term, old_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    w.install_signal_handlers()
    try:
        t, out = _run_in_thread(w)
        assert started.wait(15)
        t0 = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)
        t.join(30)
        elapsed = time.monotonic() - t0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    assert not t.is_alive() and out["drained"] is True and elapsed < 10
    assert w.stop_reason == "SIGTERM"
    r1, r2 = queue.job_row(wdb.owner, first), queue.job_row(wdb.owner, second)
    assert r1["status"] == "queued" and r1["worker_id"] is None and r1["attempts"] == 0
    assert r1["checkpoint"]["resume"] == "from-checkpoint" and r1["checkpoint"]["step"] > 0
    assert r1["last_error"]["code"] == "released"
    assert r2["status"] == "queued" and r2["attempts"] == 0  # never claimed after SIGTERM


def test_drain_deadline_leaves_uncooperative_job_to_lease_expiry(wdb: FreshDB, monkeypatch) -> None:
    started = threading.Event()

    def stubborn(payload, ctx):
        started.set()
        time.sleep(2.0)  # ignores cancellation
        return {"status": "finished"}

    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: stubborn)
    with wdb.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET status = 'cancelled', worker_id = NULL, lease_until = NULL "
                       "WHERE status IN ('queued','running','cancel_requested')"))
    jid = _enqueue(wdb, "health", {"probe": "stubborn"})
    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, lease_seconds=60, heartbeat_seconds=2,
               poll_interval=0.05, scheduler=False, invalidation=False, drain_seconds=0.3)
    t, out = _run_in_thread(w)
    assert started.wait(15)
    w.request_stop("SIGTERM")
    t.join(10)
    assert out["drained"] is False
    assert queue.job_row(wdb.owner, jid)["status"] == "running"  # recovered later by lease expiry
    deadline = time.monotonic() + 10
    while queue.job_row(wdb.owner, jid)["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert queue.job_row(wdb.owner, jid)["status"] == "succeeded"  # the fenced holder still finished


# --------------------------------------------------------------------------- R2-01 shutdown is not a cancel
def _clear_queue(d: FreshDB) -> None:
    with d.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET status = 'cancelled', worker_id = NULL, lease_until = NULL "
                       "WHERE status IN ('queued','running','cancel_requested')"))
        c.execute(text("UPDATE ops.schedule SET enabled = false"))


def _sim_like(style: str, started: threading.Event):
    """Handlers in the styles found in the code base, all stopping on ctx.is_cancelled()."""

    def handler(payload, ctx):
        started.set()
        deadline = time.monotonic() + 20
        while not ctx.is_cancelled():
            if time.monotonic() > deadline:
                return {"status": "finished"}
            time.sleep(0.02)
        if style == "complete_in":  # old simulation style: finalises the cancellation itself
            with ctx.worker_engine.begin() as conn:
                ctx.complete_in(conn, "cancelled", {"published": False})
            return {"status": "cancelled"}
        if style == "return":  # evaluation style
            return {"status": "cancelled"}
        raise jobs.JobCancelled("stopping at a safe point")

    return handler


@pytest.mark.parametrize("style", ["complete_in", "return", "raise"])
def test_sigterm_releases_job_whatever_the_handler_does(wdb: FreshDB, monkeypatch, style: str) -> None:
    _clear_queue(wdb)
    started = threading.Event()
    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: _sim_like(style, started))
    jid = _enqueue(wdb, "health", {"probe": f"r2-01-{style}"})
    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, lease_seconds=60, heartbeat_seconds=2,
               scheduler=False, invalidation=False)
    job = w.claim_one()
    assert job is not None and job.id == jid
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("o", w.execute(job)))
    t.start()
    assert started.wait(10)
    w.request_stop("SIGTERM")
    t.join(20)
    row = queue.job_row(wdb.owner, jid)
    assert out["o"].status == "released", style
    assert row["status"] == "queued" and row["attempts"] == 0 and row["worker_id"] is None
    assert row["last_error"]["code"] == "released" and row["finished_at"] is None
    w.close()


@pytest.mark.parametrize("style", ["complete_in", "return", "raise"])
def test_user_cancel_still_cancels(wdb: FreshDB, monkeypatch, style: str) -> None:
    _clear_queue(wdb)
    started = threading.Event()
    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: _sim_like(style, started))
    jid = _enqueue(wdb, "health", {"probe": f"r2-01-user-{style}"})
    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, lease_seconds=60, heartbeat_seconds=2,
               scheduler=False, invalidation=False)
    job = w.claim_one()
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("o", w.execute(job)))
    t.start()
    assert started.wait(10)
    with wdb.owner.begin() as c:
        assert c.execute(text("SELECT ops.request_cancel(:j, 'human:tester')"), {"j": jid}).scalar() == \
            "cancel_requested"
    t.join(20)
    row = queue.job_row(wdb.owner, jid)
    assert row["status"] == "cancelled" and row["finished_at"] is not None
    assert out["o"].status in ("cancelled", "completed_in_handler")
    w.close()


def test_user_cancel_during_shutdown_ends_cancelled(wdb: FreshDB, monkeypatch) -> None:
    _clear_queue(wdb)
    started = threading.Event()
    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: _sim_like("return", started))
    jid = _enqueue(wdb, "health", {"probe": "r2-01-both"})
    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, lease_seconds=60, heartbeat_seconds=2,
               scheduler=False, invalidation=False)
    job = w.claim_one()
    assert started.wait(0) is False
    with wdb.owner.begin() as c:
        c.execute(text("SELECT ops.request_cancel(:j, 'human:tester')"), {"j": jid})
    w.request_stop("SIGTERM")
    # handler never even starts (shutdown before start) -> release maps the user cancel to cancelled
    assert w.execute(job).status == "cancelled"
    assert queue.job_row(wdb.owner, jid)["status"] == "cancelled"
    w.close()


def test_context_signals_distinguish_shutdown_from_user_cancel(wdb: FreshDB, monkeypatch) -> None:
    _clear_queue(wdb)
    seen: dict = {}
    started = threading.Event()
    go = threading.Event()

    def handler(payload, ctx):
        seen["before"] = (ctx.shutdown_requested(), ctx.cancel_requested(), ctx.is_cancelled())
        started.set()
        go.wait(10)
        seen["after"] = (ctx.shutdown_requested(), ctx.cancel_requested(), ctx.is_cancelled())
        raise jobs.JobCancelled("stop")

    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: handler)
    jid = _enqueue(wdb, "health", {"probe": "r2-01-signals"})
    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, lease_seconds=60, heartbeat_seconds=2,
               scheduler=False, invalidation=False)
    job = w.claim_one()
    t = threading.Thread(target=lambda: w.execute(job))
    t.start()
    assert started.wait(10)
    w.request_stop("SIGTERM")
    go.set()
    t.join(20)
    assert seen["before"] == (False, False, False)
    assert seen["after"] == (True, False, True)  # stop, but not a user cancel
    assert queue.job_row(wdb.owner, jid)["status"] == "queued"
    w.close()


@pytest.fixture(scope="module")
def sim_baseline(wdb: FreshDB) -> dict:
    import asyncio

    from reva.adapters.connectors.registry import builtin_connectors
    from reva.belief.publication import publish_release
    from reva.evidence.runner import FixtureHttp, run_connector
    from reva.kernel.context import Origin
    from reva.kernel.time import utc_now

    async def ingest() -> None:
        for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
            c = builtin_connectors()[name]
            await run_connector(c, FixtureHttp(c.describe().name), wdb.store, wdb.worker, persist_cursor=False)

    asyncio.run(ingest())
    rel = publish_release(wdb.worker, wdb.store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
    return {"release_id": rel.release_id, "cutoff": rel.knowledge_cutoff}


def test_sigterm_during_real_simulation_requeues_job(wdb: FreshDB, sim_baseline: dict, monkeypatch) -> None:
    import json as _json

    import reva.simulation.jobs as sj
    from reva.simulation.spec import CONTRACTS, validate_scenario_spec

    _clear_queue(wdb)
    spec = _json.loads((CONTRACTS / "scenario.example.json").read_text())
    spec.update(baseline_release_id=str(sim_baseline["release_id"]),
                knowledge_cutoff=sim_baseline["cutoff"].isoformat(), title=f"R2-01 {uuid.uuid4().hex[:6]}")
    s = validate_scenario_spec(spec)
    api = make_engine("api", wdb.settings)
    try:
        with api.begin() as c:
            sid = c.execute(text("SELECT sim.create_scenario(CAST(:s AS jsonb), :b, :d, 'human:test')"),
                            {"s": _json.dumps(s.as_json()), "b": s.canonical_bytes(), "d": s.digest()}).scalar_one()
    finally:
        api.dispose()
    jid = _enqueue(wdb, "simulation", {"scenario_id": str(sid), "fidelity": "L1"})
    w = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, scheduler=False, invalidation=False,
               lease_seconds=60, heartbeat_seconds=5)
    real = sj.plan_run

    def plan_then_sigterm(*a, **k):
        p = real(*a, **k)
        w.request_stop("SIGTERM")  # what the signal handler does mid-run (e.g. a deploy)
        return p

    monkeypatch.setattr(sj, "plan_run", plan_then_sigterm)
    job = w.claim_one()
    assert job is not None and job.id == jid
    out = w.execute(job)
    row = queue.job_row(wdb.owner, jid)
    assert out.status == "released"
    assert row["status"] == "queued" and row["attempts"] == 0 and row["finished_at"] is None
    with wdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM sim.run WHERE job_id = :j"), {"j": jid}).scalar() == 0
    # A fresh worker later runs it to completion.
    monkeypatch.setattr(sj, "plan_run", real)
    w2 = Worker(wdb.settings, wdb.worker, wdb.store, wdb.sim, scheduler=False, invalidation=False,
                lease_seconds=60, heartbeat_seconds=5)
    job2 = w2.claim_one()
    assert job2 is not None and job2.id == jid and job2.attempts == 1
    w2.execute(job2)
    assert queue.job_row(wdb.owner, jid)["status"] == "succeeded"
    w.close()
    w2.close()


# --------------------------------------------------------------------------- R2-16 demo CLI
def test_demo_runs_through_the_worker_path_with_db_clock_cutoff(wdb: FreshDB, sim_baseline: dict,
                                                                 monkeypatch) -> None:
    from reva.cli import demo

    _clear_queue(wdb)
    api = make_engine("api", wdb.settings)
    engines = {"api": api, "worker": wdb.worker, "sim": wdb.sim}
    monkeypatch.setattr(demo, "get_engine", lambda kind: engines[kind])
    monkeypatch.setattr(demo, "get_store", lambda: wdb.store)
    monkeypatch.setattr(demo, "get_settings", lambda: wdb.settings)
    try:
        with pytest.raises(Exception, match="origin"):
            demo.run_demo(scenario="crude-border-price-25", countries=["FRA"], fidelity="L1", origin="bogus")
        # A worker is "running": the job is left queued for it and polled with a timeout.
        queued = demo.run_demo(scenario="crude-border-price-25", countries=["FRA", "USA"], fidelity="L1",
                               origin="test_fixture", execute_locally=False, wait_seconds=0.5)
        assert queued["execution"] == "worker" and queued["job"]["status"] == "queued"
        assert queued["results"] == [] and queued["warnings"]
        # No worker: the demo executes the job itself through the fenced worker path.
        out = demo.run_demo(scenario="crude-border-price-25", countries=["FRA", "USA"], fidelity="L1",
                            origin="test_fixture", execute_locally=True)
        assert out["execution"].startswith("local:") and out["job"]["status"] == "succeeded"
        assert out["run_id"] and out["results"]
        assert out["baseline_release"]["origin"] == "test_fixture"
        cutoff = datetime.fromisoformat(out["knowledge_cutoff"].replace("Z", "+00:00"))
        assert cutoff >= out["baseline_release"]["knowledge_cutoff"]
        with wdb.owner.connect() as c:
            assert c.execute(text("SELECT count(DISTINCT r.run_id) FROM sim.result r WHERE r.run_id = :r"),
                             {"r": out["run_id"]}).scalar() == 1
    finally:
        api.dispose()
