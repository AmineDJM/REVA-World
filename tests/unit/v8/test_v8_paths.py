"""V8 Liens path index and search (agent V8LIENS) on a synthetic graph — no database.

Nothing here is a real-world fact: ids are ``syn:`` / ``Q9xxxx`` placeholders and the families/costs are the
test registry's own.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from reva.v8.paths import index as ix
from reva.v8.paths import search as se

FAMS = ["part_of", "has_part", "located_in", "produces", "produced_by", "is_a", "context"]


def _reg(**kw: object) -> ix.Registries:
    base = dict(
        family_ids=FAMS,
        family_cost={
            "part_of": 1.0,
            "has_part": 1.0,
            "located_in": 1.0,
            "produces": 1.0,
            "produced_by": 1.0,
            "is_a": 3.0,
            "context": 3.0,
        },
        edge_family={
            "P361": ("part_of", "has_part"),
            "P131": ("located_in", "located_in"),
            "P1056": ("produces", "produced_by"),
            "P31": ("is_a", "is_a"),
            "syn:ctx": ("context", "context"),
        },
        excluded_properties=frozenset({"P31"}),
        blocked=frozenset({"syn:earth"}),
        penalised={"syn:un": 5.0},
        degree_threshold=4,
        degree_per_doubling=1.0,
        variable_atlas={"prices.cpi": ("imf.cpi",)},
        event_type_classes={"earthquake": ("syn:earthquake_class",)},
        iso3_qid={"FRA": "syn:france", "DEU": "syn:germany"},
        relations=("has_cause", "has_effect"),
        signature="test",
    )
    base.update(kw)
    return ix.Registries(**base)  # type: ignore[arg-type]


S = ix.StatementRow
C = ix.ClaimRow
E = ix.EstimateRow


def _graph() -> ix.PathIndex:
    sts = [
        S(1, "syn:wheel", "P361", "syn:car"),
        S(2, "syn:car", "P361", "syn:fleet"),
        S(3, "syn:firm", "P1056", "syn:car"),
        S(4, "syn:firm", "P131", "syn:france"),
        S(5, "syn:plant", "P131", "syn:france"),
        S(6, "syn:plant", "P1056", "syn:steel"),
        S(7, "syn:car", "P31", "syn:vehicle"),  # excluded property (is_a by P31)
        S(8, "syn:wheel", "P131", "syn:earth"),  # blocked hub
        S(9, "syn:earth", "P131", "syn:steel"),
        S(10, "syn:x", "P361", "syn:x"),  # self-loop: never indexed
        S(11, "syn:car", "syn:unmapped", "syn:steel"),  # unmapped property: never indexed
    ]
    # a hub: syn:hub has many neighbours
    sts += [S(100 + i, f"syn:leaf{i}", "P131", "syn:hub") for i in range(12)]
    sts += [S(200, "syn:wheel", "P131", "syn:hub"), S(201, "syn:steel", "P131", "syn:hub")]
    claims = [
        C(1, "syn:drought", "syn:harvest", "has_effect", "cause", "B", "T1", "syn.ds"),
        C(2, "syn:harvest", "syn:price", "has_cause", "cause", "D", "T3", "wikidata"),
        C(3, "syn:same", "syn:same", "has_cause", "cause", "A", "T1", "syn.ds"),  # self-loop dropped
    ]
    ests = [
        E(1, "pub:a", "variable", "energy.price", "variable", "prices.cpi", ("FRA",), "reported", "syn.lit"),
        E(2, "pub:b", "variable", "energy.price", "variable", "prices.cpi", ("FRA", "DEU"), "not_reported", "syn.lit"),
        E(3, "pub:c", "event_type", "earthquake", "variable", "prices.cpi", ("WLD",), "reported", "syn.lit"),
    ]
    return ix.build_index(sts, claims, ests, _reg())


def _ids(idx: ix.PathIndex, p: se.FoundPath) -> list[str]:
    return [idx.node_ids[n] for n in p.nodes]


def test_index_counts_and_virtual_nodes() -> None:
    idx = _graph()
    assert idx.meta["statements"] == 9 + 14  # self-loop and unmapped property not indexed
    assert idx.meta["causal_claims"] == 2
    assert idx.meta["effect_estimates"] == 3
    # an effect estimated for one country is its own node, bridged to the country's QID
    assert idx.idx("effvar:prices.cpi@FRA") is not None
    assert idx.meta["estimate_scopes"] == 1
    assert idx.idx("effvar:prices.cpi") is not None  # two-country scope and WLD stay generic
    # declared bridges: variable -> atlas concept, event type -> verified class
    assert {(b.a, b.b) for b in idx.bridges} == {
        ("effvar:prices.cpi", "atlas:imf.cpi"),
        ("evtype:earthquake", "syn:earthquake_class"),
    }
    assert ix.split_virtual("effvar:prices.cpi@FRA") == ("variable", "prices.cpi", "FRA")
    assert ix.split_virtual("Q142") is None
    assert bool(idx.blocked[idx.idx("syn:earth")])
    assert idx.penalty[idx.idx("syn:hub")] > 0  # degree above the threshold


def test_shortest_path_reads_families_in_path_order() -> None:
    idx = _graph()
    paths, st = se.find_paths(idx, idx.idx("syn:wheel"), idx.idx("syn:firm"), max_hops=3, k=3)
    assert paths and _ids(idx, paths[0]) == ["syn:wheel", "syn:car", "syn:firm"]
    h0, h1 = paths[0].hops
    assert h0.rec_fwd and idx.families[idx.e_fam_fwd[h0.edge]] == "part_of"
    assert not h1.rec_fwd and idx.families[idx.e_fam_rev[h1.edge]] == "produced_by"  # car is produced by firm
    assert not st.truncated


def test_family_filter_is_directional() -> None:
    idx = _graph()
    part_of = se.EdgeFilter(se.family_filter(idx, ["part_of"]))
    assert se.find_paths(idx, idx.idx("syn:wheel"), idx.idx("syn:fleet"), flt=part_of)[0]
    # read from the fleet, the same statements are "has part": not allowed by part_of
    assert not se.find_paths(idx, idx.idx("syn:fleet"), idx.idx("syn:wheel"), flt=part_of)[0]
    has_part = se.EdgeFilter(se.family_filter(idx, ["has_part"]))
    assert se.find_paths(idx, idx.idx("syn:fleet"), idx.idx("syn:wheel"), flt=has_part)[0]
    with pytest.raises(se.PathSearchError):
        se.family_filter(idx, ["causes"])


def test_excluded_property_only_when_its_family_is_named() -> None:
    idx = _graph()
    car, vehicle = idx.idx("syn:car"), idx.idx("syn:vehicle")
    assert not se.find_paths(idx, car, vehicle)[0]
    assert se.find_paths(idx, car, vehicle, flt=se.EdgeFilter(se.family_filter(idx, ["is_a"])))[0]


def test_blocked_hub_is_never_intermediate_but_can_be_an_end() -> None:
    idx = _graph()
    paths, _ = se.find_paths(idx, idx.idx("syn:wheel"), idx.idx("syn:steel"), max_hops=2)
    assert all("syn:earth" not in _ids(idx, p) for p in paths)
    paths, _ = se.find_paths(idx, idx.idx("syn:wheel"), idx.idx("syn:earth"), max_hops=1)
    assert paths and _ids(idx, paths[0]) == ["syn:wheel", "syn:earth"]


def test_hub_over_limit_is_not_expanded_and_reported() -> None:
    idx = _graph()
    paths, st = se.find_paths(idx, idx.idx("syn:leaf0"), idx.idx("syn:firm"), max_hops=4, hub_limit=5)
    assert idx.idx("syn:hub") in st.hubs_not_expanded
    assert all(_ids(idx, p)[1:-1].count("syn:hub") == 0 or len(p.hops) <= 2 for p in paths)


def test_causal_edges_only_with_include_causal_and_good_grades_first() -> None:
    idx = _graph()
    d, p = idx.idx("syn:drought"), idx.idx("syn:price")
    assert not se.find_paths(idx, d, p)[0]
    paths, _ = se.find_paths(idx, d, p, flt=se.EdgeFilter(include_causal=True))
    assert _ids(idx, paths[0]) == ["syn:drought", "syn:harvest", "syn:price"]
    h0, h1 = paths[0].hops
    assert h0.rec_fwd and h1.rec_fwd  # cause -> effect in path order
    assert h0.cost < h1.cost  # grade B cheaper than grade D
    # against the cause -> effect direction costs more
    back, _ = se.find_paths(idx, p, d, flt=se.EdgeFilter(include_causal=True))
    assert back[0].cost > paths[0].cost


def test_estimate_scope_reaches_the_country() -> None:
    idx = _graph()
    flt = se.EdgeFilter(include_causal=True)
    paths, _ = se.find_paths(idx, idx.idx("effvar:energy.price"), idx.idx("syn:firm"), max_hops=4, flt=flt)
    assert paths and _ids(idx, paths[0]) == ["effvar:energy.price", "effvar:prices.cpi@FRA", "syn:france", "syn:firm"]
    kinds = [ix.EDGE_KINDS[idx.e_kind[h.edge]] for h in paths[0].hops]
    assert kinds == ["effect_estimate", "estimate_scope", "statement"]
    paths, _ = se.find_paths(idx, idx.idx("evtype:earthquake"), idx.idx("atlas:imf.cpi"), flt=flt)
    assert [ix.EDGE_KINDS[idx.e_kind[h.edge]] for h in paths[0].hops] == ["effect_estimate", "declared_bridge"]


def test_variants_are_counted_not_listed() -> None:
    sts = [S(i, "syn:a", "P131", f"syn:m{i}") for i in range(1, 6)] + [
        S(10 + i, f"syn:m{i}", "P131", "syn:b") for i in range(1, 6)
    ]
    idx = ix.build_index(sts, [], [], _reg())
    paths, _ = se.find_paths(idx, idx.idx("syn:a"), idx.idx("syn:b"), max_hops=2, k=5)
    assert len(paths) == 1 and paths[0].variants == 4
    assert len(paths[0].variant_examples) == 4


def test_paths_are_simple_and_bounded() -> None:
    idx = _graph()
    for p in se.find_paths(idx, idx.idx("syn:wheel"), idx.idx("syn:steel"), max_hops=5, k=10)[0]:
        ids = _ids(idx, p)
        assert len(ids) == len(set(ids)) and len(p.hops) <= 5
    with pytest.raises(se.PathSearchError):
        se.find_paths(idx, 0, 0)
    with pytest.raises(se.PathSearchError):
        se.find_paths(idx, 0, 1, max_hops=7)


def test_origin_filter() -> None:
    sts = [S(1, "syn:a", "P131", "syn:b", fixture=True), S(2, "syn:b", "P131", "syn:c")]
    idx = ix.build_index(sts, [], [], _reg())
    a, c = idx.idx("syn:a"), idx.idx("syn:c")
    assert se.find_paths(idx, a, c)[0]
    assert not se.find_paths(idx, a, c, flt=se.EdgeFilter(origin="live"))[0]


def test_neighbourhood_round_robin_over_groups() -> None:
    idx = _graph()
    ents, totals = se.neighbourhood(idx, idx.idx("syn:car"), flt=se.EdgeFilter(), limit=10)
    assert totals == {"part_of": 1, "has_part": 1, "produced_by": 1}  # P31 excluded by default
    assert {e.group for e in ents} == set(totals)
    ents, totals = se.neighbourhood(idx, idx.idx("syn:hub"), limit=3)
    assert len(ents) == 3 and totals["located_in"] == 14


def test_save_load_roundtrip(tmp_path: Path) -> None:
    idx = _graph()
    idx.meta["data_key"] = "k1"
    p = ix.save(idx, tmp_path)
    back = ix.load(p)
    assert back.node_ids == idx.node_ids and back.bridges == idx.bridges and back.scopes == idx.scopes
    assert np.array_equal(back.adj_edge, idx.adj_edge) and np.allclose(back.penalty, idx.penalty)
    a, b = back.idx("syn:wheel"), back.idx("syn:firm")
    assert _ids(back, se.find_paths(back, a, b)[0][0]) == ["syn:wheel", "syn:car", "syn:firm"]
    idx.meta["data_key"] = "k2"
    ix.save(idx, tmp_path)
    assert sorted(x.name for x in tmp_path.iterdir()) == ["index-k2.npz"]  # older cache removed


def test_holder_serves_installed_index_and_reports_status() -> None:
    from reva.v8.paths.service import IndexHolder

    h = IndexHolder()
    assert h.status()["ready"] is False
    idx = _graph()
    idx.meta["data_key"] = "k"
    h.set(idx)
    st = h.status()
    assert st["ready"] and st["index"]["nodes"] == idx.n_nodes
