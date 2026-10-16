"""``co_party`` (agent LINKWIRE over A-TREATIES): states party to the same UNTC treaty over overlapping periods;
near-universal treaties damped (not expanded, recorded); untc:party / untc:former_party are path hops."""

from __future__ import annotations

from reva.links.derive import KgIndex, assert_all_evidenced, derive_all, derive_co_party
from reva.links.model import PartialDate, Stmt
from reva.links.paths import PathQuery, build_graph, find_paths
from reva.links.taxonomy import kg_allowlist, load_taxonomy, validate_taxonomy

T = "untc:treaty:XXVII-7"  # Kyoto-like, few parties here


def d(y: int, m: int = 1, day: int = 1) -> PartialDate:
    return PartialDate(y, m, day, 11)


def party(
    n: int, treaty: str, state: str, start: PartialDate, end: PartialDate | None = None, pid: str = "untc:party"
) -> Stmt:
    return Stmt(
        f"untc:{treaty}:{n}", treaty, pid, state, start=start, end=end, source_key="untc.mtdsg", origin="test_fixture"
    )


def test_co_party_overlap_withdrawal_and_exclusions() -> None:
    sts = [
        party(1, T, "Q155", d(2002, 8, 23)),
        party(2, T, "Q45", d(2002, 5, 31)),
        party(3, T, "Q16", d(2002, 12, 17), end=d(2012, 12, 15)),  # withdrawal with a date of effect
        party(4, T, "untc:participant:european-union", d(2002, 5, 31)),
        party(5, T, "iso3:XKX", d(2019, 1, 1)),
        party(6, T, "Q30", d(2001, 1, 1), pid="untc:former_party"),
    ]
    der = derive_co_party(KgIndex(sts))
    pairs = {(lk.subject, lk.object): lk for lk in der.links}
    assert set(pairs) == {("Q155", "Q45"), ("Q155", "Q16"), ("Q16", "Q45"), ("Q155", "iso3:XKX"), ("Q45", "iso3:XKX")}
    bp = pairs[("Q155", "Q45")]
    assert bp.via == (T,) and not bp.directed and bp.temporal.first_year == 2002 and bp.temporal.end is None
    assert {e.statement_id for e in bp.evidence} == {f"untc:{T}:1", f"untc:{T}:2"}
    assert bp.basis["property"] == "untc:party" and bp.basis["parties"] == 4  # states only
    assert pairs[("Q155", "Q16")].temporal.last_year == 2012
    # Canada withdrew (effect 2012) before Kosovo-like iso3:XKX joined (2019): disjoint, no link, counted
    assert ("Q16", "iso3:XKX") not in pairs and der.skipped["disjoint_periods"] == 1
    assert der.skipped["non_state_participant"] == 1 and der.skipped["former_party_end_not_stated"] == 1
    assert_all_evidenced(der.links)


def test_near_universal_treaties_are_not_expanded_and_recorded() -> None:
    big = "untc:treaty:IV-11"
    sts = [party(i, big, f"Q{1000 + i}", d(1990)) for i in range(151)]
    sts += [party(900, T, "Q155", d(2002)), party(901, T, "Q45", d(2002))]
    der = derive_co_party(KgIndex(sts))
    assert len(der.links) == 1 and der.skipped["near_universal_treaty_not_expanded"] == 1
    assert {"reason": "near_universal_treaty_not_expanded", "via": big, "parties": 151, "bound": 150} in der.samples
    assert len(derive_co_party(KgIndex(sts), max_parties=200).links) == 1 + 151 * 150 // 2
    # focus (country view) keeps only links touching the country
    out = derive_all(sts, focus={"Q155"})
    assert [(lk.subject, lk.object) for lk in out.by_type["co_party"].links] == [("Q155", "Q45")]


def test_taxonomy_and_path_hops() -> None:
    tax = load_taxonomy()
    lt = tax.link("co_party")
    assert lt.scope == "country_country" and lt.status == "derivable" and lt.sources == ("untc",)
    assert {"untc:party", "untc:former_party"} <= set(tax.edge_types) and {
        "untc:party",
        "untc:former_party",
    } <= kg_allowlist()
    import yaml

    from reva.links.taxonomy import LINK_TYPES_FILE

    assert validate_taxonomy(yaml.safe_load(LINK_TYPES_FILE.read_text()), kg_properties=kg_allowlist()) == []
    sts = [party(1, T, "Q155", d(2002)), party(2, T, "Q45", d(2002))]
    g = build_graph(sts, labels={T: {"en": "Kyoto Protocol"}, "Q155": {"en": "Brazil"}, "Q45": {"en": "Portugal"}})
    res = find_paths(g, PathQuery("Q155", "Q45", kinds=frozenset({"untc:party"}), max_hops=2, k=1))
    assert res.found and [h.edge.type_id for h in res.paths[0].hops] == ["untc:party", "untc:party"]
    assert "party to (UN Treaty Collection)" in res.paths[0].text["en"][0]
