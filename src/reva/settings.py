"""Configuration with production validation and secret redaction.

Production validation has two layers:

* ``Settings`` itself (model validator) checks role-independent rules whenever
  ``REVA_ENV=production``: S3 object storage, https public URL, TLS to
  PostgreSQL (``REVA_DB_SSLMODE`` in require/verify-ca/verify-full), no
  wildcard CORS, well-formed optional secrets (an admin hash, when present,
  must be Argon2id), the LLM budget, and the session secret (unless the
  process role is a non-API role).
* Per-process requirements come from ``Settings.required_for(role)`` /
  ``validate_for(role)``; they are enforced automatically when
  ``REVA_PROCESS_ROLE`` is set, and entrypoints call them explicitly:

  ========== ==============================================================
  role        required in production (besides the role-independent rules)
  ========== ==============================================================
  api         REVA_API_DB_PASSWORD, REVA_SESSION_SECRET (>=32 chars),
              REVA_ADMIN_PASSWORD_HASH ($argon2id$...), non-local
              REVA_ALLOWED_HOSTS, no REVA_MIGRATION_DATABASE_URL
  worker      REVA_WORKER_DB_PASSWORD, REVA_SIM_DB_PASSWORD,
              no REVA_MIGRATION_DATABASE_URL
  sim         REVA_SIM_DB_PASSWORD, no REVA_MIGRATION_DATABASE_URL
  bootstrap   REVA_MIGRATION_DATABASE_URL and all three runtime role passwords
  ========== ==============================================================

  Outside production only the runtime role password(s) are required.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from reva.kernel.errors import ConfigurationError

SECRET_ENV_KEYS = frozenset(
    {
        "REVA_MIGRATION_DATABASE_URL",
        "REVA_API_DB_PASSWORD",
        "REVA_WORKER_DB_PASSWORD",
        "REVA_SIM_DB_PASSWORD",
        "REVA_S3_ACCESS_KEY_ID",
        "REVA_S3_SECRET_ACCESS_KEY",
        "REVA_SESSION_SECRET",
        "REVA_ADMIN_PASSWORD_HASH",
        "REVA_COMTRADE_API_KEY",
        "REVA_LLM_API_KEY",
        "REVA_SANDBOX_TOKEN",
        "DATABASE_URL",
    }
)

#: Common secret-bearing names outside the Settings model (redacted, never logged).
COMMON_SECRET_ENV_KEYS = frozenset(
    {
        "PGPASSWORD",
        "PGPASSFILE",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    }
)
#: Name patterns treated as secret by redaction (*PASSWORD*, *SECRET*, *TOKEN*, *KEY*, ...).
SECRET_NAME_PATTERN = re.compile(
    r"(PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE|COOKIE|_DSN$|^DSN$|DATABASE_URL)",
    re.IGNORECASE,
)
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<pw>[^@\s/]*)@")
ARGON2ID_HASH = re.compile(r"^\$argon2id\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+={0,2}\$[A-Za-z0-9+/]+={0,2}$")
SSL_MODES_PRODUCTION = ("require", "verify-ca", "verify-full")

Role = Literal["api", "worker", "sim"]
ProcessRole = Literal["api", "worker", "sim", "bootstrap"]
ROLE_LOGIN = {"api": "reva_api", "worker": "reva_worker", "sim": "reva_sim"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    reva_env: Literal["local", "test", "production"] = "local"
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # pydantic includes each error's ``input`` (for model-level checks: the whole
        # input mapping, raw secrets included) in ValidationError.errors(); re-raise
        # with every input replaced, so no construction path can echo a secret.
        try:
            super().__init__(*args, **kwargs)
        except ValidationError as e:
            raise _redacted_validation_error(e) from None

    #: Which process this configuration is for; enables per-role production checks.
    reva_process_role: ProcessRole | None = None
    reva_log_level: str = "INFO"

    reva_migration_database_url: SecretStr | None = None
    reva_db_host: str = "127.0.0.1"
    reva_db_port: int = 5432
    reva_db_name: str = "reva"
    reva_api_db_password: SecretStr | None = None
    reva_worker_db_password: SecretStr | None = None
    reva_sim_db_password: SecretStr | None = None
    reva_db_pool_size: int = Field(2, ge=1, le=20)
    reva_db_max_overflow: int = Field(0, ge=0, le=20)
    reva_db_statement_timeout_ms: int = Field(10000, ge=100)
    reva_db_sslmode: str | None = None

    reva_object_backend: Literal["filesystem", "s3"] = "filesystem"
    reva_object_root: Path = Path("./var/objects")
    reva_s3_endpoint_url: str | None = None
    reva_s3_region: str | None = None
    reva_s3_bucket: str | None = None
    reva_s3_access_key_id: SecretStr | None = None
    reva_s3_secret_access_key: SecretStr | None = None
    reva_s3_prefix: str = "reva/"

    reva_session_secret: SecretStr | None = None
    reva_admin_password_hash: SecretStr | None = None
    reva_public_base_url: str = "http://localhost:8000"
    reva_allowed_hosts: str = "localhost,127.0.0.1"
    reva_cors_origins: str = ""

    reva_worker_concurrency: int = Field(1, ge=1, le=4)
    reva_job_lease_seconds: int = Field(120, ge=10)
    reva_job_heartbeat_seconds: int = Field(30, ge=1)
    reva_job_max_attempts: int = Field(5, ge=1)
    reva_scheduler_tick_seconds: int = Field(30, ge=1)
    reva_cpu_budget_threads: int = Field(1, ge=1)
    reva_duckdb_memory_limit: str = "256MB"
    reva_http_timeout_seconds: float = 30
    reva_http_max_bytes: int = 52_428_800
    reva_http_contact_email: str | None = None
    #: Outbound HTTP egress: "direct" (DNS-pinned, default in production) or "env"
    #: (HTTPS_PROXY from the environment; the proxy then enforces SSRF policy).
    reva_http_proxy_mode: Literal["direct", "env"] | None = None
    reva_cache_max_bytes: int = 536_870_912
    reva_comtrade_api_key: SecretStr | None = None

    reva_llm_enabled: bool = False
    reva_llm_monthly_budget_usd: float = 0
    reva_llm_provider: str | None = None
    reva_llm_model: str | None = None
    reva_llm_api_key: SecretStr | None = None

    reva_autonomy_mode: Literal["template_only", "disabled"] = "template_only"
    reva_autonomy_kill_switch: bool = False
    reva_sandbox_endpoint: str | None = None
    reva_sandbox_token: SecretStr | None = None

    port: int = 8000

    @model_validator(mode="after")
    def _production(self) -> Settings:
        if self.reva_env != "production":
            return self
        missing = []
        if self.reva_object_backend != "s3":
            missing.append("REVA_OBJECT_BACKEND=s3 (filesystem is never selected in production)")
        for name in ("reva_s3_bucket", "reva_s3_access_key_id", "reva_s3_secret_access_key"):
            if not getattr(self, name):
                missing.append(name.upper())
        # The session secret is an API secret; non-API processes (whose environment
        # never receives it) declare themselves through REVA_PROCESS_ROLE.
        if self.reva_process_role in (None, "api") and (
            not self.reva_session_secret or len(self.reva_session_secret.get_secret_value()) < 32
        ):
            missing.append("REVA_SESSION_SECRET (>=32 chars)")
        # Only the API serves HTTP; non-API processes have no public URL.
        if self.reva_process_role in (None, "api") and self.reva_public_base_url.startswith("http://"):
            missing.append("REVA_PUBLIC_BASE_URL must be https in production")
        if self.reva_llm_enabled and self.reva_llm_monthly_budget_usd <= 0:
            missing.append("REVA_LLM_MONTHLY_BUDGET_USD > 0 when LLM is enabled")
        if self.reva_db_sslmode not in SSL_MODES_PRODUCTION:
            missing.append("REVA_DB_SSLMODE must be one of " + "/".join(SSL_MODES_PRODUCTION) + " in production")
        if "*" in self.cors_origins:
            missing.append("REVA_CORS_ORIGINS must not contain '*' in production")
        if self.reva_admin_password_hash is not None and not _is_argon2id(self.reva_admin_password_hash):
            missing.append("REVA_ADMIN_PASSWORD_HASH must be an Argon2id hash ($argon2id$...)")
        if self.reva_process_role is not None:
            missing.extend(m for m in self.required_for(self.reva_process_role) if m not in missing)
        if missing:
            raise ValueError("invalid production configuration: " + "; ".join(missing))
        return self

    # --- per-process requirements ------------------------------------------
    def required_for(self, role: ProcessRole) -> list[str]:
        """Missing/invalid settings for starting ``role`` (empty list when ready).

        Names only, never values. Outside production only the database
        credentials the role logs in with are required.
        """
        problems: list[str] = []
        needed: dict[ProcessRole, tuple[Role, ...]] = {
            "api": ("api",),
            "worker": ("worker", "sim"),
            "sim": ("sim",),
            "bootstrap": ("api", "worker", "sim"),
        }
        if role not in needed:
            raise ValueError(f"unknown process role {role!r}")
        for r in needed[role]:
            if self.role_password(r) is None or not self.role_password(r).get_secret_value():  # type: ignore[union-attr]
                problems.append(f"REVA_{r.upper()}_DB_PASSWORD")
        if role == "bootstrap" and self.reva_migration_database_url is None:
            problems.append("REVA_MIGRATION_DATABASE_URL")
        if self.reva_env != "production":
            return problems
        if role in ("api", "worker", "sim") and self.reva_migration_database_url is not None:
            problems.append("REVA_MIGRATION_DATABASE_URL must not be present in a runtime process")
        if role == "api":
            if not self.reva_session_secret or len(self.reva_session_secret.get_secret_value()) < 32:
                problems.append("REVA_SESSION_SECRET (>=32 chars)")
            if self.reva_admin_password_hash is None or not _is_argon2id(self.reva_admin_password_hash):
                problems.append("REVA_ADMIN_PASSWORD_HASH (Argon2id; generate with `reva auth hash-password`)")
            if not self.allowed_hosts or set(self.allowed_hosts) <= {"localhost", "127.0.0.1", "::1"}:
                problems.append("REVA_ALLOWED_HOSTS (public hostname(s), not only localhost)")
        return problems

    def validate_for(self, role: ProcessRole) -> Settings:
        """Raise ``ConfigurationError`` naming every missing setting for ``role``."""
        problems = self.required_for(role)
        if problems:
            raise ConfigurationError(f"configuration incomplete for {role}: " + "; ".join(problems))
        return self

    # --- derived ---------------------------------------------------------
    @property
    def allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self.reva_allowed_hosts.split(",") if h.strip()]

    @property
    def cors_origins(self) -> list[str]:
        return [h.strip() for h in self.reva_cors_origins.split(",") if h.strip()]

    def role_password(self, role: Role) -> SecretStr | None:
        return {
            "api": self.reva_api_db_password,
            "worker": self.reva_worker_db_password,
            "sim": self.reva_sim_db_password,
        }[role]

    def runtime_url(self, role: Role) -> URL:
        """Safely encoded runtime DSN (URL.create, never string concatenation)."""
        pw = self.role_password(role)
        if pw is None:
            raise ConfigurationError(f"password for runtime role {role} is not configured")
        query = {"sslmode": self.reva_db_sslmode} if self.reva_db_sslmode else {}
        return URL.create(
            "postgresql+psycopg",
            username=ROLE_LOGIN[role],
            password=pw.get_secret_value(),
            host=self.reva_db_host,
            port=self.reva_db_port,
            database=self.reva_db_name,
            query=query,
        )

    def migration_url(self) -> URL:
        if self.reva_migration_database_url is None:
            raise ConfigurationError("REVA_MIGRATION_DATABASE_URL is required for bootstrap")
        url = make_url(self.reva_migration_database_url.get_secret_value())
        return url.set(drivername="postgresql+psycopg")


def load_settings() -> Settings:
    try:
        if os.environ.get("REVA_ENV") == "production":
            return Settings(_env_file=None)  # type: ignore[call-arg]  # never read ./.env in production
        return Settings()
    except ValidationError as e:
        # Redact: report only field names/messages, never input values.
        msgs = [f"{'.'.join(map(str, err['loc'])) or 'settings'}: {err['msg']}" for err in e.errors()]
        raise ConfigurationError("configuration invalid: " + " | ".join(msgs)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def _redacted_validation_error(e: ValidationError) -> ValidationError:
    line_errors: list[Any] = []
    for err in e.errors(include_url=False):
        item: dict[str, Any] = {"type": err["type"], "loc": err["loc"], "input": "<redacted>"}
        if "ctx" in err:
            item["ctx"] = err["ctx"]
        line_errors.append(item)
    return ValidationError.from_exception_data(e.title, line_errors, hide_input=True)


def _is_argon2id(value: SecretStr) -> bool:
    return bool(ARGON2ID_HASH.match(value.get_secret_value()))


def is_secret_env_key(name: str) -> bool:
    """Secret-bearing environment variable name (explicit lists plus name patterns)."""
    return name in SECRET_ENV_KEYS or name in COMMON_SECRET_ENV_KEYS or bool(SECRET_NAME_PATTERN.search(name))


def redact_value(value: str) -> str:
    """Mask passwords embedded in URLs (``scheme://user:pw@host`` -> ``scheme://user:***@host``)."""
    return _URL_USERINFO.sub(lambda m: f"{m['scheme']}{m['user']}:***@", value)


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Copy of ``env`` with secret values masked; never mutates the input."""
    return {k: ("***" if is_secret_env_key(k) else redact_value(v)) for k, v in env.items()}


def threads_budget() -> int:
    """Explicit CPU budget; never os.cpu_count(). Also respects cgroup quota."""
    budget = int(os.environ.get("REVA_CPU_BUDGET_THREADS", "1"))
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            budget = min(budget, max(1, int(int(quota) // int(period))))
    except (OSError, ValueError):
        pass
    return max(1, budget)
