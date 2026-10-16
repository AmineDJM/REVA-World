"""CAUSWD: causal harvest helpers (offline: no network, no database)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors.wikidata import SOURCE_WBAPI, extract_entity, load_allowlist
from reva.causal_harvest import core, live
from reva.kg.harvest import discover_specs, validate_spec
from reva.operations.budgets import BudgetExhausted

REPO = Path(__file__).resolve().parents[2]


def _snak(pid: str, qid: str) -> dict[str, Any]:
    return {
        "snaktype": "value",
        "property": pid,
        "datatype": "wikibase-item",
        "datavalue": {"type": "wikibase-entityid", "value": {"entity-type": "item", "id": qid}},
    }


def _time(pid: str, t: str, precision: int) -> dict[str, Any]:
    return {
        "snaktype": "value",
        "property": pid,
        "datatype": "time",
        "datavalue": {
            "type": "time",
            "value": {"time": t, "precision": precision, "calendarmodel": "http://www.wikidata.org/entity/Q1985727"},
        },
    }


def raw_entity() -> dict[str, Any]:
    """Synthetic wbgetentities entity: an event with two causal claims and a start time."""
    return {
        "id": "Q8698",
        "lastrevid": 123,
        "labels": {"en": {"language": "en", "value": "Great Depression"}},
        "sitelinks": {"enwiki": {"title": "Great Depression"}, "frwiki": {"title": "Grande Dépression"}},
        "claims": {
            "P828": [
                {
                    "id": "Q8698$A",
                    "rank": "normal",
                    "mainsnak": _snak("P828", "Q205020"),
                    "qualifiers": {"P1480": [_snak("P1480", "Q18912752")]},
                    "references": [
                        {
                            "snaks": {
                                "P248": [_snak("P248", "Q999")],
                                "P1683": [
                                    {
                                        "snaktype": "value",
                                        "property": "P1683",
                                        "datavalue": {
                                            "type": "monolingualtext",
                                            "value": {"text": "the crash triggered", "language": "en"},
                                        },
                                    }
                                ],
                                "P356": [
                                    {
                                        "snaktype": "value",
                                        "property": "P356",
                                        "datatype": "external-id",
                                        "datavalue": {"type": "string", "value": "10.1/x"},
                                    }
                                ],
                            }
                        }
                    ],
                },
                {"id": "Q8698$B", "rank": "deprecated", "mainsnak": _snak("P828", "Q1")},
            ],
            "P1542": [{"id": "Q8698$C", "rank": "normal", "mainsnak": _snak("P1542", "Q170314")}],
            "P580": [{"id": "Q8698$D", "rank": "normal", "mainsnak": _time("P580", "+1929-00-00T00:00:00Z", 9)}],
            "P1082": [{"id": "Q8698$E", "rank": "normal", "mainsnak": _snak("P1082", "Q5")}],
        },
    }


def test_causal_specs_validate_and_are_disabled_for_the_generic_runner() -> None:
    allow = load_allowlist()
    specs = discover_specs([REPO / "registry/kg/queries/causal"])
    assert {s.columns["property"] for s in specs} == set(core.CAUSAL_PIDS)
    for s in specs:
        assert validate_spec(s, allow) == [], s.id
        assert s.get("enabled") is False
        assert "statement" in s.columns and "?statement" in s.data["sparql"]


def test_direction_of_the_six_properties() -> None:
    assert core.causal_edge("Q8698", "P828", "Q205020").key == ("Q205020", "Q8698")  # has cause
    assert core.causal_edge("Q8698", "P1542", "Q170314").key == ("Q8698", "Q170314")  # has effect
    assert core.causal_edge("Qa", "P1478", "Qb") is not None
    assert core.causal_edge("Q1", "P1478", "Q2").key == ("Q2", "Q1")
    assert core.causal_edge("Q1", "P1479", "Q2").relation == "contributing_factor"
    assert core.causal_edge("Q1", "P1536", "Q2").key == ("Q1", "Q2")
    assert core.causal_edge("Q1", "P1537", "Q2").key == ("Q1", "Q2")
    assert core.causal_edge("Q1", "P31", "Q2") is None
    assert core.causal_edge("Q1", "P828", None) is None  # somevalue / novalue


def test_causal_statements_keep_full_references_and_qualifiers_others_unchanged() -> None:
    allow = load_allowlist()
    raw = raw_entity()
    e = extract_entity(raw, allow, raw_sha256="a" * 64, retrieved_at=datetime.now(UTC), properties=core.HARVEST_PIDS)
    assert {s.property for s in e.statements} == {"P828", "P1542", "P580"}  # P1082 not harvested, deprecated out
    before = {s.statement_id: s for s in e.statements}
    assert before["Q8698$A"].references == [{"P248": ["Q999"]}]  # compact global allow-list
    e2 = core.enrich_causal_statements(e, raw, allow)
    after = {s.statement_id: s for s in e2.statements}
    ref = after["Q8698$A"].references[0]
    assert ref["P356"] == ["10.1/x"] and ref["P1683"][0]["text"] == "the crash triggered"
    assert after["Q8698$A"].qualifiers == {"P1480": ["Q18912752"]}
    assert after["Q8698$D"] == before["Q8698$D"]
    assert core.causal_objects(e2) == ["Q205020", "Q170314"]
    assert core.is_dated(e2)
    assert core.reference_class(after["Q8698$A"].references) == "scholarly"
    assert core.reference_class(after["Q8698$C"].references) == "none"
    assert core.reference_class([{"P143": ["Q328"]}]) == "wikimedia_import_only"
    assert core.reference_class([{"P854": ["https://x"]}]) == "referenced"
    assert core.reference_class([], wdqs_row=True) == "unknown"


def test_time_bounds_and_temporal_check_respect_precision() -> None:
    assert core.time_bounds("+1929-10-24T00:00:00Z", 11) == ((1929, 10, 24), (1929, 10, 24))
    assert core.time_bounds("+1929-00-00T00:00:00Z", 9) == ((1929, 1, 1), (1929, 12, 31))
    assert core.time_bounds("+1920-00-00T00:00:00Z", 8) == ((1920, 1, 1), (1929, 12, 31))
    assert core.time_bounds("+1901-00-00T00:00:00Z", 7) == ((1901, 1, 1), (2000, 12, 31))
    assert core.time_bounds("+2000-00-00T00:00:00Z", 7) == ((1901, 1, 1), (2000, 12, 31))
    assert core.time_bounds("+1929-02-00T00:00:00Z", 10) == ((1929, 2, 1), (1929, 2, 31))
    assert core.time_bounds("-0400-00-00T00:00:00Z", 9) == ((-400, 1, 1), (-400, 12, 31))
    assert core.time_bounds("+1929-00-00T00:00:00Z", None) is None
    bce = core.time_bounds("-0500-00-00T00:00:00Z", 9), core.time_bounds("-0400-00-00T00:00:00Z", 9)
    assert core.temporal_check(bce[0], bce[1]) == "consistent"
    assert core.temporal_check(bce[1], bce[0]) == "inconsistent"
    crash = core.time_bounds("+1929-10-24T00:00:00Z", 11)
    depression = core.time_bounds("+1929-00-00T00:00:00Z", 9)
    later = core.time_bounds("+1933-00-00T00:00:00Z", 9)
    assert core.temporal_check(crash, later) == "consistent"
    assert core.temporal_check(later, crash) == "inconsistent"
    assert core.temporal_check(crash, depression) == "overlapping"  # year precision cannot decide
    assert core.temporal_check(None, depression) == "undetermined"


def test_cycles_and_chains() -> None:
    succ = {"Q1": ["Q2"], "Q2": ["Q3"], "Q3": ["Q1", "Q4"], "Q4": ["Q5"], "Q6": ["Q6"]}
    sccs = core.strongly_connected(["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"], succ)
    assert sccs == [["Q1", "Q2", "Q3"]]
    paths = core.chains("Q1", succ, max_hops=6)
    assert paths[0] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert all(len(set(p)) == len(p) for p in paths)  # simple paths only
    assert max(len(p) - 1 for p in core.chains("Q1", succ, max_hops=2)) == 2


def test_seeds_from_recorded_and_specs(tmp_path: Path) -> None:
    rec = tmp_path / "snapshot_claims_1.json"
    rec.write_text(
        json.dumps(
            {
                "entities": {
                    "Q362": {
                        "claims": [
                            {"p": "P828", "o": "Q12345"},
                            {"p": "P793", "o": "Q777"},
                            {"p": "P31", "o": "Q103495"},
                            {"p": "P1542", "o": "+1945"},
                        ]
                    }
                }
            }
        )
    )
    s = core.seeds_from_recorded([rec, tmp_path / "missing.json"])
    assert set(s) == {"Q362", "Q12345", "Q777"}
    spec = tmp_path / "x.yaml"
    spec.write_text(
        "sparql: |\n  SELECT ?item WHERE { VALUES ?root { wd:Q198 } } UNION { VALUES ?item { wd:Q4576 wd:Q188532 } }"
    )
    assert set(core.seeds_from_specs([spec])) == {"Q4576", "Q188532"}


def test_prioritise_orders_by_tier_indegree_sitelinks_rank() -> None:
    fr = {
        "Q9": {"tier": 1, "indegree": 0},
        "Q8": {"tier": 0},
        "Q7": {"tier": 1, "indegree": 3},
        "Q6": {"tier": 4, "rank": 5},
        "Q5": {"tier": 4, "rank": 1},
        "Q4": {"tier": 1, "indegree": 3, "via_sitelinks": 200},
    }
    assert core.prioritise(fr) == ["Q8", "Q4", "Q7", "Q9", "Q5", "Q6"]


def test_capped_http_refuses_before_the_transport() -> None:
    calls: list[str] = []

    class Inner:
        async def get(self, url: str, **kw: Any) -> str:
            calls.append(url)
            return "ok"

    h = live.CappedHttp(Inner(), 2, SOURCE_WBAPI)
    asyncio.run(h.get("u1"))
    asyncio.run(h.get("u2"))
    with pytest.raises(BudgetExhausted):
        asyncio.run(h.get("u3"))
    assert calls == ["u1", "u2"]


def test_wbapi_cap_takes_the_larger_usage_and_thirty_percent() -> None:
    cap, basis = live.wbapi_cap({"wikidata.wbapi": 0}, {"entitydata": 2600, "wbapi": 50, "wdqs": 13})
    assert basis["used_estimate"] == 2650 and cap == int((4000 - 2650) * 0.3)
    cap2, _ = live.wbapi_cap({"wikidata.wbapi": 3000}, {"entitydata": 10})
    assert cap2 == 300
    assert live.wbapi_cap({}, {"entitydata": 5000})[0] == 0


def test_usage_today_counts_only_today(tmp_path: Path) -> None:
    idx = tmp_path / "i.jsonl"
    idx.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"endpoint": "entitydata", "retrieved_at": "2026-10-16T01:00:00+00:00"},
                {"endpoint": "wbapi", "retrieved_at": "2026-10-16T02:00:00+00:00"},
                {"endpoint": "wbapi", "retrieved_at": "2026-10-15T23:00:00+00:00"},
                {"kind": "sparql_query"},
            ]
        )
        + "\n"
    )
    got = live.wikidata_usage_today(idx, now=datetime(2026, 10, 16, 12, tzinfo=UTC))
    assert got == {"entitydata": 1, "wbapi": 1}


class _Sink:
    def __init__(self) -> None:
        self.batches: list[Any] = []

    def load(self, batch: Any) -> dict[str, Any]:
        self.batches.append(batch)
        return {"statements_inserted": len(batch.statements)}


def test_ingest_loads_subset_and_expands_one_hop(tmp_path: Path) -> None:
    sess = type("S", (), {"sink": _Sink()})()
    st = live.State(tmp_path / "state.json")
    st.add("Q8698", tier=0, reason="test")
    hv = live.CausalHarvest(sess, st, max_hops=1)  # type: ignore[arg-type]
    hv.ingest(
        {"Q8698": raw_entity()},
        {"Q8698": "b" * 64},
        ["b" * 64],
        datetime.now(UTC),
        requested=["Q8698"],
        missing=[],
        redirects={},
        mode="test",
    )
    batch = sess.sink.batches[0]
    assert batch.source_key == SOURCE_WBAPI and batch.parameters["agent"] == "CAUSWD"
    assert st.fetched["Q8698"]["causal"] == 2 and st.fetched["Q8698"]["rev"] == 123
    assert st.frontier["Q205020"]["hop"] == 1 and st.frontier["Q205020"]["tier"] == 1
    assert st.frontier["Q205020"]["indegree"] == 1 and st.frontier["Q205020"]["via_sitelinks"] == 2
    # hop-1 entities are fetched but not expanded further
    raw2 = raw_entity() | {"id": "Q205020"}
    hv.ingest(
        {"Q205020": raw2},
        {"Q205020": "c" * 64},
        ["c" * 64],
        datetime.now(UTC),
        requested=["Q205020"],
        missing=[],
        redirects={},
        mode="test",
    )
    assert "Q205020" in st.fetched and st.fetched["Q205020"]["hop"] == 1
    assert set(st.frontier) == {"Q170314"}
    st.add("Q8698", tier=0, reason="again")  # fetched entities are never queued again
    assert "Q8698" not in st.frontier


def test_wdqs_listing_loads_only_rows_touching_the_seed_reach(tmp_path: Path) -> None:
    from reva.adapters.connectors.wikidata import SparqlResult

    E = "http://www.wikidata.org/entity/"

    def row(s: str, sid: str, o: str) -> dict[str, Any]:
        return {
            "item": {"type": "uri", "value": E + s},
            "statement": {"type": "uri", "value": f"{E}statement/{sid}"},
            "object": {"type": "uri", "value": E + o},
            "rank": {"type": "uri", "value": "http://wikiba.se/ontology#NormalRank"},
        }

    calls: list[str] = []

    class FakeWdqs:
        async def sparql(self, query: str, *, meta: Any = None) -> SparqlResult:
            calls.append(meta["spec"])
            rows = (
                [row("Q1", "Q1-A", "Q2"), row("Q3", "Q3-B", "Q1"), row("Q8", "Q8-C", "Q9")] if len(calls) == 1 else []
            )
            return SparqlResult(
                ("item", "statement", "object", "rank"), rows, "q" * 64, "d" * 64, 10, datetime.now(UTC), 0.1
            )

    sink = _Sink()
    sess = type("S", (), {"sink": sink, "wdqs_client": FakeWdqs()})()
    st = live.State(tmp_path / "s.json")
    st.fetched["Q1"] = {"rev": 1}
    hv = live.CausalHarvest(sess, st)  # type: ignore[arg-type]
    out = asyncio.run(hv.wdqs_listings())
    assert out["status"] == "done" and len(calls) == 9 + 9 + 4  # P828/P1542 split by QID prefix
    loaded = [s for b in sink.batches for s in b.statements]
    assert [(s.subject, s.object_qid) for s in loaded] == [("Q3", "Q1")]  # Q1's own claims come from snapshots
    assert loaded[0].source_key == "wikidata.wdqs" and loaded[0].references == []
    assert set(st.frontier) == {"Q2", "Q3"} and "Q8" not in st.frontier
    first = st.data["wdqs"]["causal.contributing_factor_of"]
    assert first["status"] == "done"
    chunk = next(iter(first["chunks"].values()))
    assert chunk["statements"] == 3 and chunk["touching_seed_reach"] == 2 and chunk["loaded_as_wdqs_rows"] == 1
