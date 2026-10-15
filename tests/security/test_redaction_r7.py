"""R7 redaction check: planted canary secrets never leak through logs, error responses, job
records, audit rows, outbox events, artifacts or exception traces.

Every test plants unique canaries (DB passwords/DSNs, admin hash, session cookies, API tokens and
their hashes, S3 keys, provider keys, Authorization headers, proxy credentials) and asserts they are
absent from what an operator, a client or another process could read. Where a guard is the thing
under test, a "sensitivity" assertion first shows the raw material really contains the canary.

Tests marked ``xfail(strict=True)`` document a leak in a file owned by another agent; the exact patch
is in docs/dev/redaction_check.md. They flip to XPASS (and fail, being strict) once the patch lands,
so the marker must then be removed.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import textwrap
import traceback
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
from reva.adapters.http.safe_client import HttpResult, SafeHttpClient, redact_url
from reva.kernel.errors import SourceResponseError, UnsafeRequest
from reva.operations import telemetry as tm
from reva.settings import Settings

REPO = Path(__file__).resolve().parents[2]


def canary(kind: str) -> str:
    """A unique, greppable secret value (no URL-special characters)."""
    return f"CANARY{kind}{uuid.uuid4().hex[:16]}"


def assert_absent(blob: str, *secrets: str) -> None:
    leaked = [s for s in secrets if s and s in blob]
    assert not leaked, f"secret(s) leaked: {[s[:12] + '...' for s in leaked]}"


@pytest.fixture()
def json_log() -> Iterator[io.StringIO]:
    """The production log pipeline (configure_logging: JSON + RedactionFilter) into a buffer, at DEBUG."""
    stream = io.StringIO()
    root = logging.getLogger()
    level = root.level
    handler = tm.configure_logging("DEBUG", stream=stream)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


# =========================================================================== 1. settings
SECRET_FIELDS = ("reva_api_db_password", "reva_worker_db_password", "reva_sim_db_password", "reva_s3_access_key_id",
                 "reva_s3_secret_access_key", "reva_session_secret", "reva_comtrade_api_key", "reva_llm_api_key",
                 "reva_sandbox_token")


def _canary_settings() -> tuple[Settings, list[str]]:
    vals = {f: canary(f.split("_", 1)[1][:6]) for f in SECRET_FIELDS}
    owner_pw = canary("ownerpw")
    admin_hash = "$argon2id$v=19$m=65536,t=3,p=4$" + "c2FsdHNhbHRzYWx0" + "$" + canary("hash")
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        reva_migration_database_url=f"postgresql://reva_owner:{owner_pw}@db.internal:5432/reva",
        reva_admin_password_hash=admin_hash,
        **vals,
    )
    return s, [*vals.values(), owner_pw, admin_hash, admin_hash.rsplit("$", 1)[1]]


def test_settings_repr_str_dump_and_dsns_never_show_secrets() -> None:
    s, secrets = _canary_settings()
    rendered = "\n".join([
        repr(s), str(s), str(s.model_dump()), s.model_dump_json(), json.dumps(s.model_dump(mode="json")),
        str(s.runtime_url("api")), repr(s.runtime_url("worker")), str(s.migration_url()), repr(s.migration_url()),
    ])
    assert_absent(rendered, *secrets)
    # sensitivity: the values are really there behind SecretStr / URL.create
    assert s.runtime_url("api").password == s.reva_api_db_password.get_secret_value()  # type: ignore[union-attr]


def test_settings_validation_errors_never_echo_secrets_on_any_construction_path() -> None:
    pw = canary("pw")
    # field-level failure on __init__
    with pytest.raises(ValidationError) as e1:
        Settings(_env_file=None, reva_db_port=f"port{pw}", reva_api_db_password=pw)  # type: ignore[call-arg]
    # model-level (production) failure through model_validate (scripts/tests rebuild Settings this way)
    with pytest.raises(ValidationError) as e2:
        Settings.model_validate({"reva_env": "production", "reva_session_secret": pw, "reva_api_db_password": pw})
    for e in (e1.value, e2.value):
        assert_absent(str(e) + repr(e.errors()) + repr(e.json()), pw)


@pytest.mark.parametrize("field", ["reva_s3_endpoint_url", "reva_public_base_url", "reva_sandbox_endpoint"])
def test_plain_url_settings_refuse_embedded_credentials(field: str) -> None:
    pw = canary("urlpw")
    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None, **{field: f"https://user:{pw}@host.example"})  # type: ignore[call-arg]
    assert_absent(str(ei.value) + repr(ei.value.errors()), pw)
    assert "credentials" in str(ei.value)


def test_env_file_only_secrets_are_redacted_from_logs(tmp_path: Path, json_log: io.StringIO,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets read from ./.env never enter os.environ; Settings registers them for value redaction."""
    pw = canary("dotenv")
    for k in [k for k in os.environ if k.startswith("REVA_")]:
        monkeypatch.delenv(k)
    (tmp_path / ".env").write_text(f"REVA_WORKER_DB_PASSWORD={pw}\n")
    s = Settings(_env_file=tmp_path / ".env")  # type: ignore[call-arg]
    assert s.reva_worker_db_password is not None and pw not in os.environ.values()
    logging.getLogger("reva.test.r7").error("connect failed for password %s (raw echo)", pw)
    assert "connect failed" in json_log.getvalue()
    assert_absent(json_log.getvalue(), pw)


def test_production_default_log_level_is_info_and_documented() -> None:
    assert Settings.model_fields["reva_log_level"].default == "INFO"
    blueprint = (REPO / "render.yaml").read_text()
    assert "REVA_LOG_LEVEL" in blueprint and "value: INFO" in blueprint.split("REVA_LOG_LEVEL", 1)[1][:60]


def test_worker_startup_refusal_logs_no_secret(tmp_path: Path) -> None:
    """Real entrypoint, real stderr: a refused production start names settings, never values."""
    secrets = {k: canary(k[5:11]) for k in ("REVA_API_DB_PASSWORD", "REVA_WORKER_DB_PASSWORD",
                                            "REVA_SIM_DB_PASSWORD", "REVA_S3_SECRET_ACCESS_KEY",
                                            "REVA_S3_ACCESS_KEY_ID", "REVA_COMTRADE_API_KEY")}
    owner_pw = canary("owner")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "REVA_ENV": "production",
           "REVA_PROCESS_ROLE": "worker", "REVA_SESSION_SECRET": "short",
           "REVA_MIGRATION_DATABASE_URL": f"postgresql://reva_owner:{owner_pw}@db:5432/reva", **secrets}
    r = subprocess.run([sys.executable, "-c", "from reva.operations.worker import main; raise SystemExit(main())"],
                       env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stderr[-2000:]
    assert "worker start refused" in r.stderr
    assert_absent(r.stdout + r.stderr, owner_pw, *secrets.values())


# =========================================================================== telemetry redaction rules
def test_redact_text_masks_every_secret_shape() -> None:
    tok, sha, cookie, pw, key, sig = (canary(k) for k in ("tok", "sha", "cookie", "pw", "key", "sig"))
    samples = [
        f"(psycopg.errors.X) boom\n[SQL: SELECT 1 WHERE h = %(h)s]\n[parameters: {{'h': '{sha}'}}]\n(Background",
        f'DETAIL:  Key (token_sha256)=({sha}) already exists.',
        f"DETAIL:  Failing row contains ({sha}, x, y).",
        f'invalid input syntax for type uuid: "{tok}"',
        f"Authorization: Bearer {tok}",
        f"{{'authorization': 'Bearer {tok}', 'Cookie': 'reva_session={cookie}'}}",
        f"Set-Cookie: reva_session={cookie}; HttpOnly",
        f"token reva_{tok}{'x' * 30} presented",
        f"GET https://comtradeapi.un.org/x?subscription-key={key}&a=1",
        f"https://bucket.s3.example/o?X-Amz-Credential={key}&X-Amz-Signature={sig}",
        f"proxy http://proxyuser:{pw}@proxy.internal:3128 refused",
        f"hash $argon2id$v=19$m=65536,t=3,p=4$c2FsdA${tok}",
        f"password={pw} api_key: {key}",
    ]
    out = tm.redact_text("\n".join(samples))
    assert_absent(out, tok, sha, cookie, pw, key, sig)
    assert "[SQL: SELECT 1 WHERE h = %(h)s]" in out  # statement text (no values) kept for debugging
    assert "Key (token_sha256)=(***)" in out and "Set-Cookie: ***" in out


def test_redact_obj_masks_by_key_and_shape_and_keeps_valid_json() -> None:
    tok, sha = canary("tok"), canary("sha")
    doc = {"token_sha256": sha, "authorization": f"Bearer {tok}", "token_id": "1b9d6bcd", "fencing_token": 7,
           "nested": [{"message": f"DETAIL:  Failing row contains ({sha}, \"q\")"}, {"api_key": tok}],
           "url": f"https://h.example/p?subscription-key={tok}"}
    red = tm.redact_obj(doc)
    blob = json.dumps(red)
    assert_absent(blob, tok, sha)
    assert json.loads(blob)["token_id"] == "1b9d6bcd" and red["fencing_token"] == 7


def test_audit_details_are_redacted_and_remain_valid_json() -> None:
    from reva.operations.audit import _bounded_json

    tok, sha = canary("tok"), canary("sha")
    raw = _bounded_json({"token": tok, "error": f"DETAIL:  Failing row contains ({sha}, \"x\")", "n": 1})
    assert json.loads(raw)["n"] == 1
    assert_absent(raw, tok, sha)


def test_log_pipeline_redacts_tracebacks_chained_causes_and_server_loggers(json_log: io.StringIO) -> None:
    """uvicorn installs its own plain handlers; configure_logging must route them through redaction."""
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    tok, pw = canary("tok"), canary("pw")
    logging.config.dictConfig(LOGGING_CONFIG)  # what `uvicorn ...` does before importing the app
    root_handler = tm.configure_logging("DEBUG", stream=json_log)  # the app's/worker's call
    try:
        assert not logging.getLogger("uvicorn.error").handlers and not logging.getLogger("uvicorn.access").handlers
        try:
            try:
                raise ValueError(f"inner Authorization: Bearer {tok}")
            except ValueError as inner:
                raise RuntimeError(f"connect postgresql://reva_api:{pw}@db/reva failed") from inner
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.1", "GET",
                                                 f"/v1/x?token={tok}", "1.1", 200)
    finally:
        logging.getLogger().removeHandler(root_handler)
    out = json_log.getvalue()
    assert "Exception in ASGI application" in out and "Traceback" in out and "direct cause" in out
    assert_absent(out, tok, pw)


async def test_debug_level_never_logs_provider_urls_or_sql_parameters(json_log: io.StringIO, db: Any) -> None:
    """At DEBUG (worst case) httpx request lines and sqlalchemy statement/parameter logs stay quiet."""
    key, sha = canary("key"), canary("sha")
    async with _client(lambda r: httpx.Response(200, json={"ok": True})) as c:
        await c.get(f"https://comtradeapi.un.org/data?subscription-key={key}")
    with db.owner.connect() as conn:
        conn.execute(text("SELECT CAST(:h AS text)"), {"h": sha})
    assert_absent(json_log.getvalue(), key, sha)


# =========================================================================== 2. SafeHttpClient
async def _noop(_: float = 0) -> None:
    return None


def _client(handler: Any, hosts: tuple[str, ...] = ("comtradeapi.un.org",), **kw: Any) -> SafeHttpClient:
    return SafeHttpClient(allowed_hosts=list(hosts), transport=httpx.MockTransport(handler),
                          resolver=lambda h: ["93.184.216.34"],
                          rate_limiter=RateLimiter(
                              default=HostPolicy(min_interval_seconds=0, max_retry_after_seconds=5)),
                          sleep=_noop, **kw)


async def test_cross_origin_redirect_drops_credential_headers() -> None:
    key, tok = canary("key"), canary("tok")
    seen: dict[str, dict[str, str]] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen[req.url.host + req.url.path] = dict(req.headers)
        if req.url.host == "a.example" and req.url.path == "/start":
            return httpx.Response(302, headers={"location": "/same"})
        if req.url.path == "/same":
            return httpx.Response(302, headers={"location": "https://b.example/other"})
        return httpx.Response(200, json={})

    async with _client(handler, ("a.example", "b.example")) as c:
        await c.get("https://a.example/start", headers={"Authorization": f"Bearer {tok}",
                                                         "Ocp-Apim-Subscription-Key": key, "Accept": "x/y"})
    assert seen["a.example/same"]["authorization"] == f"Bearer {tok}"  # same origin keeps credentials
    other = seen["b.example/other"]
    assert "accept" in other
    assert_absent(json.dumps(other), key, tok)


async def test_http_errors_redirect_refusals_and_retry_paths_never_echo_keys(json_log: io.StringIO) -> None:
    key = canary("key")
    url = f"https://comtradeapi.un.org/data?subscription-key={key}&reporterCode=12"
    calls = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"})  # honoured (<= cap)
        if calls["n"] == 2:
            raise httpx.ConnectError("connection refused", request=req)
        if calls["n"] == 3:
            return httpx.Response(302, headers={"location": f"https://evil.example/steal?k={key}"})
        return httpx.Response(200)

    rendered: list[str] = []
    async with _client(flaky, max_attempts=4) as c:
        with pytest.raises(UnsafeRequest) as ei:
            await c.get(url)
        rendered += ["".join(traceback.format_exception(ei.value)), json.dumps(ei.value.problem(), default=str)]

    def too_long(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"retry-after": "86400"})

    async with _client(too_long) as c:
        with pytest.raises(SourceResponseError) as e2:
            await c.get(url)
        rendered += ["".join(traceback.format_exception(e2.value)), json.dumps(e2.value.problem(), default=str)]

    def dead(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    async with _client(dead, max_attempts=2) as c:
        with pytest.raises(SourceResponseError) as e3:
            await c.get(url)
        rendered += ["".join(traceback.format_exception(e3.value)), json.dumps(e3.value.problem(), default=str)]
        with pytest.raises(UnsafeRequest) as e4:
            await c.get(f"http://user:{key}@comtradeapi.un.org/x")
        rendered += [json.dumps(e4.value.problem(), default=str)]
    assert calls["n"] == 3 and "evil.example" in rendered[0]
    assert_absent("\n".join(rendered) + json_log.getvalue(), key)


def test_result_repr_and_redact_url_hide_keys_userinfo_and_body() -> None:
    key, pw, body = canary("key"), canary("pw"), canary("body")
    url = f"https://u:{pw}@h.example/p?subscription-key={key}&X-Amz-Signature={key}&ok=1"
    res = HttpResult(url, url, 200, {}, body.encode(), __import__("datetime").datetime.now(), 0.1, (url,), 1)
    assert_absent(repr(res) + str(res) + redact_url(url) + json.dumps(res.response_metadata()), key, pw, body)
    assert "ok=1" in redact_url(url)


# =========================================================================== 3. DB errors (throwaway DB)
def test_real_db_errors_are_scrubbed_in_job_errors_and_logs(db: Any, json_log: io.StringIO) -> None:
    from reva.adapters.postgres import queue
    from reva.api.auth import token_sha256

    tok = "reva_" + canary("tok")
    sha = token_sha256(tok)
    # (a) permission denied (reva_sim has no grant on ops.api_token): the token hash is a bound parameter
    with pytest.raises(DBAPIError) as e1, db.sim.connect() as conn:
        conn.execute(text("SELECT id FROM ops.api_token WHERE token_sha256 = :h"), {"h": sha})
    # (b) unique violation: PostgreSQL echoes the key value in DETAIL
    with pytest.raises(DBAPIError) as e2, db.owner.begin() as conn:
        for _ in range(2):
            conn.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, '{read}', 'r7')"),
                         {"h": sha})
    raw = str(e1.value) + str(e2.value)
    assert sha in raw and "[parameters:" in raw and "Key (token_sha256)" in raw  # sensitivity
    for exc in (e1.value, e2.value):
        doc = queue.error_document("transient_error", str(exc), retryable=True, attempt=1,
                                   exception=type(exc).__name__)
        assert_absent(json.dumps(doc), sha, tok)
        try:
            raise exc
        except DBAPIError:
            logging.getLogger("reva.jobs").exception("job failed")
    assert json_log.getvalue().count('"exc"') == 2
    assert_absent(json_log.getvalue(), sha, tok)


@pytest.mark.xfail(strict=True, reason="engine.py: make_engine lacks hide_parameters=True (patch P1)")
def test_runtime_engines_hide_bound_parameters(db: Any) -> None:
    for eng in (db.api, db.worker, db.sim):
        assert eng.hide_parameters, eng.url.username


# --------------------------------------------------------------------------- fresh DB for worker/API flows
@dataclass
class Fresh:
    settings: Settings
    owner: Engine
    worker: Engine
    sim: Engine
    store: Any
    name: str


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Fresh]:
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    base = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"reva_test_r7_{uuid.uuid4().hex[:8]}"
    assert name != "reva"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp("objects_r7")
    data = base.model_dump()
    data.update(reva_migration_database_url=base.migration_url().set(database=name).render_as_string(
        hide_password=False), reva_db_name=name, reva_env="test", reva_object_root=root)
    settings = Settings.model_validate(data)
    engines: list[Engine] = []
    try:
        for attempt in range(3):
            try:
                bootstrap(settings)
                break
            except Exception:
                if attempt == 2:
                    raise
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = [make_engine("migration", settings), make_engine("worker", settings), make_engine("sim", settings)]
        seed(engines[1], store)
        yield Fresh(settings, engines[0], engines[1], engines[2], store, name)
    finally:
        for e in engines:
            e.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _sweep(owner: Engine) -> str:
    """Every operator/client-visible ops row (api_token itself holds the hash by design)."""
    with owner.connect() as c:
        parts = [c.execute(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t))::text, '') FROM {t} t")).scalar_one()
                 for t in ("ops.audit", "ops.outbox", "ops.job")]
    return "\n".join(parts)


def _store_text(root: Path) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", "replace") for p in root.rglob("*") if p.is_file())


def _enqueue(f: Fresh, payload: dict[str, Any]) -> uuid.UUID:
    from reva.adapters.postgres import queue

    with f.worker.begin() as c:
        return queue.enqueue(c, "health", payload, queue.idempotency_key("health", payload), max_attempts=3)


def _run_failing_job(f: Fresh, monkeypatch: pytest.MonkeyPatch, handler: Any) -> dict[str, Any]:
    from reva.adapters.postgres import queue
    from reva.operations import jobs
    from reva.operations.worker import Worker

    jid = _enqueue(f, {"probe": uuid.uuid4().hex})
    monkeypatch.setattr(jobs, "resolve_handler", lambda jt: handler)
    w = Worker(f.settings, f.worker, f.store, f.sim, scheduler=False, invalidation=False, lease_seconds=30,
               heartbeat_seconds=5, job_types=["health"])
    job = w.claim_one()
    assert job is not None and job.id == jid
    w.execute(job)
    row = queue.job_row(f.owner, jid)
    assert row is not None and row["last_error"]
    return row


def test_worker_db_failure_last_error_and_api_view_hold_no_parameters(
        fresh: Fresh, monkeypatch: pytest.MonkeyPatch, json_log: io.StringIO) -> None:
    from reva.api.routes.system import public_error

    sha = canary("sha")

    def handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        with ctx.worker_engine.connect() as conn:  # real failure carrying a secret bound parameter
            conn.execute(text("SELECT CAST(:h AS text) FROM ops.no_such_table_r7"), {"h": sha})
        return {}

    row = _run_failing_job(fresh, monkeypatch, handler)
    assert row["last_error"]["exception"] == "ProgrammingError"
    assert "[parameters: ***]" in row["last_error"]["message"]  # the stored text still says what failed
    assert_absent(json.dumps(row["last_error"], default=str) + json.dumps(public_error(row["last_error"]))
                  + json_log.getvalue() + _sweep(fresh.owner), sha)


@pytest.mark.xfail(strict=True, reason="queue.error_document stores exception details unredacted (patch P3)")
def test_worker_failure_details_are_redacted(fresh: Fresh, monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.api.routes.system import public_error

    key = canary("key")

    def handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise SourceResponseError("provider refused", retryable=False,
                                  request=f"https://p.example/x?subscription-key={key}",
                                  authorization=f"Bearer {key}")

    row = _run_failing_job(fresh, monkeypatch, handler)
    assert_absent(json.dumps(row["last_error"], default=str) + json.dumps(public_error(row["last_error"])), key)


# =========================================================================== 4/5. API errors, audit, outbox
ADMIN_PASSWORD = canary("adminpw")


@pytest.fixture(scope="module")
def api(fresh: Fresh) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from reva.api.app import create_app
    from reva.api.auth import hash_password

    data = fresh.settings.model_dump()
    data.update(reva_admin_password_hash=SecretStr(hash_password(ADMIN_PASSWORD)),
                reva_session_secret=SecretStr(canary("sess") * 2), reva_allowed_hosts="testserver,localhost",
                reva_cors_origins="", reva_env="test")
    app = create_app(Settings.model_validate(data), login_rate_limit_per_minute=1000)

    @app.get("/v1/_r7_boom", include_in_schema=False)
    def boom() -> None:
        raise RuntimeError(f"internal state password={ADMIN_PASSWORD} at /srv/reva/secret.py")

    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the SPA catch-all

    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client


def _mint_token(f: Fresh, label: str) -> tuple[str, str]:
    from reva.api.auth import new_api_token

    token, sha = new_api_token()
    with f.owner.begin() as c:  # exactly what `reva auth create-token` does
        tid = c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label, principal_kind) "
                             "VALUES (:s, :sc, :l, 'service') RETURNING id"),
                        {"s": sha, "sc": ["read", "ingest"], "l": label}).scalar_one()
        c.execute(text("INSERT INTO ops.audit(actor, action, target, details) VALUES ('admin:cli', 'token.create', "
                       "jsonb_build_object('token_id', CAST(:i AS text)), "
                       "jsonb_build_object('label', CAST(:l AS text)))"),
                  {"i": str(tid), "l": label})
    return token, sha


def test_api_auth_flows_leave_no_credentials_in_bodies_logs_audit_outbox_jobs(
        fresh: Fresh, api: Any, json_log: io.StringIO) -> None:
    app, client = api
    token, sha = _mint_token(fresh, "service:r7")
    wrong = canary("wrongpw")
    bodies: list[str] = []

    r = client.post("/v1/session", json={"username": "admin", "password": wrong})
    assert r.status_code == 401
    bodies.append(r.text + json.dumps(dict(r.headers)))
    r = client.post("/v1/session", json={"username": "admin", "password": [wrong]})  # contract violation
    assert r.status_code == 422
    bodies.append(r.text)
    r = client.post("/v1/session", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    cookie, csrf = client.cookies.get("reva_session"), client.cookies.get("reva_csrf")
    assert cookie and csrf
    assert r.json()["csrf_token"] == csrf  # by design: the double-submit token is handed to its own client
    assert_absent(r.text, ADMIN_PASSWORD, cookie)
    r = client.get(f"/v1/jobs?token={token}", headers={"X-CSRF-Token": csrf})  # secret in a query string
    bodies.append(r.text)
    r = client.delete("/v1/session", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    client.cookies.clear()
    r = client.get("/v1/jobs", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    bodies.append(r.text)
    r = client.get("/v1/jobs", headers={"Authorization": f"Bearer {token}x"})
    assert r.status_code == 401
    bodies.append(r.text)
    # token lookup fails inside the database (reva_sim has no grant): handled 500 with parameters in the trace
    auth = app.state.auth
    original = auth.engine_getter
    auth.engine_getter = lambda: fresh.sim
    try:
        r = client.get("/v1/jobs", headers={"Authorization": f"Bearer {token}", "X-Request-ID": "r7-db-fail"})
    finally:
        auth.engine_getter = original
    assert r.status_code == 500 and r.json()["code"] == "database_error"
    assert r.json()["request_id"] == "r7-db-fail" and r.headers["x-request-id"] == "r7-db-fail"
    bodies.append(r.text)
    assert "permission denied" in json_log.getvalue()  # the failure itself is logged for operators

    secrets = [ADMIN_PASSWORD, wrong, cookie, csrf, token, sha]
    assert_absent("\n".join(bodies), *secrets)
    assert_absent(json_log.getvalue(), *secrets)
    assert_absent(_sweep(fresh.owner), *secrets)
    assert_absent(_store_text(Path(fresh.settings.reva_object_root)), *secrets)
    for b in bodies:
        assert "Traceback" not in b and "[SQL:" not in b and "psycopg" not in b


def test_api_unhandled_error_is_generic_with_request_id(api: Any, json_log: io.StringIO) -> None:
    _, client = api
    r = client.get("/v1/_r7_boom", headers={"X-Request-ID": "r7-boom-1"})
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "internal_error" and body["detail"] == "internal error"
    assert body["request_id"] == "r7-boom-1"
    assert_absent(r.text, ADMIN_PASSWORD, "/srv/reva", "RuntimeError", "Traceback")
    assert "unhandled error" in json_log.getvalue()
    assert_absent(json_log.getvalue(), ADMIN_PASSWORD)


@pytest.mark.xfail(strict=True, reason="api/errors.py: the 500 fallback response lacks X-Request-ID (patch P5)")
def test_api_unhandled_error_response_carries_request_id_header(api: Any) -> None:
    _, client = api
    r = client.get("/v1/_r7_boom", headers={"X-Request-ID": "r7-boom-2"})
    assert r.headers.get("x-request-id") == "r7-boom-2"


_API_PROCESS = textwrap.dedent("""
    import logging.config, sys
    from uvicorn.config import LOGGING_CONFIG
    logging.config.dictConfig(LOGGING_CONFIG)          # `uvicorn reva.api.app:app` configures logging first,
    import reva.api.app as appmod                      # then imports the module-level app
    if sys.argv[1] == "patched":                       # what patch P4 adds to reva.api.app._module_app()
        from reva.operations.telemetry import configure_logging
        configure_logging("INFO")
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import ProgrammingError
    app = appmod.app
    @app.get("/v1/_boom")
    def boom():
        raise ProgrammingError("SELECT * FROM ops.api_token WHERE token_sha256 = %(h)s",
                               {"h": sys.argv[2]}, Exception("permission denied for table api_token"))
    @app.get("/v1/_boom2")
    def boom2():
        raise RuntimeError("Authorization: Bearer " + sys.argv[2])
    app.router.routes[:0] = [app.router.routes.pop(), app.router.routes.pop()]  # ahead of the SPA catch-all
    c = TestClient(app, raise_server_exceptions=False)  # no lifespan: this process never touches a database
    assert c.get("/v1/_boom").status_code == 500
    assert c.get("/v1/_boom2").status_code == 500
""")


def _api_process(tmp_path: Path, mode: str, secret: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "REVA_ENV": "local",
           "REVA_API_DB_PASSWORD": canary("apipw"), "REVA_ALLOWED_HOSTS": "testserver",
           "REVA_DB_PORT": "1", "REVA_DB_NAME": "reva_r7_unreachable"}  # never the dev database
    return subprocess.run([sys.executable, "-c", _API_PROCESS, mode, secret], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)


def test_api_process_logs_are_redacted_once_logging_is_configured(tmp_path: Path) -> None:
    sha = canary("sha")
    r = _api_process(tmp_path, "patched", sha)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "database error" in r.stderr and "unhandled error" in r.stderr
    assert_absent(r.stdout + r.stderr, sha)


@pytest.mark.xfail(strict=True, reason="reva.api.app never installs the redacting log handler (patch P4)")
def test_api_process_as_shipped_redacts_error_logs(tmp_path: Path) -> None:
    sha = canary("sha")
    r = _api_process(tmp_path, "as-shipped", sha)
    assert r.returncode == 0, r.stderr[-3000:]
    assert_absent(r.stdout + r.stderr, sha)


# =========================================================================== 6. sandbox
def test_sandbox_child_environment_contains_no_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from reva.autonomy.sandbox import scrubbed_environment

    planted = {k: canary(k[:6]) for k in ("REVA_API_DB_PASSWORD", "REVA_MIGRATION_DATABASE_URL", "PGPASSWORD",
                                          "REVA_SESSION_SECRET", "REVA_ADMIN_PASSWORD_HASH", "AWS_SECRET_ACCESS_KEY",
                                          "REVA_COMTRADE_API_KEY", "REVA_S3_SECRET_ACCESS_KEY", "DATABASE_URL")}
    planted["HTTPS_PROXY"] = f"http://proxyuser:{canary('proxypw')}@proxy.internal:3128"
    for k, v in planted.items():
        monkeypatch.setenv(k, v)
    env = scrubbed_environment()
    r = subprocess.run([sys.executable, "-I", "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
                       env={**env, "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=60)
    child = json.loads(r.stdout)
    assert set(child) <= {"LANG", "LC_ALL", "TZ", "LC_CTYPE", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
                          "REVA_SANDBOX_ISOLATION", "HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                          "https_proxy", "all_proxy", "NO_PROXY"}
    assert child["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert_absent(r.stdout, *planted.values(), planted["HTTPS_PROXY"].split(":")[2].split("@")[0])


@pytest.mark.xfail(strict=True, reason="autonomy/sandbox.py stores child stderr in evidence unscrubbed (patch P6)")
def test_sandbox_child_stderr_is_scrubbed_before_it_becomes_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.autonomy import sandbox
    from reva.ports.autonomy import SandboxPolicy

    key = canary("key")
    monkeypatch.setenv("REVA_COMTRADE_API_KEY", key)
    # A child that obtained the worker's secret (e.g. via /proc/<ppid>/environ without a PID namespace)
    # and writes it to stderr before failing.
    monkeypatch.setattr(sandbox, "namespace_isolation",
                        lambda: ("/bin/sh", "-c", f"echo 'leak {key} Authorization: Bearer {key}' >&2; exit 3", "--"))
    budget = type("B", (), {"max_cpu_seconds": 5, "max_memory_bytes": 1 << 30, "max_wall_seconds": 20,
                            "max_requests": 1, "max_bytes": 1000})()
    policy = SandboxPolicy(policy_version="r7", isolation="trusted_template", network_hosts=(), writable_paths=(),
                           secret_access=False, budget=budget)
    out, _ = sandbox.TrustedTemplateSandbox._spawn({"manifest": {}}, policy)
    assert out["ok"] is False and "leak" in out["error"]
    assert_absent(json.dumps(out), key)


# =========================================================================== 7. ingest logging
async def test_fixture_ingest_logs_no_raw_bodies(fresh: Fresh, json_log: io.StringIO) -> None:
    """Ingest at DEBUG: logs carry counts/ids, never provider bodies (which are untrusted, possibly huge)."""
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.evidence.runner import FixtureHttp, post_publish_for, run_connector

    conn = builtin_connectors()["worldbank.wdi.indicators"]
    spec = conn.describe()
    http = FixtureHttp(spec.name)
    bodies = [(http.dir / e["file"]).read_bytes() for e in http.entries.values()]
    assert bodies
    rep = await run_connector(conn, http, fresh.store, fresh.worker, persist_cursor=False,
                              post_publish=post_publish_for(spec.template))
    assert rep.inserted > 0
    out = json_log.getvalue()
    assert "published acquisition" in out
    for line in out.splitlines():
        assert len(line) < 2000, line[:200]
    for b in bodies:
        snippet = b[200:260].decode("utf-8", "replace")
        assert snippet not in out


@pytest.mark.xfail(strict=True, reason="evidence/runner.py FixtureHttp echoes the raw URL in its error (patch P7)")
async def test_fixture_http_missing_fixture_error_has_no_key() -> None:
    from reva.evidence.runner import FixtureHttp

    key = canary("key")
    with pytest.raises(SourceResponseError) as ei:
        await FixtureHttp("un.comtrade").get(f"https://comtradeapi.un.org/x?subscription-key={key}")
    assert_absent(str(ei.value) + json.dumps(ei.value.problem(), default=str), key)
