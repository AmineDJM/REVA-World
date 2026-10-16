"""Keyword planner ties (V3 e2e FIX2): broken by the headline/source-order layer when it decides, else every
tied concept is queried (bounded) and answered with its own unit, else reported as an ambiguity — never as
"not covered"."""

from __future__ import annotations

from types import SimpleNamespace

import nlqa_fakes as F

from reva.atlas.catalog import Series
from reva.nlqa.catalog import QaCatalog, build_catalog
from reva.nlqa.compose import compose
from reva.nlqa.executor import Executor
from reva.nlqa.fallback import MAX_TIED_CONCEPTS, headline_tiebreak, keyword_plan, match_concept
from reva.nlqa.plan import validate_plan
from reva.nlqa.ports import Concept

BASE = F.catalog()
ILO_LCU = "ilostat.ear_inee_cur_nb_a.EAR_INEE_CUR_NB|classif1=CUR_TYPE_LCU"
ILO_USD = "ilostat.ear_inee_cur_nb_a.EAR_INEE_CUR_NB|classif1=CUR_TYPE_USD"
OECD_H = "oecd.earnings_mw_curp.SM_WG|unit=NCU|pay_period=H|price_base=V"
MW = {
    "labour.mw.hourly": ("Salaire minimum horaire (monnaie nationale, OCDE)", OECD_H, "EUR per hour", "11.88", 2025),
    "labour.mw.monthly_lcu": ("Salaire minimum mensuel (monnaie nationale)", ILO_LCU, "EUR per month", "1867", 2026),
    "labour.mw.monthly_usd": ("Salaire minimum mensuel (dollars US, OIT)", ILO_USD, "USD per month", "2013.5", 2026),
}
Q = "Quel est le salaire minimum en France ?"


def catalog(ids=tuple(MW)) -> QaCatalog:
    cs = {cid: Concept(cid, {"fr": MW[cid][0]}, unit=MW[cid][2], source_order=(MW[cid][1],)) for cid in ids}
    return QaCatalog(cs, BASE.countries, BASE.properties)


def headline(hid: str, label: str, series: tuple[str, ...] = (), related: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        id=hid,
        labels={"fr": label},
        series=tuple(SimpleNamespace(key=Series.parse(s).key) for s in series),
        related_catalog_concepts=related,
    )


def registry(*hs: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(concepts={h.id: h for h in hs})


MW_HEAD = headline("reva.labour.minimum_wage_monthly_lcu", "Salaire minimum mensuel (monnaie nationale)", (ILO_LCU,))


def run(question: str, cat: QaCatalog, headlines):
    raw = keyword_plan(question, cat, headlines)
    plan = validate_plan(raw, cat, question, planner="keyword_fallback")
    cells = [
        F.cell("src", cid, MW[cid][1], "FRA", MW[cid][4], MW[cid][3], MW[cid][2], F.RAW_ILO, cid)
        for cid in cat.concepts
    ]
    atlas = F.FakeAtlas(cells)
    atlas.order = {cid: c.source_order for cid, c in cat.concepts.items()}
    x = Executor(F.backends(atlas=atlas), cat).run(plan)
    return plan, x, compose(x, question, cat)


def test_headline_breaks_the_tie_by_its_source_order() -> None:
    cat = catalog()
    assert match_concept(Q, cat, None) == (None, sorted(MW))
    assert match_concept(Q, cat, registry(MW_HEAD)) == ("labour.mw.monthly_lcu", [])
    # covered through related_catalog_concepts only
    rel = headline("reva.labour.mw", "Salaire minimum horaire", related=("labour.mw.hourly",))
    assert headline_tiebreak(Q, sorted(MW), cat, registry(rel)) == ("labour.mw.hourly", "reva.labour.mw")
    # a series earlier in the headline's order wins over a later one
    both = headline("reva.labour.mw2", "Salaire minimum", (ILO_USD, ILO_LCU))
    assert headline_tiebreak(Q, sorted(MW), cat, registry(both))[0] == "labour.mw.monthly_usd"
    # two headline concepts matching the question equally: a genuine ambiguity, nothing chosen
    other = headline("reva.labour.mw_hourly", "Salaire minimum horaire (OCDE)", (OECD_H,))
    assert headline_tiebreak(Q, sorted(MW), cat, registry(MW_HEAD, other)) is None
    # ... unless only one of them matches the question best
    q2 = "Quel est le salaire minimum mensuel en France ?"
    assert headline_tiebreak(q2, sorted(MW), cat, registry(MW_HEAD, other)) == (
        "labour.mw.monthly_lcu",
        "reva.labour.minimum_wage_monthly_lcu",
    )


def test_headline_choice_is_answered_and_the_other_candidates_named() -> None:
    plan, _x, c = run(Q, catalog(), registry(MW_HEAD))
    assert [(o.op, o.args["concept"], o.args["country"]) for o in plan.operations] == [
        ("atlas_value", "labour.mw.monthly_lcu", "FRA")
    ]
    assert plan.ambiguities[0]["resolution"] == "headline" and plan.as_json()["ambiguities"]
    assert "867 EUR per month" in c.text and c.sentences[0].kind == "fact", c.text
    assert "Non couvert" not in c.text
    note = next(s for s in c.sentences if s.op_id == "ambiguity")
    assert note.kind == "note" and "reva.labour.minimum_wage_monthly_lcu" in note.text
    assert "labour.mw.hourly" in note.text and "labour.mw.monthly_usd" in note.text


def test_ties_without_headline_are_each_answered_with_their_own_unit() -> None:
    plan, _x, c = run(Q, catalog(), None)
    assert sorted(o.args["concept"] for o in plan.operations) == sorted(MW)
    assert plan.ambiguities[0]["resolution"] == "each_queried"
    facts = [s for s in c.sentences if s.kind == "fact"]
    assert len(facts) == 3
    for s in facts:  # one value, one unit per sentence
        assert sum(u in s.text for u in ("EUR per hour", "EUR per month", "USD per month")) == 1, s.text
    assert "Non couvert" not in c.text and "Question ambiguë" not in c.text


def test_too_many_ties_are_reported_as_an_ambiguity_not_as_absent_data() -> None:
    n = MAX_TIED_CONCEPTS + 1
    cs = {f"labour.mw.v{i}": Concept(f"labour.mw.v{i}", {"fr": f"Salaire minimum variante{i}"}) for i in range(n)}
    cat = QaCatalog(cs, BASE.countries, BASE.properties)
    raw = keyword_plan(Q, cat, None)
    assert raw["operations"] == [] and raw["unsupported"] == []  # no unrelated KG fallback either
    plan = validate_plan(raw, cat, Q, planner="keyword_fallback")
    x = Executor(F.backends(), cat).run(plan)
    c = compose(x, Q, cat)
    amb = next(s for s in c.sentences if s.op_id == "ambiguity")
    assert amb.kind == "ambiguous" and amb.text.startswith("Question ambiguë")
    assert all(f"labour.mw.v{i}" in amb.text for i in range(n))
    assert "Non couvert" not in c.text


def test_plan_ambiguities_are_validated_against_the_catalog() -> None:
    cat = catalog()
    raw = {
        "operations": [],
        "ambiguities": [
            {"candidates": ["labour.mw.hourly", "evil.concept"], "resolution": "unresolved"},
            {"candidates": list(MW), "resolution": "headline", "chosen": "nope", "headline": "reva.labour.x"},
            {"candidates": list(MW), "resolution": "headline", "chosen": "labour.mw.hourly", "headline": "x y"},
            {"candidates": list(MW), "resolution": "made_up"},
            {"candidates": [*MW, "evil.concept"], "resolution": "unresolved"},
        ],
    }
    p = validate_plan(raw, cat, Q, planner="keyword_fallback")
    assert p.ambiguities == [{"candidates": list(MW), "resolution": "unresolved"}]


def test_france_minimum_wage_question_uses_the_registry_headline() -> None:
    """The V3 e2e question against the real catalog and ATLASOPS's real source-order registry."""
    cat = build_catalog()
    if "labour.minimum_wage.monthly.local_currency" not in cat.concepts:  # pragma: no cover - registry changed
        return
    raw = keyword_plan(Q, cat)
    assert [(o["op"], o["concept"], o["country"]) for o in raw["operations"]] == [
        ("atlas_value", "labour.minimum_wage.monthly.local_currency", "FRA")
    ]
    assert raw["ambiguities"][0]["headline"] == "reva.labour.minimum_wage_monthly_lcu"
    assert raw["unsupported"] == []
