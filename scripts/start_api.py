#!/usr/bin/env python3
"""Render dockerCommand for reva-api: validate, scrub, exec Uvicorn.

* validates the required configuration (production rules from Settings plus
  API-specific requirements);
* checks that the runtime DSN can be built safely (``Settings.runtime_url`` uses
  ``URL.create``; generated passwords may contain URL-special characters). The
  DSN itself is not exported: the app builds it from component settings;
* builds an allowlisted child environment without the migration-owner URL,
  the worker/simulation passwords or unrelated secrets;
* ``os.execvpe`` Uvicorn: one worker, proxy headers trusted only from private
  ranges (Render's internal load balancer) unless REVA_FORWARDED_ALLOW_IPS says
  otherwise.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_env import (
    EX_CONFIG,
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

APP = "reva.api.app:app"
# Loopback plus RFC1918/CGNAT ranges: the platform proxy connects from a private
# address; the service has no other ingress. Override with REVA_FORWARDED_ALLOW_IPS.
DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"
GRACEFUL_SHUTDOWN_SECONDS = 50  # below render.yaml maxShutdownDelaySeconds=60


def _port(environ: Mapping[str, str]) -> int:
    raw = environ.get("PORT", "8000")
    try:
        port = int(raw)
    except ValueError:
        raise ConfigError("PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ConfigError("PORT out of range")
    return port


def build_command(environ: Mapping[str, str]) -> list[str]:
    ips = environ.get("REVA_FORWARDED_ALLOW_IPS", DEFAULT_FORWARDED_ALLOW_IPS).strip()
    if not ips:
        raise ConfigError("REVA_FORWARDED_ALLOW_IPS must not be empty")
    return [
        sys.executable,
        "-m",
        "uvicorn",
        APP,
        "--host",
        "0.0.0.0",  # noqa: S104 - container ingress; Render routes to $PORT
        "--port",
        str(_port(environ)),
        "--workers",
        "1",
        "--proxy-headers",
        "--forwarded-allow-ips",
        ips,
        "--timeout-graceful-shutdown",
        str(GRACEFUL_SHUTDOWN_SECONDS),
        "--no-server-header",
    ]


def validate(environ: Mapping[str, str]) -> dict[str, object]:
    """Validate API configuration from ``environ``. Returns a non-secret summary."""
    settings = isolated_settings(environ)
    problems: list[str] = []
    if settings.reva_api_db_password is None:
        problems.append("REVA_API_DB_PASSWORD")
    if settings.reva_env == "production":
        h = settings.reva_admin_password_hash
        if h is None or not h.get_secret_value().startswith("$argon2id$"):
            problems.append("REVA_ADMIN_PASSWORD_HASH (Argon2id; generate with `reva auth hash-password`)")
        if not settings.allowed_hosts:
            problems.append("REVA_ALLOWED_HOSTS")
        if not settings.reva_s3_region and not settings.reva_s3_endpoint_url:
            problems.append("REVA_S3_REGION or REVA_S3_ENDPOINT_URL")
    if problems:
        raise ConfigError("missing/invalid API configuration: " + "; ".join(problems))
    url = settings.runtime_url("api")  # URL.create; raises if the password is missing
    return {
        "env": settings.reva_env,
        "db": url.render_as_string(hide_password=True),
        "object_backend": settings.reva_object_backend,
    }


def prepare(
    environ: Mapping[str, str],
    dotenv_keys: set[str] | frozenset[str] = frozenset(),
    extra_pythonpath: str | None = None,
) -> tuple[list[str], dict[str, str], dict[str, object]]:
    summary = validate(environ)
    child, removed = build_child_env(environ, "api", dotenv_keys=dotenv_keys, extra_pythonpath=extra_pythonpath)
    command = exec_target(environ, build_command(environ))
    summary.update({"removed_env": removed, "threads": child["OMP_NUM_THREADS"], "argv": build_command(environ)})
    return command, child, summary


def main() -> int:
    try:
        added = ensure_importable()
        environ, dotenv_keys = merged_environ()
        command, child, summary = prepare(environ, dotenv_keys, str(SRC) if added else None)
    except ConfigError as e:
        emit({"event": "reva.start", "component": "api", "status": "error", "error": redact(str(e))})
        return EX_CONFIG
    except Exception as e:
        emit(
            {"event": "reva.start", "component": "api", "status": "error", "error": redact(f"{type(e).__name__}: {e}")}
        )
        return EX_CONFIG
    emit({"event": "reva.start", "component": "api", "status": "exec", **summary})
    os.execvpe(command[0], command, child)  # noqa: S606 - replaces this process; no shell
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
