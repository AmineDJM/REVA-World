"""FastAPI application factory.

``reva.api.app:app`` is the ASGI entrypoint; ``create_app(settings)`` builds an
isolated instance (tests, contract export). The API connects only as the
``reva_api`` role: it reads approved projections and writes exclusively through
narrow SECURITY DEFINER functions (jobs, cancellation, reviews, scenarios) plus
audit inserts.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import Engine
from starlette.middleware.trustedhost import TrustedHostMiddleware

from reva.adapters.postgres.engine import make_engine
from reva.api.auth import CSRF_HEADER, AuthService
from reva.api.errors import NotFound, install_error_handlers
from reva.api.routes import beliefs, catalog, evidence, provenance, sim, system, world
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


def create_app(
    settings: Settings | None = None,
    *,
    static_dir: Path | None = None,
    login_rate_limit_per_minute: int | None = None,
) -> FastAPI:
    """Build the app. ``static_dir`` overrides the built UI location (default: reva/api/static).

    ``login_rate_limit_per_minute`` overrides the login rate limit (otherwise Settings/env
    ``REVA_LOGIN_RATE_LIMIT_PER_MINUTE``, default 5); the limit is always enforced.
    """
    settings = settings or get_settings()
    production = settings.reva_env == "production"
    ui_dir = static_dir or STATIC_DIR
    engine = _lazy_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
        openapi_url="/openapi.json",
        # Response schemas in serialization mode: fields with defaults are required in responses.
        separate_input_output_schemas=True,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = AuthService(settings, app.state.engine, login_rate_limit_per_minute)
    app.state.metrics = system.RequestMetrics()

    install_error_handlers(app)

    for r in (system.health_router, system.session_router, system.jobs_router, system.metrics_router,
              world.router, evidence.router, beliefs.router, provenance.router, catalog.router, sim.router,
              sim.query_router):
        app.include_router(r)

    @app.middleware("http")
    async def _headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id", "")
        if not (0 < len(rid) <= 64 and rid.replace("-", "").isalnum()):
            rid = secrets.token_hex(8)
        request.state.request_id = rid
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - t0) * 1000
        route = getattr(request.scope.get("route"), "path", "unmatched")
        app.state.metrics.observe(route, response.status_code, ms)
        h = response.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Content-Security-Policy"] = DOCS_CSP if request.url.path in ("/docs", "/docs/oauth2-redirect") else CSP
        if production:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/v1") and "cache-control" not in h:
            h["Cache-Control"] = "no-store"
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

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        index = ui_dir / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"service": "reva-world-api", "api": "/v1", "health": "/health/ready",
                             "openapi": "/openapi.json", "ui": "not built (src/reva/api/static/index.html absent)"})

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith(("v1/", "health/")) or path in ("v1", "health", "metrics"):
            raise NotFound("no such endpoint")
        index = ui_dir / "index.html"
        if not index.is_file():
            raise NotFound("no such endpoint")
        root_dir = ui_dir.resolve()
        candidate = (root_dir / path).resolve()
        if candidate.is_relative_to(root_dir) and candidate.is_file() and not any(
            part.startswith(".") for part in candidate.relative_to(root_dir).parts
        ):
            cache = "public, max-age=31536000, immutable" if "/assets/" in f"/{path}" else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def _module_app() -> Any:
    return create_app()


app = _module_app()
