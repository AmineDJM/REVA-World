"""WDOPS (V6): one Wikidata harvest manager — one shared budget ledger, explicit causal share (offline)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reva.causal_harvest import live
from reva.kg import harvest as hv


class _Engine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _Http:
    async def aclose(self) -> None:
        return None


def _patch_budget(monkeypatch: pytest.MonkeyPatch, booked: list[tuple[str, str]]) -> dict[str, _Engine]:
    import reva.adapters.postgres.engine as eng_mod
    import reva.operations.budgets as budgets

    engines: dict[str, _Engine] = {}

    class _Settings:
        def __init__(self, name: str) -> None:
            self.reva_db_name = name
            self.reva_s3_prefix = "test"

    def make_engine(role: str, settings: Any) -> _Engine:
        e = _Engine(settings.reva_db_name)
        engines[settings.reva_db_name] = e
        return e

    def reserve_ingest(engine: _Engine, *, job_id: str, source_key: str, **_: Any) -> tuple[str, str]:
        booked.append((engine.name, source_key))
        return (engine.name, source_key)

    monkeypatch.setattr(hv, "ensure_database", lambda name: _Settings(name))
    monkeypatch.setattr(hv, "harvest_settings", lambda name: _Settings(name))
    monkeypatch.setattr(hv, "live_http", lambda **_: _Http())
    monkeypatch.setattr(eng_mod, "make_engine", make_engine)
    monkeypatch.setattr(budgets, "load_budget", lambda: object())
    monkeypatch.setattr(budgets, "reserve_ingest", reserve_ingest)
    monkeypatch.setattr(budgets, "metered", lambda inner, engine, res, budget: (engine.name, res))
    monkeypatch.setattr(budgets, "settle_metered", lambda engine, h: {"ledger": engine.name})
    return engines


@pytest.mark.parametrize(("ledger_db", "expected"), [(None, "reva_kg_load"), ("reva_kg_ledger", "reva_kg_ledger")])
def test_budgeted_client_books_requests_in_the_shared_ledger(
    monkeypatch: pytest.MonkeyPatch, ledger_db: str | None, expected: str
) -> None:
    booked: list[tuple[str, str]] = []
    engines = _patch_budget(monkeypatch, booked)

    async def go() -> dict[str, Any]:
        async with hv.budgeted_client(purpose="t", db_name="reva_kg_load", ledger_db=ledger_db) as sess:
            assert sess.engine is engines["reva_kg_load"]  # loads still go to the load database
        return sess.usage

    usage = asyncio.run(go())
    assert booked == [(expected, hv.SOURCE_WDQS), (expected, hv.SOURCE_WBAPI)]
    assert usage["budget"] == [{"ledger": expected}, {"ledger": expected}]
    assert all(e.disposed for e in engines.values())


def test_causal_share_override_uses_global_remaining() -> None:
    # 30 % default share of the global remaining budget; an explicit share replaces it (never above remaining).
    cap, basis = live.wbapi_cap({"wikidata.wbapi": 1600}, {"entitydata": 1300})
    assert cap == int(2400 * 0.30) and basis["remaining_estimate"] == 2400
    cap2, _ = live.wbapi_cap({"wikidata.wbapi": 1600}, {"entitydata": 1300}, share=0.6)
    assert cap2 == 1440
    assert live.wbapi_cap({"wikidata.wbapi": 4000}, {}, share=1.0)[0] == 0
