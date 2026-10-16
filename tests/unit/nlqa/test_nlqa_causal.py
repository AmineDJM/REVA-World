"""NLQA ``why`` and ``causal_chain`` (agent CAUSUI): planned, validated, executed and worded from sourced claims.

Every causal sentence names its asserting source and tier ("selon Wikidata (T3, …)"); a chain answer says it
is REVA's composition of distinct claims whose strength is its weakest link; no causal wording appears without
a claim; candidates (T5/T6) and source-less rows never reach an answer."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import causal_fakes as C
import nlqa_fakes as F
import pytest

from reva.nlqa.causal_adapter import CausalServiceAdapter, chains_from_json, claim_from_json
from reva.nlqa.compose import compose
from reva.nlqa.executor import Executor
from reva.nlqa.fallback import causal_plan, keyword_plan
from reva.nlqa.plan import OPS, restate, validate_plan
from reva.nlqa.ports import CausalChain, CausalChainHop, chain_grade, hop_grade
from reva.nlqa.prompt import PROMPT_VERSION, output_schema, system_prompt
from reva.nlqa.rephrase import CAUSAL, check_faithful
from reva.nlqa.service import AskInput, answer_question

CAT = F.catalog()
SELON = re.compile(r"\b(selon|according to) (Wikidata|[a-z0-9_.-]+(/[a-z0-9_.-]+)?) \(T[1-4], ", re.IGNORECASE)


def run(question: str, backends: Any = None, *, lang: str | None = None):
    p = validate_plan(keyword_plan(question, CAT), CAT, question, planner="keyword_fallback")
    if lang:
        p.language = lang
    x = Executor(backends or C.backends(), CAT).run(p)
    return p, x, compose(x, question, CAT)


def evidence(x) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in x.evidence.items}


# --------------------------------------------------------------------------- plan / prompt


def test_schema_prompt_and_version() -> None:
    assert PROMPT_VERSION == "nlqa-plan-1.2.0"
    props = output_schema()["properties"]["operations"]["items"]["properties"]
    assert {"why", "causal_chain"} <= set(props["op"]["enum"]) and {"why", "causal_chain"} <= set(OPS)
    assert props["causal_direction"] == {"type": ["string", "null"], "enum": ["causes", "effects", None]}
    sp = system_prompt(CAT, "Pourquoi ?")
    assert "why(entity, causal_direction)" in sp and "causal_chain(from_entity, to_entity, max_hops)" in sp
    assert "Never use kg_path for a causal question" in sp


@pytest.mark.parametrize(
    ("q", "op", "args"),
    [
        ("Pourquoi la Première Guerre mondiale a-t-elle éclaté ?", "why", {"direction": "causes"}),
        ("Quelles sont les causes de la Première Guerre mondiale ?", "why", {"direction": "causes"}),
        ("Quelles ont été les conséquences de la Première Guerre mondiale ?", "why", {"direction": "effects"}),
        ("Why did World War I start?", "why", {"direction": "causes"}),
        ("What are the consequences of World War I?", "why", {"direction": "effects"}),
        ("Chaîne causale entre l'attentat de Sarajevo et le traité de Versailles ?", "causal_chain", {"max_hops": 4}),
        ("Comment l'attentat de Sarajevo a-t-il conduit au traité de Versailles ?", "causal_chain", {"max_hops": 4}),
    ],
)
def test_keyword_planner_recognises_causal_questions(q: str, op: str, args: dict[str, Any]) -> None:
    p = validate_plan(keyword_plan(q, CAT), CAT, q, planner="keyword_fallback")
    assert not p.rejected, p.rejected
    main = [o for o in p.operations if o.op == op]
    assert len(main) == 1 and {k: main[0].args[k] for k in args} == args
    lookups = [o.args["text"] for o in p.operations if o.op == "entity_lookup"]
    assert all(t.lower() in q.lower() for t in lookups)  # lookup terms are the question's own words
    assert (
        "causes" in restate(p, CAT)
        or "chaînes" in restate(p, CAT)
        or "consequences" in restate(p, CAT)
        or "conséquences" in restate(p, CAT)
    )


def test_non_causal_questions_keep_their_plans() -> None:
    for q in (
        "Quel est le salaire minimum au Sénégal ?",
        "Comment Christophe Colomb est-il lié au fait que les Brésiliens parlent portugais ?",
    ):
        assert causal_plan(q) is None


def test_validation_rejects_never_substitutes() -> None:
    q = "Pourquoi la Première Guerre mondiale ?"
    look = {"id": "s1", "op": "entity_lookup", "text": "Première Guerre mondiale"}
    ok = validate_plan(
        {"operations": [look, {"id": "s2", "op": "why", "entity": "s1", "causal_direction": None}]}, CAT, q
    )
    assert ok.operations[1].args == {"entity": "s1", "direction": "causes"}
    bad_dir = validate_plan(
        {"operations": [look, {"id": "s2", "op": "why", "entity": "s1", "causal_direction": "maybe"}]}, CAT, q
    )
    assert bad_dir.rejected[0]["reason"] == "invalid_argument"
    qid = validate_plan({"operations": [{"id": "s2", "op": "why", "entity": "Q361"}]}, CAT, q)
    assert qid.rejected[0]["reason"] == "unknown_entity_ref"
    q2 = "Chaîne causale entre Sarajevo et Versailles"
    looks = [
        {"id": "s1", "op": "entity_lookup", "text": "Sarajevo"},
        {"id": "s2", "op": "entity_lookup", "text": "Versailles"},
    ]
    capped = validate_plan(
        {
            "operations": [
                *looks,
                {"id": "s3", "op": "causal_chain", "from_entity": "s1", "to_entity": "s2", "max_hops": 9},
            ]
        },
        CAT,
        q2,
    )
    assert capped.operations[2].args["max_hops"] == 6 and "max_hops_capped: 9 -> 6" in capped.warnings
    same = validate_plan(
        {"operations": [*looks, {"id": "s3", "op": "causal_chain", "from_entity": "s1", "to_entity": "s1"}]}, CAT, q2
    )
    assert same.rejected[0]["reason"] == "path_to_itself"


# --------------------------------------------------------------------------- adapter mapping


def test_adapter_keeps_only_sourced_t1_t4_claims() -> None:
    assert claim_from_json(C.SARAJEVO).tier == "T3"
    assert claim_from_json(C.JULY).dataset == "icb2"
    assert claim_from_json(C.CANDIDATE) is None  # T5: candidate space, never an answer
    assert claim_from_json(C.NO_SOURCE) is None
    assert claim_from_json({**C.SARAJEVO, "grade": {"grade": "Z"}}) is None
    ungraded = claim_from_json({**C.SARAJEVO, "grade": None})
    assert ungraded is not None and ungraded.grade is None
    ch = chains_from_json(C.CHAIN)
    assert [len(h.claims) for h in ch[0].hops] == [1, 1] and chain_grade(ch[0]) == "C"


def test_grades_hop_best_chain_weakest() -> None:
    a, c = claim_from_json(C.JULY), claim_from_json(C.E_VERSAILLES)
    b = claim_from_json(C.SARAJEVO)
    h1 = CausalChainHop("x", "y", (b, a))  # B and A -> hop A
    h2 = CausalChainHop("y", "z", (c,))
    assert hop_grade(h1) == "A" and chain_grade(CausalChain((h1, h2))) == "C"
    assert (
        chain_grade(CausalChain((h1, CausalChainHop("y", "z", (claim_from_json({**C.E_VERSAILLES, "grade": None}),)))))
        is None
    )


# --------------------------------------------------------------------------- answers


def test_why_every_causal_sentence_names_source_and_tier() -> None:
    _, x, out = run("Pourquoi la Première Guerre mondiale a-t-elle éclaté ?")
    ev = evidence(x)
    causal = [s for s in out.sentences if s.kind == "causal"]
    assert len(causal) == 6  # 5 asserting claims + 1 denial; the candidate and the source-less row are dropped
    for s in causal:
        assert SELON.search(s.text), s.text
        assert s.citations and all(ev[c]["kind"] == "causal_claim" for c in s.citations)
        cited = ev[s.citations[0]]["citation"]
        assert cited["source"] and cited["tier"] in ("T1", "T2", "T3", "T4")
    texts = [s.text for s in causal]
    # lanes first: immediate, underlying, contributing, coded, general; within a lane best grade first
    assert texts[0].startswith(
        "Selon Wikidata (T3, déclaration Wikidata avec références ; donnée communautaire), Première Guerre mondiale a pour cause immédiate attentat de Sarajevo"
    )
    assert any(t.startswith("Selon icb.data/icb2 (T1, jeu de données codé par des experts)") for t in texts)
    assert any("mécanisme cité par la source : « FIXTURE codebook definition of a crisis trigger »" in t for t in texts)
    assert any("incohérence temporelle signalée" in t and "preuve E" in t for t in texts)
    assert any("contestée par 1 autre(s) affirmation(s)" in t for t in texts)
    denial = [t for t in texts if "il est faux que" in t]
    assert len(denial) == 1 and denial[0].startswith("Selon fixture.refs/refs (T2, référence savante citée)")
    note = next(s for s in out.sentences if s.kind == "note" and s.op_id == "s2")
    assert "REVA n'établit aucune causalité" in note.text
    assert not any("cand:" in str(e.get("claim_key")) for e in ev.values())


def test_no_causal_wording_without_a_claim() -> None:
    for q in (
        "Pourquoi la Première Guerre mondiale a-t-elle éclaté ?",
        "Chaîne causale entre l'attentat de Sarajevo et le traité de Versailles ?",
    ):
        _, x, out = run(q)
        ev = evidence(x)
        for s in out.sentences:
            if CAUSAL.search(s.text) and s.kind not in ("missing", "unavailable"):
                assert any(ev[c]["kind"] == "causal_claim" for c in s.citations), s.text


def test_effects_are_worded_cause_to_effect() -> None:
    _, _, out = run("Quelles ont été les conséquences de la Première Guerre mondiale ?")
    causal = [s.text for s in out.sentences if s.kind == "causal"]
    assert any("Première Guerre mondiale est un facteur contributif de révolution russe" in t for t in causal)
    assert any("Première Guerre mondiale a pour effet traité de Versailles" in t for t in causal)


def test_chain_answer_carries_the_composition_caveat_and_sources_per_hop() -> None:
    _, _, out = run("Chaîne causale entre l'attentat de Sarajevo et le traité de Versailles ?")
    head = next(s for s in out.sentences if s.kind == "path")
    assert "Chaîne composée par REVA à partir de 2 affirmation(s) distincte(s)" in head.text
    assert "aucune source n'affirme la chaîne entière ; force = maillon le plus faible (preuve C)" in head.text
    hops = [s for s in out.sentences if s.kind == "causal"]
    assert [h.text.split(" :")[0] for h in hops] == ["Maillon 1", "Maillon 2"]
    for h in hops:
        assert SELON.search(h.text), h.text
        assert "grade du maillon" in h.text


def test_english_answer() -> None:
    _, _, out = run("Why did World War I start?", lang="en")
    causal = [s.text for s in out.sentences if s.kind == "causal"]
    assert causal[0].startswith(
        "According to Wikidata (T3, Wikidata statement with references; community-edited), World War I has immediate cause assassination in Sarajevo"
    )
    _, _, out2 = run("Causal chain between assassination in Sarajevo and Treaty of Versailles?", lang="en")
    assert any(
        "Chain composed by REVA from 2 separately asserted claim(s)" in s.text and "strength = weakest link" in s.text
        for s in out2.sentences
    )


def test_missing_and_unavailable_are_said_never_guessed() -> None:
    svc = C.FakeCausalService(causes=[], effects=[], chain=None)
    _, _, out = run("Pourquoi la Grande Dépression ?", C.backends(causal=C.adapter(svc)))
    miss = [s for s in out.sentences if s.kind == "missing"]
    assert any(
        "Aucune affirmation causale sourcée sur les causes de Grande Dépression (Q8698)" in s.text
        and "REVA ne déduit aucune cause" in s.text
        for s in miss
    )
    assert not [s for s in out.sentences if s.kind == "causal"]
    _, _, out2 = run(
        "Chaîne causale entre l'attentat de Sarajevo et la Grande Dépression ?", C.backends(causal=C.adapter(svc))
    )
    assert any("cela ne prouve ni l'absence ni l'existence d'un lien causal" in s.text for s in out2.sentences)
    _, _, out3 = run("Pourquoi la Première Guerre mondiale ?", C.backends(causal=None))
    assert any(s.kind == "unavailable" and "le service des affirmations causales" in s.text for s in out3.sentences)


def test_backend_failure_is_unavailable() -> None:
    def boom(*_: Any) -> Any:
        raise RuntimeError("db down")

    _, _, out = run("Pourquoi la Première Guerre mondiale ?", C.backends(causal=CausalServiceAdapter(boom, boom)))
    assert any(s.kind == "unavailable" for s in out.sentences)
    assert not [s for s in out.sentences if s.kind == "causal"]


def test_rephrase_cannot_drop_the_attribution() -> None:
    _, x, out = run("Pourquoi la Première Guerre mondiale a-t-elle éclaté ?")
    ev = evidence(x)
    sents = out.sentences
    honest = {"sentences": [{"id": s.id, "text": s.text} for s in sents]}
    report, _ = check_faithful(sents, honest, ev)
    assert report.ok, report.problems
    s0 = next(s for s in sents if s.kind == "causal")
    stripped = {
        "sentences": [{"id": s.id, "text": re.sub(r"^Selon .+?\), ", "", s.text) if s is s0 else s.text} for s in sents]
    }
    report2, _ = check_faithful(sents, stripped, ev)
    assert not report2.ok


def test_service_keyword_planner_end_to_end() -> None:
    r = asyncio.run(
        answer_question(
            AskInput("Pourquoi la Première Guerre mondiale a-t-elle éclaté ?", "a" * 64),
            catalog=CAT,
            backends=C.backends(),
            drafters=None,
            ledger=None,
            budget=None,
        )
    )
    j = r.as_json()
    assert j["prompt_version"] == "nlqa-plan-1.2.0" and j["planner"] == "keyword_fallback"
    assert [o["op"] for o in j["plan"]["operations"]] == ["entity_lookup", "why"]
    assert any(
        s["kind"] == "causal"
        and s["citation_refs"][0].startswith("E")
        and "claim by wikidata (T3)" in s["citation_refs"][0]
        for s in j["sentences"]
    )


UI_FIXTURE = Path(__file__).resolve().parents[3] / "ui" / "src" / "v3" / "causal" / "ask_causal.fixture.json"
UI_QUESTIONS = {
    "why": "Pourquoi la Première Guerre mondiale a-t-elle éclaté ?",
    "chain": "Chaîne causale entre l'attentat de Sarajevo et le traité de Versailles ?",
}


def _ui_answers() -> dict[str, Any]:
    out: dict[str, Any] = {
        "_note": "Generated by the real NLQA pipeline (reva.nlqa.service.answer_question, keyword planner) over the "
        "NLQA causal TEST FIXTURES (tests/fixtures/nlqa/causal_fakes.py, origin test_fixture); not real data. "
        "Regenerate: REVA_REGEN_UI_FIXTURE=1 uv run pytest tests/unit/nlqa/test_nlqa_causal.py -k ui_fixture"
    }
    for key, q in UI_QUESTIONS.items():
        r = asyncio.run(
            answer_question(
                AskInput(q, "a" * 64), catalog=CAT, backends=C.backends(), drafters=None, ledger=None, budget=None
            )
        )
        j = r.as_json()
        j["latency_ms"] = 0.0
        out[key] = j
    return json.loads(json.dumps(out, ensure_ascii=False))


def test_ui_fixture_is_the_current_pipeline_output() -> None:
    """The UI's Demander fixture (ui/src/v3/causal/ask_causal.fixture.json) is this pipeline's real output."""
    fresh = _ui_answers()
    if os.environ.get("REVA_REGEN_UI_FIXTURE") == "1":
        UI_FIXTURE.write_text(json.dumps(fresh, ensure_ascii=False, indent=1) + "\n")
    assert json.loads(UI_FIXTURE.read_text()) == fresh, "stale UI fixture: regenerate (see its _note)"


def test_adapter_reads_why_lanes_as_causcore_returns_them() -> None:
    """reva.causal.graph.why returns lanes as a list of {lane, label, claims} plus dissent (denials)."""
    why = {
        "id": "Q361",
        "lanes": [
            {"lane": "immediate", "label": {"fr": "causes immédiates"}, "claims": [C.SARAJEVO]},
            {"lane": "coded", "label": {"fr": "déclencheurs codés"}, "claims": [C.JULY, C.CANDIDATE]},
        ],
        "dissent": [C.IMPERIAL_DENIED],
    }
    a = CausalServiceAdapter(lambda *_: why, lambda *_: {"paths": []})
    res = a.explain("Q361", direction="causes", limit=10)
    assert [c.claim_key for c in res.claims] == [C.SARAJEVO["claim_key"], C.JULY["claim_key"]]
    assert [c.stance for c in res.dissent] == ["denies"]
