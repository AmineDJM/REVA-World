"""LINKS2 dataset-statement derivations (reva.links.dataset_kg): CShapes borders and colonial rule, UCDP sides,
Cliopatria overlay, with code entities mapped through dated geopol:corresponds_to_iso3 statements."""

from __future__ import annotations

from reva.links.dataset_kg import derive_dataset_links
from reva.links.derive import assert_all_evidenced
from reva.links.model import PartialDate, Stmt


def corr(node: str, iso3: str, a: int, b: int) -> Stmt:
    return Stmt(
        f"c:{node}:{iso3}:{a}",
        node,
        "geopol:corresponds_to_iso3",
        value=iso3,
        start=PartialDate(a),
        end=PartialDate(b),
        source_key="cshapes.cran",
    )


def d(y: int, m: int = 1, day: int = 1) -> PartialDate:
    return PartialDate(y, m, day, 11)


def node_of(iso3: str) -> str:
    return {"FRA": "Q142"}.get(iso3) or f"iso3:{iso3}"


QID_ISO3 = {"Q142": "FRA"}


def test_cshapes_colonial_rule_names_the_later_state_and_cites_every_statement() -> None:
    sts = [
        Stmt(
            "r1",
            "cshapes:gw:615",
            "cshapes:ruled_by",
            "gw:220",
            start=d(1886),
            end=d(1962, 7, 4),
            qualifiers={"status": ("colony",)},
            source_key="cshapes.cran",
        ),
        corr("cshapes:gw:615", "DZA", 1962, 2019),
        corr("gw:220", "FRA", 1816, 2019),
    ]
    out = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)
    (lk,) = out["colonial_rule_cshapes"].links
    assert (lk.subject, lk.object, lk.directed) == ("iso3:DZA", "Q142", True)
    assert lk.basis["status"] == "colony" and lk.basis["territory_mapping"] == "later_state"
    assert {e.statement_id for e in lk.evidence} == {"r1", "c:cshapes:gw:615:DZA:1962", "c:gw:220:FRA:1816"}
    assert lk.temporal.first_year == 1886 and lk.temporal.last_year == 1962
    assert_all_evidenced(lk for d_ in out.values() for lk in d_.links)


def test_cshapes_border_holds_only_while_both_are_states_and_keeps_unmapped_territories() -> None:
    sts = [
        Stmt(
            "b1", "cshapes:gw:615", "cshapes:shares_border_with", "cshapes:gw:600", start=d(1956), end=d(2019, 12, 31)
        ),
        corr("cshapes:gw:615", "DZA", 1962, 2019),
        corr("cshapes:gw:600", "MAR", 1956, 2019),
        Stmt("b2", "cshapes:gw:999", "cshapes:shares_border_with", "cshapes:gw:600", start=d(1900), end=d(1910)),
    ]
    out = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)["land_border_cshapes"]
    got = {(lk.subject, lk.object, lk.temporal.first_year, lk.temporal.last_year) for lk in out.links}
    assert ("iso3:DZA", "iso3:MAR", 1962, 2019) in got
    assert ("cshapes:gw:600", "cshapes:gw:999", 1900, 1910) in got  # MAR's correspondence starts 1956
    assert out.skipped["territory_without_country"] == 1


def test_ucdp_sides_same_and_opposing_over_overlapping_years() -> None:
    c = "ucdp:conflict:200"
    sts = [
        Stmt(
            "p1",
            c,
            "ucdp:primary_party",
            "gw:2",
            start=PartialDate(2001),
            end=PartialDate(2014),
            qualifiers={"side": ("a",), "years": tuple(str(y) for y in range(2001, 2015))},
        ),
        Stmt(
            "p2",
            c,
            "ucdp:secondary_party",
            "gw:200",
            start=PartialDate(2001),
            end=PartialDate(2014),
            qualifiers={"side": ("a",), "years": ("2001", "2002")},
        ),
        Stmt(
            "p3",
            c,
            "ucdp:primary_party",
            "gw:700",
            start=PartialDate(2001),
            end=PartialDate(2001),
            qualifiers={"side": ("b",), "years": ("2001",)},
        ),
        corr("gw:2", "USA", 1816, 2025),
        corr("gw:200", "GBR", 1816, 2025),
        corr("gw:700", "AFG", 1919, 2025),
    ]
    out = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)
    same = {(lk.subject, lk.object) for lk in out["conflict_same_side"].links}
    opp = {
        (lk.subject, lk.object, lk.temporal.first_year, lk.temporal.last_year)
        for lk in out["conflict_opposing_sides"].links
    }
    assert same == {("iso3:GBR", "iso3:USA")}
    assert ("iso3:AFG", "iso3:USA", 2001, 2001) in opp
    lk = next(iter(out["conflict_same_side"].links))
    assert lk.via == (c,) and lk.basis["common_years"] == ["2001", "2002"]


def test_shared_polity_territory_and_hub_rule() -> None:
    sts = [
        Stmt(
            f"o{i}",
            "cliopatria:ottoman",
            "geo:covered_territory_of",
            f"iso3:{x}",
            start=PartialDate(1550),
            end=PartialDate(1830),
        )
        for i, x in enumerate(("DZA", "TUN"))
    ] + [Stmt(f"h{i}", "cliopatria:huge", "geo:covered_territory_of", f"iso3:X{i:02d}") for i in range(70)]
    from reva.links.derive import derive_all

    out = derive_all(sts).by_type["shared_polity_territory"]
    assert [(lk.subject, lk.object, lk.via) for lk in out.links] == [("iso3:DZA", "iso3:TUN", ("cliopatria:ottoman",))]
    assert out.skipped["hub_subject_not_expanded"] == 1


def test_peacekeeping_contributor_to_host_via_the_mission() -> None:
    m = "unpk:mission:unifil"
    sts = [
        Stmt(
            "k1",
            "Q142",
            "unpk:contributed_personnel_to",
            m,
            start=d(2010, 1, 31),
            end=d(2010, 12, 31),
            qualifiers={"year": ("2010",), "max_uniformed_personnel_reported": ("1500",)},
            source_key="un.psdata",
        ),
        Stmt(
            "k2",
            m,
            "unpk:mission_country",
            "iso3:LBN",
            start=d(1990, 11, 30),
            end=d(2009, 12, 31),
            source_key="un.psdata",
        ),
        Stmt(
            "k3", m, "unpk:mission_country", "iso3:ISR", start=d(2010, 3, 1), end=d(2026, 7, 31), source_key="un.psdata"
        ),
    ]
    out = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)["peacekeeping_contributor_to_host"]
    assert [(lk.subject, lk.object, lk.via) for lk in out.links] == [("Q142", "iso3:ISR", (m,))]
    assert out.skipped["disjoint_periods"] == 1 and out.links[0].basis["max_uniformed_personnel_reported"] == "1500"
    assert out.links[0].weight is None


def test_glottolog_genealogy_and_spoken_in_with_hub_rule() -> None:
    from reva.links.derive import derive_all

    sts = [
        Stmt("o1", "Q155", "P37", "Q5146"),  # Brazil: Portuguese
        Stmt("o2", "Q29", "P37", "Q1321"),  # Spain: Spanish
        Stmt("x1", "glottolog:port1283", "glottolog:wikidata_item", "Q5146", source_key="wikidata.wdqs"),
        Stmt("x2", "glottolog:stan1288", "glottolog:wikidata_item", "Q1321", source_key="wikidata.wdqs"),
        Stmt("g1", "glottolog:port1283", "glottolog:parent", "glottolog:west2343", source_key="glottolog.cldf"),
        Stmt("g2", "glottolog:stan1288", "glottolog:parent", "glottolog:west2343", source_key="glottolog.cldf"),
        Stmt("s1", "glottolog:port1283", "glottolog:spoken_in", "Q155", source_key="glottolog.cldf"),
        Stmt("s2", "glottolog:port1283", "glottolog:spoken_in", "Q45", source_key="glottolog.cldf"),
    ] + [
        Stmt(f"e{i}", "glottolog:stan1293", "glottolog:spoken_in", f"iso3:X{i:02d}", source_key="glottolog.cldf")
        for i in range(70)
    ]
    out = derive_all(sts, countries={"Q155", "Q29", "Q45"})
    (fam,) = out.by_type["shared_language_family"].links
    assert fam.via == ("glottolog:west2343",) and {e.statement_id for e in fam.evidence} >= {"x1", "x2", "g1", "g2"}
    sp = out.by_type["shared_language_glottolog"]
    assert [(lk.subject, lk.object, lk.via) for lk in sp.links] == [("Q155", "Q45", ("glottolog:port1283",))]
    assert sp.skipped["hub_subject_not_expanded"] == 1  # a language listed in 70 countries is a hub


def test_namespaced_predicates_are_hops_brazil_to_portuguese_empire() -> None:
    from reva.links.paths import PathQuery, build_graph, find_paths

    sts = [
        Stmt(
            "c1",
            "cliopatria:portuguese_empire",
            "geo:covered_territory_of",
            "Q155",
            start=PartialDate(1500),
            end=PartialDate(1822),
            source_key="cliopatria.github",
        ),
        Stmt("c2", "cliopatria:portuguese_empire", "cliopatria:denotes", "Q200464", source_key="cliopatria.github"),
    ]
    res = find_paths(
        build_graph(sts),
        PathQuery("Q155", "Q200464", max_hops=2, kinds=frozenset({"geo:covered_territory_of", "cliopatria:denotes"})),
    )
    assert res.found
    assert [h.edge.type_id for h in res.paths[0].hops] == ["geo:covered_territory_of", "cliopatria:denotes"]
    assert "computed overlay" in res.paths[0].hops[0].text["en"]
