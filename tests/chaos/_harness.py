"""Harness for real-process fault-injection tests: throwaway databases, worker subprocesses,
event logs and polling helpers. Never touches the dev database ``reva``."""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres import queue
from reva.adapters.postgres.engine import make_engine
from reva.settings import Settings

REPO = Path(__file__).resolve().parents[2]
# CHAOS_LAUNCHER lets a reviewer run the same tests against a launcher that applies a candidate
# src patch in-process (e.g. to validate the CHAOS-1 fix before it lands); default: the plain launcher.
LAUNCHER = Path(os.environ.get("CHAOS_LAUNCHER") or Path(__file__).with_name("_chaos_worker.py"))
LEASE_SECONDS = 10  # Settings.reva_job_lease_seconds minimum
HEARTBEAT_SECONDS = 1
DRAIN_SECONDS = 5


@dataclass
class ChaosDB:
    settings: Settings
    name: str
    url: str
    root: Path
    owner: Engine
    api: Engine
    worker: Engine
    sim: Engine
    store: FilesystemArtifactStore


def fresh_db(tmp_path_factory: pytest.TempPathFactory, label: str) -> Iterator[ChaosDB]:
    """Same recipe as ``tests/conftest.py::db``: new uniquely named DB, bootstrap, seed."""
    base = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"reva_test_chaos_{label}_{uuid.uuid4().hex[:8]}"
    assert name != "reva"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp(f"chaos_objects_{label}")
    url = base.migration_url().set(database=name).render_as_string(hide_password=False)
    data = base.model_dump()
    data.update(reva_migration_database_url=url, reva_db_name=name, reva_env="test", reva_object_root=root)
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
    pooled = settings.model_copy(update={"reva_db_pool_size": 4, "reva_db_max_overflow": 4})
    d = ChaosDB(settings, name, url, root, make_engine("migration", settings), make_engine("api", pooled),
                make_engine("worker", pooled), make_engine("sim", settings), store)
    if "seed_registry" not in inspect.signature(bootstrap).parameters:
        seed(d.worker, store)  # older bootstrap: seed separately (newer bootstrap seeds as the owner)
    with d.owner.begin() as c:
        c.execute(text("UPDATE ops.schedule SET enabled = false"))  # no scheduler noise
    try:
        yield d
    finally:
        for e in (d.owner, d.api, d.worker, d.sim):
            e.dispose()
        if not os.environ.get("REVA_KEEP_TEST_DB"):
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --------------------------------------------------------------------------- worker processes
@dataclass
class WorkerProc:
    proc: subprocess.Popen[bytes]
    log_path: Path
    started: float
    exit_time: float | None = None
    _log: Any = field(default=None, repr=False)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def signal(self, sig: int) -> None:
        if self.alive():
            os.kill(self.proc.pid, sig)

    def wait(self, timeout: float) -> int | None:
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        if self.exit_time is None:
            self.exit_time = time.monotonic()
        return rc

    def log(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self.alive():
            self.signal(signal.SIGKILL)
            self.wait(10)
        if self._log is not None:
            self._log.close()


def worker_env(d: ChaosDB, chaos_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "REVA_ENV": "test",
        "REVA_DB_NAME": d.name,  # the throwaway database, never the dev DB
        "REVA_OBJECT_ROOT": str(d.root),
        # The worker strips this in test (warning); point it at the throwaway DB regardless.
        "REVA_MIGRATION_DATABASE_URL": d.url,
        "REVA_JOB_LEASE_SECONDS": str(LEASE_SECONDS),
        "REVA_JOB_HEARTBEAT_SECONDS": str(HEARTBEAT_SECONDS),
        "REVA_WORKER_DRAIN_SECONDS": str(DRAIN_SECONDS),
        "REVA_SCHEDULER_TICK_SECONDS": "3600",
        "REVA_WORKER_CONCURRENCY": "1",
        "REVA_LOG_LEVEL": "INFO",
        "PYTHONUNBUFFERED": "1",
        "CHAOS_DIR": str(chaos_dir),
    })
    env.update(extra)
    return env


def reva_cli() -> list[str]:
    exe = Path(sys.executable).with_name("reva")
    if exe.exists():
        return [str(exe), "worker"]
    return [sys.executable, "-c", "from reva.cli.main import app; app()", "worker"]


def start_worker(d: ChaosDB, chaos_dir: Path, *, cli: bool = False, **extra: str) -> WorkerProc:
    """Spawn a real worker process: ``reva worker`` (cli=True) or the chaos launcher
    (production ``worker.main()`` plus the test-only ``chaos_slow`` job type / delay hooks)."""
    chaos_dir.mkdir(parents=True, exist_ok=True)
    cmd = reva_cli() if cli else [sys.executable, str(LAUNCHER)]
    log_path = chaos_dir / f"worker-{uuid.uuid4().hex[:6]}.log"
    fh = open(log_path, "wb")  # noqa: SIM115 - closed in stop()
    p = subprocess.Popen(cmd, cwd=REPO, env=worker_env(d, chaos_dir, **extra), stdout=fh,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return WorkerProc(p, log_path, time.monotonic(), _log=fh)


def events(chaos_dir: Path, event: str | None = None, **match: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(chaos_dir.glob("events-*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue  # a line torn by SIGKILL
            if (event is None or e["event"] == event) and all(e.get(k) == v for k, v in match.items()):
                out.append(e)
    return sorted(out, key=lambda e: e["t"])


def wait_until(pred: Callable[[], Any], timeout: float, interval: float = 0.1, what: str = "condition",
               procs: list[WorkerProc] | None = None) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            logs = "\n".join(f"--- worker {p.pid} (rc={p.proc.poll()})\n{p.log()[-4000:]}" for p in procs or [])
            raise AssertionError(f"timed out after {timeout}s waiting for {what}\n{logs}")
        time.sleep(interval)


def enqueue(d: ChaosDB, job_type: str, payload: dict[str, Any], **kw: Any) -> uuid.UUID:
    with d.worker.begin() as c:
        return queue.enqueue(c, job_type, payload, queue.idempotency_key(job_type, payload), **kw)


def job(d: ChaosDB, jid: uuid.UUID) -> dict[str, Any]:
    row = queue.job_row(d.owner, jid)
    assert row is not None
    return row


def clear_queue(d: ChaosDB) -> None:
    with d.owner.begin() as c:
        c.execute(text("UPDATE ops.job SET status = 'cancelled', worker_id = NULL, lease_until = NULL, "
                       "finished_at = clock_timestamp() WHERE status IN ('queued','running','cancel_requested')"))
        c.execute(text("UPDATE ops.schedule SET enabled = false"))


def db_now(d: ChaosDB) -> Any:
    with d.owner.connect() as c:
        return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def terminate_worker_backends(d: ChaosDB, where: str = "true") -> int:
    with d.owner.connect() as c:
        n = c.execute(text(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = :db AND usename IN ('reva_worker','reva_sim') "
            "AND application_name LIKE 'reva-worker%' AND " + where), {"db": d.name}).scalar_one()
        c.commit()
    return int(n)


TERMINAL = ("succeeded", "failed", "cancelled", "dead")
