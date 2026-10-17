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


# ----------------------------------------------------------------------------- QLever statement packs (synthetic rows)
def _b(item: str, pid: str, guid: str, v: str, rank: str = "Normal", **labels: str) -> dict[str, Any]:
    e = "http://www.wikidata.org/entity/"
    row: dict[str, Any] = {
        "item": {"value": e + item},
        "prop": {"value": pid},
        "st": {"value": f"http://www.wikidata.org/entity/statement/{item.lower()}-{guid}"},
        "v": {"value": v if v.startswith(("http", "_:")) else e + v},
        "rank": {"value": f"http://wikiba.se/ontology#{rank}Rank"},
    }
    row.update({k: {"value": x} for k, x in labels.items()})
    return row


def test_qlever_pack_rows_are_parsed_deduplicated_and_never_doubled() -> None:
    from datetime import UTC, datetime

    from reva.adapters.connectors import atlas_wdops_qlever as wq

    packs = wq.load_packs()
    assert {"eco_food", "chem_petroleum", "health_treat"} <= set(packs)
    pack = packs["eco_food"]
    q = wq.pack_query(pack)
    assert "FILTER(?rank" not in q and q.rstrip().endswith(f"LIMIT {pack.max_rows + 1}")
    bindings = [
        _b("Q1", "P1034", "a", "Q10", en="lion", ven="zebra"),
        _b("Q1", "P1034", "a", "Q10", en="lion", fr="lion"),  # label OPTIONALs repeat the statement
        _b("Q2", "P1034", "b", "Q11", rank="Deprecated"),
        _b("Q3", "P1034", "c", "http://www.wikidata.org/.well-known/genid/x"),  # some value
        _b("Q4", "P1034", "d", "Q10", rank="Preferred"),
    ]
    rows, labels, skipped = wq.parse_rows(bindings, pack)
    assert [(r.item, r.statement_id, r.rank) for r in rows] == [("Q1", "Q1$a", "normal"), ("Q4", "Q4$d", "preferred")]
    assert skipped == {"deprecated": 1, "object_not_item": 1}
    assert labels["Q1"] == {"en": "lion", "fr": "lion"} and labels["Q10"] == {"en": "zebra"}
    t = datetime(2026, 10, 17, tzinfo=UTC)
    ents, sts, rep = wq.build(
        rows, labels, pack, raw_sha256="r" * 64, query_sha256="q" * 64, retrieved_at=t,
        qlever_index="idx", held_by_snapshot={"Q4$d"}, known_entities={"Q10"},
    )  # fmt: skip
    assert [s.statement_id for s in sts] == ["wdops:eco_food:Q1$a"] and rep["held_by_snapshot"] == 1
    st = sts[0]
    assert st.property_pid == "P1034" and st.object_qid == "Q10" and st.source_revision == "qlever@2026-10-17T00:00:00Z"
    assert st.qualifiers["_revision"]["wikidata_revision"] == "unknown"
    assert st.references[0]["wikidata_references"] == "unknown (not read)"
    assert [e.qid for e in ents] == ["Q1"]  # Q10 already known, Q4 not loaded (snapshot holds its statement)
