"""Keyword planner (no LLM): deterministic, lexical, validated like an LLM plan."""

from __future__ import annotations

import nlqa_fakes as F

from reva.nlqa.catalog import QaCatalog
from reva.nlqa.fallback import keyword_plan, match_concept
from reva.nlqa.plan import validate_plan
from reva.nlqa.ports import Concept

CAT = F.catalog()


def ops(q: str, cat: QaCatalog = CAT) -> list[tuple]:
    p = validate_plan(keyword_plan(q, cat), cat, q, planner="keyword_fallback")
    return [(o.op, *(o.args.get(k) for k in ("concept", "country", "year", "text"))) for o in p.operations]


def test_value_rank_series_and_relation() -> None:
    assert ops("Quel est le salaire minimum au Sénégal ?") == [
        ("atlas_value", "labour.minimum_wage_monthly", "SEN", None, None)
    ]
    assert ops("Quels pays ont la plus forte densité de population en 2022 ?")[0][:4] == (
        "atlas_rank",
        "population.density",
        None,
        2022,
    )
    assert ops("Taux de chômage de la France de 2015 à 2020")[0][0] == "atlas_series"
    rel = ops("Comment Christophe Colomb est-il lié au Portugal ?")
    assert [o[0] for o in rel] == ["entity_lookup", "entity_lookup", "kg_path"]
    assert rel[0][4] == "Christophe Colomb"


def test_ties_are_queried_when_few_and_reported_when_many() -> None:
    def cat_with(n: int) -> QaCatalog:
        cs = {f"labour.mw.v{i}": Concept(f"labour.mw.v{i}", {"fr": f"Salaire minimum variante{i}"}) for i in range(n)}
        return QaCatalog(cs, CAT.countries, CAT.properties)

    assert [o[1] for o in ops("salaire minimum au Senegal", cat_with(2))] == ["labour.mw.v0", "labour.mw.v1"]
    plan = keyword_plan("salaire minimum au Senegal", cat_with(6))
    assert plan["operations"] == [] and plan["unsupported"] == []
    assert plan["ambiguities"][0]["resolution"] == "unresolved" and len(plan["ambiguities"][0]["candidates"]) == 6
    assert match_concept("météo à Paris", CAT) == (None, [])


def test_unrecognised_question_is_unsupported_not_guessed() -> None:
    plan = keyword_plan("pourquoi le ciel est-il bleu ?", CAT)
    assert plan["operations"] == [] and plan["unsupported"]
