"""Golden questions of the V6 domains (agent NLQA6), fixture mode: the real keyword planner, plan validation,
executor and composer over the in-memory fakes of ``tests/fixtures/nlqa/domain_fakes.py`` (origin test_fixture).

Every answer: each sentence cites evidence that exists; a "Sources et licences" sentence lists the sources and
licences of what is cited; missing data is said as "pas de donnée chargée" / "aucune …", never as a zero;
associations are never worded as causes; effect estimates are never averaged; a decision scenario is labelled
« scénario non validé — pas une probabilité »; a forecast names its method and score status."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import domain_fakes as D
import pytest

from reva.nlqa.fallback import keyword_plan
from reva.nlqa.plan import validate_plan
from reva.nlqa.service import AskInput, answer_question

CAT = D.catalog()


def ask(question: str, backends: Any = None, *, lang: str | None = None) -> dict[str, Any]:
    r = asyncio.run(
        answer_question(
            AskInput(question, "a" * 64, lang=lang) if lang else AskInput(question, "a" * 64),
            catalog=CAT,
            backends=backends or D.backends(),
            drafters=None,
            ledger=None,
            budget=None,
        )
    )
    return r.as_json()


def ops(j: dict[str, Any]) -> list[str]:
    return [o["op"] for o in j["plan"]["operations"]]


def text(j: dict[str, Any]) -> str:
    return " ".join(s["text"] for s in j["sentences"])


# question, operations planned, substrings that must appear, substrings that must not appear
GOLDEN: list[tuple[str, list[str], list[str], list[str]]] = [
    # ---------------------------------------------------------------- composition / structure
    ("De quoi est fait le méthane ?", ["entity_lookup", "kg_families"],
     ["méthane — « est composé de »", "carbone (fixture:carbon", "part 74,87 % (mass_share)", "hydrogène",
      "« CH4 »", "fixture.chebi (CC-BY-4.0)"], ["cause"]),
    ("What is methane made of?", ["entity_lookup", "kg_families"],
     ["methane — “is made of”", "carbon (fixture:carbon", "share 74.87% (mass_share)", "Sources and licences"], []),
    ("De quoi est fait le pétrole ?", ["entity_lookup", "kg_families"],
     ["pétrole — « est composé de »", "alcanes", "part 25 % (P1107)", "donnée communautaire", "CC0-1.0"], []),
    ("Composition du pétrole ?", ["entity_lookup", "kg_families"], ["alcanes"], []),
    ("De quoi est composé le cœur ?", ["entity_lookup", "kg_families"],
     ["identifie à la même entité", "pas une fusion de fiches", "ventricule gauche", "oreillette droite"], []),
    ("Quelles sont les parties du cœur ?", ["entity_lookup", "kg_families"], ["ventricule gauche"], []),
    ("Quels gènes sur le chromosome 17 ?", ["entity_lookup", "kg_families", "kg_families"],
     ["« contient (lieu) »", "TP53", "BRCA1", "fixture.hgnc"], []),
    ("De quoi est fait le granite ?", ["entity_lookup", "kg_families"], ["Aucune entité ne correspond à « granite »"],
     ["Sources et licences"]),
    ("Composition chimique de l'eau ?", ["entity_lookup", "kg_families"],
     ["Pas de donnée chargée", "pas une absence dans le monde"], []),
    # ---------------------------------------------------------------- living world
    ("Que mange le loup ?", ["entity_lookup", "kg_families"],
     ["loup — « mange »", "cerf", "lapin", "fixture.globi (licence_unclear (fixture))"], []),
    ("What does the wolf eat?", ["entity_lookup", "kg_families"], ["wolf — “eats”", "deer", "rabbit"], []),
    ("Qui mange le lapin ?", ["entity_lookup", "kg_families"], ["« est mangé par »", "loup"], ["cerf"]),
    ("Que mange le lapi ?", ["entity_lookup", "kg_families"],
     ["Aucune entité au libellé ou alias exact pour « lapi »", "correspondances approchées"], ["lapin — "]),
    ("Où vit le lion ?", ["entity_lookup", "kg_families"],
     ["réponse lue sur lion (fixture:lion_b)", "fiches non fusionnées", "Afrique", "fixture.iucn (CC-BY-4.0)"], []),
    ("Where does the lion live?", ["entity_lookup", "kg_families"], ["Africa", "records not merged"], []),
    ("Combien d'espèces de mammifères ?", ["entity_lookup", "kg_families", "kg_families"],
     ["Rattachements directs chargés pour mammifères", "2 déclaration(s)", "pas un recensement"], []),
    ("How many species of mammals?", ["entity_lookup", "kg_families", "kg_families"], ["not a census"], []),
    ("Que mange le tigre ?", ["entity_lookup", "kg_families"], ["Aucune entité ne correspond à « tigre »"], []),
    ("Habitat du loup ?", ["entity_lookup", "kg_families"], ["Pas de donnée chargée", "« located_in »"], []),
    # ---------------------------------------------------------------- energy / industry
    ("Qui possède la mine KOV ?", ["entity_lookup", "kg_families"],
     ["« est possédé ou contrôlé par »", "Glencore (fixture:glencore", "« Glencore plc » (part publiée : 75)",
      "« Gécamines »", "aucun rapprochement par le nom"], []),
    ("Who owns the KOV mine?", ["entity_lookup", "kg_families"], ["Glencore", "no matching by name"], []),
    ("Que possède Glencore ?", ["entity_lookup", "kg_families"], ["« possède ou contrôle »", "KOV Mine"], []),
    ("Qui produit du cuivre ?", ["entity_lookup", "kg_families"], ["« est produit par »", "KOV Mine"], []),
    ("Quelles centrales au charbon en Inde ?", ["entity_lookup", "entity_lookup", "kg_cross"],
     ["CENTRALE UN", "CENTRALE DEUX", "2 trouvée(s)", "« combustible principal »", "fixture.gppd (CC-BY-4.0)"],
     ["CENTRALE GAZ", "CENTRALE CHINE"]),
    ("Which coal power plants are in India?", ["entity_lookup", "entity_lookup", "kg_cross"],
     ["PLANT ONE", "PLANT TWO", "2 found"], ["GAS PLANT", "CHINA PLANT"]),
    ("Prix du Brent en 2020", ["atlas_period"],
     ["Prix spot du pétrole brut Brent (quotidien), 2020 — 4 valeur(s)", "première (2020-01-02) : 67,05",
      "plus basse (2020-04-21) : 9,12", "plus haute (2020-01-06) : 70,25", "dernière (2020-12-31) : 51,22",
      "aucune moyenne calculée"], ["77,24"]),
    ("Quel est le prix du Brent ?", ["atlas_period"], ["77,24"], ["67,05"]),
    # ---------------------------------------------------------------- health
    ("Quelles maladies causées par Mycobacterium tuberculosis ?", ["entity_lookup", "why", "kg_families"],
     ["Selon fixture.doid/doid (T1", "a pour effet tuberculose", "immunodéficience",
      "association signalée par la source, jamais une cause", "interaction enregistrée, pas une cause"], []),
    ("Diseases caused by Mycobacterium tuberculosis?", ["entity_lookup", "why", "kg_families"],
     ["According to fixture.doid/doid (T1", "never a cause"], []),
    ("Causes de décès en France", ["atlas_value", "atlas_value"],
     ["Part des décès dus aux maladies cardiovasculaires — France, 2021", "23,26", "6,42", "fixture_who/gho"], []),
    ("Causes of death in France in 2021", ["atlas_value", "atlas_value"], ["23.26", "6.42"], []),
    ("Causes de décès au Sénégal", ["atlas_value", "atlas_value"], ["Aucune source consultée ne fournit"], ["23,26"]),
    ("Quelles maladies causées par le virus Zika ?", ["entity_lookup", "why", "kg_families"],
     ["Aucune entité ne correspond"], []),
    # ---------------------------------------------------------------- prediction
    ("Quelle est la prévision d'inflation pour la France ?", ["forecast"],
     ["Familles de questions retenues pour « inflation »", "Prévisions scellées du registre REVA",
      "forecast.registry_persistence : 1,9 [5 %–95 % : -0,4 à 4,2] (méthode : random walk",
      "en attente de résolution", "aucun score encore", "Statut : résolue — valeur publiée 2,3",
      "abs_error 0,2", "abstention (no WEO projection", "pas un conseil"], []),
    ("What is the inflation forecast for France?", ["forecast"], ["no score yet", "method: AR(p)"], []),
    ("Prévision d'inflation pour le Japon ?", ["forecast"], ["ne couvrent pas", "Pas de donnée chargée"], []),
    ("Prévision du chômage en France ?", ["forecast"], ["Pas de donnée chargée", "awaiting_table"], []),
    ("Que se passe-t-il si la BCE monte ses taux de 50 pb ?", ["decision"],
     ["Scénario non validé — pas une probabilité : moteur de décision REVA", "ecb_plus_50bp_2027q1",
      "PIB réel (niveau) — FRA : variation -0,14 % en 2027Q1", "pub:fixture:rate_gdp", "rien n'est additionné ni moyenné"],
     ["grandeur de la question appliquée"]),
    ("What happens if the ECB raises rates by 25 bp?", ["decision"],
     ["Unvalidated scenario — not a probability", "magnitude of the question applied: 25 bp instead of 50 bp",
      "-0.07 %"], []),
    ("Et si l'OPEP+ réduit sa production de 2 mb/j ?", ["decision"], ["opec_plus_cut_2mbd_2027q1"],
     ["grandeur de la question appliquée"]),
    ("Que se passe-t-il si la BCE monte ses taux de 2 % ?", ["decision"], ["n'est pas une unité de cet instrument"], []),
    ("Que se passerait-il si les licornes votaient ?", ["decision"],
     ["Aucune décision de référence unique", "aucun scénario n'est calculé"], ["Scénario non validé — pas une probabilité :"]),
    # ---------------------------------------------------------------- effects
    ("Quel est l'effet d'une hausse du pétrole sur l'inflation ?", ["effects"],
     ["Termes retenus pour la cause « hausse du pétrole »", "Prix mondial du pétrole brut (oil_price.global",
      "Selon Fixture & Author (2019)", "est de 0.2 point(s)", "Selon Other Fixture (2021)", "est de 0.5 point(s)",
      "ne les moyenne pas", "doi:10.0000/fixture", "extrait : «"], ["0.35", "0,35"]),
    ("What is the effect of an oil price increase on inflation?", ["effects"],
     ["According to Fixture & Author (2019)", "does not average"], ["0.35"]),
    ("Quel est l'impact d'une hausse des taux directeurs sur le PIB ?", ["effects"], ["Rate Fixture (2018)", "-0.3"], []),
    ("Quel est l'effet d'une hausse du pétrole sur le chômage ?", ["effects"],
     ["aucun terme de la couche d'estimations d'effet ne correspond à « chômage »"], []),
    ("Quel est l'effet du pétrole sur le PIB ?", ["effects"],
     ["Pas de donnée chargée : aucune estimation publiée", "non équivalentes"], []),
]  # fmt: skip


def test_at_least_forty_golden_questions() -> None:
    assert len(GOLDEN) >= 40
    assert len({q for q, *_ in GOLDEN}) == len(GOLDEN)


@pytest.mark.parametrize(("question", "planned", "present", "absent"), GOLDEN, ids=[g[0][:48] for g in GOLDEN])
def test_golden_question(question: str, planned: list[str], present: list[str], absent: list[str]) -> None:
    j = ask(question)
    assert ops(j) == planned, j["plan"]
    assert j["planner"] == "keyword_fallback"
    t = text(j)
    for s in present:
        assert s in t, f"missing {s!r} in: {t}"
    for s in absent:
        assert s not in t, f"unexpected {s!r} in: {t}"
    _honesty(j)


def _honesty(j: dict[str, Any]) -> None:
    ev = {e["id"]: e for e in j["evidence"]}
    for s in j["sentences"]:
        assert s["citations"], s
        assert all(c in ev for c in s["citations"]), s
    sourced = [e for e in j["evidence"] if e["kind"] not in ("absence", "entity_identification", "registry_term")]
    srcs = [s for s in j["sentences"] if s["kind"] == "sources"]
    if sourced:
        assert len(srcs) == 1 and srcs[0]["text"].startswith(("Sources et licences", "Sources and licences"))
    for s in j["sentences"]:  # associations / interactions / context are never worded as causes
        if any(w in s["text"] for w in ("« est associé à »", "« interagit avec »", "“is associated with”")):
            assert re.search(r"jamais une cause|pas une cause|never a cause|not a cause", s["text"]), s
    for e in j["evidence"]:  # family edges are never causal
        if isinstance(e.get("family"), dict):
            assert e["family"]["causal"] is False


def test_effect_estimates_are_listed_one_by_one_never_pooled() -> None:
    j = ask("Quel est l'effet d'une hausse du pétrole sur l'inflation ?")
    est = [s for s in j["sentences"] if s["kind"] == "estimate"]
    assert len(est) == 2 and all(len(s["citations"]) == 1 for s in est)
    kinds = {e["kind"] for e in j["evidence"]}
    assert "effect_estimate" in kinds
    e = next(e for e in j["evidence"] if e["kind"] == "effect_estimate")
    assert e["citation"]["quote"] and e["citation"]["locator"] and e["citation"]["raw_sha256"] == D.SHA


def test_decision_magnitude_is_applied_to_a_copy_of_the_reference() -> None:
    b = D.backends()
    ask("What happens if the ECB raises rates by 25 bp?", b)
    run = b.decisions.runs[-1]
    assert run["magnitude"] == {"value": "25", "unit": "bp"} and run["id"] == "ecb_plus_50bp_2027q1.ask"
    ask("Que se passe-t-il si la BCE baisse ses taux de 50 pb ?", b)
    assert b.decisions.runs[-1]["magnitude"] == {"value": "-50", "unit": "bp"}


def test_forecast_evidence_is_the_sealed_record() -> None:
    j = ask("Quelle est la prévision d'inflation pour la France ?")
    fc = [e for e in j["evidence"] if e["kind"] == "sealed_forecast"]
    assert {e["status"] for e in fc} == {"pending", "resolved"}
    assert all(e["citation"]["seal_verified"] and e["citation"]["batch_sha256"] == "1" * 64 for e in fc)
    methods = {f["model"]: f["method"] for e in fc for f in e["forecasts"]}
    assert methods["forecast.registry_ar"].startswith("AR(p)")


@pytest.mark.parametrize(
    ("question", "backend", "word"),
    [
        ("Que mange le loup ?", "families", "l'index des familles de relations"),
        ("Quel est l'effet d'une hausse du pétrole sur l'inflation ?", "effects", "la couche d'estimations d'effet"),
        ("Quelle est la prévision d'inflation pour la France ?", "forecasts", "le registre de prévisions"),
        ("Que se passe-t-il si la BCE monte ses taux de 50 pb ?", "decisions", "le moteur de décision"),
    ],
)
def test_absent_backend_is_capability_unavailable(question: str, backend: str, word: str) -> None:
    j = ask(question, D.backends(**{backend: None}))
    t = text(j)
    assert f"Indisponible : {word} n'est pas accessible" in t
    assert any(s["kind"] == "unavailable" for s in j["sentences"])


def test_atlas_without_period_reader_is_unavailable() -> None:
    import nlqa_fakes as F

    j = ask("Prix du Brent en 2020", D.backends(atlas=F.FakeAtlas()))
    assert "capability_unavailable" in text(j)


def test_no_name_to_id_table_entities_come_from_the_label_index() -> None:
    """Entity ids in the answers come only from the (fake) KG label index: a name absent from it is never
    resolved, whatever the question."""
    for q in ("Que mange le panda ?", "Qui possède la mine Kamoto ?", "De quoi est fait le diamant ?"):
        j = ask(q)
        assert not [e for e in j["evidence"] if e["kind"] == "entity_identification"]
        assert "Aucune entité ne correspond" in text(j)


def test_restatement_and_plan_validation_of_new_operations() -> None:
    q = "Quelles centrales au charbon en Inde ?"
    p = validate_plan(keyword_plan(q, CAT), CAT, q, planner="keyword_fallback")
    assert not p.rejected
    raw = {
        "input_language": "fr",
        "restatement": "",
        "unsupported": [],
        "operations": [
            {"id": "s1", "op": "entity_lookup", "text": "loup"},
            {"id": "s2", "op": "kg_families", "entity": "s1", "families": ["causes"]},
            {"id": "s3", "op": "effects", "cause_text": "taux de change", "effect_text": "inflation"},
            {"id": "s4", "op": "decision", "text": "loup", "magnitude": "5", "unit": "furlongs"},
            {"id": "s5", "op": "forecast", "text": "loup", "country": "XXX"},
        ],
    }
    p = validate_plan(raw, CAT, "Que mange le loup ?")
    reasons = {r["id"]: r["reason"] for r in p.rejected}
    assert reasons == {
        "s2": "family_not_in_list",
        "s3": "text_not_in_question",
        "s4": "unit_not_in_list",
        "s5": "country_not_in_list",
    }


def test_latency_of_fixture_answers() -> None:
    t0 = time.perf_counter()
    for q, *_ in GOLDEN[:20]:
        ask(q)
    assert (time.perf_counter() - t0) / 20 < 0.5  # pipeline overhead per question, fixtures
