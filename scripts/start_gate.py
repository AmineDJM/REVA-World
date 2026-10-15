#!/usr/bin/env python3
"""Render dockerCommand for reva-gate: the independent promotion gate process.

Mirrors ``start_worker.py`` and fails closed the same way:

* refuses to start in production when REVA_MIGRATION_DATABASE_URL or DATABASE_URL is
  present (render.yaml never gives them to the gate); outside production they are
  stripped;
* refuses a conflicting REVA_PROCESS_ROLE and validates the gate configuration
  (REVA_GATE_DB_PASSWORD, object store) on the environment the child will receive;
* waits with bounded retry until the reva_gate login sees the expected schema and may
  execute ``ops.record_gate_result`` (the API pre-deploy provisions it), then exits
  with a clear error after REVA_GATE_STARTUP_DEADLINE_SECONDS so the platform restarts
  it;
* builds an allowlisted environment that contains the gate's own DB password and
  object-store credential but none of the worker/sim/API passwords, the session
  secret, the admin hash or provider keys, and execs ``python -m reva.gate.service``.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_env import (
    EX_CONFIG,
    EX_TEMPFAIL,
    SRC,
    ConfigError,
    build_child_env,
    check_process_role,
    emit,
    ensure_importable,
    exec_target,
    merged_environ,
    redact,
    validate_for,
)

OWNER_KEYS = ("REVA_MIGRATION_DATABASE_URL", "DATABASE_URL")
#: Other processes' secrets; the gate must never receive them (checked on the child env).
FOREIGN_SECRETS = ("REVA_WORKER_DB_PASSWORD", "REVA_SIM_DB_PASSWORD", "REVA_API_DB_PASSWORD",
                   "REVA_SESSION_SECRET", "REVA_ADMIN_PASSWORD_HASH")
DEFAULT_DEADLINE_SECONDS = 300
REQUIRED_FUNCTION = "ops.record_gate_result(uuid,text,text,text,text,text,jsonb)"


def build_command(environ: Mapping[str, str]) -> list[str]:
    _ = environ
    return [sys.executable, "-m", "reva.gate.service"]


def check_owner_credentials(environ: Mapping[str, str]) -> list[str]:
    """Raise in production if owner credentials are present; else list them for stripping."""
    present = [k for k in OWNER_KEYS if environ.get(k)]
    if present and environ.get("REVA_ENV") == "production":
        raise ConfigError(
            "gate refuses to start: migration-owner credential variable(s) present: "
            + ", ".join(present)
            + " (remove them from the reva-gate service)"
        )
    return present


def validate(environ: Mapping[str, str], dotenv_keys: set[str] | frozenset[str] = frozenset()) -> Any:
    """Validate the configuration the gate child will actually receive (REVA_PROCESS_ROLE=gate)."""
    check_process_role(environ, "gate")
    child, _ = build_child_env(environ, "gate", dotenv_keys=dotenv_keys)
    leaked = [k for k in FOREIGN_SECRETS if child.get(k)]
    if leaked:  # defence in depth: the allowlist must never forward these
        raise ConfigError("gate child environment would contain foreign secrets: " + ", ".join(leaked))
    settings = validate_for(child, "gate")
    if settings.reva_gate_db_password is None:
        raise ConfigError("missing gate configuration: REVA_GATE_DB_PASSWORD")
    settings.runtime_url("gate")  # URL.create; validates encoding/presence
    return settings


def deadline_seconds(environ: Mapping[str, str]) -> float:
    raw = environ.get("REVA_GATE_STARTUP_DEADLINE_SECONDS", str(DEFAULT_DEADLINE_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("REVA_GATE_STARTUP_DEADLINE_SECONDS must be a number") from None
    if not 0 < value <= 3600:
        raise ConfigError("REVA_GATE_STARTUP_DEADLINE_SECONDS must be in (0, 3600]")
    return value


def schema_state(settings: Any) -> dict[str, Any]:
    """One probe with the gate login. Raises when not ready."""
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from reva.adapters.postgres.engine import make_engine
    from reva.cli.bootstrap import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    expected = script.get_current_head()
    known = {rev.revision for rev in script.walk_revisions()}
    state: dict[str, Any] = {"expected_head": expected}
    engine = make_engine("gate", settings)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT (SELECT version_num FROM public.alembic_version LIMIT 1), "
                    "session_user::text, to_regclass('ops.gate_request') IS NOT NULL, "
                    "to_regprocedure(:f) IS NOT NULL AND has_function_privilege(:f, 'EXECUTE')"
                ),
                {"f": REQUIRED_FUNCTION},
            ).one()
    finally:
        engine.dispose()
    version, login, has_requests, can_record = row
    if login != "reva_gate":
        raise RuntimeError(f"gate connected as {login}, expected reva_gate")
    if not (has_requests and can_record):
        raise RuntimeError("gate login lacks gate objects/privileges (bootstrap not finished)")
    if version != expected:
        if version in known:
            raise RuntimeError(f"schema at {version}, image expects {expected} (predeploy pending)")
        state["newer_schema"] = version
    state["gate_ok"] = True
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
        except Exception as e:
            orig = getattr(e, "orig", None)
            last = redact(str(orig) if orig is not None else f"{type(e).__name__}: {e}")
        remaining = deadline - (clock() - start)
        if remaining <= 0:
            raise TimeoutError(f"schema/gate role not ready after {attempts} attempts in {deadline:.0f}s: {last}")
        emit({"event": "reva.start", "component": "gate", "status": "waiting", "attempt": attempts, "reason": last})
        sleep(min(delay, remaining))
        delay = min(delay * 2, 30.0)


def prepare(
    environ: Mapping[str, str],
    dotenv_keys: set[str] | frozenset[str] = frozenset(),
    extra_pythonpath: str | None = None,
) -> tuple[list[str], dict[str, str], dict[str, Any], Any]:
    stripped = check_owner_credentials(environ)
    settings = validate(environ, dotenv_keys)
    child, removed = build_child_env(environ, "gate", dotenv_keys=dotenv_keys, extra_pythonpath=extra_pythonpath)
    command = exec_target(environ, build_command(environ))
    summary: dict[str, Any] = {
        "env": settings.reva_env,
        "process_role": "gate",
        "removed_env": removed,
        "stripped_owner_keys": stripped,
        "threads": child["OMP_NUM_THREADS"],
        "argv": build_command(environ),
    }
    return command, child, summary, settings


def main() -> int:
    try:
        added = ensure_importable()
        environ, dotenv_keys = merged_environ()
        command, child, summary, settings = prepare(environ, dotenv_keys, str(SRC) if added else None)
        deadline = deadline_seconds(environ)
    except ConfigError as e:
        emit({"event": "reva.start", "component": "gate", "status": "error", "error": redact(str(e))})
        return EX_CONFIG
    except Exception as e:
        emit({"event": "reva.start", "component": "gate", "status": "error",
              "error": redact(f"{type(e).__name__}: {e}")})
        return EX_CONFIG
    skip_wait = environ.get("REVA_ENV") == "test" and environ.get("REVA_SKIP_SCHEMA_WAIT") == "1"
    if not skip_wait:
        try:
            summary["schema"] = wait_for_schema(settings, deadline)
        except TimeoutError as e:
            emit({"event": "reva.start", "component": "gate", "status": "error", "error": redact(str(e))})
            return EX_TEMPFAIL
    emit({"event": "reva.start", "component": "gate", "status": "exec", **summary})
    os.execvpe(command[0], command, child)  # noqa: S606 - replaces this process; no shell
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
