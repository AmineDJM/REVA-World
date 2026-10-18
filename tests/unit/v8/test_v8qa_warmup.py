"""V8QA: start-up warm-up runs registered warmers in one background thread, never blocking, never raising."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from reva.api import warmup as W


def _app(env: str = "local") -> Any:
    return SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(reva_env=env)))


def test_runs_every_warmer_in_order_and_survives_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(W.ENV, raising=False)
    gate = threading.Event()
    calls: list[str] = []

    def slow(app: Any) -> str:
        gate.wait(5)
        calls.append("slow")
        return "ok"

    def boom(app: Any) -> None:
        calls.append("boom")
        raise RuntimeError("cold cache unavailable")

    def last(app: Any) -> str:
        calls.append("last")
        return "done"

    app = _app()
    t0 = time.monotonic()
    th = W.start(app, [("slow", slow), ("boom", boom), ("last", last)])
    assert th is not None and time.monotonic() - t0 < 0.5  # the lifespan is not held by the warmers
    gate.set()
    th.join(5)
    assert calls == ["slow", "boom", "last"]
    rep = app.state.v8_warmup
    assert rep["slow"]["ok"] and rep["last"]["ok"]
    assert rep["boom"] == {"ok": False, "seconds": rep["boom"]["seconds"], "detail": "RuntimeError"}


def test_disabled_under_test_env_and_by_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(W.ENV, raising=False)
    assert W.start(_app("test"), [("x", lambda a: None)]) is None
    monkeypatch.setenv(W.ENV, "0")
    assert W.start(_app("local"), [("x", lambda a: None)]) is None
    monkeypatch.setenv(W.ENV, "1")
    th = W.start(_app("test"), [("x", lambda a: None)])
    assert th is not None
    th.join(2)


def test_discovers_the_v8_route_warmers() -> None:
    names = [n for n, _ in W.discover()]
    assert names[0] == "forecast_registry"
    for mod in ("v8_decision", "v8_entity", "v8_paths", "v8_scores", "v8_series"):
        assert mod in names


def test_request_for_binds_the_app() -> None:
    app = _app()
    assert W.request_for(app).app is app
