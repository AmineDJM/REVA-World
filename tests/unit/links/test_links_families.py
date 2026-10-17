"""V4CORE: the link path engine ("Neurones") traverses every property mapped to a universal relation family,
labels hops by family, and filters by family in reading direction. Synthetic ids; not real-world facts."""

from __future__ import annotations

from reva.links.model import Stmt
from reva.links.paths import PathQuery, build_graph, find_paths
from reva.links.taxonomy import load_taxonomy


def _g():  # type: ignore[no-untyped-def]
    sts = [
        Stmt("s1", "Q9001", "P186", object="Q9002", source_key="wikidata"),  # petroleum made of alkane
        Stmt("s2", "Q9002", "P361", object="Q9003", source_key="wikidata"),  # alkane part of hydrocarbon
        Stmt("s3", "Q9001", "P31", object="Q9004", source_key="wikidata"),  # instance of (opt-in)
        Stmt("s4", "Q9003", "P31", object="Q9004", source_key="wikidata"),
    ]
    labels = {q: {"fr": f"n{q}", "en": f"n{q}"} for q in ("Q9001", "Q9002", "Q9003", "Q9004")}
    return build_graph(sts, labels=labels)


def test_family_edges_in_taxonomy() -> None:
    tax = load_taxonomy()
    assert tax.edge_types["P186"].family == "made_of" and not tax.edge_types["P186"].causal
    assert tax.edge_types["P186"].inverse_labels["fr"] == "est un constituant de"
    assert "P31" in tax.opt_in_properties or "P31" in tax.edge_types
    assert all(not tax.edge_types[p].family for p in ("P828", "P1542") if p in tax.edge_types)


def test_family_hops_and_filter() -> None:
    g = _g()
    res = find_paths(g, PathQuery("Q9001", "Q9003", max_hops=3))
    assert res.found
    hops = res.paths[0].to_json()["hops"]
    assert [h["family"] for h in hops] == ["made_of", "part_of"]
    assert res.paths[0].text["fr"][0].startswith("nQ9001 — est composé de → nQ9002")
    # families filter in reading direction
    ok = find_paths(g, PathQuery("Q9001", "Q9003", families=frozenset({"made_of", "part_of"})))
    assert ok.found
    rev = find_paths(g, PathQuery("Q9003", "Q9001", families=frozenset({"made_of", "part_of"})))
    assert not rev.found  # read backwards the hops are has_part / constituent-of
    # instance of is opt-in: only when the family is named
    assert not find_paths(g, PathQuery("Q9001", "Q9004", max_hops=1)).found
    assert find_paths(g, PathQuery("Q9001", "Q9004", max_hops=1, families=frozenset({"is_a"}))).found
