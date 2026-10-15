"""Numeric faithfulness of the optional LLM rephrase: an added/changed number, date, name, cause or a
dropped negation discards the whole rephrase (template text returned)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import nlqa_fakes as F
import pytest

from reva.nl.budget import load_llm_budget
from reva.nl.ports import LlmRequest, LlmResponse, LlmUsage
from reva.nlqa.compose import Sentence
from reva.nlqa.recorded import PlanReplay
from reva.nlqa.rephrase import check_faithful, check_sentence, numbers
from reva.nlqa.service import AskInput, answer_question

ORIG = (
    "Salaire minimum mensuel (monnaie locale) — Sénégal, 2019 : 58 900 XOF per month (source : ilo/ilostat_mw, "
    "indicateur EAR_INEE_NOC_NB|cur=LCU ; signalements de la source : note=national legislation)."
)
EV = json.dumps({"value": "58900", "iso3": "SEN", "period_start": "2019-01-01", "indicator": "EAR_INEE_NOC_NB|cur=LCU"})
GOOD = (
    "Au Sénégal, le salaire minimum mensuel (monnaie locale) était de 58 900 XOF per month en 2019 (source : "
    "ilo/ilostat_mw, indicateur EAR_INEE_NOC_NB|cur=LCU ; note de la source : national legislation)."
)


def test_faithful_rephrase_passes() -> None:
    assert check_sentence(ORIG, GOOD, EV) == []


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        (GOOD.replace("58 900", "59 000"), "number_not_in_evidence:59000"),
        (GOOD.replace("58 900", "58 900 (environ 90 euros)"), "number_not_in_evidence:90"),
        (GOOD.replace("en 2019", "en 2021"), "number_not_in_evidence:2021"),
        (GOOD.replace(" en 2019", ""), "number_dropped:2019"),
        (GOOD.replace("Au Sénégal,", "Au Sénégal, sous Macky Sall,"), "name_not_in_evidence:macky"),
        (GOOD.replace("était de", "était, à cause de l'inflation, de"), "causal_wording_added"),
        (GOOD.replace("(source :", "(estimation REVA, sans source :"), "name_not_in_evidence:reva"),
    ],
)
def test_unfaithful_rephrase_is_detected(bad: str, problem: str) -> None:
    assert problem in check_sentence(ORIG, bad, EV)


def test_dropped_negation_or_attribution_is_detected() -> None:
    orig = "Aucune source consultée ne fournit Taux de chômage pour Sénégal (2020) (sources consultées : wb.wdi)."
    assert "marker_dropped:aucune" in check_sentence(
        orig, "Le Taux de chômage du Sénégal (2020) est fourni par wb.wdi.", ""
    )
    path = "A est relié(e) à B via 2 étape(s) ; ce chemin montre une connexion, pas une cause."
    assert "marker_dropped:pas une cause" in check_sentence(path, "A est relié(e) à B via 2 étape(s).", "")


def test_numbers_tokeniser_handles_grouping() -> None:
    assert {"58900", "2019"} <= numbers("58 900 en 2019") and "26337,46" in numbers("≈ 26 337,46")
    assert "26337.46" in numbers("26,337.46")


def test_ids_must_match_one_to_one() -> None:
    s = [Sentence("S1", ORIG, ["E1"], "fact"), Sentence("S2", "x", ["E1"], "note")]
    rep, _ = check_faithful(s, {"sentences": [{"id": "S1", "text": GOOD}]}, {"E1": {"value": "58900"}})
    assert not rep.ok and rep.problems[0]["problems"] == ["ids_do_not_match"]
    rep, _ = check_faithful(s, "not json", {})
    assert not rep.ok


class _Ledger:
    def reserve(self, *a: Any, **k: Any) -> None:
        pass

    def settle(self, *a: Any, **k: Any) -> None:
        pass


class _Rephraser:
    """Plans from the fixtures; answers the rephrase request with a fixed (possibly unfaithful) text."""

    provider = "openai"
    model = "gpt-4.1"

    def __init__(self, text: str | None) -> None:
        self.plans = PlanReplay(F.RECORDED_DIR)
        self.text = text
        self.rephrase_requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        if request.schema_name != "reva_answer_rephrase":
            return await self.plans.complete(request)
        self.rephrase_requests.append(request)
        if self.text is None:
            return await self.plans.complete(request)
        sentences = json.loads(request.user_content)["sentences"]
        out = {"sentences": [{"id": s["id"], "text": self.text} for s in sentences]}
        return LlmResponse("openai", "gpt-4.1", "synthetic-adversarial", out, LlmUsage(0, 0, 0), 0.0, recorded=True)


def ask(drafter: Any, q: str = "Quel est le salaire minimum au Sénégal ?") -> Any:
    return asyncio.run(
        answer_question(
            AskInput(q, "4" * 64, rephrase=True),
            catalog=F.catalog(),
            backends=F.backends(),
            drafters=[drafter],
            ledger=_Ledger(),
            budget=load_llm_budget(),
        )
    )


def test_service_accepts_a_faithful_rephrase_from_the_fixture() -> None:
    d = _Rephraser(None)
    r = ask(d)
    assert r.rephrase["status"] == "accepted", r.rephrase
    assert r.answer_text.startswith("Au Sénégal, le salaire minimum mensuel")
    assert r.composed.sentences[0].citations and r.rephrase["template_text"]["S1"].startswith("Salaire minimum")
    # the rephrase request carries only the composed sentences (data), never the evidence or the question
    req = d.rephrase_requests[0]
    assert "Quel est" not in req.user_content and "raw_sha256" not in req.user_content


@pytest.mark.parametrize(
    "bad",
    [
        "Au Sénégal, le salaire minimum mensuel était de 60 000 XOF per month en 2019 (source : ilo/ilostat_mw).",
        "Le salaire minimum du Sénégal, fixé par Macky Sall, était de 58 900 XOF per month en 2019 (source : ilo).",
    ],
)
def test_service_returns_template_text_when_rephrase_is_unfaithful(bad: str) -> None:
    r = ask(_Rephraser(bad))
    assert r.rephrase["status"] == "rejected_unfaithful"
    assert r.answer_text.startswith("Salaire minimum mensuel (monnaie locale) — Sénégal, 2019 : 58 900")
    assert any(w.startswith("rephrase_rejected") for w in r.warnings)
