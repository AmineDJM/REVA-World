"""Configuration with production validation and secret redaction."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

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

Role = Literal["api", "worker", "sim"]
ROLE_LOGIN = {"api": "reva_api", "worker": "reva_worker", "sim": "reva_sim"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    reva_env: Literal["local", "test", "production"] = "local"
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
        if not self.reva_session_secret or len(self.reva_session_secret.get_secret_value()) < 32:
            missing.append("REVA_SESSION_SECRET (>=32 chars)")
        if self.reva_public_base_url.startswith("http://"):
            missing.append("REVA_PUBLIC_BASE_URL must be https in production")
        if self.reva_llm_enabled and self.reva_llm_monthly_budget_usd <= 0:
            missing.append("REVA_LLM_MONTHLY_BUDGET_USD > 0 when LLM is enabled")
        if missing:
            raise ValueError("invalid production configuration: " + "; ".join(missing))
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
        return Settings()
    except ValidationError as e:
        # Redact: report only field names/messages, never input values.
        msgs = [f"{'.'.join(map(str, err['loc'])) or 'settings'}: {err['msg']}" for err in e.errors()]
        raise ConfigurationError("configuration invalid: " + " | ".join(msgs)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def redact_env(env: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if k in SECRET_ENV_KEYS else v) for k, v in env.items()}


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
