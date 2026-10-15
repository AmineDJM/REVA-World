"""Authentication: Argon2id admin login, signed short-lived sessions, CSRF
double-submit, login rate limiting with audit records, and hashed, scoped,
revocable API tokens.

* Sessions are signed (itsdangerous) HttpOnly cookies, SameSite=Strict, Secure in
  production, valid for ``SESSION_TTL``. A companion non-HttpOnly CSRF cookie must
  be echoed in ``X-CSRF-Token`` on every unsafe cookie-authenticated request; it is
  compared in constant time with both the cookie and the value bound inside the
  signed session.
* A session is bound to the admin password hash in force when it was issued (an HMAC fingerprint inside
  the signed cookie): rotating ``REVA_ADMIN_PASSWORD_HASH`` ends every existing session. Logging in again
  with a still-valid session cookie revokes that older session.
* API tokens (``Authorization: Bearer``) are looked up by SHA-256 in
  ``ops.api_token``; only the hash is stored. Tokens are never stored client-side
  by the app (no localStorage).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import text

from reva.api.errors import ApiProblem, Forbidden, Unauthenticated
from reva.kernel.errors import ConfigurationError
from reva.settings import Settings

SESSION_COOKIE = "reva_session"
CSRF_COOKIE = "reva_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL = timedelta(hours=8)
ALL_SCOPES = frozenset({"read", "simulate", "ingest", "review"})
ADMIN_USERNAME = "admin"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HUMAN_TOKEN_LABEL = re.compile(r"human:[a-z0-9][a-z0-9._-]{0,63}")
LOGIN_ATTEMPTS_PER_MINUTE = 5  # default when not configured
LOGIN_RATE_LIMIT_ENV = "REVA_LOGIN_RATE_LIMIT_PER_MINUTE"
LOGIN_RATE_LIMIT_MAX = 1000

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

    principal_kind: str | None = None  # ops.api_token.principal_kind when the column exists

    @property
    def human_reviewer(self) -> str | None:
        """Reviewer identity for ops.review_proposal (must be human:<name>), or None.

        Only (a) the interactive admin session, or (b) an API token that is explicitly a human reviewer
        token: it carries the ``review`` scope, its label is ``human:<name>`` (lowercase name, not the
        reserved session identity ``human:admin``), and, once ``ops.api_token.principal_kind`` exists, its
        principal kind is ``human``. Service tokens never review, whatever their label.
        """
        if self.method == "session":
            return f"human:{self.username}"
        label = self.token_label or ""
        if "review" not in self.scopes or not HUMAN_TOKEN_LABEL.fullmatch(label):
            return None
        if label == f"human:{ADMIN_USERNAME}":
            return None  # reserved: indistinguishable from the interactive admin session
        if self.principal_kind is not None and self.principal_kind != "human":
            return None
        return label


def login_rate_limit_per_minute(settings: Settings, override: int | None = None) -> int:
    """Login attempts allowed per client per 60 s window (always enforced; never disabled).

    Resolution order: explicit ``override`` (``create_app(login_rate_limit_per_minute=...)``), a
    ``reva_login_rate_limit_per_minute`` Settings field once Settings declares it, then the environment
    variable ``REVA_LOGIN_RATE_LIMIT_PER_MINUTE``, then the default 5. Values outside 1..1000 or
    non-integers are a configuration error (fail closed at startup).
    """
    value: Any = override
    if value is None:
        value = getattr(settings, "reva_login_rate_limit_per_minute", None)
    source = "override/settings"
    if value is None:
        raw = os.environ.get(LOGIN_RATE_LIMIT_ENV)
        if raw is None or not raw.strip():
            return LOGIN_ATTEMPTS_PER_MINUTE
        value, source = raw.strip(), LOGIN_RATE_LIMIT_ENV
    try:
        if isinstance(value, bool):
            raise ValueError
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{LOGIN_RATE_LIMIT_ENV} must be an integer", source=source) from None
    if not 1 <= n <= LOGIN_RATE_LIMIT_MAX:
        raise ConfigurationError(f"{LOGIN_RATE_LIMIT_ENV} must be between 1 and {LOGIN_RATE_LIMIT_MAX}", source=source)
    return n


class LoginRateLimiter:
    """In-memory sliding window per client plus a global window (single API process in V1).

    Memory is bounded: at most ``max_keys`` client buckets are kept in LRU order and idle buckets (no
    attempt within the window) are evicted from the front, so every attempt costs amortised O(1). The
    global bucket (``global_limit`` attempts per window across all clients) bounds Argon2 work under
    distributed guessing; it is checked before any password verification.
    """

    GLOBAL_KEY = "__global__"

    def __init__(
        self,
        limit: int = LOGIN_ATTEMPTS_PER_MINUTE,
        window_seconds: float = 60.0,
        max_keys: int = 10_000,
        global_limit: int | None = None,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self.global_limit = global_limit if global_limit is not None else max(60, 12 * limit)
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.window:
            q.popleft()

    def _evict(self, now: float) -> None:
        # Front = least recently used. Drop idle buckets, then enforce the size bound.
        while self._hits:
            q = next(iter(self._hits.values()))
            if (q and now - q[-1] > self.window) or not q or len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
                continue
            break

    def __len__(self) -> int:
        return len(self._hits)

    def check_and_record(self, key: str) -> float | None:
        """Record an attempt; return seconds to wait if a limit is exceeded."""
        now = time.monotonic()
        with self._lock:
            self._prune(self._global, now)
            if len(self._global) >= self.global_limit:
                return max(1.0, self.window - (now - self._global[0]))
            q = self._hits.get(key)
            if q is None:
                q = self._hits[key] = deque()
            else:
                self._hits.move_to_end(key)
            self._prune(q, now)
            if len(q) >= self.limit:
                return max(1.0, self.window - (now - q[0]))
            q.append(now)
            self._global.append(now)
            self._evict(now)
            return None


@dataclass
class AuthService:
    settings: Settings
    engine_getter: Any
    login_limit_override: int | None = None
    limiter: LoginRateLimiter = field(init=False)
    _revoked: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Client key -> monotonic time of the last audited rate-limit rejection (bounded, LRU).
    _rl_audited: OrderedDict[str, float] = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        self.limiter = LoginRateLimiter(login_rate_limit_per_minute(self.settings, self.login_limit_override))
        secret = self.settings.reva_session_secret
        if secret is not None and secret.get_secret_value():
            key = secret.get_secret_value()
        elif self.settings.reva_env == "production":
            raise ApiProblem(500, "configuration_error", "REVA_SESSION_SECRET is required in production")
        else:
            key = secrets.token_urlsafe(48)  # ephemeral local secret: sessions end on restart
        self._serializer = URLSafeTimedSerializer(key, salt="reva-session-v1")
        self._fp_key = hashlib.sha256(b"reva-session-password-binding\x00" + key.encode()).digest()
        self._audit_key = hashlib.sha256(b"reva-audit-username-tag\x00" + key.encode()).digest()

    # --- configuration --------------------------------------------------
    @property
    def secure_cookies(self) -> bool:
        return self.settings.reva_env == "production" or self.settings.reva_public_base_url.startswith("https://")

    def _admin_hash(self) -> str:
        h = self.settings.reva_admin_password_hash
        value = h.get_secret_value() if h is not None else ""
        if not value.startswith("$argon2id$"):
            raise ApiProblem(503, "auth_not_configured", "REVA_ADMIN_PASSWORD_HASH is not set to an Argon2id hash")
        return value

    def _password_binding(self, admin_hash: str) -> str:
        """Keyed fingerprint of the admin hash carried in the session (reveals nothing about the hash)."""
        return hmac.new(self._fp_key, admin_hash.encode(), hashlib.sha256).hexdigest()[:32]

    def username_tag(self, username: str) -> str:
        """Keyed, truncated tag of a submitted username for failed-login audit rows.

        Audit rows are immutable, and a password typed into the username field must never persist there,
        so the typed value is not stored. The tag groups repeated attempts with the same input without
        being reversible offline (it is keyed by the session secret, unlike a plain hash of a guessable
        string).
        """
        return hmac.new(self._audit_key, username.encode(), hashlib.sha256).hexdigest()[:16]

    def _login_target(self, username: str) -> dict[str, Any]:
        matched = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
        return {"username_matched_admin": matched, "username_tag": self.username_tag(username)}

    def _audit_rate_limited(self, ck: str) -> bool:
        """Audit one rate-limit rejection per client per window, not every rejected request.

        Rejected requests cost nothing else; auditing each would let an unauthenticated client append
        unbounded rows to the append-only ops.audit table.
        """
        now = time.monotonic()
        with self._lock:
            last = self._rl_audited.get(ck)
            if last is not None and now - last < self.limiter.window:
                return False
            self._rl_audited[ck] = now
            self._rl_audited.move_to_end(ck)
            while len(self._rl_audited) > self.limiter.max_keys:
                self._rl_audited.popitem(last=False)
            return True

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
            if self._audit_rate_limited(ck):
                self.audit(
                    "anonymous",
                    "session.login_rate_limited",
                    self._login_target(username),
                    {"client": ck, "note": "first rejection in this window; later ones are not audited"},
                )
            raise ApiProblem(429, "rate_limited", "too many login attempts; retry later", retry_after=int(wait) + 1)
        stored = self._admin_hash()
        user_ok = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
        pw_ok = verify_password(stored if user_ok else _DUMMY_HASH, password)
        if not (user_ok and pw_ok):
            self.audit(
                "anonymous",
                "session.login_failed",
                self._login_target(username),
                {"client": ck, "reason": "invalid_credentials"},
            )
            raise ApiProblem(401, "invalid_credentials", "invalid username or password")
        # A fresh session id on every login (no fixation); a still-valid session presented with this login is
        # revoked so at most the new cookie remains usable from this client.
        previous = request.cookies.get(SESSION_COOKIE)
        if previous:
            try:
                self.logout(self._from_session(previous))
            except Unauthenticated:
                pass
        csrf = secrets.token_urlsafe(32)
        sid = secrets.token_urlsafe(16)
        expires = datetime.now(UTC) + SESSION_TTL
        cookie = self._serializer.dumps(
            {"u": ADMIN_USERNAME, "sid": sid, "csrf": csrf, "pb": self._password_binding(stored)}
        )
        principal = Principal(ADMIN_USERNAME, f"human:{ADMIN_USERNAME}", "session", ALL_SCOPES, csrf, sid, expires)
        self.audit(
            principal.actor,
            "session.login",
            {"username": ADMIN_USERNAME},
            {"client": ck, "session": hashlib.sha256(sid.encode()).hexdigest()[:16]},
        )
        return principal, cookie

    @staticmethod
    def _sid_key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def logout(self, principal: Principal) -> None:
        """End the session for good: the revocation is persisted (ops.session_revocation), so a copied
        cookie stays invalid across API restarts until it would have expired anyway."""
        if principal.session_id:
            with self._lock:
                now = time.time()
                self._revoked = {k: v for k, v in self._revoked.items() if v > now}
                self._revoked[principal.session_id] = now + SESSION_TTL.total_seconds()
            expires = principal.expires_at or (datetime.now(UTC) + SESSION_TTL)
            with self.engine_getter().begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO ops.session_revocation(session_id, expires_at) VALUES (:s, :e) "
                        "ON CONFLICT (session_id) DO NOTHING"
                    ),
                    {"s": self._sid_key(principal.session_id), "e": expires},
                )
            self.audit(
                principal.actor,
                "session.logout",
                {"username": principal.username},
                {"session": self._sid_key(principal.session_id)[:16]},
            )

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
        if len(sid) < 16:
            raise Unauthenticated("session is invalid")
        try:
            binding = self._password_binding(self._admin_hash())
        except ApiProblem:
            raise Unauthenticated("session is invalid") from None
        if not hmac.compare_digest(str(data.get("pb", "")).encode(), binding.encode()):
            raise Unauthenticated("session is invalid")  # issued under a different admin password hash
        with self._lock:
            if sid in self._revoked:
                raise Unauthenticated("session has ended")
        with self.engine_getter().connect() as conn:
            revoked = conn.execute(
                text("SELECT 1 FROM ops.session_revocation WHERE session_id = :s"), {"s": self._sid_key(sid)}
            ).scalar_one_or_none()
        if revoked is not None:
            raise Unauthenticated("session has ended")
        return Principal(
            ADMIN_USERNAME,
            f"human:{ADMIN_USERNAME}",
            "session",
            ALL_SCOPES,
            str(data.get("csrf", "")),
            sid,
            signed_at + SESSION_TTL,
        )

    def _from_token(self, token: str) -> Principal:
        if not token or len(token) > 512:
            raise Unauthenticated("invalid token")
        with self.engine_getter().connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM ops.api_token WHERE token_sha256 = :h"),
                    {"h": token_sha256(token)},
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["revoked_at"] is not None:
            raise Unauthenticated("token is invalid or revoked")
        scopes = frozenset(s for s in (row["scopes"] or []) if s in ALL_SCOPES)
        label = str(row["label"])
        return Principal(
            label,
            f"token:{label}",
            "token",
            scopes,
            token_id=str(row["id"]),
            token_label=label,
            principal_kind=row.get("principal_kind"),
        )

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
