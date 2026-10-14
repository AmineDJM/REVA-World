"""Startup environment scrubbing: the CHILD environment is what is tested.

The entrypoints are executed as real subprocesses. With REVA_ENV=test the exec
target can be replaced (REVA_EXEC_OVERRIDE) by a stub that records the
environment and argv it actually received, so these tests observe exactly what
Uvicorn/the worker would inherit.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

runtime_env = importlib.import_module("_runtime_env")
start_api = importlib.import_module("start_api")
start_worker = importlib.import_module("start_worker")

# Distinctive secret values (URL-special characters on purpose).
OWNER_PW = "own3r:p@ss/w%rd?#"
OWNER_URL = "postgresql://reva_owner:own3r%3Ap%40ss%2Fw%25rd%3F%23@db.internal:5432/reva"
SECRETS = {
    "REVA_MIGRATION_DATABASE_URL": OWNER_URL,
    "DATABASE_URL": "postgresql://reva_owner:dburl-secret-value@db.internal/reva",
    "REVA_API_DB_PASSWORD": "api-pw-@:/?#-value",
    "REVA_WORKER_DB_PASSWORD": "worker-pw-@:/?#-value",
    "REVA_SIM_DB_PASSWORD": "sim-pw-@:/?#-value",
    "REVA_SESSION_SECRET": "session-secret-" + "x" * 40,
    "REVA_ADMIN_PASSWORD_HASH": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo",
    "REVA_S3_ACCESS_KEY_ID": "AKIA-SERVICE-KEY-ID",
    "REVA_S3_SECRET_ACCESS_KEY": "service-s3-secret-value",
    "REVA_COMTRADE_API_KEY": "comtrade-key-value",
    "REVA_LLM_API_KEY": "llm-key-value",
    "REVA_SANDBOX_TOKEN": "sandbox-token-value",
    # Unrelated secrets that must never reach either component.
    "AWS_SECRET_ACCESS_KEY": "unrelated-aws-secret",
    "GITHUB_TOKEN": "unrelated-gh-token",
    "REVA_WORKER_S3_SECRET_ACCESS_KEY": "other-component-s3-secret",
}
CONFIG = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/home/reva",
    "REVA_LOG_LEVEL": "INFO",
    "REVA_DB_HOST": "db.internal",
    "REVA_DB_PORT": "5432",
    "REVA_DB_SSLMODE": "require",
    "REVA_DB_NAME": "reva",
    "REVA_OBJECT_BACKEND": "s3",
    "REVA_S3_BUCKET": "reva-bucket",
    "REVA_S3_REGION": "eu-central-1",
    "REVA_S3_PREFIX": "reva/",
    "REVA_PUBLIC_BASE_URL": "https://reva-api.onrender.com",
    "REVA_ALLOWED_HOSTS": "reva-api.onrender.com",
    "REVA_HTTP_CONTACT_EMAIL": "ops@example.org",
    "REVA_CPU_BUDGET_THREADS": "1",
    "RENDER_GIT_COMMIT": "abc123",
    "OMP_NUM_THREADS": "64",
    "HTTPS_PROXY": "http://proxy.internal:3128",
    "UNRELATED_SETTING": "x",
    "PORT": "10000",
}
PROD = {**CONFIG, **SECRETS, "REVA_ENV": "production"}
NO_CGROUP = Path("/nonexistent-cgroup-root")


def _secret_values(keys: set[str]) -> list[str]:
    return [SECRETS[k] for k in keys]


# --- pure functions ---------------------------------------------------------------


def test_api_child_env_keeps_only_api_credentials() -> None:
    child, removed = runtime_env.build_child_env(PROD, "api", cgroup_root=NO_CGROUP)
    for key in (
        "REVA_MIGRATION_DATABASE_URL",
        "DATABASE_URL",
        "REVA_WORKER_DB_PASSWORD",
        "REVA_SIM_DB_PASSWORD",
        "REVA_COMTRADE_API_KEY",
        "REVA_LLM_API_KEY",
        "REVA_SANDBOX_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "REVA_WORKER_S3_SECRET_ACCESS_KEY",
        "UNRELATED_SETTING",
        "HTTPS_PROXY",
    ):
        assert key not in child, key
        assert key in removed
    for key in (
        "REVA_API_DB_PASSWORD",
        "REVA_SESSION_SECRET",
        "REVA_ADMIN_PASSWORD_HASH",
        "REVA_S3_ACCESS_KEY_ID",
        "REVA_S3_SECRET_ACCESS_KEY",
    ):
        assert child[key] == PROD[key]
    blob = json.dumps(child)
    for value in _secret_values(set(SECRETS) - runtime_env.COMPONENT_SECRETS["api"]):
        assert value not in blob
    assert OWNER_PW not in blob and "own3r" not in blob
    # The only S3 credential the API child holds is the API service's own one.
    s3_like = {k for k in child if "S3" in k and ("KEY" in k or "SECRET" in k)}
    assert s3_like == {"REVA_S3_ACCESS_KEY_ID", "REVA_S3_SECRET_ACCESS_KEY"}
    assert child["REVA_DB_HOST"] == "db.internal" and child["PORT"] == "10000"
    assert child["REVA_PROCESS_ROLE"] == "api" and child["REVA_DB_SSLMODE"] == "require"
    assert child["OMP_NUM_THREADS"] == "1" and child["POLARS_MAX_THREADS"] == "1"


def test_worker_child_env_has_no_api_or_owner_secrets() -> None:
    env = {**PROD, "REVA_ENV": "local"}
    child, removed = runtime_env.build_child_env(env, "worker", cgroup_root=NO_CGROUP)
    for key in (
        "REVA_MIGRATION_DATABASE_URL",
        "DATABASE_URL",
        "REVA_API_DB_PASSWORD",
        "REVA_SESSION_SECRET",
        "REVA_ADMIN_PASSWORD_HASH",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "REVA_WORKER_S3_SECRET_ACCESS_KEY",
        "REVA_LLM_API_KEY",
    ):
        assert key not in child, key
    assert "REVA_MIGRATION_DATABASE_URL" in removed
    for key in (
        "REVA_WORKER_DB_PASSWORD",
        "REVA_SIM_DB_PASSWORD",
        "REVA_S3_ACCESS_KEY_ID",
        "REVA_S3_SECRET_ACCESS_KEY",
        "REVA_COMTRADE_API_KEY",
    ):
        assert child[key] == env[key]
    assert child["HTTPS_PROXY"] == env["HTTPS_PROXY"]  # the worker performs egress
    llm_child, _ = runtime_env.build_child_env({**env, "REVA_LLM_ENABLED": "true"}, "worker", cgroup_root=NO_CGROUP)
    assert llm_child["REVA_LLM_API_KEY"] == env["REVA_LLM_API_KEY"]


def test_thread_budget_is_explicit_and_capped_by_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> int:
        raise AssertionError("os.cpu_count() must not be used for the thread budget")

    monkeypatch.setattr(os, "cpu_count", boom)
    env = {**CONFIG, "REVA_CPU_BUDGET_THREADS": "3", "OMP_NUM_THREADS": "64"}
    child, _ = runtime_env.build_child_env(env, "worker", cgroup_root=NO_CGROUP)
    assert {child[v] for v in runtime_env.THREAD_VARS} == {"3"}
    (tmp_path / "cpu.max").write_text("200000 100000\n")  # 2 CPUs allowed
    child, _ = runtime_env.build_child_env(env, "worker", cgroup_root=tmp_path)
    assert {child[v] for v in runtime_env.THREAD_VARS} == {"2"}
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert runtime_env.thread_budget(env, tmp_path) == 3
    with pytest.raises(ValueError):
        runtime_env.thread_budget({"REVA_CPU_BUDGET_THREADS": "0"}, NO_CGROUP)


def test_dotenv_secrets_are_neutralised_for_child() -> None:
    env = {**CONFIG, "REVA_ENV": "local", **SECRETS}
    child, _ = runtime_env.build_child_env(
        env,
        "api",
        dotenv_keys={"REVA_MIGRATION_DATABASE_URL", "REVA_WORKER_DB_PASSWORD", "REVA_DB_HOST"},
        cgroup_root=NO_CGROUP,
    )
    assert child["REVA_MIGRATION_DATABASE_URL"] == ""
    assert child["REVA_WORKER_DB_PASSWORD"] == ""
    assert child["REVA_DB_HOST"] == "db.internal"


def test_process_role_is_declared_and_conflicts_refused() -> None:
    wchild, _ = runtime_env.build_child_env({**PROD, "REVA_PROCESS_ROLE": "api"}, "worker", cgroup_root=NO_CGROUP)
    assert wchild["REVA_PROCESS_ROLE"] == "worker"
    with pytest.raises(runtime_env.ConfigError, match="REVA_PROCESS_ROLE"):
        start_api.validate({**PROD, "REVA_PROCESS_ROLE": "worker"})
    with pytest.raises(runtime_env.ConfigError, match="REVA_PROCESS_ROLE"):
        start_worker.validate({**PROD, "REVA_ENV": "local", "REVA_PROCESS_ROLE": "api"})
    # Production worker validation does not demand API-only secrets.
    worker_env = {
        k: v
        for k, v in PROD.items()
        if k not in {"REVA_SESSION_SECRET", "REVA_ADMIN_PASSWORD_HASH", "REVA_API_DB_PASSWORD"}
    }
    settings = start_worker.validate({**worker_env, "REVA_PROCESS_ROLE": "worker"})
    assert settings.reva_process_role == "worker"
    with pytest.raises(runtime_env.ConfigError, match="REVA_DB_SSLMODE"):
        start_api.validate({**PROD, "REVA_DB_SSLMODE": "disable"})


def test_command_lines() -> None:
    api = start_api.build_command(PROD)
    assert api[:4] == [sys.executable, "-m", "uvicorn", "reva.api.app:app"]
    joined = " ".join(api)
    assert "--host 0.0.0.0 --port 10000 --workers 1 --proxy-headers" in joined
    ips = api[api.index("--forwarded-allow-ips") + 1]
    assert "*" not in ips and "10.0.0.0/8" in ips
    assert (
        start_api.build_command({**PROD, "REVA_FORWARDED_ALLOW_IPS": "10.1.0.0/16"})[
            api.index("--forwarded-allow-ips") + 1
        ]
        == "10.1.0.0/16"
    )
    with pytest.raises(runtime_env.ConfigError):
        start_api.build_command({**PROD, "PORT": "http"})
    assert start_worker.build_command(PROD) == [sys.executable, "-m", "reva.operations.worker"]


def test_api_production_validation_redacts() -> None:
    summary = start_api.validate(PROD)
    assert "api-pw" not in json.dumps(summary) and summary["db"].startswith("postgresql+psycopg://reva_api:***@")
    bad = {**PROD, "REVA_ADMIN_PASSWORD_HASH": "plaintext-password"}
    with pytest.raises(runtime_env.ConfigError) as ei:
        start_api.validate(bad)
    assert "plaintext-password" not in str(ei.value) and "REVA_ADMIN_PASSWORD_HASH" in str(ei.value)
    with pytest.raises(runtime_env.ConfigError) as ei:
        start_api.validate({**PROD, "REVA_PUBLIC_BASE_URL": "http://insecure", "REVA_OBJECT_BACKEND": "filesystem"})
    assert "https" in str(ei.value) and "filesystem" in str(ei.value)


def test_worker_refuses_owner_url_in_production_and_strips_otherwise() -> None:
    with pytest.raises(runtime_env.ConfigError) as ei:
        start_worker.check_owner_credentials(PROD)
    assert OWNER_URL not in str(ei.value) and "REVA_MIGRATION_DATABASE_URL" in str(ei.value)
    assert start_worker.check_owner_credentials({**PROD, "REVA_ENV": "local"}) == [
        "REVA_MIGRATION_DATABASE_URL",
        "DATABASE_URL",
    ]
    with pytest.raises(runtime_env.ConfigError):
        start_worker.validate(
            {**PROD, "REVA_ENV": "production", "REVA_HTTP_CONTACT_EMAIL": ""} | {"REVA_MIGRATION_DATABASE_URL": ""}
        )


def test_redact_removes_url_passwords_and_bound_parameters() -> None:
    msg = (
        f"(psycopg.OperationalError) failed {OWNER_URL} [SQL: ALTER ROLE] "
        f"[parameters: {{'p': '{SECRETS['REVA_API_DB_PASSWORD']}'}}]"
    )
    out = runtime_env.redact(msg, SECRETS)
    assert SECRETS["REVA_API_DB_PASSWORD"] not in out and "own3r" not in out
    assert "reva_owner:***@" in out


def test_exec_override_refused_outside_test() -> None:
    with pytest.raises(runtime_env.ConfigError):
        runtime_env.exec_target({"REVA_ENV": "production", "REVA_EXEC_OVERRIDE": "/tmp/x"}, ["true"])
    with pytest.raises(runtime_env.ConfigError):
        runtime_env.exec_target({"REVA_ENV": "local", "REVA_EXEC_OVERRIDE": "/tmp/x"}, ["true"])


def test_wait_for_schema_is_bounded() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    def probe(_: object) -> dict[str, object]:
        raise RuntimeError(f"connection refused {OWNER_URL}")

    with pytest.raises(TimeoutError) as ei:
        start_worker.wait_for_schema(object(), 60, probe=probe, sleep=sleep, clock=clock)
    assert sum(sleeps) == pytest.approx(60) and max(sleeps) <= 30
    assert "own3r" not in str(ei.value)


# --- real subprocesses --------------------------------------------------------------


def _stub(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "child.json"
    stub = tmp_path / "stub_exec.py"
    stub.write_text(
        "import json, os, sys\n"
        f"open({str(out)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ)}}))\n"
    )
    return stub, out


def _run(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assert_no_values(text: str, keys: set[str]) -> None:
    for value in _secret_values(keys):
        assert value not in text, "secret value printed"
    assert "own3r" not in text


def test_start_api_subprocess_child_environment(tmp_path: Path) -> None:
    stub, out = _stub(tmp_path)
    env = {
        **CONFIG,
        **SECRETS,
        "REVA_ENV": "test",
        "REVA_EXEC_OVERRIDE": str(stub),
        "REVA_OBJECT_BACKEND": "filesystem",
    }
    proc = _run("start_api.py", env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    _assert_no_values(proc.stdout + proc.stderr, set(SECRETS))
    child = json.loads(out.read_text())
    cenv, argv = child["env"], child["argv"]
    assert argv[1:] == [
        "-m",
        "uvicorn",
        "reva.api.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "10000",
        "--workers",
        "1",
        "--proxy-headers",
        "--forwarded-allow-ips",
        start_api.DEFAULT_FORWARDED_ALLOW_IPS,
        "--timeout-graceful-shutdown",
        "50",
        "--no-server-header",
    ]
    for key in (
        "REVA_MIGRATION_DATABASE_URL",
        "DATABASE_URL",
        "REVA_WORKER_DB_PASSWORD",
        "REVA_SIM_DB_PASSWORD",
        "REVA_EXEC_OVERRIDE",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "REVA_COMTRADE_API_KEY",
    ):
        assert not cenv.get(key), key
    _assert_no_values(json.dumps(cenv), set(SECRETS) - runtime_env.COMPONENT_SECRETS["api"])
    assert cenv["REVA_API_DB_PASSWORD"] == SECRETS["REVA_API_DB_PASSWORD"]
    assert cenv["OMP_NUM_THREADS"] == "1"


def test_start_api_neutralises_local_dotenv(tmp_path: Path) -> None:
    stub, out = _stub(tmp_path)
    (tmp_path / ".env").write_text(
        f"REVA_MIGRATION_DATABASE_URL={OWNER_URL}\nREVA_WORKER_DB_PASSWORD=dotenv-worker-pw\n"
        "REVA_API_DB_PASSWORD=dotenv-api-pw\n"
    )
    env = {**CONFIG, "REVA_ENV": "test", "REVA_EXEC_OVERRIDE": str(stub), "REVA_OBJECT_BACKEND": "filesystem"}
    proc = _run("start_api.py", env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "dotenv-api-pw" not in proc.stdout + proc.stderr
    cenv = json.loads(out.read_text())["env"]
    # The child's Settings() would re-read .env from its cwd; process env wins,
    # so the owner URL and the worker password are blanked, the API password kept.
    assert cenv["REVA_MIGRATION_DATABASE_URL"] == "" and cenv["REVA_WORKER_DB_PASSWORD"] == ""
    assert cenv["REVA_API_DB_PASSWORD"] == "dotenv-api-pw"
    # What the child's own Settings() sees, from the same cwd (with the .env file).
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from reva.settings import Settings; s = Settings(); "
            "v = lambda x: x.get_secret_value() if x else None; "
            "print(json.dumps([v(s.reva_migration_database_url), v(s.reva_worker_db_password), "
            "v(s.reva_api_db_password)]))",
        ],
        env=cenv,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    seen = json.loads(probe.stdout)
    assert not seen[0] and not seen[1] and seen[2] == "dotenv-api-pw"


def test_start_api_fails_closed_without_printing_secrets(tmp_path: Path) -> None:
    env = {**PROD, "REVA_ADMIN_PASSWORD_HASH": "plain-secret-hash", "REVA_SESSION_SECRET": "short-secret"}
    proc = _run("start_api.py", env, tmp_path)
    assert proc.returncode == runtime_env.EX_CONFIG
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert err["status"] == "error" and "REVA_SESSION_SECRET" in err["error"]
    assert "plain-secret-hash" not in proc.stderr and "short-secret" not in proc.stderr
    _assert_no_values(proc.stdout + proc.stderr, set(SECRETS))


def test_start_worker_refuses_owner_url_in_production(tmp_path: Path) -> None:
    proc = _run("start_worker.py", dict(PROD), tmp_path)
    assert proc.returncode == runtime_env.EX_CONFIG
    assert "REVA_MIGRATION_DATABASE_URL" in proc.stderr
    _assert_no_values(proc.stdout + proc.stderr, set(SECRETS))


def test_start_worker_subprocess_child_environment(tmp_path: Path) -> None:
    stub, out = _stub(tmp_path)
    env = {
        **CONFIG,
        **SECRETS,
        "REVA_ENV": "test",
        "REVA_EXEC_OVERRIDE": str(stub),
        "REVA_SKIP_SCHEMA_WAIT": "1",
        "REVA_OBJECT_BACKEND": "filesystem",
        "REVA_CPU_BUDGET_THREADS": "1",
    }
    proc = _run("start_worker.py", env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    _assert_no_values(proc.stdout + proc.stderr, set(SECRETS))
    child = json.loads(out.read_text())
    assert child["argv"][1:] == ["-m", "reva.operations.worker"]
    cenv = child["env"]
    for key in (
        "REVA_MIGRATION_DATABASE_URL",
        "DATABASE_URL",
        "REVA_API_DB_PASSWORD",
        "REVA_SESSION_SECRET",
        "REVA_ADMIN_PASSWORD_HASH",
        "REVA_EXEC_OVERRIDE",
        "REVA_SKIP_SCHEMA_WAIT",
        "GITHUB_TOKEN",
    ):
        assert not cenv.get(key), key
    assert cenv["REVA_WORKER_DB_PASSWORD"] == SECRETS["REVA_WORKER_DB_PASSWORD"]
    assert cenv["REVA_SIM_DB_PASSWORD"] == SECRETS["REVA_SIM_DB_PASSWORD"]
    assert {cenv[v] for v in runtime_env.THREAD_VARS} == {"1"}
    stderr_events = [json.loads(line) for line in proc.stderr.splitlines() if line.startswith("{")]
    assert stderr_events[-1]["stripped_owner_keys"] == ["REVA_MIGRATION_DATABASE_URL", "DATABASE_URL"]


def test_bootstrap_failure_is_redacted(tmp_path: Path) -> None:
    env = {
        "PATH": CONFIG["PATH"],
        "HOME": "/tmp",
        "REVA_ENV": "test",
        "REVA_MIGRATION_DATABASE_URL": "postgresql://reva_owner:boot-secret-pw@127.0.0.1:1/reva",
        "REVA_API_DB_PASSWORD": "a-boot-pw",
        "REVA_WORKER_DB_PASSWORD": "w-boot-pw",
        "REVA_SIM_DB_PASSWORD": "s-boot-pw",
    }
    proc = _run("bootstrap_runtime.py", env, tmp_path)
    assert proc.returncode != 0
    assert proc.stdout == ""
    report = json.loads(proc.stderr.strip().splitlines()[-1])
    assert report["status"] == "error"
    for secret in ("boot-secret-pw", "a-boot-pw", "w-boot-pw", "s-boot-pw"):
        assert secret not in proc.stderr


# --- against the real test database ---------------------------------------------------


@pytest.mark.db
def test_bootstrap_runtime_reports_schema_without_secrets(db, tmp_path: Path) -> None:
    s = db.settings
    env = {
        "PATH": CONFIG["PATH"],
        "HOME": "/tmp",
        "REVA_ENV": "test",
        "REVA_MIGRATION_DATABASE_URL": s.reva_migration_database_url.get_secret_value(),
        "REVA_DB_HOST": s.reva_db_host,
        "REVA_DB_PORT": str(s.reva_db_port),
        "REVA_DB_NAME": db.name,
        "REVA_API_DB_PASSWORD": s.reva_api_db_password.get_secret_value(),
        "REVA_WORKER_DB_PASSWORD": s.reva_worker_db_password.get_secret_value(),
        "REVA_SIM_DB_PASSWORD": s.reva_sim_db_password.get_secret_value(),
        # As on Render: the pre-deploy runs in the API service's environment.
        "REVA_PROCESS_ROLE": "api",
    }
    proc = _run("bootstrap_runtime.py", env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "ok" and report["roles"] == ["reva_api", "reva_worker", "reva_sim"]
    from alembic.script import ScriptDirectory

    from reva.cli.bootstrap import alembic_config

    assert report["schema_version"] == ScriptDirectory.from_config(alembic_config()).get_current_head()
    owner_pw = s.migration_url().password
    for secret in (owner_pw, env["REVA_API_DB_PASSWORD"], env["REVA_WORKER_DB_PASSWORD"]):
        assert secret and secret not in proc.stdout + proc.stderr


@pytest.mark.db
def test_worker_schema_wait_against_database(db) -> None:
    state = start_worker.wait_for_schema(db.settings, 10)
    assert state["worker_ok"] and state["sim_ok"] and state["attempts"] == 1
    assert state["schema_version"] == state["expected_head"]
