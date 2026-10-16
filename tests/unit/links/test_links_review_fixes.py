"""Regression tests for the V3 review fixes in the links layer (docs/dev/review_v3.md: LINKS-1..3, SEC-1).

Synthetic ids (Q9001xx); nothing here is a real-world fact.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from reva.links.model import AtlasRowRef, Evidence, Stmt
from reva.links.paths import (
    KgNeighbourhood,
    PathQuery,
    build_graph,
    find_chain,
    find_paths,
)

S, M, T = "Q900101", "Q900103", "Q900102"


def _graph() -> Any:
    return build_graph([Stmt("syn:1", S, "P37", M), Stmt("syn:2", T, "P37", M)])


def test_found_path_over_a_truncated_neighbourhood_is_still_flagged() -> None:
    g = _graph()
    g.truncated_nodes = {M}
    res = find_paths(g, PathQuery(S, T))
    assert res.found and res.truncated and res.truncated_nodes == {M}
    j = res.to_json()
    assert j["truncated"] and j["truncated_nodes"] == [M] and "neighbourhood truncated" in j["truncation_reasons"][0]


def test_untruncated_search_is_not_flagged_and_hops_carry_their_relation_label() -> None:
    res = find_paths(_graph(), PathQuery(S, T))
    assert res.found and not res.truncated and res.truncation_reasons == []
    hops = res.to_json()["paths"][0]["hops"]
    assert hops[0]["label"] == {"fr": "langue officielle", "en": "official language"}
    assert set(hops[1]["label"]) == {"fr", "en"} and hops[1]["label"] != hops[0]["label"]  # inverse direction


def test_deadline_cuts_candidate_ranking_and_chains() -> None:
    past = time.monotonic() - 1
    res = find_paths(_graph(), PathQuery(S, T), deadline=past)
    assert res.truncated and any("time budget" in r for r in res.truncation_reasons)
    chain = find_chain(_graph(), [S, M, T], deadline=past)
    assert chain.truncated and chain.reason and "time_budget_exhausted" in chain.reason


class _FakeNb(KgNeighbourhood):
    """KgNeighbourhood over in-memory rows shaped like kg.statement_current."""

    def __init__(self, rows: list[dict[str, Any]], **kw: Any) -> None:
        super().__init__(conn=None, **kw)
        self.rows = rows
        self.calls = 0

    def _rows(self, sql: str, params: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if "'P298'" in sql:
            return [r for r in self.rows if r["property_pid"] == "P298"]
        f = set(params.get("f") or ())
        return [r for r in self.rows if r["property_pid"] != "P298" and {r["subject_qid"], r["object_qid"]} & f]


def _row(key: str, s: str, p: str, o: str | None = None, value: dict | None = None, rank: str = "normal") -> dict:
    return {
        "statement_key": key,
        "statement_id": key,
        "subject_qid": s,
        "property_pid": p,
        "object_qid": o,
        "value": value,
        "rank": rank,
        "source_key": "wikidata",
        "origin": "test_fixture",
    }


def test_duplicate_iso3_is_reported_never_resolved_by_id_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """LINKS-2: PSE/YUG/ATA are held by several Wikidata items; no silent pick (not by rank, not by QID order)."""
    import reva.kg.query as kq

    monkeypatch.setattr(kq, "country_qid_registry", lambda *a, **k: {})
    iso = {"type": "external_id", "value": "ZZP"}
    nb = _FakeNb(
        [
            _row("a", "Q900201", "P298", value=iso, rank="preferred"),
            _row("b", "Q900202", "P298", value=iso),
            _row("c", "Q900203", "P298", value={**iso, "value": "ZZQ"}),
        ]
    )
    assert nb.iso3_to_qid() == {"ZZQ": "Q900203"}
    assert nb.ambiguous_iso3 == {"ZZP": ["Q900201", "Q900202"]}
    from reva.links.service import AmbiguousCountry, resolve_node

    with pytest.raises(AmbiguousCountry) as e:
        resolve_node(nb, "ZZP")
    assert e.value.candidates == ["Q900201", "Q900202"]
    # the reviewed registry decides when it lists the code
    monkeypatch.setattr(kq, "country_qid_registry", lambda *a, **k: {"ZZP": "Q900202"})
    nb2 = _FakeNb(nb.rows)
    assert resolve_node(nb2, "ZZP") == "Q900202" and nb2.ambiguous_iso3 == {}


def test_neighbourhood_reads_are_bounded_and_flag_what_they_did_not_read() -> None:
    """SEC-1: rows per neighbourhood and the deadline stop expansion; unexpanded nodes are truncated."""
    rows = [_row(f"k{i}", S, "P463", f"Q9003{i:02d}") for i in range(10)]
    rows += [_row(f"m{i}", f"Q9003{i:02d}", "P463", f"Q9004{i:02d}") for i in range(10)]
    nb = _FakeNb(rows, max_total_rows=5)
    _stmts, _reached, truncated = nb.expand([S], 3, ["P463"])
    assert truncated == {f"Q9003{i:02d}" for i in range(10)} and nb.calls == 1
    nb = _FakeNb(rows, deadline=time.monotonic() - 1)
    stmts, _reached, truncated = nb.expand([S], 3, ["P463"])
    assert stmts == [] and truncated == {S} and nb.calls == 0


def test_atlas_row_evidence_keeps_its_origin_and_never_defaults_to_live() -> None:
    """LINKS-3."""
    base = {
        "source": "imf_dots",
        "dataset": "dots",
        "indicator": "TXG|counterpart=FRA",
        "iso3": "SEN",
        "period_start": "2020-01-01",
        "period_end": "2021-01-01",
        "value": "1",
        "raw_sha256": "e" * 64,
        "selector": "row 1",
    }
    ev = Evidence.of_row(AtlasRowRef.from_mapping({**base, "origin": "test_fixture"}))
    assert ev.origin == "test_fixture" and ev.cite().endswith("[fixture]")
    assert Evidence.of_row(AtlasRowRef.from_mapping(base)).origin is None

    from reva.api.deps import Timer
    from reva.api.routes.links import _envelope, _origins

    assert _origins([{"origin": None}, {"origin": "live"}]) == ["live", "unknown"]
    env = _envelope(["unknown"], False, Timer(), 1)
    assert env["origin"] == "none" and [w.code for w in env["warnings"]] == ["origin_unknown"]
    env = _envelope(["live", "unknown"], False, Timer(), 2)
    assert env["origin"] == "mixed" and "origin_unknown" in [w.code for w in env["warnings"]]
    assert _envelope(["live"], False, Timer(), 1)["origin"] == "live"
