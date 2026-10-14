#!/usr/bin/env python3
"""Render preDeployCommand: provision schema and runtime roles.

Wraps ``reva.cli.bootstrap.bootstrap(load_settings())``:

1. acquire the migration advisory lock;
2. verify PostgreSQL >= 17, PostGIS availability and CREATEROLE capability;
3. apply Alembic migrations with the migration-owner credential;
4. set LOGIN + generated passwords on reva_api / reva_worker / reva_sim;
5. verify that runtime roles own no scientific objects and hold no deletion role;
6. release the lock.

Prints exactly one JSON report on stdout (no secrets). On failure prints a
redacted JSON error on stderr and exits non-zero, which aborts the Render deploy.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_env import EX_CONFIG, ConfigError, emit, ensure_importable, redact


def main() -> int:
    started = time.monotonic()
    try:
        ensure_importable()
        from reva.cli.bootstrap import bootstrap
        from reva.kernel.errors import ConfigurationError
        from reva.settings import load_settings
    except Exception as e:
        emit({"event": "reva.bootstrap", "status": "error", "error": redact(f"{type(e).__name__}: {e}")})
        return 1
    try:
        # The pre-deploy runs in the API service's environment (which may declare
        # REVA_PROCESS_ROLE=api); this process is the bootstrap role, the only one
        # allowed to hold the migration-owner URL.
        os.environ["REVA_PROCESS_ROLE"] = "bootstrap"
        settings = load_settings()
        validate = getattr(settings, "validate_for", None)
        if validate is not None:
            validate("bootstrap")
        report = bootstrap(settings)
    except (ConfigurationError, ConfigError) as e:
        emit({"event": "reva.bootstrap", "status": "error", "kind": "configuration", "error": redact(str(e))})
        return EX_CONFIG
    except Exception as e:
        # SQLAlchemy wraps driver errors; the wrapped message may echo bound
        # parameters (role passwords). Report only the driver-level message.
        orig = getattr(e, "orig", None)
        detail = str(orig) if orig is not None else str(e)
        emit({"event": "reva.bootstrap", "status": "error", "kind": type(e).__name__, "error": redact(detail)})
        return 1
    out = {
        "event": "reva.bootstrap",
        "status": "ok",
        "schema_version": report.get("schema_version"),
        "capabilities": report.get("capabilities"),
        "roles": report.get("roles"),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    emit(out, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
