"""Derivations on small synthetic inputs (logic only; ids prefixed ``syn:``, QIDs ``Q9000xx`` are
placeholders, not claims about the world). Real Wikidata statements: test_links_fixture.py."""

from __future__ import annotations

from reva.links.derive import (
    KgIndex,
    assert_all_evidenced,
    derive_atlas_dyadic,
    derive_atlas_shared_value,
    derive_co_membership,
    derive_co_signatory,
    derive_colonial,
    derive_conflict_co_participants,
    derive_conflict_sides,
    derive_former_colony,
    derive_language_family,
    derive_shared_object,
    derive_succession,
    derive_symmetric,
    polities,
)
from reva.links.model import AtlasRowRef, PartialDate, Stmt, Temporal

A, B, C, D_ = "Q900001", "Q900002", "Q900003", "Q900004"
ORG, ALLY, LANG1, LANG2, FAM, COLONY, POWER, TREATY, WAR = (
    "Q900010",
    "Q900011",
    "Q900020",
    "Q900021",
    "Q900022",
    "Q900030",
    "Q900031",
    "Q900040",
    "Q900050",
)
_n = iter(range(1, 10_000))


def st(
    subject: str,
    pid: str,
    obj: str | None = None,
    *,
    value: str | None = None,
    start=None,
    end=None,
    point=None,
    value_date=None,
    qualifiers=None,
) -> Stmt:
    return Stmt(
        statement_id=f"syn:{next(_n)}",
        subject=subject,
        pid=pid,
        object=obj,
        value=value,
        value_date=PartialDate.parse(value_date) if value_date is not None else None,
        start=PartialDate.parse(start),
        end=PartialDate.parse(end),
        point=PartialDate.parse(point),
        qualifiers=qualifiers or {},
    )


COUNTRY = "Q6256"
BASE = [st(x, "P31", COUNTRY) for x in (A, B, C, D_)]


def test_co_membership_uses_the_overlapping_interval() -> None:
    idx = KgIndex(
        [
            *BASE,
            st(A, "P463", ORG, start=1949),
            st(B, "P463", ORG, start=1955, end=1990),
            st(C, "P463", ORG, start=1995),
            st(ALLY, "P31", "Q1127126"),
            st(A, "P463", ALLY, start=1950),
            st(D_, "P463", ALLY, start=1960, end=1970),
        ]
    )
    co, al = derive_co_membership(idx, polities(idx))
    pairs = {(lk.subject, lk.object): lk for lk in co.links}
    assert pairs[(A, B)].temporal == Temporal.of(PartialDate(1955), PartialDate(1990))
    assert pairs[(A, C)].temporal == Temporal.of(PartialDate(1995), None)
    assert (B, C) not in pairs  # 1955–1990 vs since 1995: disjoint, no link
    assert co.skipped["disjoint_periods"] == 1
    (al_link,) = al.links
    assert (al_link.subject, al_link.object, al_link.via) == (A, D_, (ALLY,))
    assert al_link.temporal == Temporal.of(PartialDate(1960), PartialDate(1970))
    assert {e.pid for e in al_link.evidence} == {"P463", "P31"}  # the alliance classification is cited
    assert_all_evidenced(co.links + al.links)


def test_hub_organisations_not_expanded() -> None:
    idx = KgIndex([*BASE, *(st(x, "P463", ORG) for x in (A, B, C))])
    co, _ = derive_co_membership(idx, polities(idx), max_members=2)
    assert co.links == [] and co.skipped["hub_object_not_expanded"] == 1


def test_shared_official_language_and_focus() -> None:
    idx = KgIndex([*BASE, st(A, "P37", LANG1), st(B, "P37", LANG1), st(C, "P37", LANG1)])
    d = derive_shared_object(idx, "P37", "shared_official_language", polities(idx))
    assert len(d.links) == 3 and all(lk.temporal.kind == "undated" for lk in d.links)
    assert all(lk.basis["temporal_basis"] == "undated" for lk in d.links)
    f = derive_shared_object(idx, "P37", "shared_official_language", polities(idx), focus={A})
    assert {(lk.subject, lk.object) for lk in f.links} == {(A, B), (A, C)}


def test_symmetric_property_merges_both_directions() -> None:
    idx = KgIndex([*BASE, st(A, "P47", B), st(B, "P47", A), st(A, "P47", "Q900099")])
    d = derive_symmetric(idx, "P47", "border_wikidata", polities(idx))
    (lk,) = d.links
    assert (lk.subject, lk.object) == (A, B) and len(lk.evidence) == 2
    assert d.skipped["endpoint_not_a_polity"] == 1


def test_language_family_nearest_common_ancestor() -> None:
    idx = KgIndex(
        [
            *BASE,
            st(A, "P37", LANG1),
            st(B, "P37", LANG2),
            st(LANG1, "P279", FAM),
            st(LANG2, "P279", FAM),
            st(FAM, "P279", "Q34770"),
        ]
    )
    d = derive_language_family(idx, polities(idx))
    (lk,) = d.links
    assert lk.via == (FAM,) and lk.basis["languages"] == [LANG1, LANG2]
    assert len(lk.evidence) == 4  # two P37 + two P279


def test_colonial_ties_and_former_colony() -> None:
    idx = KgIndex(
        [
            st(POWER, "P31", "Q3024240"),
            st(COLONY, "P31", "Q900060"),
            st("Q900060", "P279", "Q133156"),  # a subclass of colony
            st(COLONY, "P17", POWER, start=1530, end=1815),
            st(B, "P31", COUNTRY),
            st(B, "P1365", "Q900070"),
            st("Q900070", "P31", "Q3024240"),
            st("Q900070", "P1365", COLONY),
        ]
    )
    pol = polities(idx)
    col = derive_colonial(idx, pol)
    (c,) = col.links
    assert (c.subject, c.object, c.directed) == (COLONY, POWER, True)
    assert c.temporal == Temporal.of(PartialDate(1530), PartialDate(1815))
    assert {e.pid for e in c.evidence} == {"P31", "P279", "P17"}
    former = derive_former_colony(idx, pol, col.links)
    (f,) = [lk for lk in former.links if lk.subject == B]
    assert f.object == POWER and f.via == ("Q900070", COLONY) and f.temporal == c.temporal
    succ = derive_succession(idx, pol)
    assert {(lk.subject, lk.object) for lk in succ.links} == {(B, "Q900070"), ("Q900070", COLONY)}


def test_colony_dates_fall_back_to_inception_and_dissolution() -> None:
    idx = KgIndex(
        [
            st(POWER, "P31", "Q3024240"),
            st(COLONY, "P31", "Q133156"),
            st(COLONY, "P361", POWER),
            st(COLONY, "P571", value="+1534", value_date="+1534-00-00T00:00:00Z"),
            st(COLONY, "P576", value="+1815", value_date="+1815-12-16T00:00:00Z"),
        ]
    )
    (c,) = derive_colonial(idx, polities(idx)).links
    assert c.temporal.start == PartialDate(1534) and c.temporal.end.year == 1815
    assert c.basis["temporal_basis"] == "entity_dates"


def test_co_signatory_dates() -> None:
    idx = KgIndex(
        [st(TREATY, "P1891", A, point="+1494-07-02T00:00:00Z"), st(TREATY, "P1891", B, point="+1494-09-05T00:00:00Z")]
    )
    (lk,) = derive_co_signatory(idx).links
    assert lk.via == (TREATY,) and lk.temporal.kind == "interval"
    assert (lk.temporal.start.month, lk.temporal.end.month) == (7, 9)


def test_conflict_participants_never_claim_sides() -> None:
    idx = KgIndex(
        [
            st(WAR, "P710", A),
            st(B, "P1344", WAR),
            st(C, "P607", WAR),
            st(WAR, "P580", value="x", value_date="+1956-10-29T00:00:00Z"),
            st(WAR, "P582", value="y", value_date="+1957-03-01T00:00:00Z"),
        ]
    )
    d = derive_conflict_co_participants(idx)
    assert len(d.links) == 3
    assert all(lk.basis["sides"] == "not_stated_by_source" for lk in d.links)
    assert all(lk.temporal == Temporal.of(PartialDate(1956, 10, 29, 11), PartialDate(1957, 3, 1, 11)) for lk in d.links)
    same, opp = derive_conflict_sides(idx)
    assert same.links == [] and opp.links == []  # Wikidata gives no sides
    sided = KgIndex(
        [
            st(WAR, "P710", A, qualifiers={"ucdp:side": ("A",)}),
            st(WAR, "P710", B, qualifiers={"ucdp:side": ("B",)}),
            st(WAR, "P710", C, qualifiers={"ucdp:side": ("A",)}),
        ]
    )
    same, opp = derive_conflict_sides(sided)
    assert {(lk.subject, lk.object) for lk in same.links} == {(A, C)}
    assert {(lk.subject, lk.object) for lk in opp.links} == {(A, B), (B, C)}


def _row(iso3: str, indicator: str, value: str | None, **kw) -> AtlasRowRef:
    return AtlasRowRef.from_mapping(
        {
            "source": "imf",
            "dataset": "dots",
            "indicator": indicator,
            "iso3": iso3,
            "period_start": "2022-01-01",
            "period_end": "2023-01-01",
            "value": value,
            "value_text": kw.get("value_text"),
            "unit": "USD",
            "raw_sha256": "c" * 64,
            "selector": f'{{"row":{next(_n)}}}',
            "flags": kw.get("flags", "{}"),
            "concept_id": kw.get("concept_id"),
        }
    )


def test_atlas_dyadic_never_fills_missing() -> None:
    rows = [
        _row("BRA", "X|counterpart=PRT", "1234.5"),
        _row("BRA", "X|counterpart=ARG", None),
        _row("BRA", "X|counterpart=CHL", "0"),
        _row("BRA", "X", "5"),
        _row("BRA", "X|counterpart=W00", "9", flags='{"aggregate": true}'),
        _row("BRA", "X|counterpart=ZZZ", "7"),
    ]
    d = derive_atlas_dyadic(rows, "bilateral_trade", directed=True, iso3_to_qid={"BRA": "Q155", "PRT": "Q45"})
    by_obj = {lk.object: lk for lk in d.links}
    assert by_obj["Q45"].weight == "1234.5" and by_obj["Q45"].temporal == Temporal("snapshot", year=2022)
    assert by_obj["iso3:ZZZ"].basis["qid_unmapped"] == ["ZZZ"]  # kept, labelled, not dropped
    assert d.skipped["missing_value"] == 1 and d.skipped["reported_zero"] == 1
    assert d.skipped["no_counterpart"] == 1 and d.skipped["counterpart_not_a_country"] == 1
    assert len(d.links) == 2
    assert all(e.kind == "atlas_row" and e.raw_sha256 and e.selector for lk in d.links for e in lk.evidence)


def test_atlas_shared_value() -> None:
    rows = [
        _row(x, "LEGAL", None, value_text=v, concept_id="law.legal_system_family")
        for x, v in (("FRA", "Civil law"), ("BRA", "civil law"), ("GBR", "Common law"))
    ]
    d = derive_atlas_shared_value(rows, "shared_legal_family", iso3_to_qid={"FRA": "Q142", "BRA": "Q155"})
    (lk,) = d.links
    assert {lk.subject, lk.object} == {"Q142", "Q155"} and lk.basis["reported"] == {
        "FRA": "Civil law",
        "BRA": "civil law",
    }
