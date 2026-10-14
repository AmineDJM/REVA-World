"""Durable queue reliability: idempotency, lock release, leases/fencing, concurrent
claims, scheduler slots, retry/backoff/dead-letter, cancellation, crash windows of
an ingest job, and worker start-up refusals.

Runs against its own fresh database (module scope) so queue/evidence state is
not shared with other test modules.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres import queue
from reva.adapters.postgres.engine import make_engine
from reva.kernel.errors import ConfigurationError, StaleLease, ValidationProblem
from reva.operations import jobs, schedules
from reva.operations.context import JobContext
from reva.operations.worker import Worker, check_environment, configure_threads, verify_runtime, wait_for_schema
from reva.settings import Settings, threads_budget

pytestmark = pytest.mark.db
REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- fresh database
@dataclass
class FreshDB:
    settings: Settings
    owner: Engine
    api: Engine
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
    d = FreshDB(settings, make_engine("migration", settings), make_engine("api", settings),
                make_engine("worker", pooled), make_engine("sim", settings), store)
    seed(d.worker, store)
    try:
        yield d
    finally:
        for e in (d.owner, d.api, d.worker, d.sim):
            e.dispose()
        if not os.environ.get("REVA_KEEP_TEST_DB"):
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def qdb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FreshDB]:
    yield from fresh_database(tmp_path_factory, "queue")


@pytest.fixture(autouse=True)
def _clean_queue(qdb: FreshDB) -> None:
    with qdb.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET status = 'cancelled', worker_id = NULL, lease_until = NULL, "
                       "finished_at = clock_timestamp() WHERE status IN ('queued','running','cancel_requested')"))
        c.execute(text("UPDATE ops.schedule SET enabled = false"))


def _enqueue(d: FreshDB, job_type: str, payload: dict, **kw) -> uuid.UUID:
    with d.worker.begin() as c:
        return queue.enqueue(c, job_type, payload, queue.idempotency_key(job_type, payload), **kw)


def _status(d: FreshDB, jid: uuid.UUID) -> dict:
    row = queue.job_row(d.owner, jid)
    assert row is not None
    return row


def _make_available(d: FreshDB, jid: uuid.UUID) -> None:
    with d.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET available_at = clock_timestamp() WHERE id = :j"), {"j": jid})


def _expire_lease(d: FreshDB, jid: uuid.UUID) -> None:
    with d.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET lease_until = clock_timestamp() - interval '1 second' WHERE id = :j"),
                  {"j": jid})


def _worker(d: FreshDB, **kw) -> Worker:
    kw.setdefault("scheduler", False)
    kw.setdefault("invalidation", False)
    kw.setdefault("lease_seconds", 30)
    kw.setdefault("heartbeat_seconds", 5)
    return Worker(d.settings, d.worker, d.store, d.sim, **kw)


def _ctx(d: FreshDB, job: queue.ClaimedJob) -> JobContext:
    return JobContext(job_id=job.id, job_type=job.job_type, worker_id=job.worker_id,
                      fencing_token=job.fencing_token, attempt=job.attempts, settings=d.settings,
                      worker_engine=d.worker, sim_engine=None, store=d.store,
                      heartbeat=lambda p=None: queue.heartbeat(d.worker, job, p))


# --------------------------------------------------------------------------- enqueue / claim
def test_idempotent_enqueue(qdb: FreshDB) -> None:
    payload = {"connector": "worldbank.wdi.indicators", "mode": "fixture", "spec_sha256": "ab" * 32}
    key = queue.idempotency_key("ingest", payload)
    assert key.startswith("ingest:") and key == queue.idempotency_key("ingest", dict(reversed(payload.items())))
    assert key != queue.idempotency_key("health", payload)
    a = _enqueue(qdb, "ingest", payload)
    b = _enqueue(qdb, "ingest", payload)
    assert a == b
    with qdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ops.job WHERE idempotency_key = :k"), {"k": key}).scalar() == 1
    with pytest.raises(ValidationProblem), qdb.worker.begin() as c:
        queue.enqueue(c, "ingest", {**payload, "mode": "live"}, key)


def test_claim_commits_and_releases_row_locks_before_work(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"probe": "locks"})
    job = queue.claim(qdb.worker, "w-locks", 30)
    assert job is not None and job.id == jid and job.attempts == 1 and job.fencing_token > 0
    # Immediately afterwards another session can lock and update the row: no lock is held.
    with qdb.owner.begin() as c:
        c.execute(text("SET LOCAL lock_timeout = '200ms'"))
        row = c.execute(text("SELECT status, worker_id FROM ops.job WHERE id = :j FOR UPDATE NOWAIT"),
                        {"j": jid}).one()
        assert row.status == "running" and row.worker_id == "w-locks"
        c.execute(text("UPDATE ops.job SET progress = '{\"probe\": 1}' WHERE id = :j"), {"j": jid})
    # Lease comes from the database clock.
    with qdb.owner.connect() as c:
        remaining = c.execute(text("SELECT EXTRACT(EPOCH FROM lease_until - clock_timestamp()) FROM ops.job "
                                   "WHERE id = :j"), {"j": jid}).scalar()
    assert 20 < float(remaining) <= 30
    queue.complete(qdb.worker, job, {"ok": True})
    assert _status(qdb, jid)["status"] == "succeeded"


def test_expired_lease_reclaimed_with_higher_token_and_stale_holder_cannot_commit(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"probe": "fencing"})
    a = queue.claim(qdb.worker, "worker-A", 30)
    assert a is not None and a.id == jid
    _expire_lease(qdb, jid)
    b = queue.claim(qdb.worker, "worker-B", 30)
    assert b is not None and b.id == jid
    assert b.fencing_token > a.fencing_token and b.attempts == 2 and b.expired_requeued >= 1
    with pytest.raises(StaleLease):
        queue.complete(qdb.worker, a, {"stale": True})
    with pytest.raises(StaleLease):
        queue.heartbeat(qdb.worker, a)
    with pytest.raises(DBAPIError) as ei, qdb.worker.begin() as c:  # the raw function raises too
        c.execute(text("SELECT ops.complete_job(:j, :w, :t, 'succeeded', NULL, NULL)"),
                  {"j": jid, "w": a.worker_id, "t": a.fencing_token})
    assert getattr(ei.value.orig, "sqlstate", None) == "RV001"
    assert queue.heartbeat(qdb.worker, b) == "running"
    queue.complete(qdb.worker, b, {"by": "B"})
    row = _status(qdb, jid)
    assert row["status"] == "succeeded" and row["result_ref"] == {"by": "B"} and row["worker_id"] == "worker-B"


def test_expired_lease_at_max_attempts_is_dead_lettered(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"probe": "dead-on-expiry"}, max_attempts=1)
    a = queue.claim(qdb.worker, "worker-A", 30)
    assert a is not None and a.id == jid
    _expire_lease(qdb, jid)
    assert queue.claim(qdb.worker, "worker-B", 30) is None
    row = _status(qdb, jid)
    assert row["status"] == "dead" and row["last_error"]["code"] == "lease_expired"


def test_concurrent_claims_never_return_the_same_job(qdb: FreshDB) -> None:
    ids = {_enqueue(qdb, "health", {"probe": "concurrent", "n": i}) for i in range(16)}
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def run(name: str) -> None:
        try:
            barrier.wait()
            while (job := queue.claim(qdb.worker, name, 30)) is not None:
                with lock:
                    claimed.append(job.id)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert len(claimed) == len(set(claimed))
    assert ids <= set(claimed)
    with qdb.owner.connect() as c:
        tokens = c.execute(text("SELECT fencing_token FROM ops.job WHERE id = ANY(:i)"),
                           {"i": list(ids)}).scalars().all()
    assert len(set(tokens)) == len(ids)


def test_priority_and_starvation_protection(qdb: FreshDB) -> None:
    old_low = _enqueue(qdb, "health", {"probe": "aging-low"}, priority=-100)
    with qdb.owner.begin() as c:  # waited 4 hours -> +200 aging cap
        c.execute(text("UPDATE ops.job SET created_at = clock_timestamp() - interval '4 hours' WHERE id = :j"),
                  {"j": old_low})
    fresh_high = _enqueue(qdb, "health", {"probe": "aging-high"}, priority=100)
    mid = _enqueue(qdb, "health", {"probe": "mid"}, priority=50)
    order = [queue.claim(qdb.worker, "w", 30) for _ in range(3)]
    assert [j.id for j in order if j] == [old_low, fresh_high, mid]


# --------------------------------------------------------------------------- scheduler
def test_two_schedulers_create_exactly_one_job_per_slot(qdb: FreshDB) -> None:
    keys = [f"test:sched:{uuid.uuid4().hex[:8]}" for _ in range(6)]
    with qdb.owner.begin() as c:
        for i, k in enumerate(keys):
            c.execute(text("""INSERT INTO ops.schedule(schedule_key, job_type, parameters, cadence_spec, next_due_at)
                              VALUES (:k, 'health', '{}', '{"interval_seconds": 3600}',
                                      clock_timestamp() - make_interval(secs => :lag))"""),
                      {"k": k, "lag": 10 + i * 3600 * (i % 2)})  # some schedules missed several slots
    barrier = threading.Barrier(2)
    results: list[list[schedules.ScheduledJob]] = []

    def run() -> None:
        barrier.wait()
        results.append(schedules.tick(qdb.worker))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    created = [s for r in results for s in r if s.schedule_key in keys and s.created]
    assert sorted(s.schedule_key for s in created) == sorted(keys)
    with qdb.owner.connect() as c:
        for k in keys:
            n = c.execute(text("SELECT count(*) FROM ops.job WHERE idempotency_key LIKE :p"),
                          {"p": f"schedule:{k}:%"}).scalar()
            assert n == 1, k
            nxt, last = c.execute(text("SELECT next_due_at, last_slot FROM ops.schedule WHERE schedule_key = :k"),
                                  {"k": k}).one()
            assert nxt - last == timedelta(hours=1)
            assert c.execute(text("SELECT next_due_at > clock_timestamp() FROM ops.schedule WHERE schedule_key=:k"),
                             {"k": k}).scalar()
        # Re-processing the same slot (e.g. a reset) is absorbed by the idempotency key.
        c.commit()
    with qdb.owner.begin() as c:
        c.execute(text("UPDATE ops.schedule SET next_due_at = last_slot WHERE schedule_key = :k"), {"k": keys[0]})
    again = schedules.tick(qdb.worker)
    assert [s.created for s in again if s.schedule_key == keys[0]] == [False]
    with qdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ops.job WHERE idempotency_key LIKE :p"),
                         {"p": f"schedule:{keys[0]}:%"}).scalar() == 1


def test_schedule_payload_and_slot_key() -> None:
    from datetime import UTC, datetime

    assert schedules.schedule_payload("ingest", {"connector": "x"}) == {"connector": "x", "mode": "live"}
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    assert schedules.due_slot(t0, t0 + timedelta(hours=3, minutes=5), timedelta(hours=1)) == t0 + timedelta(hours=3)
    assert schedules.slot_key("gc:orphans", t0) == "schedule:gc:orphans:2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- retry / dead letter
def test_retry_with_exponential_backoff_then_dead_letter(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"probe": "retry"}, max_attempts=3)
    statuses = []
    for attempt in (1, 2, 3):
        job = queue.claim(qdb.worker, "w-retry", 30)
        assert job is not None and job.id == jid and job.attempts == attempt
        st = queue.fail(qdb.worker, job, queue.error_document("transient_error", "boom", retryable=True,
                                                              attempt=attempt), retryable=True,
                        backoff_base_seconds=5)
        statuses.append(st)
        row = _status(qdb, jid)
        if st == "queued":
            with qdb.owner.connect() as c:
                delay = c.execute(text("SELECT EXTRACT(EPOCH FROM available_at - clock_timestamp()) FROM ops.job "
                                       "WHERE id = :j"), {"j": jid}).scalar()
            expected = 5 * 2 ** (attempt - 1)
            assert expected - 2 < float(delay) <= expected
            assert row["last_error"]["backoff_seconds"] == expected
            assert queue.claim(qdb.worker, "w-retry", 30) is None  # not yet available
            _make_available(qdb, jid)
    assert statuses == ["queued", "queued", "dead"]
    row = _status(qdb, jid)
    assert row["status"] == "dead" and row["last_error"]["dead_letter"] is True and row["finished_at"] is not None


def test_non_retryable_failure_is_immediate(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"probe": "nonretryable"}, max_attempts=5)
    job = queue.claim(qdb.worker, "w", 30)
    assert job is not None and job.id == jid
    assert queue.fail(qdb.worker, job, {"code": "validation_error", "message": "bad"}, retryable=False) == "failed"
    assert _status(qdb, jid)["attempts"] == 1


def test_missing_handler_is_a_typed_failure_never_success(qdb: FreshDB, monkeypatch) -> None:
    monkeypatch.setitem(jobs.HANDLERS, "population", "reva.population.no_such_module:handle_population")
    monkeypatch.setitem(jobs.HANDLERS, "evaluation", "reva.operations.jobs:no_such_function")
    for jt in ("population", "evaluation"):
        jid = _enqueue(qdb, jt, {"probe": "unavailable"})
        w = _worker(qdb)
        job = w.claim_one()
        assert job is not None and job.id == jid
        out = w.execute(job)
        row = _status(qdb, jid)
        assert out.status == "failed" and row["status"] == "failed", jt
        assert row["last_error"]["code"] == "handler_unavailable" and row["last_error"]["retryable"] is False


# --------------------------------------------------------------------------- cancellation
def test_cancel_queued_job(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"probe": "cancel-queued"})
    assert queue.cancel(qdb.api, jid, "human:tester") == "cancelled"
    assert _status(qdb, jid)["status"] == "cancelled"
    assert queue.claim(qdb.worker, "w", 30) is None


def test_cancel_running_job_is_honoured_cooperatively(qdb: FreshDB, monkeypatch) -> None:
    started = threading.Event()
    seen = {}

    def slow_handler(payload, ctx):
        started.set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ctx.is_cancelled():
                seen["cancelled_at"] = time.monotonic()
                raise jobs.JobCancelled("stopping at a safe point")
            time.sleep(0.05)
        return {"status": "finished"}

    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: slow_handler)
    jid = _enqueue(qdb, "health", {"probe": "cancel-running"})
    w = _worker(qdb)
    job = w.claim_one()
    assert job is not None and job.id == jid
    outcome = {}
    t = threading.Thread(target=lambda: outcome.setdefault("o", w.execute(job)))
    t.start()
    assert started.wait(10)
    assert queue.cancel(qdb.api, jid, "human:tester") == "cancel_requested"
    t.join(20)
    assert outcome["o"].status == "cancelled"
    row = _status(qdb, jid)
    assert row["status"] == "cancelled" and row["finished_at"] is not None
    with qdb.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ops.audit WHERE action = 'job.cancel' AND target->>'job_id' = :j"),
                         {"j": str(jid)}).scalar() == 1


# --------------------------------------------------------------------------- crash windows (ingest)
def _acquisitions(d: FreshDB, connector: str) -> list[tuple[str, int]]:
    with d.owner.connect() as c:
        return [tuple(r) for r in c.execute(text(
            """SELECT a.request_fingerprint, count(*) FROM evidence.acquisition a
                 JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
                WHERE cv.name = :n GROUP BY a.request_fingerprint"""), {"n": connector}).all()]


def test_kill_after_object_upload_then_retry_yields_one_acquisition(qdb: FreshDB, monkeypatch) -> None:
    import reva.evidence.runner as runner

    real = runner.publish_acquisition
    calls = {"n": 0}

    def faulty(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(*a, **kw, fault_after_object_write=True)
        return real(*a, **kw)

    monkeypatch.setattr(runner, "publish_acquisition", faulty)
    jid = _enqueue(qdb, "ingest", {"connector": "crossref.works.doi", "mode": "fixture"})
    w = _worker(qdb)
    job = w.claim_one()
    assert job is not None and job.id == jid
    first = w.execute(job)
    assert first.status == "queued" and first.error["retryable"] is True  # crash after upload: retried
    assert _acquisitions(qdb, "crossref.works.doi") == []  # nothing visible after the crash
    _make_available(qdb, jid)
    job2 = w.claim_one()
    assert job2 is not None and job2.id == jid and job2.fencing_token > job.fencing_token
    second = w.execute(job2)
    assert second.status == "succeeded"
    acq = _acquisitions(qdb, "crossref.works.doi")
    assert len(acq) == 1 and all(n == 1 for _, n in acq)
    assert _status(qdb, jid)["status"] == "succeeded"


def test_kill_after_db_commit_before_ack_yields_one_acquisition(qdb: FreshDB) -> None:
    payload = {"connector": "worldbank.wdi.indicators", "mode": "fixture"}
    jid = _enqueue(qdb, "ingest", payload)
    a = queue.claim(qdb.worker, "worker-A", 30)
    assert a is not None and a.id == jid
    res = jobs.handle_ingest(dict(a.payload), _ctx(qdb, a))  # commits evidence ...
    assert res["inserted"] > 0
    # ... then the process dies before acknowledging: the lease expires.
    _expire_lease(qdb, jid)
    w = _worker(qdb, worker_id="worker-B", job_types=["ingest"])
    b = w.claim_one()
    assert b is not None and b.id == jid and b.fencing_token > a.fencing_token
    out = w.execute(b)
    assert out.status == "succeeded"
    row = _status(qdb, jid)
    assert row["result_ref"]["inserted"] == 0 and row["result_ref"]["unchanged"] == 0
    with pytest.raises(StaleLease):
        queue.complete(qdb.worker, a, res)
    acq = _acquisitions(qdb, "worldbank.wdi.indicators")
    assert len(acq) == 5 and all(n == 1 for _, n in acq)
    with qdb.owner.connect() as c:
        pubs = c.execute(text("SELECT count(*) FROM ops.job WHERE job_type = 'publish_beliefs' AND created_by = :c"),
                         {"c": f"job:{jid}"}).scalar()
    assert pubs == 1  # the first (crashed) attempt enqueued publication; the retry did not duplicate it


# --------------------------------------------------------------------------- start-up refusals
def _production_settings(**over) -> Settings:
    data = {
        "reva_env": "production", "reva_object_backend": "s3", "reva_s3_bucket": "b",
        "reva_s3_access_key_id": "AKIAEXAMPLE", "reva_s3_secret_access_key": "x" * 40,
        "reva_session_secret": "s" * 40, "reva_public_base_url": "https://reva.example",
        "reva_worker_db_password": "pw-worker-example", "reva_db_sslmode": "require",
    }
    data.update(over)
    return Settings(_env_file=None, **data)  # type: ignore[call-arg]


def test_worker_refuses_migration_url_in_production() -> None:
    env = {"REVA_MIGRATION_DATABASE_URL": "postgresql://owner:hunter2@db/reva"}
    with pytest.raises(ConfigurationError):
        check_environment(_production_settings(), env)
    with pytest.raises(ConfigurationError):
        check_environment(_production_settings(reva_migration_database_url="postgresql://o:p@db/r"), {})
    assert check_environment(_production_settings(), {}).reva_env == "production"


def test_worker_ignores_migration_url_locally(caplog) -> None:
    env = {"REVA_MIGRATION_DATABASE_URL": "postgresql://owner:hunter2@db/reva", "OTHER": "1"}
    s = Settings(_env_file=None, reva_env="local",  # type: ignore[call-arg]
                 reva_migration_database_url="postgresql://o:p@db/r")
    out = check_environment(s, env)
    assert out.reva_migration_database_url is None and "REVA_MIGRATION_DATABASE_URL" not in env
    assert any("ignoring" in r.getMessage() for r in caplog.records)


def test_thread_budget_ignores_cpu_count(monkeypatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 128)
    monkeypatch.setenv("REVA_CPU_BUDGET_THREADS", "2")
    env: dict[str, str] = {}
    n = configure_threads(env)
    assert n == threads_budget() and 1 <= n <= 2
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "POLARS_MAX_THREADS"):
        assert env[k] == str(n)


def test_schema_wait_is_bounded_and_role_checked(qdb: FreshDB) -> None:
    with qdb.worker.connect() as c:
        info = verify_runtime(c)
    assert info["schema_revision"].startswith("0007") and info["role"] == "reva_worker"
    with qdb.owner.connect() as c, pytest.raises(ConfigurationError):
        verify_runtime(c)  # the migration owner is never an acceptable worker identity
    bogus = make_engine("worker", qdb.settings.model_copy(update={"reva_db_name": "reva_no_such_db_x"}))
    sleeps: list[float] = []
    with pytest.raises(ConfigurationError):
        wait_for_schema(bogus, attempts=3, base_delay=0.5, sleep=sleeps.append)
    assert sleeps == [0.5, 1.0, 2.0]
    bogus.dispose()


# --------------------------------------------------------------------------- health job
def test_health_job_appends_cadence_relative_vector(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"connector": "worldbank.wdi.indicators"})
    w = _worker(qdb, job_types=["health"])
    job = w.claim_one()
    assert job is not None and job.id == jid
    assert w.execute(job).status == "succeeded"
    with qdb.owner.connect() as c:
        m, status = c.execute(text(
            """SELECT h.metrics, h.status FROM ops.connector_health h
                 JOIN evidence.connector_version cv ON cv.id = h.connector_version_id
                WHERE cv.name = 'worldbank.wdi.indicators' AND h.metrics->>'kind' = 'health_vector'
                ORDER BY h.recorded_at DESC LIMIT 1""")).one()
    assert m["runs_considered"] >= 1 and m["success_ratio"] == 1.0
    f = m["freshness"]
    assert f["cadence_seconds"] == 30 * 86400 and f["lag_cadences"] < 0.01 and f["late"] is False
    assert m["schema_drift"]["drift"] is False
    assert m["completeness"]["validated_acquisition_ratio"] == 1.0
    assert status == m["status"] == "healthy"


# --------------------------------------------------------------------------- R2-08 bounded waiting
def _backdate(d: FreshDB, jid: uuid.UUID, minutes: float) -> None:
    with d.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET created_at = clock_timestamp() - make_interval(mins => :m) "
                       "WHERE id = :j"), {"j": jid, "m": minutes})


def test_old_low_priority_job_beats_aged_high_priority_backlog(qdb: FreshDB) -> None:
    # The review's counter-example to capped aging: p=-100 waited 600 min, p=100 waited 5 min.
    low = _enqueue(qdb, "health", {"probe": "starve-low"}, priority=-100)
    high = _enqueue(qdb, "health", {"probe": "starve-high"}, priority=100)
    _backdate(qdb, low, 600)
    _backdate(qdb, high, 5)
    got = queue.claim(qdb.worker, "w", 30)
    assert got is not None and got.id == low


def test_overdue_jobs_are_claimed_fifo_before_any_priority(qdb: FreshDB) -> None:
    old_low = _enqueue(qdb, "health", {"probe": "fifo-old-low"}, priority=-100)
    newer_high = _enqueue(qdb, "health", {"probe": "fifo-newer-high"}, priority=100)
    fresh_top = _enqueue(qdb, "health", {"probe": "fifo-fresh-top"}, priority=100)
    _backdate(qdb, old_low, 40)  # overdue (> 30 min)
    _backdate(qdb, newer_high, 35)  # overdue, created later
    order = [queue.claim(qdb.worker, "w", 30) for _ in range(3)]
    assert [j.id for j in order if j] == [old_low, newer_high, fresh_top]


def test_uncapped_aging_below_the_deadline(qdb: FreshDB) -> None:
    # Neither job is overdue: p=-100 aged 150 min scores 50 and loses to p=60 fresh (60) ...
    low = _enqueue(qdb, "health", {"probe": "age-low"}, priority=-100)
    mid = _enqueue(qdb, "health", {"probe": "age-mid"}, priority=60)
    _backdate(qdb, low, 150)
    got = queue.claim(qdb.worker, "w", 30, max_wait_seconds=86_400)
    assert got is not None and got.id == mid
    # ... but beats p=40 fresh (no cap at the priority span: 50 > 40).
    p40 = _enqueue(qdb, "health", {"probe": "age-40"}, priority=40)
    got = queue.claim(qdb.worker, "w", 30, max_wait_seconds=86_400)
    assert got is not None and got.id == low
    assert queue.claim(qdb.worker, "w", 30).id == p40


# --------------------------------------------------------------------------- R2-14 heartbeat vs long locks
def test_heartbeat_gives_up_quickly_when_row_is_locked(qdb: FreshDB) -> None:
    from reva.operations.worker import HEARTBEAT_LOCK_TIMEOUT_MS, _Active

    jid = _enqueue(qdb, "health", {"probe": "hb-busy"})
    job = queue.claim(qdb.worker, "w-hb", 30)
    assert job is not None and job.id == jid
    w = _worker(qdb)
    assert w.heartbeat_engine is not w.engine  # dedicated heartbeat pool
    with qdb.worker.connect() as holder:  # a long publication holding the job row lock
        holder.execute(text("SELECT ops.assert_lease(:j, :w, :t)"),
                       {"j": job.id, "w": job.worker_id, "t": job.fencing_token})
        t0 = time.monotonic()
        with pytest.raises(DBAPIError) as ei:
            queue.heartbeat(w.heartbeat_engine, job, None, 30, lock_timeout_ms=200, statement_timeout_ms=2000)
        assert queue.is_lock_busy(ei.value) and time.monotonic() - t0 < 2
        st = _Active(job)
        t0 = time.monotonic()
        assert w._beat(st, None) == "running"  # busy -> last known status, lease not lost
        assert not st.lost and time.monotonic() - t0 < HEARTBEAT_LOCK_TIMEOUT_MS / 1000 + 2
        holder.rollback()
    assert queue.heartbeat(w.heartbeat_engine, job, None, 30) == "running"
    w.close()


def test_non_locking_lease_check_does_not_block_heartbeat(qdb: FreshDB) -> None:
    jid = _enqueue(qdb, "health", {"probe": "hb-check-lease"})
    job = queue.claim(qdb.worker, "w-hb2", 30)
    assert job is not None and job.id == jid
    ctx = _ctx(qdb, job)
    with qdb.worker.connect() as pub:  # what handle_publish_beliefs now passes as the fence
        ctx.check_lease(pub)
        t0 = time.monotonic()
        assert queue.heartbeat(qdb.worker, job, None, 30, lock_timeout_ms=500) == "running"
        assert time.monotonic() - t0 < 0.5
        pub.rollback()
    _expire_lease(qdb, jid)
    with qdb.worker.connect() as pub, pytest.raises(StaleLease):
        ctx.check_lease(pub)


# --------------------------------------------------------------------------- R2-02 internal key conflicts
def test_internal_enqueue_refuses_api_namespace(qdb: FreshDB) -> None:
    with qdb.worker.begin() as c, pytest.raises(ValidationProblem):
        queue.enqueue(c, "health", {"probe": "ns"}, "api:someone:health:k-12345678")


def test_publish_beliefs_key_held_by_foreign_job_is_disambiguated_and_audited(qdb: FreshDB) -> None:
    payload = {"origin": "test_fixture", "through_outbox_id": 987654}
    key = queue.idempotency_key("publish_beliefs", payload)
    with qdb.owner.begin() as c:  # a foreign job pre-claims the internal key
        foreign = c.execute(text("INSERT INTO ops.job(job_type, payload, idempotency_key, created_by) "
                                 "VALUES ('replay', '{}', :k, 'api:attacker') RETURNING id"), {"k": key}).scalar()
    with qdb.worker.begin() as c:
        jid = jobs.enqueue_publish_beliefs(c, "test_fixture", 987654, created_by="worker:test",
                                           audit_actor="worker:test")
    with qdb.worker.begin() as c:  # converges on the same job
        assert jobs.enqueue_publish_beliefs(c, "test_fixture", 987654, created_by="worker:test") == jid
    row = _status(qdb, jid)
    assert jid != foreign and row["job_type"] == "publish_beliefs" and row["payload"] == payload
    assert row["idempotency_key"] == f"{key}#alt1"
    with qdb.owner.connect() as c:
        audit = c.execute(text("SELECT details FROM ops.audit WHERE action = 'jobs.publish_key_conflict' "
                               "AND target->>'job_id' = :j"), {"j": str(jid)}).scalars().all()
    assert len(audit) == 1 and audit[0]["foreign_job_id"] == str(foreign)


def test_claim_job_claims_only_that_job(qdb: FreshDB) -> None:
    other = _enqueue(qdb, "health", {"probe": "claim-job-other"}, priority=100)
    mine = _enqueue(qdb, "health", {"probe": "claim-job-mine"}, priority=-100)
    job = queue.claim_job(qdb.worker, mine, "cli-demo", 30)
    assert job is not None and job.id == mine and job.fencing_token > 0
    assert queue.claim_job(qdb.worker, mine, "cli-demo", 30) is None  # no longer queued
    assert _status(qdb, other)["status"] == "queued"


# --------------------------------------------------------------------------- invariant 30 ingestion budgets
def test_budget_reservation_refuses_runs_that_would_exceed_daily_limits(qdb: FreshDB) -> None:
    from reva.operations.budgets import BudgetExhausted, IngestBudget, reserve_ingest, settle, usage_today

    src = f"test.source.{uuid.uuid4().hex[:6]}"
    b = IngestBudget("t", "", total_requests=10**6, total_bytes=10**12, per_source_requests={src: 100})
    r1 = reserve_ingest(qdb.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=48, budget=b)
    assert r1.max_requests == 100  # run cap: what was left for this source when it started
    with pytest.raises(BudgetExhausted) as ei:  # 48 + 60 > 100: not started
        reserve_ingest(qdb.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=60, budget=b)
    assert ei.value.code == "budget_exhausted" and ei.value.details["used_source_requests"] == 48
    settle(qdb.worker, r1, requests=40, nbytes=1234)  # consumed replaces the reservation
    with qdb.worker.connect() as c:
        u = usage_today(c, src)
    assert u["source_requests"] == 40
    r2 = reserve_ingest(qdb.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=60, budget=b)
    assert r2.max_requests == 60
    tight = IngestBudget("t", "", total_requests=10**6, total_bytes=u["total_bytes"], per_source_requests={})
    with pytest.raises(BudgetExhausted):  # daily byte budget already used up
        reserve_ingest(qdb.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=1, budget=tight)


def test_live_ingest_over_budget_fails_with_budget_exhausted_without_requests(qdb: FreshDB, monkeypatch) -> None:
    from reva.evidence import runner

    def no_network(*a, **k):
        raise AssertionError("a live client must not be created when the budget is exhausted")

    monkeypatch.setattr(runner, "make_live_client", no_network)
    with qdb.owner.begin() as c:  # today's WDI request budget (200) already consumed
        c.execute(text("INSERT INTO ops.budget_ledger(account_key, operation_id, unit, reserved, consumed) "
                       "VALUES ('source:worldbank.wdi:requests', :o, 'requests', 200, 200)"),
                  {"o": f"test-prefill-{uuid.uuid4().hex}"})
    jid = _enqueue(qdb, "ingest", {"connector": "worldbank.wdi.indicators", "mode": "live", "probe": "budget"})
    w = _worker(qdb)
    job = w.claim_one()
    assert job is not None and job.id == jid
    out = w.execute(job)
    row = _status(qdb, jid)
    assert out.status == "failed" and row["status"] == "failed"
    assert row["last_error"]["code"] == "budget_exhausted" and row["last_error"]["retryable"] is False
    assert row["result_ref"] is None


def test_tracing_is_a_no_op_without_endpoint(monkeypatch) -> None:
    from reva.operations import tracing

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(tracing, "_resolved", False)
    monkeypatch.setattr(tracing, "_tracer", None)
    with tracing.span("job.execute", job_type="health") as sp:
        assert sp is None
    assert tracing.enabled() is False
    sentinel = object()
    assert tracing.traced_connector(sentinel) is sentinel


def test_generic_budget_helpers(qdb: FreshDB) -> None:
    from reva.operations.budgets import BudgetExhausted, consume, reserve, used_today

    acct = f"autonomy:test:{uuid.uuid4().hex[:6]}:requests"
    assert reserve(qdb.worker, account_key=acct, operation_id=f"{acct}:1", unit="requests", amount=7,
                   daily_limit=10) == 10
    with pytest.raises(BudgetExhausted):
        reserve(qdb.worker, account_key=acct, operation_id=f"{acct}:2", unit="requests", amount=4, daily_limit=10)
    consume(qdb.worker, f"{acct}:1", 5)
    with qdb.worker.connect() as c:
        assert used_today(c, acct) == 5


def test_promoted_connector_cannot_shadow_a_builtin(qdb: FreshDB, monkeypatch) -> None:
    import reva.autonomy.jobs as aj

    impostor = object()
    monkeypatch.setattr(aj, "promoted_connectors", lambda engine, store: {"worldbank.wdi.indicators": impostor,
                                                                           "eurostat.test.promoted": impostor})
    job = queue.claim(qdb.worker, "w", 30) if _enqueue(qdb, "health", {"probe": "shadow"}) else None
    assert job is not None
    got = jobs._all_connectors(_ctx(qdb, job))
    assert got["worldbank.wdi.indicators"] is not impostor and got["eurostat.test.promoted"] is impostor
    with qdb.owner.connect() as c:
        names = c.execute(text("SELECT details->'names' FROM ops.audit WHERE action = "
                               "'connectors.promoted_name_collision' AND target->>'job_id' = :j"),
                          {"j": str(job.id)}).scalar()
    assert names == ["worldbank.wdi.indicators"]
