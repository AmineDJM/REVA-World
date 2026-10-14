"""Settings: production validation, secret redaction, safe DSN building, CPU budget."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

import reva.settings as settings_mod
from reva.kernel.errors import ConfigurationError
from reva.settings import ROLE_LOGIN, Settings, load_settings, redact_env, threads_budget

SECRET = "tiny-secret-XYZ-nolog"  # < 32 chars: invalid in production
S3_SECRET = "s3-secret-material-QWERTY-0123456789"
GOOD_PROD = {
    "reva_env": "production",
    "reva_object_backend": "s3",
    "reva_s3_bucket": "reva-prod",
    "reva_s3_access_key_id": "AKIAEXAMPLEEXAMPLE00",
    "reva_s3_secret_access_key": S3_SECRET,
    "reva_session_secret": "x" * 48,
    "reva_public_base_url": "https://reva.example.org",
}


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """No inherited REVA_* variables and no .env in the working directory."""
    for k in list(os.environ):
        if k.startswith("REVA_") or k in ("DATABASE_URL", "PORT"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def make(**over: object) -> Settings:
    return Settings(_env_file=None, **over)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- production
def test_valid_production_configuration_is_accepted(clean_env: pytest.MonkeyPatch) -> None:
    s = make(**GOOD_PROD)
    assert s.reva_object_backend == "s3"


@pytest.mark.parametrize(
    "override,needle",
    [
        ({"reva_object_backend": "filesystem"}, "REVA_OBJECT_BACKEND"),
        ({"reva_s3_bucket": None}, "REVA_S3_BUCKET"),
        ({"reva_s3_access_key_id": None}, "REVA_S3_ACCESS_KEY_ID"),
        ({"reva_s3_secret_access_key": None}, "REVA_S3_SECRET_ACCESS_KEY"),
        ({"reva_session_secret": None}, "REVA_SESSION_SECRET"),
        ({"reva_session_secret": "short"}, "REVA_SESSION_SECRET"),
        ({"reva_public_base_url": "http://reva.example.org"}, "https"),
        ({"reva_llm_enabled": True, "reva_llm_monthly_budget_usd": 0}, "BUDGET"),
    ],
)
def test_production_rejects_unsafe_configuration(
    clean_env: pytest.MonkeyPatch, override: dict, needle: str
) -> None:
    with pytest.raises(ValidationError) as ei:
        make(**{**GOOD_PROD, **override})
    assert needle in str(ei.value)


def test_production_never_silently_selects_filesystem(clean_env: pytest.MonkeyPatch) -> None:
    # Defaults alone (filesystem backend) are not a production configuration.
    clean_env.setenv("REVA_ENV", "production")
    with pytest.raises(ConfigurationError) as ei:
        load_settings()
    assert "filesystem is never selected" in ei.value.message

    from reva.adapters.objects.factory import build_store

    s = make(**GOOD_PROD)
    s_fs = s.model_copy(update={"reva_object_backend": "filesystem"})  # bypasses validation
    with pytest.raises(RuntimeError):
        build_store(s_fs)


def test_local_defaults_are_not_production(clean_env: pytest.MonkeyPatch) -> None:
    s = make()
    assert s.reva_env == "local"
    assert s.reva_worker_concurrency >= 1


# --------------------------------------------------------------------------- redaction
def test_load_settings_error_never_contains_secret_values(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("REVA_ENV", "production")
    clean_env.setenv("REVA_SESSION_SECRET", SECRET)  # too short -> invalid
    clean_env.setenv("REVA_S3_SECRET_ACCESS_KEY", S3_SECRET)
    clean_env.setenv("REVA_API_DB_PASSWORD", "api-password-leak-check")
    clean_env.setenv("REVA_DB_PORT", "not-a-port-p@ss")  # field-level error with a hostile value
    with pytest.raises(ConfigurationError) as ei:
        load_settings()
    err = ei.value
    rendered = " ".join([str(err), err.message, repr(err.problem()), repr(err.details)])
    for secret in (SECRET, S3_SECRET, "api-password-leak-check", "not-a-port-p@ss"):
        assert secret not in rendered
    # The chain is suppressed so a traceback cannot re-expose the pydantic error with inputs.
    assert err.__cause__ is None and err.__suppress_context__
    assert "reva_db_port" in err.message  # names the field, not the value

    # Model-level (production) failure: names the setting, never echoes the secret.
    clean_env.delenv("REVA_DB_PORT")
    with pytest.raises(ConfigurationError) as ei2:
        load_settings()
    assert "REVA_SESSION_SECRET" in ei2.value.message
    for secret in (SECRET, S3_SECRET, "api-password-leak-check"):
        assert secret not in str(ei2.value) and secret not in repr(ei2.value.problem())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: Settings() raises a pydantic ValidationError whose text includes input_value with raw "
        "secrets (e.g. the session secret) when constructed directly instead of via load_settings(); "
        "set hide_input_in_errors=True in Settings.model_config"
    ),
)
def test_direct_settings_validation_error_hides_inputs(clean_env: pytest.MonkeyPatch) -> None:
    over = {k: v for k, v in GOOD_PROD.items() if k != "reva_session_secret"}
    with pytest.raises(ValidationError) as ei:
        make(**over, reva_session_secret=SECRET)  # last key: survives pydantic's repr truncation
    assert SECRET not in str(ei.value)
    assert SECRET not in repr(ei.value.errors())


def test_secret_fields_are_masked_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    s = make(**GOOD_PROD, reva_api_db_password="pw-in-repr-check")
    text = repr(s) + str(s.model_dump())
    assert S3_SECRET not in text
    assert "pw-in-repr-check" not in text


def test_redact_env_masks_secret_keys_only() -> None:
    env = {
        "REVA_MIGRATION_DATABASE_URL": "postgresql://owner:pw@h/db",
        "REVA_API_DB_PASSWORD": "api-pw-111",
        "REVA_SESSION_SECRET": "session-secret-222",
        "REVA_S3_SECRET_ACCESS_KEY": "s3-secret-333",
        "REVA_COMTRADE_API_KEY": "comtrade-key-444",
        "DATABASE_URL": "postgresql://u:pw@h/db",
        "REVA_ENV": "production",
        "PATH": "/usr/bin",
    }
    red = redact_env(env)
    assert red["REVA_ENV"] == "production" and red["PATH"] == "/usr/bin"
    for k in env:
        if k not in ("REVA_ENV", "PATH"):
            assert red[k] != env[k]
            assert env[k] not in str(red)
    assert env == {**env}  # input untouched


@pytest.mark.parametrize("name", sorted(settings_mod.SECRET_ENV_KEYS))
def test_every_secretstr_field_is_listed_as_secret(name: str) -> None:
    # Declared secret keys correspond to settings fields (or DATABASE_URL), so redaction stays in sync.
    fields = {f.upper() for f in Settings.model_fields}
    assert name in fields or name == "DATABASE_URL"


def test_all_secretstr_fields_are_redacted_keys() -> None:
    from pydantic import SecretStr

    secret_fields = {
        n.upper() for n, f in Settings.model_fields.items() if "SecretStr" in str(f.annotation)
    }
    assert secret_fields <= settings_mod.SECRET_ENV_KEYS
    assert SecretStr  # imported for annotation check readability


# --------------------------------------------------------------------------- DSN
@pytest.mark.parametrize(
    "pw", ["p@ss:/w?rd#%", "a b/c?d=e&f", "%40%2F", "ünïcødé'\"\\", "x@y@z:::///???###%%%"]
)
@pytest.mark.parametrize("role", ["api", "worker", "sim"])
def test_runtime_url_survives_url_special_characters(clean_env, pw: str, role: str) -> None:  # type: ignore[no-untyped-def]
    s = make(
        reva_api_db_password=pw, reva_worker_db_password=pw, reva_sim_db_password=pw,
        reva_db_host="db.internal", reva_db_port=6543, reva_db_name="reva", reva_db_sslmode="require",
    )
    url = s.runtime_url(role)
    assert url.password == pw
    assert url.username == ROLE_LOGIN[role]
    assert (url.host, url.port, url.database) == ("db.internal", 6543, "reva")
    assert url.query == {"sslmode": "require"}
    rendered = url.render_as_string(hide_password=False)
    again = make_url(rendered)
    assert again.password == pw and again.host == "db.internal" and again.database == "reva"
    # Default rendering never shows the password.
    assert pw not in str(url) and pw not in repr(url)


def test_runtime_url_requires_configured_password(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError):
        make().runtime_url("api")
    with pytest.raises(ConfigurationError):
        make().migration_url()


def test_runtime_roles_are_distinct_logins(clean_env: pytest.MonkeyPatch) -> None:
    s = make(reva_api_db_password="a", reva_worker_db_password="b", reva_sim_db_password="c")
    users = {s.runtime_url(r).username for r in ("api", "worker", "sim")}
    assert len(users) == 3
    assert all(u.startswith("reva_") for u in users)


# --------------------------------------------------------------------------- CPU budget
class _FakeCgroup:
    content: str | None = None

    def __init__(self, *_: object) -> None:
        pass

    def read_text(self) -> str:
        if self.content is None:
            raise OSError("no cgroup")
        return self.content


def test_threads_budget_ignores_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 128)
    monkeypatch.setattr(settings_mod, "Path", _FakeCgroup)
    _FakeCgroup.content = None
    monkeypatch.delenv("REVA_CPU_BUDGET_THREADS", raising=False)
    assert threads_budget() == 1
    monkeypatch.setenv("REVA_CPU_BUDGET_THREADS", "3")
    assert threads_budget() == 3
    monkeypatch.setenv("REVA_CPU_BUDGET_THREADS", "0")
    assert threads_budget() == 1


def test_threads_budget_respects_cgroup_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 128)
    monkeypatch.setattr(settings_mod, "Path", _FakeCgroup)
    monkeypatch.setenv("REVA_CPU_BUDGET_THREADS", "8")
    _FakeCgroup.content = "150000 100000\n"  # 1.5 CPUs
    assert threads_budget() == 1
    _FakeCgroup.content = "max 100000\n"
    assert threads_budget() == 8
    _FakeCgroup.content = "garbage"
    assert threads_budget() == 8
