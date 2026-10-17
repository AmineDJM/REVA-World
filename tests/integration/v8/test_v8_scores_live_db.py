"""V8SCORES live registry state against the dev database `reva` (read-only; skipped when it is not reachable).

Checks that the live counts agree with direct SQL over model.evaluation and that the warm path is fast."""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def ctx() -> dict[str, Any]:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    s = get_settings()
    try:
        eng = make_engine("api", s)
        with eng.connect() as c:
            n = c.execute(
                text("SELECT count(*) FROM model.evaluation WHERE task_scope->>'kind' = 'forecast_registry'")
            ).scalar_one()
    except Exception:
        pytest.skip("dev database not reachable")
    if not n:
        pytest.skip("no forecast registry rows in the dev database")
    return {"engine": eng, "store": build_store(s)}


def test_live_state_matches_sql_and_is_cached(ctx: dict[str, Any]) -> None:
    from reva.v8.scores.live import LiveService, sealed_models

    svc = LiveService()
    t0 = time.perf_counter()
    st = svc.get(ctx["engine"], ctx["store"], "live")
    while st is not None and st.get("partial") and time.perf_counter() - t0 < 300:  # cold: partial first
        time.sleep(0.5)
        st = svc.get(ctx["engine"], ctx["store"], "live")
    cold = time.perf_counter() - t0
    assert st is not None and not st["partial"]
    with ctx["engine"].connect() as c:
        n_batches = c.execute(
            text(
                """SELECT count(DISTINCT evaluation_manifest_sha256) FROM model.evaluation
                   WHERE metrics->>'kind' IN ('registry_issue', 'health_issue') AND task_scope->>'origin' = 'live'"""
            )
        ).scalar_one()
        sealed = sealed_models(c, "live")
    assert st["totals"]["sealed_batches"] == n_batches
    t = st["totals"]
    assert t["questions"] == t["pending"] + t["resolved"] + t["void"] + t["not_prospective"]
    assert st["upcoming"] == sorted(st["upcoming"], key=lambda e: (e["date"], e["registry"], e["family"]))
    assert all(e["n_questions"] > 0 for e in st["upcoming"])
    assert sum(sealed.values()) == t["forecasts"]
    t1 = time.perf_counter()
    for _ in range(20):
        assert svc.get(ctx["engine"], ctx["store"], "live") is st
    warm = (time.perf_counter() - t1) / 20
    print(f"live state: cold {cold * 1000:.0f} ms, warm {warm * 1000:.2f} ms")
    assert warm < 0.3


def test_cold_live_state_answers_a_partial_state_without_waiting(ctx: dict[str, Any], monkeypatch: Any) -> None:
    from reva.v8.scores import live as L

    monkeypatch.setattr(L, "COLD_WAIT_S", 0.0)
    monkeypatch.setattr(L, "_SUMMARIES", {})  # force a slow full computation
    svc = L.LiveService()
    t0 = time.perf_counter()
    st = svc.get(ctx["engine"], ctx["store"], "live")
    assert st is not None and st["partial"] is True and st["upcoming"] == []
    assert set(st["totals"]) == {"sealed_batches", "questions", "forecasts"}  # no invented resolution counts
    assert time.perf_counter() - t0 < 2.0
