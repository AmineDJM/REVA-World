"""End-to-end on the offline fixture of *real* Wikidata statements (CC0, captured 2026-10-15 through
WDHARVEST's locked client; origin ``test_fixture``): derivations, the Columbus → Tordesillas → Brazil →
Portuguese chain and the United States / decolonisation 1950s–60s chain, every hop cited.

The assertions are about what Wikidata states in that capture (statement GUIDs, revisions), not about
history: if a future re-capture changes a statement, these tests must be re-read, not "fixed" by hand.
"""

from __future__ import annotations

import pytest

from reva.links.derive import assert_all_evidenced, derive_all
from reva.links.fixture import FIXTURE_FILE, load_fixture, load_seed, seed_qids
from reva.links.model import NoEvidence, PartialDate, Temporal
from reva.links.paths import PathQuery, build_graph, find_chain, find_paths

COLUMBUS, TORDESILLAS, BRAZIL, PORTUGUESE, PORTUGAL = "Q7322", "Q180897", "Q155", "Q5146", "Q45"
COLONIAL_BRAZIL, PORTUGUESE_EMPIRE, ISABELLA, KINGDOM_PT = "Q2088324", "Q200464", "Q45859", "Q45670"
USA, EISENHOWER, SUEZ, UK, COLD_WAR, RES_1514, DECOLONIZATION, UN = (
    "Q30", "Q9916", "Q49101", "Q145", "Q8683", "Q2629473", "Q230533", "Q1065"
)


@pytest.fixture(scope="module")
def fx():
    return load_fixture()


@pytest.fixture(scope="module")
def derived(fx):
    return derive_all(fx.statements)


@pytest.fixture(scope="module")
def graph(fx, derived):
    return build_graph(fx.statements, derived.links, fx.labels)


def test_fixture_is_real_labelled_and_complete(fx) -> None:
    assert FIXTURE_FILE.exists()
    assert fx.meta["community_edited"] is True and len(fx.meta["raw_sha256s"]) >= 1
    assert set(fx.entities) == set(seed_qids(load_seed()))
    for s in fx.statements:
        assert s.origin == "test_fixture" and s.source_key == "wikidata"
        assert "$" in s.statement_id  # Wikidata statement GUID
        assert s.source_revision and s.raw_sha256 in fx.meta["raw_sha256s"]
        assert s.rank in ("preferred", "normal")
    # seed labels were checked against the captured entities
    for g in load_seed()["groups"]:
        for it in g["entities"]:
            assert fx.entities[it["qid"]]["labels"]["en"] == it["label"], it
    assert 300 <= len(fx.statements) <= 2000


def test_no_evidence_no_link_on_every_derived_link(derived) -> None:
    assert_all_evidenced(derived.links)
    ids = {e.statement_id for lk in derived.links for e in lk.evidence}
    assert all(i and "$" in i for i in ids)
    with pytest.raises(NoEvidence):
        type(derived.links[0])(BRAZIL, PORTUGAL, "shared_official_language", False, Temporal("undated"), ())


def test_colonial_derivation_brazil(derived) -> None:
    col = {(lk.subject, lk.object, lk.temporal.label()) for lk in derived.by_type["colony_of"].links}
    # Colonial Brazil P31 colony; P361 Portuguese Empire with P580 1500 / P582 1822
    assert (COLONIAL_BRAZIL, PORTUGUESE_EMPIRE, "1500–1822") in col
    former = [lk for lk in derived.by_type["former_colony_of"].links if lk.subject == BRAZIL]
    lk = next(x for x in former if x.object == PORTUGUESE_EMPIRE and x.temporal.label() == "1500–1822")
    assert lk.via == (COLONIAL_BRAZIL,)
    props = {e.pid for e in lk.evidence}
    assert props == {"P1365", "P31", "P361"}  # Brazil replaces Colonial Brazil; colony; part of the empire
    assert lk.temporal == Temporal.of(PartialDate(1500), PartialDate(1822))


def test_shared_language_and_co_membership(derived) -> None:
    sol = {(lk.subject, lk.object) for lk in derived.by_type["shared_official_language"].links if lk.via == (PORTUGUESE,)}
    assert (BRAZIL, PORTUGAL) in sol or (PORTUGAL, BRAZIL) in sol
    # co-membership derived only from overlapping dated memberships; disjoint ones counted, not linked
    co = derived.by_type["co_member"]
    assert co.links and co.skipped["disjoint_periods"] >= 1
    assert all(lk.temporal.kind in ("interval", "point", "undated") for lk in co.links)
    us_uk = [lk for lk in co.links if {lk.subject, lk.object} == {USA, UK} and lk.via == (UN,)]
    assert us_uk and us_uk[0].temporal.start == PartialDate(1945, 10, 24, 11)


def test_conflict_participants_without_sides(derived) -> None:
    suez = [lk for lk in derived.by_type["conflict_co_participant"].links if lk.via == (SUEZ,)]
    pairs = {frozenset((lk.subject, lk.object)) for lk in suez}
    assert frozenset((UK, "Q142")) in pairs and frozenset((UK, "Q79")) in pairs
    assert all(lk.basis["sides"] == "not_stated_by_source" for lk in suez)
    # the Suez Crisis entity's own dates (P580 1956-10-29, P582 1956-11-07) date the co-participation
    assert suez[0].temporal == Temporal.of(PartialDate(1956, 10, 29, 11), PartialDate(1956, 11, 7, 11))
    assert not any(USA in (lk.subject, lk.object) for lk in suez)  # the US is not a P710 participant here


def test_columbus_tordesillas_brazil_portuguese_chain(graph) -> None:
    res = find_chain(graph, [COLUMBUS, TORDESILLAS, BRAZIL, PORTUGUESE], max_hops=3)
    assert res.found, res.reason
    (path,) = res.paths
    assert path.nodes[0] == COLUMBUS and path.nodes[-1] == PORTUGUESE
    assert TORDESILLAS in path.nodes and BRAZIL in path.nodes and ISABELLA in path.nodes
    fr, en = path.text["fr"], path.text["en"]
    assert fr[0].startswith("Christophe Colomb — personnalité importante → Isabelle la Catholique")
    assert fr[1].startswith("Isabelle la Catholique — signataire de → traité de Tordesillas (1494-07-02)")
    assert en[-1].startswith("Brazil — official language → Portuguese (undated in source)")
    for hop in path.hops:
        assert hop.edge.evidence and all(e.statement_id for e in hop.edge.evidence)
        assert "[wikidata:" in hop.text["en"] and "[fixture]" in hop.text["en"]
        assert not hop.edge.causal and "cause" not in hop.text["en"].lower()


def test_columbus_to_brazil_shortest_paths_are_cited(graph) -> None:
    res = find_paths(graph, PathQuery(COLUMBUS, BRAZIL, k=5, max_hops=4))
    assert len(res.paths) == 5
    costs = [p.cost for p in res.paths]
    assert costs == sorted(costs)
    # Wikidata states Columbus spoke Portuguese (P1412) and Portuguese P17 Brazil: the cheapest connection
    assert [h.edge.type_id for h in res.paths[0].hops] == ["P1412", "P17"]
    assert res.to_json()["paths"][0]["hops"][0]["evidence"][0]["statement_id"].startswith("Q7322$")


def test_tordesillas_to_brazil_via_portuguese_empire(graph) -> None:
    res = find_paths(graph, PathQuery(TORDESILLAS, BRAZIL, k=3, max_hops=3))
    texts = [" | ".join(t.split(" [")[0] for t in p.text["en"]) for p in res.paths]
    assert any(
        "Treaty of Tordesillas — significant event of → Portuguese Empire" in t
        and "Portuguese Empire — country of → Brazil (1500–1815)" in t
        for t in texts
    ), texts


def test_us_decolonisation_1950s_chain_is_connection_not_stance(graph) -> None:
    res = find_chain(graph, [USA, EISENHOWER, SUEZ, UK], max_hops=3)
    (path,) = res.paths
    en = [t.split(" [")[0] for t in path.text["en"]]
    assert en == [
        "United States — head of government → Dwight D. Eisenhower (1953-01-20–1961-01-20)",
        "Dwight D. Eisenhower — conflict → Cold War (undated in source)",
        "Cold War — has part → Suez Crisis (undated in source)",
        "Suez Crisis — participant → United Kingdom (undated in source)",
    ]
    assert all(not h.edge.causal for h in path.hops)
    assert "not a causal explanation" in res.to_json()["disclaimer"]["en"]


def test_time_filter_on_real_statements(graph) -> None:
    # as of 1956, Eisenhower's head-of-government statement (1953–1961) holds...
    q = PathQuery(USA, COLD_WAR, as_of=1956, include_undated=True, kinds=frozenset({"P6", "P607"}))
    assert find_paths(graph, q).paths[0].nodes == [USA, EISENHOWER, COLD_WAR]
    # ...as of 1965 it does not, and nothing else of those kinds connects them
    q65 = PathQuery(USA, COLD_WAR, as_of=1965, include_undated=True, kinds=frozenset({"P6", "P607"}))
    assert not find_paths(graph, q65).found


def test_resolution_1514_and_us_stance_not_established(graph, fx) -> None:
    """Wikidata (in this capture) states nothing traversable about UNGA Resolution 1514 beyond its class,
    and no US vote or position: the engine says so instead of inventing a connection."""
    res = find_paths(graph, PathQuery(USA, RES_1514, max_hops=6))
    assert not res.found and res.reason == "no_traversable_statements"
    assert not find_paths(graph, PathQuery(USA, DECOLONIZATION, max_hops=6)).found
    assert not [s for s in fx.statements if s.subject == RES_1514 and s.object == USA]


def test_hub_damping_on_real_graph(graph) -> None:
    # without damping, "both members of the United Nations" would be the cheapest US–UK connection
    res = find_paths(graph, PathQuery(USA, UK, k=5))
    vias = [h.edge.link.via for p in res.paths for h in p.hops if h.edge.link is not None]
    first = res.paths[0]
    assert all(UN not in (h.edge.link.via if h.edge.link else ()) for h in first.hops)
    assert all(UN not in p.nodes[1:-1] for p in res.paths[:1])
    assert vias is not None


def test_no_causal_wording_anywhere_in_fixture_paths(graph) -> None:
    for a, b in ((COLUMBUS, PORTUGUESE), (USA, SUEZ), (BRAZIL, PORTUGAL), (TORDESILLAS, BRAZIL)):
        for p in find_paths(graph, PathQuery(a, b, k=3, max_hops=4)).paths:
            for h in p.hops:
                if not h.edge.causal:
                    assert "cause" not in h.text["en"].lower() and "cause" not in h.text["fr"].lower()
