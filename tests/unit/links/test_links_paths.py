"""Path engine on synthetic graphs (logic only: hub damping, time filters, bounds, rendering).

QIDs ``Q9000xx`` are placeholders; real chains are tested in test_links_fixture.py."""

from __future__ import annotations

import pytest

from reva.links.model import Evidence, Link, PartialDate, Stmt, Temporal
from reva.links.paths import PathQuery, PathQueryError, build_graph, find_paths, render_hop, temporal_allows
from reva.links.taxonomy import Hubs, load_hubs

S, T, M1, M2, M3 = "Q900101", "Q900102", "Q900103", "Q900104", "Q900105"
HUMAN, UN = "Q5", "Q1065"
_n = iter(range(1, 10_000))
LABELS = {
    S: {"fr": "Source", "en": "Source"},
    T: {"fr": "Cible", "en": "Target"},
    M1: {"en": "M1"},
    M2: {"en": "M2"},
    M3: {"en": "M3"},
    HUMAN: {"fr": "être humain", "en": "human"},
    UN: {"fr": "Organisation des Nations unies", "en": "United Nations"},
}


def st(s: str, p: str, o: str, *, start=None, end=None, point=None, source: str = "wikidata") -> Stmt:
    return Stmt(
        f"syn:{next(_n)}",
        s,
        p,
        o,
        start=PartialDate.parse(start),
        end=PartialDate.parse(end),
        point=PartialDate.parse(point),
        source_key=source,
    )


def test_query_bounds() -> None:
    with pytest.raises(PathQueryError):
        PathQuery(S, T, max_hops=7)
    with pytest.raises(PathQueryError):
        PathQuery(S, S)
    with pytest.raises(PathQueryError):
        PathQuery(S, T, as_of=1950, window=(1940, 1960))
    with pytest.raises(PathQueryError):
        PathQuery(S, T, k=0)


def test_blocked_hub_is_not_an_intermediate_node() -> None:
    g = build_graph([st(S, "P27", HUMAN), st(T, "P27", HUMAN)], labels=LABELS)  # P27 just to have an edge kind
    assert not find_paths(g, PathQuery(S, T)).found
    res = find_paths(g, PathQuery(S, T, include_blocked_hubs=True))
    assert res.found and res.paths[0].nodes == [S, HUMAN, T]


def test_penalised_hub_ranks_after_specific_path() -> None:
    stmts = [st(S, "P463", UN), st(T, "P463", UN), st(S, "P37", M1), st(T, "P37", M1)]
    res = find_paths(build_graph(stmts, labels=LABELS), PathQuery(S, T, k=2))
    assert [p.nodes for p in res.paths] == [[S, M1, T], [S, UN, T]]
    assert res.paths[0].cost < res.paths[1].cost


def test_degree_penalty_prefers_low_degree_intermediate() -> None:
    fan = [st(f"Q9{i:05d}", "P463", M2) for i in range(10, 300)]  # M2 has ~290 neighbours
    stmts = [*fan, st(S, "P463", M2), st(T, "P463", M2), st(S, "P463", M3), st(T, "P463", M3)]
    hubs = load_hubs()
    res = find_paths(build_graph(stmts, labels=LABELS, hubs=hubs), PathQuery(S, T, k=2))
    assert res.paths[0].nodes == [S, M3, T]
    assert res.paths[1].nodes == [S, M2, T] and res.paths[1].cost > res.paths[0].cost


def test_excluded_properties_need_explicit_opt_in() -> None:
    stmts = [st(S, "P30", M1), st(T, "P30", M1)]  # continent: excluded by default
    g = build_graph(stmts, labels=LABELS)
    assert not find_paths(g, PathQuery(S, T)).found
    assert find_paths(g, PathQuery(S, T, kinds=frozenset({"P30"}))).found


def test_time_filter_as_of_and_window() -> None:
    stmts = [st(S, "P463", M1, start=1949, end=1966), st(T, "P463", M1, start=1955), st(S, "P37", M2), st(T, "P37", M2)]
    g = build_graph(stmts, labels=LABELS)
    assert find_paths(g, PathQuery(S, T, as_of=1960, k=3, include_undated=False)).paths[0].nodes == [S, M1, T]
    assert not find_paths(g, PathQuery(S, T, as_of=1970, include_undated=False)).found
    r = find_paths(g, PathQuery(S, T, as_of=1970))  # undated statements kept and flagged
    assert r.paths[0].nodes == [S, M2, T]
    assert "undated in source" in r.paths[0].hops[0].text["en"]
    assert find_paths(g, PathQuery(S, T, window=(1940, 1950), include_undated=False)).found is False
    assert temporal_allows(Temporal("point", point=PartialDate(1494)), PathQuery(S, T, as_of=1960))
    assert not temporal_allows(Temporal("point", point=PartialDate(1494)), PathQuery(S, T, window=(1950, 1970)))
    assert temporal_allows(Temporal("snapshot", year=2020), PathQuery(S, T, window=(2019, 2021)))


def test_kind_filter_and_max_hops() -> None:
    stmts = [st(S, "P37", M1), st(M1, "P279", M2), st(M2, "P279", M3), st(T, "P37", M3)]
    g = build_graph(stmts, labels=LABELS)
    assert find_paths(g, PathQuery(S, T, max_hops=4)).found
    assert not find_paths(g, PathQuery(S, T, max_hops=3)).found
    assert not find_paths(g, PathQuery(S, T, max_hops=4, kinds=frozenset({"P37"}))).found
    assert not find_paths(g, PathQuery(S, T, exclude_kinds=frozenset({"statement:P279"}))).found


def test_unknown_node() -> None:
    res = find_paths(build_graph([st(S, "P37", M1)]), PathQuery(S, T))
    assert res.reason == "unknown_node" and not res.found


def test_rendering_directions_dates_citations() -> None:
    s = st("Q155", "P37", "Q5146")
    g = build_graph(
        [s, st("Q45", "P37", "Q5146", start=1290)],
        labels={
            "Q155": {"fr": "Brésil", "en": "Brazil"},
            "Q5146": {"fr": "portugais", "en": "Portuguese"},
            "Q45": {"fr": "Portugal", "en": "Portugal"},
        },
    )
    res = find_paths(g, PathQuery("Q155", "Q45"))
    h1, h2 = res.paths[0].hops
    assert h1.text["fr"].startswith("Brésil — langue officielle → portugais (non daté dans la source) [wikidata:syn:")
    assert h1.text["en"].startswith("Brazil — official language → Portuguese (undated in source)")
    assert h2.text["fr"].startswith("portugais — langue officielle de → Portugal (depuis 1290)")
    assert h2.text["en"].startswith("Portuguese — official language of → Portugal (since 1290)")
    assert all("cause" not in h.text["en"] for h in res.paths[0].hops)
    js = res.to_json()
    assert js["paths"][0]["hops"][0]["evidence"][0]["statement_id"] == s.statement_id
    assert "not a causal explanation" in js["disclaimer"]["en"]


def test_causal_wording_only_for_causal_properties_and_attributed() -> None:
    cause = st(M1, "P828", M2)  # M1 has cause M2
    g = build_graph([cause, st(S, "P793", M1), st(T, "P793", M2)], labels=LABELS)
    res = find_paths(g, PathQuery(S, T))
    texts = [h.text for h in res.paths[0].hops]
    causal = [t for t in texts if "cause" in t["en"]]
    assert len(causal) == 1
    assert causal[0]["fr"].startswith("selon Wikidata (contribution communautaire), M1 a pour cause M2")
    assert causal[0]["en"].startswith("according to Wikidata (community-edited), M1 has cause M2")
    # the claim keeps its own direction even when traversed from the effect side
    res2 = find_paths(g, PathQuery(T, S))
    assert any("M1 a pour cause M2" in h.text["fr"] for h in res2.paths[0].hops)


def test_derived_links_in_paths_with_via_and_weight() -> None:
    ev = Evidence(kind="atlas_row", source="imf_dots", raw_sha256="d" * 64, selector='{"row":1}')
    trade = Link(S, T, "bilateral_trade", True, Temporal("snapshot", year=2022), (ev,), weight="42.5", unit="USD")
    lang = Link(
        S,
        T,
        "shared_official_language",
        False,
        Temporal("undated"),
        (
            Evidence(kind="kg_statement", source="wikidata", statement_id="syn:a"),
            Evidence(kind="kg_statement", source="wikidata", statement_id="syn:b"),
        ),
        via=(M1,),
    )
    g = build_graph([], [trade, lang], labels=LABELS)
    res = find_paths(g, PathQuery(T, S, k=1, kinds=frozenset({"link:bilateral_trade"})))
    (hop,) = res.paths[0].hops
    assert hop.text["en"].startswith("Target — export destination of → Source = 42.5 USD (2022)")
    assert "imf_dots:dddddddddddd" in hop.text["en"]
    res = find_paths(g, PathQuery(S, T, k=1, kinds=frozenset({"shared_official_language"})))
    assert res.paths[0].hops[0].text["fr"].startswith("Source — langue officielle commune (via M1) → Cible")


def test_custom_hubs_object() -> None:
    hubs = Hubs(frozenset({M1}), {}, frozenset(), 64, 1.0)
    g = build_graph([st(S, "P37", M1), st(T, "P37", M1)], hubs=hubs)
    assert not find_paths(g, PathQuery(S, T)).found
    assert render_hop(g.adj[S][0], g, "en").startswith(f"{S} — official language → {M1}")
