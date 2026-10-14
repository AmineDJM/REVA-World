#!/usr/bin/env python3
"""Render dockerCommand for reva-worker: refuse owner creds, wait, scrub, exec.

* refuses to start in production when REVA_MIGRATION_DATABASE_URL or
  DATABASE_URL is present (render.yaml never gives them to the worker); outside
  production they are stripped;
* validates worker configuration (worker + simulation role passwords, object
  store, contact e-mail for provider user agents in production);
* waits with bounded retry for the expected schema and runtime roles (initial
  deploy ordering between API predeploy and worker is not guaranteed), then exits
  with a clear error after REVA_WORKER_STARTUP_DEADLINE_SECONDS so the platform
  restarts it;
* sets OMP/OpenBLAS/MKL/Polars thread variables from the explicit CPU budget
  (never os.cpu_count());
* builds an allowlisted environment without the API password, session secret or
  admin hash and execs ``python -m reva.operations.worker``. The worker keeps the
  PID, so SIGTERM from the platform reaches it directly for drain/checkpoint.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_env import (  # noqa: E402
    EX_CONFIG,
    EX_TEMPFAIL,
    SRC,
    ConfigError,
    build_child_env,
    emit,
    ensure_importable,
    exec_target,
    isolated_settings,
    merged_environ,
    redact,
)

OWNER_KEYS = ("REVA_MIGRATION_DATABASE_URL", "DATABASE_URL")
DEFAULT_DEADLINE_SECONDS = 300
REQUIRED_FUNCTION = "ops.complete_job(uuid,text,bigint,text,jsonb,jsonb)"


def build_command(environ: Mapping[str, str]) -> list[str]:
    _ = environ
    return [sys.executable, "-m", "reva.operations.worker"]


def check_owner_credentials(environ: Mapping[str, str]) -> list[str]:
    """Raise in production if owner credentials are present; else list them for stripping."""
    present = [k for k in OWNER_KEYS if environ.get(k)]
    if present and environ.get("REVA_ENV") == "production":
        raise ConfigError(
            "worker refuses to start: migration-owner credential variable(s) present: "
            + ", ".join(present) + " (remove them from the reva-worker service)"
        )
    return present


def validate(environ: Mapping[str, str]) -> Any:
    settings = isolated_settings(environ)
    problems = [
        name for name, value in (
            ("REVA_WORKER_DB_PASSWORD", settings.reva_worker_db_password),
            ("REVA_SIM_DB_PASSWORD", settings.reva_sim_db_password),
        ) if value is None
    ]
    if settings.reva_env == "production" and not settings.reva_http_contact_email:
        problems.append("REVA_HTTP_CONTACT_EMAIL (provider user-agent contact)")
    if problems:
        raise ConfigError("missing worker configuration: " + "; ".join(problems))
    settings.runtime_url("worker")  # URL.create; validates encoding/presence
    settings.runtime_url("sim")
    return settings


def deadline_seconds(environ: Mapping[str, str]) -> float:
    raw = environ.get("REVA_WORKER_STARTUP_DEADLINE_SECONDS", str(DEFAULT_DEADLINE_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("REVA_WORKER_STARTUP_DEADLINE_SECONDS must be a number") from None
    if not 0 < value <= 3600:
        raise ConfigError("REVA_WORKER_STARTUP_DEADLINE_SECONDS must be in (0, 3600]")
    return value


def schema_state(settings: Any) -> dict[str, Any]:
    """One probe with the worker and simulation logins. Raises when not ready."""
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from reva.adapters.postgres.engine import make_engine
    from reva.cli.bootstrap import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    expected = script.get_current_head()
    known = {rev.revision for rev in script.walk_revisions()}
    state: dict[str, Any] = {"expected_head": expected}
    for kind in ("worker", "sim"):
        engine = make_engine(kind, settings)  # type: ignore[arg-type]
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT (SELECT version_num FROM public.alembic_version LIMIT 1), "
                        "to_regclass('ops.job') IS NOT NULL, "
                        "to_regprocedure(:f) IS NOT NULL AND has_function_privilege(:f, 'EXECUTE')"
                    ),
                    {"f": REQUIRED_FUNCTION},
                ).one()
        finally:
            engine.dispose()
        version, has_queue, can_complete = row
        if not (has_queue and can_complete):
            raise RuntimeError(f"{kind} login lacks queue objects/privileges (bootstrap not finished)")
        if version != expected:
            if version in known:
                raise RuntimeError(f"schema at {version}, image expects {expected} (predeploy pending)")
            # Unknown revision: the database is newer than this image. Migrations are
            # expand-compatible with the previous release, so proceed and report it.
            state["newer_schema"] = version
        state[f"{kind}_ok"] = True
        state["schema_version"] = version
    return state


def wait_for_schema(
    settings: Any,
    deadline: float,
    *,
    probe: Callable[[Any], dict[str, Any]] = schema_state,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    start = clock()
    delay = 2.0
    attempts = 0
    last = "not attempted"
    while True:
        attempts += 1
        try:
            state = probe(settings)
            state["attempts"] = attempts
            return state
        except Exception as e:  # noqa: BLE001 - every failure is retried until the deadline
            orig = getattr(e, "orig", None)
            last = redact(str(orig) if orig is not None else f"{type(e).__name__}: {e}")
        remaining = deadline - (clock() - start)
        if remaining <= 0:
            raise TimeoutError(f"schema/roles not ready after {attempts} attempts in {deadline:.0f}s: {last}")
        emit({"event": "reva.start", "component": "worker", "status": "waiting", "attempt": attempts,
              "reason": last})
        sleep(min(delay, remaining))
        delay = min(delay * 2, 30.0)


def prepare(environ: Mapping[str, str], dotenv_keys: set[str] | frozenset[str] = frozenset(),
            extra_pythonpath: str | None = None) -> tuple[list[str], dict[str, str], dict[str, Any], Any]:
    stripped = check_owner_credentials(environ)
    settings = validate(environ)
    child, removed = build_child_env(environ, "worker", dotenv_keys=dotenv_keys,
                                     extra_pythonpath=extra_pythonpath)
    command = exec_target(environ, build_command(environ))
    summary: dict[str, Any] = {"env": settings.reva_env, "removed_env": removed, "stripped_owner_keys": stripped,
                               "threads": child["OMP_NUM_THREADS"], "argv": build_command(environ)}
    return command, child, summary, settings


def main() -> int:
    try:
        added = ensure_importable()
        environ, dotenv_keys = merged_environ()
        command, child, summary, settings = prepare(environ, dotenv_keys, str(SRC) if added else None)
        deadline = deadline_seconds(environ)
    except ConfigError as e:
        emit({"event": "reva.start", "component": "worker", "status": "error", "error": redact(str(e))})
        return EX_CONFIG
    except Exception as e:  # noqa: BLE001
        emit({"event": "reva.start", "component": "worker", "status": "error",
              "error": redact(f"{type(e).__name__}: {e}")})
        return EX_CONFIG
    skip_wait = environ.get("REVA_ENV") == "test" and environ.get("REVA_SKIP_SCHEMA_WAIT") == "1"
    if not skip_wait:
        try:
            summary["schema"] = wait_for_schema(settings, deadline)
        except TimeoutError as e:
            emit({"event": "reva.start", "component": "worker", "status": "error", "error": redact(str(e))})
            return EX_TEMPFAIL
    emit({"event": "reva.start", "component": "worker", "status": "exec", **summary})
    os.execvpe(command[0], command, child)  # noqa: S606 - replaces this process; no shell
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
