"""FastAPI application factory.

``reva.api.app:app`` is the ASGI entrypoint; ``create_app(settings)`` builds an
isolated instance (tests, contract export). The API connects only as the
``reva_api`` role: it reads approved projections and writes exclusively through
narrow SECURITY DEFINER functions (jobs, cancellation, reviews, scenarios) plus
audit inserts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import Engine
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reva.adapters.postgres.engine import make_engine
from reva.api.auth import CSRF_HEADER, AuthService, require_scope
from reva.api.errors import ApiProblem, NotFound, install_error_handlers, problem_response
from reva.api.routes import beliefs, catalog, evidence, provenance, sim, system, world
from reva.kernel.errors import ConfigurationError
from reva.operations.telemetry import configure_logging, log_context
from reva.settings import Settings, get_settings

log = logging.getLogger("reva.api.access")
API_VERSION = "1.0.0"
STATIC_DIR = Path(__file__).resolve().parent / "static"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
)
# Largest accepted request body. Every request body is a small JSON document (login, ScenarioSpec, QuerySpec,
# job parameters); the cap is enforced before the body is buffered or parsed, including for the
# unauthenticated login route.
MAX_REQUEST_BODY_BYTES = 1_048_576


def apply_security_headers(headers: MutableHeaders, path: str, production: bool) -> None:
    """Static security headers on every response the app produces (including error pages)."""
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    headers["X-Frame-Options"] = "DENY"
    headers["Cross-Origin-Opener-Policy"] = "same-origin"
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    headers["Content-Security-Policy"] = DOCS_CSP if path in ("/docs", "/docs/oauth2-redirect") else CSP
    if production:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if path.startswith("/v1") and "cache-control" not in headers:
        headers["Cache-Control"] = "no-store"


class BodySizeLimit:
    """Reject request bodies above ``max_bytes`` with a 413 problem before they are buffered.

    A declared ``Content-Length`` above the cap is refused without reading the body; a chunked body is
    counted as it streams and the read fails with 413 as soon as it crosses the cap.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = None
        for k, v in scope.get("headers") or ():
            if k == b"content-length":
                declared = v
                break
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True  # unparseable length: refuse rather than guess
            if too_large:
                await self._reject(scope, receive, send)
                return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise HTTPException(413, f"request body larger than {self.max_bytes} bytes")
            return message

        await self.app(scope, limited, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        problem = ApiProblem(413, "payload_too_large", f"request body larger than {self.max_bytes} bytes",
                             max_bytes=self.max_bytes).problem()
        await problem_response(Request(scope), problem, {"Connection": "close"})(scope, receive, send)


def _lazy_engine(settings: Settings) -> Callable[[], Engine]:
    lock = threading.Lock()
    holder: dict[str, Engine] = {}

    def get() -> Engine:
        with lock:
            if "e" not in holder:
                holder["e"] = make_engine("api", settings)
            return holder["e"]

    get.dispose = lambda: holder.pop("e").dispose() if "e" in holder else None  # type: ignore[attr-defined]
    return get


def _check_schema_contract(engine: Callable[[], Engine], settings: Settings) -> None:
    """Startup guard: refuse to serve against a schema that differs from the committed contract.

    ``SchemaDriftError`` (a ``ConfigurationError``) is enforced in production and only logged in warn mode
    (``REVA_ENV=test`` default), where an unconfigured database (no runtime role password) is not fatal.
    """
    from reva.adapters.postgres.schema_contract import assert_schema_contract, contract_mode

    mode = contract_mode(reva_env=settings.reva_env)
    if mode == "off":
        return
    try:
        eng = engine()
    except ConfigurationError as e:
        if mode == "warn":
            log.warning("schema contract not verified: %s", type(e).__name__)
            return
        raise
    assert_schema_contract(eng, mode=mode, reva_env=settings.reva_env)


def create_app(
    settings: Settings | None = None,
    *,
    static_dir: Path | None = None,
    login_rate_limit_per_minute: int | None = None,
    max_body_bytes: int = MAX_REQUEST_BODY_BYTES,
) -> FastAPI:
    """Build the app. ``static_dir`` overrides the built UI location (default: reva/api/static).

    ``login_rate_limit_per_minute`` overrides the login rate limit (otherwise Settings/env
    ``REVA_LOGIN_RATE_LIMIT_PER_MINUTE``, default 5); the limit is always enforced.
    ``max_body_bytes`` caps request bodies (413 above it).

    In production the OpenAPI document is served only to authenticated principals (``read`` scope) and the
    interactive ``/docs`` page does not exist.
    """
    settings = settings or get_settings()
    production = settings.reva_env == "production"
    if "*" in settings.cors_origins:
        # Starlette reflects any Origin for allow_origins=["*"] with credentials: never allow it.
        raise ConfigurationError("REVA_CORS_ORIGINS must list explicit origins, not '*'",
                                 source="REVA_CORS_ORIGINS")
    ui_dir = static_dir or STATIC_DIR
    engine = _lazy_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Refuse to serve against a schema that differs from the committed contract (SchemaDriftError, a
        # ConfigurationError): enforced in production, a logged warning under REVA_ENV=test.
        await asyncio.to_thread(_check_schema_contract, engine, settings)
        yield
        engine.dispose()  # type: ignore[attr-defined]

    app = FastAPI(
        lifespan=lifespan,
        title="REVA World API",
        version=API_VERSION,
        description=(
            "Versioned federation of evidence, estimates and models. Every /v1 endpoint requires authentication "
            "(session cookie + X-CSRF-Token for unsafe methods, or Bearer API token). Errors are RFC 9457 "
            "problem details. Missing values are typed and never zero."
        ),
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
        # Response schemas in serialization mode: fields with defaults are required in responses.
        separate_input_output_schemas=True,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = AuthService(settings, app.state.engine, login_rate_limit_per_minute)
    app.state.metrics = system.RequestMetrics()
    app.state.apply_security_headers = lambda headers, path: apply_security_headers(headers, path, production)

    install_error_handlers(app)

    for r in (system.health_router, system.session_router, system.jobs_router, system.metrics_router,
              world.router, evidence.router, beliefs.router, provenance.router, catalog.router, sim.router,
              sim.query_router):
        app.include_router(r)

    # Added before the header middleware so that it runs inside it: its 413 responses get headers/metrics.
    app.add_middleware(BodySizeLimit, max_bytes=max_body_bytes)

    @app.middleware("http")
    async def _headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id", "")
        if not (0 < len(rid) <= 64 and rid.replace("-", "").isalnum()):
            rid = secrets.token_hex(8)
        request.state.request_id = rid
        t0 = time.perf_counter()
        with log_context(request_id=rid):  # correlates every log line emitted while serving the request
            response = await call_next(request)
        ms = (time.perf_counter() - t0) * 1000
        route = getattr(request.scope.get("route"), "path", "unmatched")
        app.state.metrics.observe(route, response.status_code, ms)
        h = response.headers
        h["X-Request-ID"] = rid
        apply_security_headers(h, request.url.path, production)
        log.info(json.dumps({"request_id": rid, "method": request.method, "route": route,
                             "status": response.status_code, "ms": round(ms, 2)}))
        return response

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "Authorization", CSRF_HEADER, "Idempotency-Key", "X-Request-ID"],
            expose_headers=["Location", "X-Request-ID", "Retry-After"],
            max_age=600,
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts or ["localhost"])

    if production:
        @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_scope("read"))])
        def openapi_authenticated() -> JSONResponse:
            return JSONResponse(app.openapi())

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        index = ui_dir / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"service": "reva-world-api", "api": "/v1", "health": "/health/ready",
                             "openapi": "/openapi.json" + (" (authenticated)" if production else ""),
                             "ui": "not built (src/reva/api/static/index.html absent)"})

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith(("v1/", "health/")) or path in ("v1", "health", "metrics"):
            raise NotFound("no such endpoint")
        index = ui_dir / "index.html"
        if not index.is_file():
            raise NotFound("no such endpoint")
        root_dir = ui_dir.resolve()
        try:
            candidate = (root_dir / path).resolve()
            is_file = candidate.is_file()
        except (ValueError, OSError):  # NUL byte, over-long name, ...: never a static file
            raise NotFound("no such endpoint") from None
        if candidate.is_relative_to(root_dir) and is_file and not any(
            part.startswith(".") for part in candidate.relative_to(root_dir).parts
        ):
            cache = "public, max-age=31536000, immutable" if "/assets/" in f"/{path}" else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def _module_app() -> Any:
    """The served app (``uvicorn reva.api.app:app``): install the redacting JSON log handler first.

    Without it, uvicorn's "Exception in ASGI application" tracebacks and ``log.exception`` output fall
    through to Python's last-resort handler unredacted (bound parameters, Authorization values).
    ``configure_logging`` also adopts the uvicorn loggers. ``create_app()`` alone (tests, embedding)
    leaves logging configuration to the caller.
    """
    settings = get_settings()
    configure_logging(settings.reva_log_level)
    return create_app(settings)


app = _module_app()
