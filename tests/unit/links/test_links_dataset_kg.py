"""LINKS2 dataset-statement derivations (reva.links.dataset_kg): CShapes borders and colonial rule, UCDP sides,
Cliopatria overlay, with code entities mapped through dated geopol:corresponds_to_iso3 statements."""

from __future__ import annotations

from reva.links.dataset_kg import derive_dataset_links
from reva.links.derive import assert_all_evidenced
from reva.links.model import PartialDate, Stmt


def corr(node: str, iso3: str, a: int, b: int) -> Stmt:
    return Stmt(f"c:{node}:{iso3}:{a}", node, "geopol:corresponds_to_iso3", value=iso3, start=PartialDate(a),
                end=PartialDate(b), source_key="cshapes.cran")  # fmt: skip


def d(y: int, m: int = 1, day: int = 1) -> PartialDate:
    return PartialDate(y, m, day, 11)


def node_of(iso3: str) -> str:
    return {"FRA": "Q142"}.get(iso3) or f"iso3:{iso3}"


QID_ISO3 = {"Q142": "FRA"}


def test_cshapes_colonial_rule_names_the_later_state_and_cites_every_statement() -> None:
    sts = [
        Stmt("r1", "cshapes:gw:615", "cshapes:ruled_by", "gw:220", start=d(1886), end=d(1962, 7, 4),
             qualifiers={"status": ("colony",)}, source_key="cshapes.cran"),  # fmt: skip
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
        Stmt("b1", "cshapes:gw:615", "cshapes:shares_border_with", "cshapes:gw:600", start=d(1956), end=d(2019, 12, 31)),
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
        Stmt("p1", c, "ucdp:primary_party", "gw:2", start=PartialDate(2001), end=PartialDate(2014),
             qualifiers={"side": ("a",), "years": tuple(str(y) for y in range(2001, 2015))}),  # fmt: skip
        Stmt("p2", c, "ucdp:secondary_party", "gw:200", start=PartialDate(2001), end=PartialDate(2014),
             qualifiers={"side": ("a",), "years": ("2001", "2002")}),  # fmt: skip
        Stmt("p3", c, "ucdp:primary_party", "gw:700", start=PartialDate(2001), end=PartialDate(2001),
             qualifiers={"side": ("b",), "years": ("2001",)}),  # fmt: skip
        corr("gw:2", "USA", 1816, 2025),
        corr("gw:200", "GBR", 1816, 2025),
        corr("gw:700", "AFG", 1919, 2025),
    ]
    out = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)
    same = {(lk.subject, lk.object) for lk in out["conflict_same_side"].links}
    opp = {(lk.subject, lk.object, lk.temporal.first_year, lk.temporal.last_year) for lk in out["conflict_opposing_sides"].links}
    assert same == {("iso3:GBR", "iso3:USA")}
    assert ("iso3:AFG", "iso3:USA", 2001, 2001) in opp
    lk = next(iter(out["conflict_same_side"].links))
    assert lk.via == (c,) and lk.basis["common_years"] == ["2001", "2002"]


def test_shared_polity_territory_and_hub_rule() -> None:
    sts = [
        Stmt(f"o{i}", "cliopatria:ottoman", "geo:covered_territory_of", f"iso3:{x}", start=PartialDate(1550), end=PartialDate(1830))
        for i, x in enumerate(("DZA", "TUN"))
    ] + [Stmt(f"h{i}", "cliopatria:huge", "geo:covered_territory_of", f"iso3:X{i:02d}") for i in range(70)]
    out = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)["shared_polity_territory"]
    assert [(lk.subject, lk.object, lk.via) for lk in out.links] == [("iso3:DZA", "iso3:TUN", ("cliopatria:ottoman",))]
    assert out.skipped["hub_subject_not_expanded"] == 1
