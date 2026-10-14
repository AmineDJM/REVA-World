"""Shared helpers for the process entrypoints (bootstrap/start_api/start_worker).

These scripts run as ``python scripts/<name>.py`` both from the Docker image
(package installed editable into /opt/venv, sources at /app/src) and from a
repository checkout. They build an *allowlisted* child environment: anything not
explicitly needed by the component is dropped, and the migration-owner
credential is never forwarded.

Nothing in this module prints secret values. Error messages are passed through
``redact`` before they are written.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

Component = Literal["api", "worker"]

# Exit codes (sysexits.h): configuration error / temporary failure.
EX_CONFIG = 78
EX_TEMPFAIL = 75


def ensure_importable() -> bool:
    """Make ``reva`` importable. Returns True when ``src`` had to be added."""
    try:
        import reva
    except ModuleNotFoundError:
        sys.path.insert(0, str(SRC))
        import reva  # noqa: F401

        return True
    return False


# --- environment classification ---------------------------------------------

#: Always removed from every runtime child environment.
NEVER_FORWARD = frozenset({"REVA_MIGRATION_DATABASE_URL", "DATABASE_URL", "PGPASSWORD", "PGPASSFILE"})

#: Secrets each component legitimately needs. Everything else secret is dropped.
COMPONENT_SECRETS: dict[Component, frozenset[str]] = {
    # The API's S3 credential is the read-only API credential configured on the
    # reva-api service (render.yaml); the worker's prefix-scoped write credential
    # is configured only on reva-worker.
    "api": frozenset(
        {
            "REVA_API_DB_PASSWORD",
            "REVA_SESSION_SECRET",
            "REVA_ADMIN_PASSWORD_HASH",
            "REVA_S3_ACCESS_KEY_ID",
            "REVA_S3_SECRET_ACCESS_KEY",
        }
    ),
    "worker": frozenset(
        {
            "REVA_WORKER_DB_PASSWORD",
            "REVA_SIM_DB_PASSWORD",
            "REVA_S3_ACCESS_KEY_ID",
            "REVA_S3_SECRET_ACCESS_KEY",
            "REVA_COMTRADE_API_KEY",
            "REVA_SANDBOX_TOKEN",
            "REVA_LLM_API_KEY",  # forwarded only when REVA_LLM_ENABLED is true
        }
    ),
}

SYSTEM_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "HOSTNAME",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TERM",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONFAULTHANDLER",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "PORT",
    }
)
# Render's documented default (non-secret) service variables.
RENDER_KEYS = frozenset(
    {
        "RENDER",
        "RENDER_SERVICE_ID",
        "RENDER_SERVICE_NAME",
        "RENDER_SERVICE_TYPE",
        "RENDER_INSTANCE_ID",
        "RENDER_GIT_COMMIT",
        "RENDER_GIT_BRANCH",
        "RENDER_GIT_REPO_SLUG",
        "RENDER_EXTERNAL_URL",
        "RENDER_EXTERNAL_HOSTNAME",
        "RENDER_DISCOVERY_SERVICE",
        "IS_PULL_REQUEST",
    }
)
# Only the worker performs outbound HTTP (through SafeHttpClient).
PROXY_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"})
#: Non-secret REVA_* variables used by the entrypoints themselves or the build.
EXTRA_REVA_CONFIG = frozenset(
    {
        "REVA_FORWARDED_ALLOW_IPS",
        "REVA_GIT_COMMIT",
        "REVA_IMAGE_DIGEST",
        "REVA_WORKER_STARTUP_DEADLINE_SECONDS",
    }
)
THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "POLARS_MAX_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "RAYON_NUM_THREADS",
)
#: Variables honoured only when REVA_ENV=test (never in local/production).
TEST_ONLY = frozenset({"REVA_EXEC_OVERRIDE", "REVA_SKIP_SCHEMA_WAIT"})

SECRET_NAME = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|ACCESS_KEY|PRIVATE|CREDENTIAL|DATABASE_URL|_DSN$|_HASH$)"
)


def _secret_env_keys() -> frozenset[str]:
    from reva.settings import SECRET_ENV_KEYS

    return frozenset(SECRET_ENV_KEYS)


def is_secret_name(name: str) -> bool:
    return name in _secret_env_keys() or name in NEVER_FORWARD or bool(SECRET_NAME.search(name))


def known_reva_config() -> frozenset[str]:
    """Non-secret REVA_* settings fields (derived from the Settings model)."""
    from reva.settings import Settings

    names = {f.upper() for f in Settings.model_fields if f.startswith("reva_")}
    return frozenset(n for n in names if not is_secret_name(n)) | EXTRA_REVA_CONFIG


def truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


# --- CPU budget ---------------------------------------------------------------


def cgroup_cpu_limit(root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """CPU quota from cgroup v2 (cpu.max) or v1 (cfs quota). None when unlimited."""
    try:
        quota, period = (root / "cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, int(quota) // int(period))
        return None
    except (OSError, ValueError):
        pass
    try:
        q = int((root / "cpu" / "cpu.cfs_quota_us").read_text().strip())
        p = int((root / "cpu" / "cpu.cfs_period_us").read_text().strip())
        if q > 0 and p > 0:
            return max(1, q // p)
    except (OSError, ValueError):
        pass
    return None


def thread_budget(environ: Mapping[str, str], cgroup_root: Path = Path("/sys/fs/cgroup")) -> int:
    """Explicit REVA_CPU_BUDGET_THREADS, capped by the cgroup quota.

    Never derived from os.cpu_count(): on shared hosts that reports the host's
    CPUs, not this container's allowance (invariant 29).
    """
    raw = environ.get("REVA_CPU_BUDGET_THREADS", "1").strip() or "1"
    try:
        budget = int(raw)
    except ValueError:
        raise ValueError("REVA_CPU_BUDGET_THREADS must be a positive integer") from None
    if budget < 1:
        raise ValueError("REVA_CPU_BUDGET_THREADS must be a positive integer")
    limit = cgroup_cpu_limit(cgroup_root)
    if limit is not None:
        budget = min(budget, limit)
    return budget


# --- child environment ----------------------------------------------------------


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a local .env (development convenience only)."""
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def merged_environ(
    environ: Mapping[str, str] | None = None, cwd: Path | None = None
) -> tuple[dict[str, str], set[str]]:
    """Process environment plus a local .env (process env wins).

    Returns (merged, dotenv_keys). In production a .env file is refused: the
    image never contains one and configuration comes from the platform.
    """
    env = dict(os.environ if environ is None else environ)
    dotenv_path = (cwd or Path.cwd()) / ".env"
    if env.get("REVA_ENV") == "production":
        if dotenv_path.exists():
            raise ConfigError(".env file present in the working directory in production; refusing to start")
        return env, set()
    dot = read_dotenv(dotenv_path)
    merged = {**dot, **env}
    return merged, set(dot)


class ConfigError(Exception):
    """Configuration problem; the message must never contain secret values."""


def build_child_env(
    environ: Mapping[str, str],
    component: Component,
    *,
    dotenv_keys: Iterable[str] = (),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    extra_pythonpath: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Allowlisted environment for ``component``. Returns (child_env, removed_names).

    * migration-owner URL/DATABASE_URL and other components' secrets are removed;
    * unknown variables are removed unless they are system/Render/OTel variables or
      non-secret REVA_* configuration;
    * thread variables are set from the explicit CPU budget;
    * keys that a local .env would re-inject into the child's Settings are
      neutralised to empty strings (pydantic-settings gives process env priority).
    """
    allowed_secrets = set(COMPONENT_SECRETS[component])
    if component == "worker" and not truthy(environ.get("REVA_LLM_ENABLED")):
        allowed_secrets.discard("REVA_LLM_API_KEY")
    reva_config = known_reva_config()

    child: dict[str, str] = {}
    removed: list[str] = []
    for key, value in environ.items():
        keep = False
        if key in NEVER_FORWARD:
            keep = False
        elif key in TEST_ONLY:
            keep = False  # consumed by the entrypoint itself, never by the child
        elif key in allowed_secrets:
            keep = True
        elif is_secret_name(key):
            keep = False
        elif key in SYSTEM_KEYS or key in RENDER_KEYS or key.startswith("LC_") or key in reva_config:
            keep = True
        elif key.startswith("OTEL_"):
            keep = True
        elif key in PROXY_KEYS:
            keep = component == "worker"
        elif key in THREAD_VARS:
            keep = False  # re-derived from the budget below
        if keep:
            child[key] = value
        else:
            removed.append(key)

    threads = str(thread_budget(environ, cgroup_root))
    for var in THREAD_VARS:
        child[var] = threads

    # A local .env would be re-read by the child's Settings(); blank the
    # removed secrets so that the child cannot recover them from the file.
    for key in dotenv_keys:
        if key not in child and is_secret_name(key):
            child[key] = ""

    # The child declares which process it is (Settings applies per-role production
    # rules, e.g. no session secret is expected in the worker).
    child["REVA_PROCESS_ROLE"] = component

    if extra_pythonpath:
        existing = child.get("PYTHONPATH")
        child["PYTHONPATH"] = extra_pythonpath + (os.pathsep + existing if existing else "")
    child.setdefault("PYTHONUNBUFFERED", "1")
    child.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return child, sorted(set(removed))


# --- settings validation (isolated from the parent's os.environ) ---------------


def check_process_role(environ: Mapping[str, str], component: str) -> None:
    """Refuse a conflicting REVA_PROCESS_ROLE (e.g. the worker command on the API service)."""
    declared = (environ.get("REVA_PROCESS_ROLE") or "").strip()
    if declared and declared != component:
        raise ConfigError(f"REVA_PROCESS_ROLE={declared} but this entrypoint starts the {component} process")


def validate_for(environ: Mapping[str, str], role: str) -> Any:
    """Isolated Settings for ``environ`` plus ``Settings.validate_for(role)`` (names only)."""
    from reva.kernel.errors import ConfigurationError

    settings = isolated_settings({**environ, "REVA_PROCESS_ROLE": role})
    checker = getattr(settings, "validate_for", None)
    if checker is not None:
        try:
            checker(role)
        except ConfigurationError as e:
            raise ConfigError(str(getattr(e, "message", e))) from None
    return settings


def isolated_settings(environ: Mapping[str, str]) -> Any:
    """Validate configuration from exactly ``environ`` (no os.environ, no .env)."""
    from pydantic import ValidationError

    from reva.settings import Settings

    class _Isolated(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, *args, **kwargs):  # type: ignore[no-untyped-def,override]
            return (init_settings,)

    fields = Settings.model_fields
    values = {name: environ[name.upper()] for name in fields if name.upper() in environ}
    # Empty strings mean "not set" for optional values (neutralised .env keys).
    values = {k: v for k, v in values.items() if v != ""}
    try:
        return _Isolated(**values)
    except ValidationError as e:
        msgs = [f"{'.'.join(map(str, err['loc'])) or 'settings'}: {err['msg']}" for err in e.errors()]
        raise ConfigError("configuration invalid: " + " | ".join(msgs)) from None


# --- redaction / reporting --------------------------------------------------------

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<pw>[^@\s]*)@")
_PARAMS = re.compile(r"\[parameters: .*?\]", re.DOTALL)


def redact(message: str, environ: Mapping[str, str] | None = None) -> str:
    """Remove secret values, URL passwords and SQL bound parameters from text."""
    env = os.environ if environ is None else environ
    out = _PARAMS.sub("[parameters: <redacted>]", message)
    out = _URL_USERINFO.sub(lambda m: f"{m['scheme']}{m['user']}:***@", out)
    for key, value in env.items():
        if value and len(value) >= 4 and is_secret_name(key):
            out = out.replace(value, "***")
    return out


def emit(event: dict[str, Any], stream: Any = None) -> None:
    """One JSON line (stderr by default). Callers pass only non-secret fields."""
    print(json.dumps(event, sort_keys=True, default=str), file=stream or sys.stderr, flush=True)


def exec_target(environ: Mapping[str, str], command: list[str]) -> list[str]:
    """Apply the test-only exec override (REVA_ENV=test). Refuses it elsewhere."""
    override = environ.get("REVA_EXEC_OVERRIDE")
    if not override:
        return command
    if environ.get("REVA_ENV") != "test":
        raise ConfigError("REVA_EXEC_OVERRIDE is honoured only when REVA_ENV=test")
    return [sys.executable, override, *command]
