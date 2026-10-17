"""Demonstration on real Wikidata causal statements (CC0, community-edited), loaded through the real path:
captured ``wbgetentities``/EntityData JSON -> WDHARVEST extraction (``tests/unit/causal/fixtures``) -> KGCORE
``load_batch`` -> CAUSCORE ``load_kg_claims`` -> ``grade_claims`` -> graph services.

What Wikidata asserts (revisions in the fixture), not what REVA concludes:
* Great Depression (Q8698) has cause Wall Street crash of 1929 (Q201684), and the crash has effect the Great
  Depression — two unreferenced statements on the two pages: one "Wikidata editors" source group (T4, D);
* 1918–1920 flu pandemic (Q178275) has cause World War I (Q361) — unreferenced (T4, D), time-consistent;
* WWI "has effect" Russian Empire / Ottoman Empire / German Empire / Austria-Hungary, entities founded long
  before 1914: flagged ``inconsistent`` and graded E, shown, not dropped.
"""

from __future__ import annotations

import pytest

from reva.causal import grade_claims, load_kg_claims
from reva.causal import graph as cg
from reva.causal.fixture import FIXTURE_FILE, load_into_kg

pytestmark = pytest.mark.db
GD, CRASH, FLU, WWI, SARAJEVO, RUSSIAN_EMPIRE = "Q8698", "Q201684", "Q178275", "Q361", "Q192050", "Q34266"


@pytest.fixture(scope="module")
def real(db):  # type: ignore[no-untyped-def]
    if not FIXTURE_FILE.exists():
        pytest.skip("causal fixture not captured")
    rep = load_into_kg(db.worker, db.store)
    assert rep.rejected_total == 0, rep.rejected[:5]
    load_kg_claims(db.worker, origin="test_fixture", source_keys=["wikidata.causal_fixture"])
    grade_claims(db.worker, origin="test_fixture")
    with db.api.connect() as conn:
        yield conn


def _by_pair(claims: list[dict]) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for c in claims:
        out.setdefault((c["cause"]["id"], c["effect"]["id"]), []).append(c)
    return out


def test_great_depression_and_the_1929_crash(real) -> None:  # type: ignore[no-untyped-def]
    res = cg.causes(real, GD, origin="test_fixture")
    pair = _by_pair(res["claims"])[(CRASH, GD)]
    # both directions of the source normalise to one (cause, effect): P828 on Q8698 and P1542 on Q201684
    assert sorted(c["source"]["selector"]["property"] for c in pair) == ["P1542", "P828"]
    assert {c["asserted_on"] for c in pair} == {"cause", "effect"}
    for c in pair:
        assert c["source"]["tier"] == "T4" and c["source"]["references"] == []
        assert c["source"]["verification"] == "community_edited_not_verified"
        g = c["grade"]
        assert g["grade"] == "D" and g["temporal_check"] == "consistent"
        # crash starts 1929-10-24 (day precision), the Great Depression 1929-10-29 (start time, day precision)
        assert (g["temporal"]["cause_start"]["from"], g["temporal"]["effect_start"]["from"]) == (
            "1929-10-24",
            "1929-10-29",
        )
        # two unreferenced statements of the same community are one source group, not corroboration
        assert g["corroboration"] == 1 and g["upstream_groups"] == [["community:wikidata"]]
        assert c["wording"].startswith("According to Wikidata (statement ")
        assert "REVA does not establish causation" in c["wording"]
        assert c["wording_fr"].startswith("Selon Wikidata (déclaration ")
    p828 = next(c for c in pair if c["source"]["selector"]["property"] == "P828")
    assert "Great Depression has cause Wall Street crash of 1929" in p828["wording"]
    assert p828["qualifiers"] == {"P3831": ["Q20204892"]}  # the source's "object has role" qualifier, kept


def test_1918_flu_and_world_war_one_as_asserted_by_wikidata(real) -> None:  # type: ignore[no-untyped-def]
    why = cg.why(real, FLU, origin="test_fixture")
    general = next(lane for lane in why["lanes"] if lane["lane"] == "general")
    c = next(x for x in general["claims"] if x["cause"]["id"] == WWI)
    assert c["relation"] == "has_cause" and c["grade"]["grade"] == "D"
    assert c["grade"]["temporal_check"] == "consistent"  # WWI 1914-07-28 < flu 1918-01 (month precision)
    assert c["grade"]["temporal"]["effect_start"] == {
        "from": "1918-01-01",
        "to": "1918-01-31",
        "basis": "P580",
        "precision": 10,
        "times": ["+1918-01-00T00:00:00Z"],
    }
    immediate = next(lane for lane in why["lanes"] if lane["lane"] == "immediate")
    assert [x["cause"]["id"] for x in immediate["claims"]] == ["Q161393"]  # has immediate cause H1N1 (P1478)
    # a composition: Sarajevo assassination -> WWI -> flu, each hop asserted separately by Wikidata
    ch = cg.chain(real, SARAJEVO, FLU, max_hops=4, origin="test_fixture")
    assert ch["found"] and ch["paths"][0]["length"] == 2
    p = ch["paths"][0]
    assert [h["to"]["id"] for h in p["hops"]] == [WWI, FLU] and p["chain_grade"] == "D"
    assert "composition by REVA" in ch["note"] and "weakest hop" in ch["note"]
    # the first hop is asserted twice (P1536 on the assassination, P1478 on WWI): both claims listed
    assert {c["source"]["selector"]["property"] for c in p["hops"][0]["claims"]} == {"P1478", "P1536"}


def test_entity_effects_dated_by_inception_and_import_pointers(real) -> None:  # type: ignore[no-untyped-def]
    """grading.yaml v2. "WWI has effect Russian Empire" (P1542, its only reference: imported from a Wikimedia
    project, P143): T4 — a pointer to another wiki is not a source — and, the Russian Empire being dated by its
    inception (1721) and dissolution (1917), not as an event, the time order is undetermined (not E)."""
    eff = _by_pair(cg.effects(real, WWI, origin="test_fixture", limit=500)["claims"])
    c = eff[(WWI, RUSSIAN_EMPIRE)][0]
    assert c["source"]["tier"] == "T4" and c["grade"]["grade"] == "D"
    assert c["grade"]["temporal_check"] == "undetermined"
    ids = c["grade"]["rule_ids"]
    assert "tier.T4.wikimedia_import_only" in ids and "temporal.entity_dated_by_inception" in ids
    t = c["grade"]["temporal"]
    # Russian Empire inception 1721-10-22 (Julian) is read as 1721-11-02 (proleptic Gregorian); the span ends at
    # its dissolution (P576 1917-09-01 Julian -> 1917-09-14)
    assert t["effect_start"]["from"] == "1721-11-02" and t["effect_time"]["kind"] == "entity_existence"
    assert (t["effect_time"]["from"], t["effect_time"]["to"]) == ("1721-11-02", "1917-09-14")
    assert t["entity_dated_by_inception"] == ["effect"] and "not an event" in t["reason"]
    cons = cg.consistency(real, origin="test_fixture", limit=200)
    flagged = {(x["cause"]["id"], x["effect"]["id"]) for x in cons["temporal_violations"]["sample"]}
    assert (WWI, RUSSIAN_EMPIRE) not in flagged
    # WWI (1914) -> Weimar Republic (inception 1918-11-09): the cause precedes the whole existence -> consistent
    w = eff[(WWI, "Q41304")][0]
    assert (
        w["grade"]["temporal_check"] == "consistent" and w["grade"]["temporal"]["effect_time"]["to"] == "1933-12-31"
    )  # year precision


def test_reload_and_regrade_are_idempotent(real, db) -> None:  # type: ignore[no-untyped-def]
    assert load_into_kg(db.worker, db.store).statements_inserted == 0
    r = load_kg_claims(db.worker, origin="test_fixture", source_keys=["wikidata.causal_fixture"])
    assert (r.inserted, r.retracted, r.unchanged) == (0, 0, r.scanned) and r.scanned >= 30
    g = grade_claims(db.worker, origin="test_fixture")
    assert g.written == 0
