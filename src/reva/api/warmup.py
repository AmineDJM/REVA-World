"""V8 start-up warm-up (V8QA): prefetch the cold caches of the V8 console in ONE background thread.

Every ``reva.api.routes.v8_<name>`` module that exposes ``warm(app)`` is a warmer (no shared-file edit per
route, like the router discovery in ``app.py``); ``FORECAST_REGISTRY`` (the verified manifest index behind
``/v1/forecasts/*``, the Fiche of a question, boards and decisions) runs first. Warmers are called one after the
other, each inside ``try/except`` with a log line; the thread is a daemon started from the lifespan, so readiness
(``/health/ready``) never waits for it. A warmer that fails only means the first request pays the cost.

``REVA_V8_WARMUP`` (``1``/``0``) switches it; default on, except ``REVA_ENV=test`` (unit tests and e2e stacks).
The outcome per warmer is kept on ``app.state.v8_warmup`` (name -> {ok, seconds, detail}).
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import threading
import time
from collections.abc import Callable
from typing import Any

from starlette.requests import Request

log = logging.getLogger("reva.api.warmup")
ENV = "REVA_V8_WARMUP"
Warmer = Callable[[Any], Any]


def enabled(settings: Any) -> bool:
    v = os.environ.get(ENV)
    if v is not None:
        return v.strip().lower() in ("1", "true", "yes")
    return getattr(settings, "reva_env", "") != "test"


def request_for(app: Any) -> Request:
    """A minimal request bound to ``app`` so warmers reuse the routes' own helpers (store, engine, snapshot)."""
    return Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": [], "query_string": b""})


def forecast_registry(app: Any) -> str:
    """Verify and index the sealed forecast manifests once (PERFQ's shared manifest index, ~5 s cold)."""
    from sqlalchemy import text

    from reva.api.routes.v8_scores import _store
    from reva.evaluation.registry import read as R

    with app.state.engine().connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        R.questions(conn, _store(request_for(app)), None, limit=1)
    return "manifest index warm"


def discover() -> list[tuple[str, Warmer]]:
    """``forecast_registry`` then every v8 route module's ``warm(app)``, in module-name order."""
    from reva.api import routes as pkg

    out: list[tuple[str, Warmer]] = [("forecast_registry", forecast_registry)]
    for name in sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if m.name.startswith("v8_")):
        fn = getattr(importlib.import_module(f"reva.api.routes.{name}"), "warm", None)
        if callable(fn):
            out.append((name, fn))
    return out


def run(app: Any, warmers: list[tuple[str, Warmer]]) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    app.state.v8_warmup = report
    for name, fn in warmers:
        t0 = time.monotonic()
        try:
            detail = fn(app)
            report[name] = {"ok": True, "seconds": round(time.monotonic() - t0, 2), "detail": detail}
            log.info("v8 warm-up %s done in %.1f s", name, time.monotonic() - t0)
        except Exception as e:  # an optimisation only: the first request pays the cost instead
            report[name] = {"ok": False, "seconds": round(time.monotonic() - t0, 2), "detail": type(e).__name__}
            log.warning("v8 warm-up %s failed", name, exc_info=True)
    return report


def start(app: Any, warmers: list[tuple[str, Warmer]] | None = None) -> threading.Thread | None:
    """Start the warm-up thread (never blocks the caller); None when disabled."""
    if not enabled(getattr(app.state, "settings", None)):
        return None
    try:
        ws = warmers if warmers is not None else discover()
    except Exception:
        log.warning("v8 warm-up: discovery failed", exc_info=True)
        return None
    t = threading.Thread(target=run, args=(app, ws), name="reva-v8-warmup", daemon=True)
    t.start()
    return t
