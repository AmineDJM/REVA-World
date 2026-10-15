"""Adversarial review of the HTTP surface (docs/dev/review_api_security.md).

The first group needs no database: authentication is decided before any query, oversized bodies are
refused before parsing, and configuration errors fail at app construction. The second group (marked
``db``) exercises login, sessions, CSRF, tokens and job submission through the reva_api role.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr
from sqlalchemy import text

from reva.api.app import create_app
from reva.api.auth import hash_password, new_api_token
from reva.kernel.errors import ConfigurationError
from reva.settings import Settings

PASSWORD = "adversarial review local password"

# (method, route template) pairs reachable without credentials. Anything else must answer 401.
PUBLIC = {
    ("GET", "/health/live"), ("GET", "/health/ready"), ("POST", "/v1/session"),
    ("GET", "/"), ("GET", "/{path:path}"), ("GET", "/openapi.json"), ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
}
PARAM_VALUES = {"kind": "observation", "model_ref": "naive_last_value@1.0.0", "entity_ref": "FRA"}


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in list(os.environ):
        if k.startswith("REVA_") or k == "DATABASE_URL":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)


def _local(**over: Any) -> Settings:
    return Settings(_env_file=None, reva_env="test", reva_allowed_hosts="testserver",  # type: ignore[call-arg]
                    reva_session_secret=SecretStr("x" * 48), **over)


def _production(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "reva_env": "production", "reva_object_backend": "s3", "reva_s3_bucket": "b",
        "reva_s3_access_key_id": "AKIAEXAMPLE", "reva_s3_secret_access_key": "s3-secret-value-xyz",
        "reva_session_secret": "s" * 48, "reva_public_base_url": "https://reva.example.org",
        "reva_db_sslmode": "verify-full", "reva_allowed_hosts": "testserver",
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _concrete(path: str) -> str:
    def sub(m: re.Match[str]) -> str:
        return PARAM_VALUES.get(m.group(1), str(uuid.UUID(int=7)))
    return re.sub(r"\{([a-z_]+)(?::[a-z]+)?\}", sub, path)


def _flat_routes(routes: list[Any]) -> Iterator[Any]:
    """Every concrete route, descending into included routers (FastAPI wraps them in an include node)."""
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _flat_routes(inner.routes)
        else:
            yield r


def _routes(app: Any) -> list[tuple[str, str]]:
    out = []
    for r in _flat_routes(app.router.routes):
        methods = getattr(r, "methods", None) or set()
        for m in sorted(methods - {"HEAD", "OPTIONS"}):
            out.append((m, r.path))
    # Cross-check with the public contract: nothing documented may be missed by the walk.
    documented = {(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops}
    assert documented <= set(out), documented - set(out)
    return out


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
def test_every_non_public_route_requires_authentication() -> None:
    """Enumerate the app's routes: everything outside the public allowlist answers 401, whatever the
    body, with no cookie, with a session cookie signed by another key, and with a non-Bearer scheme."""
    app = create_app(_local())
    routes = _routes(app)
    assert ("POST", "/v1/jobs/{job_id}/cancel") in routes and ("POST", "/v1/proposals/{proposal_id}/review") in routes
    forged = URLSafeTimedSerializer("attacker-key" * 4, salt="reva-session-v1").dumps(
        {"u": "admin", "sid": "a" * 22, "csrf": "c" * 20})
    checked = 0
    with TestClient(app) as c:
        for method, path in routes:
            if (method, path) in PUBLIC:
                continue
            url = _concrete(path)
            for headers, cookies in (({}, {}), ({}, {"reva_session": forged}),
                                     ({"Authorization": "Basic YWRtaW46eA=="}, {})):
                c.cookies.clear()
                for k, v in cookies.items():
                    c.cookies.set(k, v)
                r = c.request(method, url, json={}, headers={"Idempotency-Key": "auth-enum-key", **headers})
                assert r.status_code == 401, (method, path, headers, cookies, r.status_code, r.text)
                assert r.json()["code"] == "unauthenticated"
            checked += 1
    assert checked >= 30  # the whole /v1 surface plus /metrics


@pytest.mark.usefixtures("clean_env")
def test_api_routes_all_declare_an_auth_dependency() -> None:
    """Static complement of the request test: every non-public APIRoute depends on the authenticator."""
    from reva.api.auth import authenticated

    app = create_app(_local())

    def calls(dep: Any) -> set[str]:
        names = {getattr(dep.call, "__name__", "")} if dep.call is not None else set()
        for d in dep.dependencies:
            names |= calls(d)
        return names

    seen = 0
    for r in _flat_routes(app.router.routes):
        if not isinstance(r, APIRoute) or any((m, r.path) in PUBLIC for m in r.methods):
            continue
        names = calls(r.dependant)
        assert names & {authenticated.__name__, "require_read", "require_simulate", "require_ingest",
                        "require_review"}, r.path
        seen += 1
    assert seen >= 30


@pytest.mark.usefixtures("clean_env")
def test_production_hides_docs_and_requires_auth_for_openapi() -> None:
    app = create_app(_production(), static_dir=Path("/nonexistent-ui"))
    # No lifespan: production enforces the schema contract at startup and there is no database here.
    c = TestClient(app)
    assert c.get("/docs").status_code == 404
    r = c.get("/openapi.json")
    assert r.status_code == 401 and "paths" not in r.text
    h = c.get("/health/live").headers
    assert h["strict-transport-security"].startswith("max-age=")
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-frame-options"] == "DENY"
    # The committed contract is still generated from the same app object.
    assert "/v1/session" in app.openapi()["paths"]


@pytest.mark.usefixtures("clean_env")
def test_local_still_serves_openapi_and_docs() -> None:
    with TestClient(create_app(_local())) as c:
        assert "/v1/entities" in c.get("/openapi.json").json()["paths"]
        assert c.get("/docs").status_code == 200


@pytest.mark.usefixtures("clean_env")
def test_oversized_bodies_are_refused_before_parsing() -> None:
    app = create_app(_local(), max_body_bytes=4096)
    with TestClient(app) as c:
        big = b'{"username":"admin","password":"' + b"x" * 10_000 + b'"}'
        r = c.post("/v1/session", content=big, headers={"content-type": "application/json"})
        assert r.status_code == 413 and r.json()["code"] == "payload_too_large", r.text
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-type"].startswith("application/problem+json")

        def chunks() -> Iterator[bytes]:  # no Content-Length: counted while streaming
            yield b'{"username":"admin","password":"'
            for _ in range(10):
                yield b"y" * 1000
            yield b'"}'

        r = c.post("/v1/session", content=chunks(), headers={"content-type": "application/json"})
        assert r.status_code == 413 and r.json()["code"] == "payload_too_large", r.text
        # An authenticated-only route refuses the body as well (before authentication is even attempted).
        r = c.post("/v1/scenarios", content=big, headers={"content-type": "application/json"})
        assert r.status_code == 413
        r = c.post("/v1/session", content=b"{}", headers={"content-type": "application/json",
                                                           "content-length": "nonsense"})
        assert r.status_code in (400, 413)


@pytest.mark.usefixtures("clean_env")
def test_default_body_cap_is_one_mebibyte() -> None:
    with TestClient(create_app(_local())) as c:
        r = c.post("/v1/session", content=b" " * (1_048_576 + 1), headers={"content-type": "application/json"})
        assert r.status_code == 413


@pytest.mark.usefixtures("clean_env")
def test_wildcard_cors_with_credentials_is_refused_everywhere() -> None:
    with pytest.raises(ConfigurationError):
        create_app(_local(reva_cors_origins="*"))
    with pytest.raises(ConfigurationError):
        create_app(_local(reva_cors_origins="http://localhost:5173,*"))


@pytest.mark.usefixtures("clean_env")
def test_spa_path_tricks_never_500_or_escape(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>REVA</title>")
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    (ui / "link.txt").symlink_to(tmp_path / "secret.txt")
    with TestClient(create_app(_local(), static_dir=ui), raise_server_exceptions=False) as c:
        for p in ("/%00", "/a%00b", "/assets/%00.js", "/..%2Fsecret.txt", "/%2e%2e/secret.txt", "/link.txt",
                  "/..%5csecret.txt", "/" + "a" * 5000):
            r = c.get(p)
            assert r.status_code in (200, 404), (p, r.status_code)
            assert "TOP-SECRET" not in r.text, p


@pytest.mark.usefixtures("clean_env")
def test_internal_errors_carry_security_headers_and_no_internals() -> None:
    app = create_app(_local())

    @app.get("/v1/_boom_for_test", include_in_schema=False)
    def boom() -> None:
        raise RuntimeError("SELECT secret FROM internals")

    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the SPA catch-all
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/v1/_boom_for_test")
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    assert "SELECT" not in r.text and "RuntimeError" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _db_settings(db: Any, **over: Any) -> Settings:
    data = db.settings.model_dump()
    data.update(reva_admin_password_hash=SecretStr(hash_password(PASSWORD)),
                reva_session_secret=SecretStr("q" * 48), reva_allowed_hosts="testserver",
                reva_cors_origins="", reva_env="test")
    data.update(over)
    return Settings.model_validate(data)


@pytest.fixture()
def dbapp(db: Any) -> Iterator[Any]:
    app = create_app(_db_settings(db))
    yield app
    app.state.engine.dispose()


def _login(c: TestClient, **kw: Any) -> Any:
    return c.post("/v1/session", json={"username": "admin", "password": PASSWORD}, **kw)


def _token(db: Any, scopes: list[str], label: str, kind: str = "service") -> dict[str, str]:
    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label, principal_kind) "
                       "VALUES (:h, :s, :l, :k)"), {"h": sha, "s": scopes, "l": label, "k": kind})
        stored = c.execute(text("SELECT token_sha256 FROM ops.api_token WHERE token_sha256 = :h"),
                           {"h": sha}).scalar_one()
    assert re.fullmatch(r"[0-9a-f]{64}", stored) and tok not in stored  # only the digest is at rest
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.db
@pytest.mark.parametrize("spoof", ["X-Forwarded-For", "X-Real-IP", "Forwarded", "True-Client-IP", "CF-Connecting-IP"])
def test_login_rate_limit_ignores_client_supplied_address_headers(db: Any, spoof: str) -> None:
    app = create_app(_db_settings(db), login_rate_limit_per_minute=3)
    with TestClient(app) as c:
        codes = []
        for i in range(5):
            value = f"for=203.0.113.{i}" if spoof == "Forwarded" else f"203.0.113.{i}"
            codes.append(c.post("/v1/session", json={"username": "admin", "password": f"bad-{i}"},
                                headers={spoof: value}).status_code)
        assert codes == [401, 401, 401, 429, 429]
    app.state.engine.dispose()


@pytest.mark.db
def test_rate_limited_attempts_are_audited_once_per_window(db: Any) -> None:
    marker = f"flood-{uuid.uuid4().hex[:8]}"
    app = create_app(_db_settings(db), login_rate_limit_per_minute=1)
    with TestClient(app) as c:
        codes = [c.post("/v1/session", json={"username": marker, "password": "x"}).status_code for _ in range(25)]
    app.state.engine.dispose()
    assert codes[0] == 401 and set(codes[1:]) == {429}
    tag = app.state.auth.username_tag(marker)
    with db.owner.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM ops.audit WHERE action = 'session.login_rate_limited' "
                              "AND target->>'username_tag' = :u"), {"u": tag}).scalar_one()
        leaked = conn.execute(text("SELECT count(*) FROM ops.audit WHERE action LIKE 'session.login_%' "
                                   "AND (target::text LIKE :m OR details::text LIKE :m)"),
                              {"m": f"%{marker}%"}).scalar_one()
    assert n == 1
    assert leaked == 0  # P8: the typed username is not stored verbatim


@pytest.mark.db
def test_relogin_rotates_session_and_revokes_the_presented_one(dbapp: Any) -> None:
    with TestClient(dbapp) as c:
        assert _login(c).status_code == 200
        first = c.cookies.get("reva_session")
        assert _login(c).status_code == 200  # the first cookie is presented with this login
        second = c.cookies.get("reva_session")
        assert second and second != first
        assert c.get("/v1/session").status_code == 200
        c.cookies.set("reva_session", first)
        assert c.get("/v1/session").status_code == 401


@pytest.mark.db
def test_rotating_the_admin_password_ends_existing_sessions(db: Any) -> None:
    app1 = create_app(_db_settings(db))
    with TestClient(app1) as c:
        assert _login(c).status_code == 200
        cookie = c.cookies.get("reva_session")
    app1.state.engine.dispose()
    same = create_app(_db_settings(db, reva_admin_password_hash=app1.state.settings.reva_admin_password_hash))
    with TestClient(same) as c:
        c.cookies.set("reva_session", cookie)
        assert c.get("/v1/session").status_code == 200  # same secret + same hash: still valid
    same.state.engine.dispose()
    rotated = create_app(_db_settings(db))  # same session secret, new password hash
    with TestClient(rotated) as c:
        c.cookies.set("reva_session", cookie)
        assert c.get("/v1/session").status_code == 401
    rotated.state.engine.dispose()


@pytest.mark.db
def test_csrf_double_submit_cannot_be_satisfied_by_a_tossed_cookie(dbapp: Any) -> None:
    """An attacker able to set the reva_csrf cookie (cookie tossing from a sibling host) and the header to
    the same value still fails: the token is also bound inside the signed session."""
    with TestClient(dbapp) as c:
        assert _login(c).status_code == 200
        real = c.cookies.get("reva_csrf")
        job = f"/v1/jobs/{uuid.uuid4()}/cancel"
        assert c.post(job).status_code == 403
        assert c.delete("/v1/session").status_code == 403
        c.cookies.set("reva_csrf", "attacker-chosen-value")
        r = c.post(job, headers={"X-CSRF-Token": "attacker-chosen-value"})
        assert r.status_code == 403 and r.json()["code"] == "forbidden"
        c.cookies.set("reva_csrf", real)
        assert c.post(job, headers={"X-CSRF-Token": real}).status_code == 404  # authenticated; no such job
        # Login cookies: HttpOnly session, script-readable CSRF token, both SameSite=Strict, host-only.
        set_cookies = _login(c).headers.get_list("set-cookie")
        sess = next(x for x in set_cookies if x.startswith("reva_session="))
        csrf = next(x for x in set_cookies if x.startswith("reva_csrf="))
        for ck in (sess, csrf):
            assert "samesite=strict" in ck.lower() and "domain=" not in ck.lower() and "Path=/" in ck
        assert "HttpOnly" in sess and "HttpOnly" not in csrf


@pytest.mark.db
def test_secure_flag_on_cookies_in_production(db: Any) -> None:
    s = _db_settings(db, reva_public_base_url="https://reva.example.org")
    app = create_app(s)
    with TestClient(app, base_url="https://testserver") as c:
        r = _login(c)
        assert r.status_code == 200
        for ck in r.headers.get_list("set-cookie"):
            assert "Secure" in ck, ck
    app.state.engine.dispose()


@pytest.mark.db
def test_service_token_with_human_label_cannot_review(db: Any, dbapp: Any) -> None:
    """principal_kind is authoritative: a service token is never a reviewer, even labelled human:<name>."""
    pid = uuid.uuid4()
    svc = _token(db, ["read", "review"], f"human:svc{uuid.uuid4().hex[:6]}", kind="service")
    human = _token(db, ["read", "review"], f"human:rev{uuid.uuid4().hex[:6]}", kind="human")
    with TestClient(dbapp) as c:
        r = c.post(f"/v1/proposals/{pid}/review", json={"decision": "approve"}, headers=svc)
        assert r.status_code == 403
        r = c.post(f"/v1/proposals/{pid}/review", json={"decision": "approve"}, headers=human)
        assert r.status_code == 404  # identity accepted; proposal does not exist


@pytest.mark.db
def test_idempotency_keys_are_namespaced_per_principal(db: Any, dbapp: Any) -> None:
    tok_a = _token(db, ["read", "simulate"], f"svc-a-{uuid.uuid4().hex[:6]}")
    tok_b = _token(db, ["read", "simulate"], f"svc-b-{uuid.uuid4().hex[:6]}")
    body = {"mode": "operational_as_known", "cutoff": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "concepts": ["demography.population.wdi_midyear_total"]}
    key = {"Idempotency-Key": f"shared-key-{uuid.uuid4().hex}"}
    ids: list[str] = []
    with TestClient(dbapp) as c:
        try:
            ra = c.post("/v1/replay", json=body, headers={**tok_a, **key})
            assert ra.status_code == 202, ra.text
            ids.append(ra.json()["job_id"])
            rb = c.post("/v1/replay", json=body, headers={**tok_b, **key})
            assert rb.status_code == 202, rb.text
            ids.append(rb.json()["job_id"])
            assert ids[0] != ids[1]  # another principal's key never returns (or blocks) my job
            again = c.post("/v1/replay", json=body, headers={**tok_a, **key})
            assert again.json()["job_id"] == ids[0]  # same principal, same request: same job
            changed = c.post("/v1/replay", json={**body, "horizon_years": 2}, headers={**tok_a, **key})
            assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
            # Session principal: its own namespace too.
            assert _login(c).status_code == 200
            rs = c.post("/v1/replay", json=body, headers={**key, "X-CSRF-Token": c.cookies.get("reva_csrf")})
            assert rs.status_code == 202 and rs.json()["job_id"] not in ids
            ids.append(rs.json()["job_id"])
            with db.owner.connect() as conn:
                creators = dict(conn.execute(text("SELECT id::text, created_by FROM ops.job WHERE id = ANY(:i)"),
                                             {"i": [uuid.UUID(i) for i in ids]}).all())
            assert creators[ids[2]] == "human:admin" and creators[ids[0]].startswith("token:svc-a-")
        finally:
            for jid in ids:  # never leave queued jobs for workers in the shared test database
                c.post(f"/v1/jobs/{jid}/cancel", headers=tok_a)


@pytest.mark.db
def test_cancel_requires_the_job_types_scope_even_for_the_creator(db: Any, dbapp: Any) -> None:
    """Cancellation is authorised by scope per job type; read-only principals cannot cancel anything."""
    reader = _token(db, ["read"], f"reader-{uuid.uuid4().hex[:6]}")
    sim = _token(db, ["read", "simulate"], f"sim-{uuid.uuid4().hex[:6]}")
    body = {"mode": "operational_as_known", "cutoff": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    with TestClient(dbapp) as c:
        r = c.post("/v1/replay", json=body, headers={**sim, "Idempotency-Key": f"cancel-{uuid.uuid4().hex}"})
        assert r.status_code == 202, r.text
        jid = r.json()["job_id"]
        r = c.post(f"/v1/jobs/{jid}/cancel", headers=reader)
        assert r.status_code == 403 and r.json()["details"]["required_scope"] == "simulate"
        assert c.post(f"/v1/jobs/{jid}/cancel", headers=sim).json()["status"] == "cancelled"


@pytest.mark.db
def test_database_errors_do_not_leak_sql(dbapp: Any, db: Any) -> None:
    sim = _token(db, ["read", "simulate"], f"leak-{uuid.uuid4().hex[:6]}")
    with TestClient(dbapp) as c:
        # A 300-character key passes no client check of length 8..200: rejected by the API, not the DB.
        r = c.post("/v1/replay", json={"mode": "operational_as_known", "cutoff": "2020-01-01T00:00:00Z"},
                   headers={**sim, "Idempotency-Key": "k" * 300})
        assert r.status_code == 400 and r.json()["code"] == "invalid_idempotency_key"
        for url in ("/v1/jobs?cursor=%00%00", "/v1/beliefs?as_of=not-a-time", f"/v1/provenance/artifact/{'z' * 64}"):
            r = c.get(url, headers=sim)
            assert r.status_code in (400, 404, 422), (url, r.status_code)
            for marker in ("SELECT", "psycopg", "Traceback", "[SQL", "sqlalchemy"):
                assert marker not in r.text, (url, marker)
