"""Authentication, session, CSRF, token scopes, rate limiting and audit."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from reva.api.app import create_app
from reva.api.auth import hash_password, new_api_token, token_sha256, verify_password
from reva.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "a very long local test password"


def settings_for(db: Any, **over: Any) -> Settings:
    data = db.settings.model_dump()
    data.update(reva_admin_password_hash=SecretStr(hash_password(PASSWORD)), reva_session_secret=SecretStr("k" * 48),
                reva_allowed_hosts="testserver,localhost", reva_env="test")
    data.update(over)
    return Settings.model_validate(data)


@pytest.fixture()
def client(db: Any) -> Iterator[TestClient]:
    app = create_app(settings_for(db))
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


def _token(db: Any, scopes: list[str], label: str = "test-token", revoked: bool = False) -> str:
    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label, revoked_at) "
                       "VALUES (:h, :s, :l, CASE WHEN :r THEN clock_timestamp() END)"),
                  {"h": sha, "s": scopes, "l": label, "r": revoked})
    return tok


def _login(c: TestClient) -> Any:
    return c.post("/v1/session", json={"username": "admin", "password": PASSWORD})


def test_hash_password_is_argon2id() -> None:
    h = hash_password(PASSWORD)
    assert h.startswith("$argon2id$")
    assert verify_password(h, PASSWORD) and not verify_password(h, PASSWORD + "x")
    with pytest.raises(ValueError):
        hash_password("short")
    assert token_sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/entities"), ("GET", "/v1/beliefs"), ("GET", "/v1/session"), ("POST", "/v1/simulations"),
    ("POST", "/v1/scenarios"), ("POST", "/v1/discovery"), ("POST", "/v1/query"), ("GET", "/metrics"),
    ("GET", f"/v1/jobs/{uuid.UUID(int=1)}"), ("DELETE", "/v1/session"),
])
def test_unauthenticated_is_401(client: TestClient, method: str, path: str) -> None:
    r = client.request(method, path, json={}, headers={"Idempotency-Key": "unauth-test-key"})
    assert r.status_code == 401, (path, r.text)
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "unauthenticated"


def test_wrong_password_is_401_and_audited(db: Any, client: TestClient) -> None:
    marker = f"user-{secrets.token_hex(4)}"
    r = client.post("/v1/session", json={"username": marker, "password": "nope"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"
    assert "nope" not in r.text
    r2 = client.post("/v1/session", json={"username": "admin", "password": "wrong password"})
    assert r2.status_code == 401
    with db.owner.connect() as c:
        rows = c.execute(text("SELECT actor, details FROM ops.audit WHERE action = 'session.login_failed' "
                              "AND target->>'username' = :u"), {"u": marker}).mappings().all()
    assert len(rows) == 1 and rows[0]["actor"] == "anonymous"
    assert "nope" not in str(rows[0]["details"])


def test_login_sets_httponly_session_and_csrf_cookie(db: Any, client: TestClient) -> None:
    r = _login(client)
    assert r.status_code == 200, r.text
    cookies = r.headers.get_list("set-cookie")
    sess = next(c for c in cookies if c.startswith("reva_session="))
    csrf = next(c for c in cookies if c.startswith("reva_csrf="))
    assert "HttpOnly" in sess and "samesite=strict" in sess.lower() and "Max-Age=28800" in sess
    assert "HttpOnly" not in csrf
    assert r.json()["csrf_token"] == client.cookies.get("reva_csrf")
    assert r.json()["principal"] == "human:admin"
    me = client.get("/v1/session")
    assert me.status_code == 200 and set(me.json()["scopes"]) == {"read", "simulate", "ingest", "review"}
    with db.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ops.audit WHERE action = 'session.login'")).scalar_one() >= 1


def test_secure_cookie_in_production_settings(db: Any) -> None:
    from reva.api.auth import AuthService

    s = settings_for(db, reva_public_base_url="https://reva.example.org")
    assert AuthService(s, lambda: None).secure_cookies


def test_unsafe_request_requires_csrf_header(client: TestClient) -> None:
    assert _login(client).status_code == 200
    q = {"kind": "relations", "entity_id": str(uuid.UUID(int=5)), "max_edges": 1}
    r = client.post("/v1/query", json=q)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    bad = client.post("/v1/query", json=q, headers={"X-CSRF-Token": "forged"})
    assert bad.status_code == 403
    ok = client.post("/v1/query", json=q, headers={"X-CSRF-Token": client.cookies.get("reva_csrf")})
    assert ok.status_code in (200, 404), ok.text  # authenticated; entity may not exist
    assert ok.status_code != 403


def test_tampered_session_cookie_is_401(client: TestClient) -> None:
    assert _login(client).status_code == 200
    value = client.cookies.get("reva_session")
    client.cookies.set("reva_session", value[:-2] + ("AA" if not value.endswith("AA") else "BB"))
    assert client.get("/v1/session").status_code == 401


def test_logout_ends_session(client: TestClient) -> None:
    assert _login(client).status_code == 200
    stolen = client.cookies.get("reva_session")
    r = client.delete("/v1/session", headers={"X-CSRF-Token": client.cookies.get("reva_csrf")})
    assert r.status_code == 204
    client.cookies.set("reva_session", stolen)  # a copied cookie is not valid after logout
    assert client.get("/v1/session").status_code == 401


def test_token_scopes_and_revocation(db: Any, client: TestClient) -> None:
    read = _token(db, ["read"])
    h = {"Authorization": f"Bearer {read}"}
    assert client.get("/v1/entities", headers=h).status_code == 200
    assert client.get("/metrics", headers=h).status_code == 200
    # bearer requests are not cookie-authenticated: no CSRF needed, but scope is enforced
    r = client.post("/v1/simulations", json={"scenario_id": str(uuid.uuid4()), "fidelity": "L1"},
                    headers={**h, "Idempotency-Key": "token-scope-test"})
    assert r.status_code == 403 and r.json()["details"]["required_scope"] == "simulate"
    r = client.post("/v1/discovery", json={"query": "population"}, headers={**h, "Idempotency-Key": "token-scope-2"})
    assert r.status_code == 403
    revoked = _token(db, ["read", "simulate"], revoked=True)
    r = client.get("/v1/entities", headers={"Authorization": f"Bearer {revoked}"})
    assert r.status_code == 401
    assert client.get("/v1/entities", headers={"Authorization": "Bearer reva_not_a_token"}).status_code == 401
    assert client.get("/v1/entities", headers={"Authorization": f"Basic {read}"}).status_code == 401


def test_review_requires_human_identity(db: Any, client: TestClient) -> None:
    svc = _token(db, ["read", "review"], label="service-bot")
    r = client.post(f"/v1/proposals/{uuid.uuid4()}/review", json={"decision": "approve"},
                    headers={"Authorization": f"Bearer {svc}"})
    assert r.status_code == 403


def test_login_rate_limited_after_repeated_failures(db: Any) -> None:
    app = create_app(settings_for(db))
    with TestClient(app) as c:
        codes = [c.post("/v1/session", json={"username": "admin", "password": f"bad-{i}"}).status_code
                 for i in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        r = c.post("/v1/session", json={"username": "admin", "password": PASSWORD})
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    app.state.engine.dispose()


def test_login_without_configured_hash_is_unavailable(db: Any) -> None:
    app = create_app(settings_for(db, reva_admin_password_hash=None))
    with TestClient(app) as c:
        r = c.post("/v1/session", json={"username": "admin", "password": PASSWORD})
        assert r.status_code == 503 and r.json()["code"] == "auth_not_configured"
    app.state.engine.dispose()


def test_validation_errors_never_echo_password(client: TestClient) -> None:
    r = client.post("/v1/session", json={"username": "", "password": "secret-value-123"})
    assert r.status_code == 422 and "secret-value-123" not in r.text


def test_security_headers_and_trusted_hosts(client: TestClient) -> None:
    r = client.get("/health/live")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/health/live", headers={"Host": "evil.example"}).status_code == 400


def test_cors_only_for_configured_origins(db: Any) -> None:
    app = create_app(settings_for(db, reva_cors_origins="http://localhost:5173"))
    with TestClient(app) as c:
        pre = {"Access-Control-Request-Method": "GET"}
        ok = c.options("/v1/entities", headers={"Origin": "http://localhost:5173", **pre})
        assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
        bad = c.options("/v1/entities", headers={"Origin": "https://evil.example", **pre})
        assert "access-control-allow-origin" not in bad.headers
    app.state.engine.dispose()
