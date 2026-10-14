"""Authentication: Argon2id admin login, signed short-lived sessions, CSRF
double-submit, login rate limiting with audit records, and hashed, scoped,
revocable API tokens.

* Sessions are signed (itsdangerous) HttpOnly cookies, SameSite=Strict, Secure in
  production, valid for ``SESSION_TTL``. A companion non-HttpOnly CSRF cookie must
  be echoed in ``X-CSRF-Token`` on every unsafe cookie-authenticated request; it is
  compared in constant time with both the cookie and the value bound inside the
  signed session.
* API tokens (``Authorization: Bearer``) are looked up by SHA-256 in
  ``ops.api_token``; only the hash is stored. Tokens are never stored client-side
  by the app (no localStorage).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import Engine, text

from reva.api.errors import ApiProblem, Forbidden, Unauthenticated
from reva.settings import Settings

SESSION_COOKIE = "reva_session"
CSRF_COOKIE = "reva_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL = timedelta(hours=8)
ALL_SCOPES = frozenset({"read", "simulate", "ingest", "review"})
ADMIN_USERNAME = "admin"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOGIN_ATTEMPTS_PER_MINUTE = 5

_hasher = PasswordHasher()  # argon2-cffi default: Argon2id
# A valid Argon2id hash used to equalise timing when the username is wrong.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    """Argon2id hash for REVA_ADMIN_PASSWORD_HASH (CLI: ``reva auth hash-password``)."""
    if not password or len(password) < 12:
        raise ValueError("admin password must have at least 12 characters")
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_api_token() -> tuple[str, str]:
    """Return (token, sha256). Store only the hash in ops.api_token.token_sha256."""
    tok = "reva_" + secrets.token_urlsafe(32)
    return tok, token_sha256(tok)


def client_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return hashlib.sha256(host.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Principal:
    username: str
    actor: str  # audit identity, e.g. human:admin or token:<label>
    method: str  # "session" | "token"
    scopes: frozenset[str]
    csrf: str | None = None
    session_id: str | None = None
    expires_at: datetime | None = None
    token_id: str | None = None
    token_label: str | None = None

    @property
    def human_reviewer(self) -> str | None:
        """Reviewer identity for ops.review_proposal (must be human:<name>)."""
        if self.method == "session":
            return f"human:{self.username}"
        if self.token_label and self.token_label.startswith("human:"):
            return self.token_label
        return None


class LoginRateLimiter:
    """In-memory sliding window per client (single API process in V1)."""

    def __init__(self, limit: int = LOGIN_ATTEMPTS_PER_MINUTE, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, key: str) -> float | None:
        """Record an attempt; return seconds to wait if the limit is exceeded."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1.0, self.window - (now - q[0]))
            q.append(now)
            if len(self._hits) > 10_000:  # bound memory
                for k in list(self._hits)[:5_000]:
                    if not self._hits[k]:
                        del self._hits[k]
            return None


@dataclass
class AuthService:
    settings: Settings
    engine_getter: Any
    limiter: LoginRateLimiter = field(default_factory=LoginRateLimiter)
    _revoked: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        secret = self.settings.reva_session_secret
        if secret is not None and secret.get_secret_value():
            key = secret.get_secret_value()
        elif self.settings.reva_env == "production":
            raise ApiProblem(500, "configuration_error", "REVA_SESSION_SECRET is required in production")
        else:
            key = secrets.token_urlsafe(48)  # ephemeral local secret: sessions end on restart
        self._serializer = URLSafeTimedSerializer(key, salt="reva-session-v1")

    # --- configuration --------------------------------------------------
    @property
    def secure_cookies(self) -> bool:
        return self.settings.reva_env == "production" or self.settings.reva_public_base_url.startswith("https://")

    def _admin_hash(self) -> str:
        h = self.settings.reva_admin_password_hash
        value = h.get_secret_value() if h is not None else ""
        if not value.startswith("$argon2id$"):
            raise ApiProblem(503, "auth_not_configured",
                             "REVA_ADMIN_PASSWORD_HASH is not set to an Argon2id hash")
        return value

    # --- audit ----------------------------------------------------------
    def audit(self, actor: str, action: str, target: dict[str, Any], details: dict[str, Any]) -> None:
        with self.engine_getter().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ops.audit(actor, action, target, details) "
                    "VALUES (:a, :ac, CAST(:t AS jsonb), CAST(:d AS jsonb))"
                ),
                {"a": actor[:200], "ac": action, "t": json.dumps(target), "d": json.dumps(details)},
            )

    # --- login/logout ---------------------------------------------------
    def login(self, request: Request, username: str, password: str) -> tuple[Principal, str]:
        ck = client_key(request)
        wait = self.limiter.check_and_record(ck)
        if wait is not None:
            self.audit("anonymous", "session.login_rate_limited", {"username": username[:64]}, {"client": ck})
            raise ApiProblem(429, "rate_limited", "too many login attempts; retry later",
                             retry_after=int(wait) + 1)
        stored = self._admin_hash()
        user_ok = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
        pw_ok = verify_password(stored if user_ok else _DUMMY_HASH, password)
        if not (user_ok and pw_ok):
            self.audit("anonymous", "session.login_failed", {"username": username[:64]},
                       {"client": ck, "reason": "invalid_credentials"})
            raise ApiProblem(401, "invalid_credentials", "invalid username or password")
        csrf = secrets.token_urlsafe(32)
        sid = secrets.token_urlsafe(16)
        expires = datetime.now(UTC) + SESSION_TTL
        cookie = self._serializer.dumps({"u": ADMIN_USERNAME, "sid": sid, "csrf": csrf})
        principal = Principal(ADMIN_USERNAME, f"human:{ADMIN_USERNAME}", "session", ALL_SCOPES, csrf, sid, expires)
        self.audit(principal.actor, "session.login", {"username": ADMIN_USERNAME},
                   {"client": ck, "session": hashlib.sha256(sid.encode()).hexdigest()[:16]})
        return principal, cookie

    def logout(self, principal: Principal) -> None:
        if principal.session_id:
            with self._lock:
                now = time.time()
                self._revoked = {k: v for k, v in self._revoked.items() if v > now}
                self._revoked[principal.session_id] = now + SESSION_TTL.total_seconds()
            self.audit(principal.actor, "session.logout", {"username": principal.username}, {})

    # --- authentication -------------------------------------------------
    def _from_session(self, cookie: str) -> Principal:
        try:
            data, signed_at = self._serializer.loads(
                cookie, max_age=int(SESSION_TTL.total_seconds()), return_timestamp=True
            )
        except BadSignature:
            raise Unauthenticated("session is invalid or expired") from None
        if not isinstance(data, dict) or data.get("u") != ADMIN_USERNAME:
            raise Unauthenticated("session is invalid")
        sid = str(data.get("sid", ""))
        with self._lock:
            if sid in self._revoked:
                raise Unauthenticated("session has ended")
        return Principal(ADMIN_USERNAME, f"human:{ADMIN_USERNAME}", "session", ALL_SCOPES,
                         str(data.get("csrf", "")), sid, signed_at + SESSION_TTL)

    def _from_token(self, token: str) -> Principal:
        if not token or len(token) > 512:
            raise Unauthenticated("invalid token")
        with self.engine_getter().connect() as conn:
            row = conn.execute(
                text("SELECT id, scopes, label, revoked_at FROM ops.api_token WHERE token_sha256 = :h"),
                {"h": token_sha256(token)},
            ).mappings().one_or_none()
        if row is None or row["revoked_at"] is not None:
            raise Unauthenticated("token is invalid or revoked")
        scopes = frozenset(s for s in (row["scopes"] or []) if s in ALL_SCOPES)
        label = str(row["label"])
        return Principal(label, f"token:{label}", "token", scopes, token_id=str(row["id"]), token_label=label)

    def authenticate(self, request: Request) -> Principal:
        cached = getattr(request.state, "principal", None)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        authz = request.headers.get("authorization")
        if authz is not None:
            scheme, _, tok = authz.partition(" ")
            if scheme.lower() != "bearer":
                raise Unauthenticated("unsupported authorization scheme")
            principal = self._from_token(tok.strip())
        else:
            cookie = request.cookies.get(SESSION_COOKIE)
            if not cookie:
                raise Unauthenticated()
            principal = self._from_session(cookie)
            if request.method not in SAFE_METHODS:
                self.check_csrf(request, principal)
        request.state.principal = principal
        return principal

    @staticmethod
    def check_csrf(request: Request, principal: Principal) -> None:
        header = request.headers.get(CSRF_HEADER, "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        expected = principal.csrf or ""
        ok = bool(header) and bool(expected)
        ok = hmac.compare_digest(header.encode(), cookie.encode()) and ok
        ok = hmac.compare_digest(header.encode(), expected.encode()) and ok
        if not ok:
            raise Forbidden("missing or invalid CSRF token", required_header=CSRF_HEADER)


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth  # type: ignore[no-any-return]


def authenticated(request: Request) -> Principal:
    return get_auth(request).authenticate(request)


def require_scope(scope: str) -> Any:
    if scope not in ALL_SCOPES:
        raise ValueError(scope)

    def dep(request: Request) -> Principal:
        principal = get_auth(request).authenticate(request)
        if scope not in principal.scopes:
            raise Forbidden(f"scope '{scope}' required", required_scope=scope)
        return principal

    dep.__name__ = f"require_{scope}"
    return dep
