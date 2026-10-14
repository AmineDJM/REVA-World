#!/usr/bin/env python3
"""Start a complete, disposable local REVA stack for the UI end-to-end suite (CI item 10).

What it does, in order:

1. creates a fresh, uniquely named PostgreSQL database with the local migration
   owner from ``.env`` (same pattern as the ``db`` fixture in tests/conftest.py),
   applies migrations/role passwords (``bootstrap``) and seeds the registry;
2. ingests the captured provider fixtures (World Bank WDI, UN Comtrade, Natural
   Earth, Crossref) offline through ``run_connector`` + ``FixtureHttp`` +
   ``post_publish_for`` and publishes a ``test_fixture`` belief release;
3. builds the UI (``npm --prefix ui run build``) into a private directory
   (``--outDir``; the repository's src/reva/api/static is never touched);
4. serves ``create_app`` with uvicorn on a free 127.0.0.1 port, authenticated by a
   freshly generated admin password (Argon2id hash only in memory) and session
   secret, plus a real ``Worker`` thread (worker + simulation roles; scheduler and
   outbox invalidation disabled so nothing touches live providers);
5. signs in once over HTTP and writes a Playwright ``storageState`` file, then
   writes ``stack.json`` (base URL, credentials, database name, release id,
   cutoff, storage-state path) for the Playwright specs;
6. on exit, SIGINT or SIGTERM: stops the worker, drops the database and removes
   the temporary object store and UI build.

Usage::

    uv run python scripts/e2e_stack.py                 # serve until Ctrl-C
    uv run python scripts/e2e_stack.py --run           # serve, run `npm --prefix ui run e2e`, tear down
    uv run python scripts/e2e_stack.py --run -- --grep provenance   # extra Playwright args

The default stack file is ``var/e2e/stack.json`` (gitignored); the specs in
ui/e2e read it (or ``REVA_E2E_STACK_FILE``) when ``REVA_E2E_BASE_URL`` is unset.
The live development database ``reva`` is never read or written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

log = logging.getLogger("reva.e2e_stack")

FIXTURE_CONNECTORS = (
    "worldbank.wdi.indicators",
    "un.comtrade.public_exact",
    "naturalearth.admin0_110m",
    "crossref.works.doi",
)
DEFAULT_STACK_FILE = ROOT / "var" / "e2e" / "stack.json"
ADMIN_USERNAME = "admin"


def with_connection_retry(what: str, fn: Any, *, deadline: float = 300.0) -> Any:
    """Retry ``fn`` while PostgreSQL refuses logins for a busy role.

    Runtime roles carry a cluster-wide connection limit shared with every other local
    test session; a refused login happens before any transaction, so retrying is safe.
    """
    from sqlalchemy.exc import OperationalError

    t0 = time.monotonic()
    delay = 1.0
    while True:
        try:
            return fn()
        except OperationalError as e:
            if "too many connections" not in str(e) or time.monotonic() - t0 > deadline:
                raise
            log.warning("%s: database role connection limit reached; retrying in %.0fs", what, delay)
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Stack:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.db_name = f"reva_e2e_{uuid.uuid4().hex[:10]}"
        self.tmp = Path(tempfile.mkdtemp(prefix="reva-e2e-"))
        self.object_root = self.tmp / "objects"
        self.ui_dir = self.tmp / "ui"
        self.admin_engine: Any = None
        self.db_created = False
        self.engines: list[Any] = []
        self.worker: Any = None
        self.worker_thread: threading.Thread | None = None
        self.server: Any = None
        self.stack_file: Path = Path(args.stack_file).resolve()
        self.storage_state = self.stack_file.with_name("storage-state.json")
        self._torn_down = False

    # ------------------------------------------------------------------ database
    def create_database(self) -> Any:
        from pydantic import SecretStr
        from sqlalchemy import create_engine, text

        from reva.settings import Settings

        base = Settings(_env_file=ROOT / ".env")  # type: ignore[call-arg]
        if base.reva_migration_database_url is None:
            raise SystemExit("REVA_MIGRATION_DATABASE_URL is not configured (.env); cannot create the e2e database")
        self.admin_engine = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
        with self.admin_engine.connect() as c:
            c.execute(text(f'CREATE DATABASE "{self.db_name}"'))
        self.db_created = True
        url = base.migration_url().set(database=self.db_name).render_as_string(hide_password=False)
        return Settings.model_validate(
            {
                **base.model_dump(),
                "reva_migration_database_url": SecretStr(url),
                "reva_db_name": self.db_name,
                "reva_env": "test",
                "reva_object_backend": "filesystem",
                "reva_object_root": self.object_root,
            }
        )

    def prepare_data(self, settings: Any) -> dict[str, Any]:
        from reva.adapters.connectors.registry import builtin_connectors
        from reva.adapters.objects.filesystem import FilesystemArtifactStore
        from reva.adapters.postgres.engine import make_engine
        from reva.belief.publication import publish_release
        from reva.cli.bootstrap import bootstrap
        from reva.cli.seed import seed
        from reva.evidence.runner import FixtureHttp, post_publish_for, run_connector
        from reva.kernel.context import Origin

        for attempt in range(3):  # concurrent bootstraps may race on cluster-global role DDL
            try:
                bootstrap(settings)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        store = FilesystemArtifactStore(self.object_root, settings.reva_s3_prefix)
        worker = make_engine("worker", settings)
        try:
            with_connection_retry("seed", lambda: seed(worker, store))
            connectors = builtin_connectors()
            ingest: dict[str, Any] = {}
            for name in FIXTURE_CONNECTORS:
                c = connectors[name]
                spec = c.describe()
                rep = with_connection_retry(
                    f"ingest {name}",
                    lambda c=c, spec=spec: asyncio.run(
                        run_connector(
                            c,
                            FixtureHttp(spec.name),
                            store,
                            worker,
                            persist_cursor=False,
                            post_publish=post_publish_for(spec.template),
                        )
                    ),
                )
                ingest[name] = {
                    "inserted": rep.inserted,
                    "unchanged": rep.unchanged,
                    "errors": rep.errors,
                    "outcomes": [o.status for o in rep.outcomes],
                }
                if rep.errors:
                    log.warning("fixture ingest %s reported errors: %s", name, rep.errors)
                log.info("ingested %s: %d inserted, %d unchanged", name, rep.inserted, rep.unchanged)
            release = with_connection_retry(
                "publish release",
                lambda: publish_release(
                    worker, store, cutoff=datetime.now(UTC), origin=Origin.test_fixture, environment="test"
                ),
            )
        finally:
            worker.dispose()
        rel = {
            "id": str(release.release_id),
            "context_id": str(release.context_id),
            "sequence": release.sequence,
            "estimates": release.estimates,
            "manifest_sha256": release.manifest_sha256,
            "knowledge_cutoff": release.knowledge_cutoff.isoformat(),
            "origin": "test_fixture",
        }
        log.info("published test_fixture release: %s", rel)
        return {"ingest": ingest, "release": rel}

    # ------------------------------------------------------------------ UI
    def build_ui(self) -> Path:
        if self.args.static_dir:
            path = Path(self.args.static_dir).resolve()
            if not (path / "index.html").is_file():
                raise SystemExit(f"--static-dir {path} has no index.html")
            return path
        cmd = ["npm", "--prefix", str(ROOT / "ui"), "run", "build", "--", "--outDir", str(self.ui_dir), "--emptyOutDir"]
        log.info("building UI: %s", " ".join(cmd))
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
            raise SystemExit(f"UI build failed (exit {proc.returncode})")
        return self.ui_dir

    # ------------------------------------------------------------------ processes
    def start_worker(self, settings: Any) -> None:
        from reva.adapters.objects.filesystem import FilesystemArtifactStore
        from reva.adapters.postgres.engine import make_engine
        from reva.operations.worker import Worker, wait_for_schema

        ws = settings.model_copy(
            update={"reva_migration_database_url": None, "reva_db_pool_size": 4, "reva_db_max_overflow": 0}
        )
        engine = make_engine("worker", ws)
        sim_engine = make_engine("sim", ws)
        self.engines += [engine, sim_engine]
        with_connection_retry("worker schema check", lambda: wait_for_schema(engine, attempts=5))
        store = FilesystemArtifactStore(self.object_root, ws.reva_s3_prefix)
        # Scheduler off (it would enqueue live acquisitions); invalidation off (it would publish
        # further releases mid-suite). Queue claiming, leases, heartbeats and handlers are real.
        self.worker = Worker(
            ws,
            engine,
            store,
            sim_engine,
            scheduler=False,
            invalidation=False,
            poll_interval=0.2,
            lease_seconds=60,
            heartbeat_seconds=5,
            drain_seconds=5,
        )
        self.worker_thread = threading.Thread(target=self.worker.run, name="reva-e2e-worker", daemon=True)
        self.worker_thread.start()

    def make_server(self, settings: Any, static_dir: Path, password: str, port: int) -> Any:
        import uvicorn
        from pydantic import SecretStr

        from reva.api.app import create_app
        from reva.api.auth import hash_password

        base_url = f"http://127.0.0.1:{port}"
        api_settings = settings.model_copy(
            update={
                "reva_migration_database_url": None,
                "reva_admin_password_hash": SecretStr(hash_password(password)),
                "reva_session_secret": SecretStr(secrets.token_urlsafe(48)),
                "reva_allowed_hosts": "127.0.0.1,localhost",
                "reva_public_base_url": base_url,
                "reva_cors_origins": "",
                "reva_db_pool_size": 3,
            }
        )
        app = create_app(api_settings, static_dir=static_dir, login_rate_limit_per_minute=self.args.login_rate_limit)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="on")
        self.server = uvicorn.Server(config)
        return base_url

    # ------------------------------------------------------------------ session
    def wait_ready(self, base_url: str, deadline: float = 60.0) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline:
            try:
                with urllib.request.urlopen(f"{base_url}/health/ready", timeout=5) as r:  # noqa: S310
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.3)
        raise RuntimeError(f"API not ready at {base_url} after {deadline}s")

    def write_storage_state(self, base_url: str, password: str) -> None:
        """One real login; the cookies become the Playwright storageState shared by all specs."""
        req = urllib.request.Request(  # noqa: S310
            f"{base_url}/v1/session",
            method="POST",
            data=json.dumps({"username": ADMIN_USERNAME, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            set_cookies = r.headers.get_all("Set-Cookie") or []
        cookies = []
        for header in set_cookies:
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            for name, morsel in jar.items():
                same_site = (morsel["samesite"] or "Lax").capitalize()
                max_age = morsel["max-age"]
                cookies.append(
                    {
                        "name": name,
                        "value": morsel.value,
                        "domain": "127.0.0.1",
                        "path": morsel["path"] or "/",
                        "expires": time.time() + int(max_age) if max_age else -1,
                        "httpOnly": bool(morsel["httponly"]),
                        "secure": bool(morsel["secure"]),
                        "sameSite": same_site if same_site in ("Strict", "Lax", "None") else "Lax",
                    }
                )
        names = {c["name"] for c in cookies}
        if "reva_session" not in names:
            raise RuntimeError(f"login did not set a session cookie (got {sorted(names)})")
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        self.storage_state.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=2))
        os.chmod(self.storage_state, 0o600)

    # ------------------------------------------------------------------ teardown
    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        if self.worker is not None:
            self.worker.request_stop("e2e teardown")
            if self.worker_thread is not None:
                self.worker_thread.join(timeout=15)
        for e in self.engines:
            try:
                e.dispose()
            except Exception as exc:  # noqa: BLE001
                log.debug("engine dispose failed: %s", exc)
        if self.admin_engine is not None:
            if self.db_created and not self.args.keep_db:
                from sqlalchemy import text

                try:
                    with self.admin_engine.connect() as c:
                        c.execute(text(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)'))
                    log.info("dropped database %s", self.db_name)
                except Exception as e:
                    log.error("could not drop database %s: %s", self.db_name, e)
            self.admin_engine.dispose()
        for p in (self.stack_file, self.storage_state):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        if not self.args.keep_db:
            shutil.rmtree(self.tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack-file", default=os.environ.get("REVA_E2E_STACK_FILE", str(DEFAULT_STACK_FILE)))
    ap.add_argument("--port", type=int, default=0, help="API port (default: a free port)")
    ap.add_argument("--static-dir", help="use an existing UI build instead of building")
    ap.add_argument(
        "--login-rate-limit",
        type=int,
        default=10,
        help="login attempts per client per minute (always enforced; default 10)",
    )
    ap.add_argument("--keep-db", action="store_true", help="keep the database and temp files for debugging")
    ap.add_argument("--run", action="store_true", help="run the Playwright suite against the stack, then exit")
    ap.add_argument("playwright_args", nargs="*", help="extra Playwright arguments (after --)")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("REVA_E2E_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("reva.api.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    stack = Stack(args)

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("received %s; shutting down", signal.Signals(signum).name)
        if stack.server is not None:
            stack.server.should_exit = True
        else:
            stack.teardown()
            raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    exit_code = 0
    try:
        t0 = time.monotonic()
        settings = stack.create_database()
        log.info("created database %s", stack.db_name)
        data = stack.prepare_data(settings)
        static_dir = stack.build_ui()
        stack.start_worker(settings)
        password = secrets.token_urlsafe(18)
        port = args.port or free_port()
        base_url = stack.make_server(settings, static_dir, password, port)

        server_thread = threading.Thread(target=stack.server.run, name="reva-e2e-api", daemon=True)
        # uvicorn installs signal handlers only on the main thread; ours above stay in effect.
        server_thread.start()
        stack.wait_ready(base_url)
        stack.write_storage_state(base_url, password)
        info = {
            "base_url": base_url,
            "username": ADMIN_USERNAME,
            "password": password,
            "storage_state": str(stack.storage_state),
            "database": stack.db_name,
            "release": data["release"],
            "ingest": data["ingest"],
            "started_at": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
            "login_rate_limit_per_minute": args.login_rate_limit,
        }
        stack.stack_file.parent.mkdir(parents=True, exist_ok=True)
        stack.stack_file.write_text(json.dumps(info, indent=2, default=str))
        os.chmod(stack.stack_file, 0o600)
        log.info("stack ready in %.1fs: %s (stack file %s)", time.monotonic() - t0, base_url, stack.stack_file)
        print(json.dumps({"base_url": base_url, "stack_file": str(stack.stack_file)}), flush=True)

        if args.run:
            env = {
                **os.environ,
                "REVA_E2E_BASE_URL": base_url,
                "REVA_E2E_USERNAME": ADMIN_USERNAME,
                "REVA_E2E_PASSWORD": password,
                "REVA_E2E_STACK_FILE": str(stack.stack_file),
                "REVA_E2E_STORAGE_STATE": str(stack.storage_state),
            }
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
            env.pop("REVA_MIGRATION_DATABASE_URL", None)
            cmd = ["npm", "--prefix", str(ROOT / "ui"), "run", "e2e", "--", *args.playwright_args]
            log.info("running: %s", " ".join(cmd))
            proc = subprocess.Popen(cmd, cwd=ROOT / "ui", env=env)
            while proc.poll() is None and not stack.server.should_exit:
                time.sleep(0.5)
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=30)
            exit_code = proc.returncode or 0
        else:
            while server_thread.is_alive() and not stack.server.should_exit:
                time.sleep(0.5)
        stack.server.should_exit = True
        server_thread.join(timeout=15)
        if stack.worker_thread is not None and not stack.worker_thread.is_alive():
            log.error("worker thread exited unexpectedly")
    except SystemExit as e:
        exit_code = int(e.code) if isinstance(e.code, int) else 1
        if e.code not in (0, None) and not isinstance(e.code, int):
            log.error("%s", e.code)
    except Exception:
        log.exception("e2e stack failed")
        exit_code = 1
    finally:
        stack.teardown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
