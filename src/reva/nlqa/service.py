"""Question → sourced answer: the server pipeline around the planner.

1. sanitize the question (NFC, controls removed, ≤ :data:`MAX_QUESTION_BYTES`);
2. plan: the LLM (``gpt-4.1`` via :mod:`reva.nl.providers`, strict JSON schema, temperature 0,
   budgeted through the LLM ledger *before* sending) translates the question into a typed plan —
   or, with no LLM configured, the deterministic keyword planner does (:mod:`reva.nlqa.fallback`);
3. validate the plan against the allow-lists (:func:`reva.nlqa.plan.validate_plan`): unknown
   concepts, countries, properties or operations are rejected and reported, never substituted;
4. execute it against the adapter ports (:mod:`reva.nlqa.executor`): every record read becomes
   cited evidence; an absent backend is ``capability_unavailable``;
5. compose the answer from deterministic fr/en templates (:mod:`reva.nlqa.compose`);
6. optionally let the LLM rephrase it, accepted only when :func:`reva.nlqa.rephrase.check_faithful`
   passes for every sentence (else the template text is returned, with a warning).

Nothing is stored except the ledger rows (and, in the API, one audit row without the text).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from reva.nl.budget import LlmBudget, LlmLedger
from reva.nl.guards import RULES, TextTooLong, sanitize_text
from reva.nl.ports import LlmProviderError, LlmRequest, LlmResponse, LlmUnavailable, ScenarioDrafter
from reva.nl.service import call_providers
from reva.nlqa.catalog import QaCatalog
from reva.nlqa.compose import Composed, compose
from reva.nlqa.executor import Executor
from reva.nlqa.fallback import keyword_plan
from reva.nlqa.plan import QueryPlan, restate, validate_plan
from reva.nlqa.ports import Backends
from reva.nlqa.prompt import (
    PROMPT_VERSION,
    REPHRASE_PROMPT_VERSION,
    REPHRASE_RULES,
    REPHRASE_SCHEMA_NAME,
    SCHEMA_NAME,
    output_schema,
    prompt_digest,
    rephrase_schema,
    system_prompt,
    user_envelope,
)
from reva.nlqa.rephrase import check_faithful, rephrase_payload
from reva.operations.budgets import BudgetExhausted

MAX_QUESTION_BYTES = 2048
#: Largest composed answer sent for rephrasing (characters / sentences).
MAX_REPHRASE_CHARS = 6000
MAX_REPHRASE_SENTENCES = 40
_INSTRUCTION_RULE = next(r for r in RULES if r.capability_key == "nl.instruction_in_text")


@dataclass
class AskInput:
    question: str
    operation_key: str  # 32-64 hex chars; budget operation ids derive from it
    lang: str | None = None
    rephrase: bool = False


@dataclass
class AskResult:
    question_sha256: str
    plan: QueryPlan
    restatement: str
    answer_text: str
    composed: Composed
    evidence: list[dict[str, Any]]
    planner: str
    provider: str | None = None
    model: str | None = None
    model_requested: str | None = None
    prompt_version: str = PROMPT_VERSION
    prompt_sha256: str | None = None
    catalog_digest: str = ""
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "requests": 0}
    )
    rephrase: dict[str, Any] = field(default_factory=lambda: {"requested": False, "status": "not_requested"})
    warnings: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    def as_json(self) -> dict[str, Any]:
        ev = {e["id"]: e for e in self.evidence}
        return {
            "question_sha256": self.question_sha256,
            "language": self.plan.language,
            "plan": self.plan.as_json(),
            "restatement": self.restatement,
            "llm_restatement": self.plan.llm_restatement,
            "answer_text": self.answer_text,
            "sentences": [s.as_json(ev) for s in self.composed.sentences],
            "evidence": self.evidence,
            "unsupported": self.plan.unsupported,
            "planner": self.planner,
            "provider": self.provider,
            "model": self.model,
            "model_requested": self.model_requested,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "catalog_digest": self.catalog_digest,
            "usage": self.usage,
            "rephrase": self.rephrase,
            "warnings": self.warnings,
            "latency_ms": self.latency_ms,
        }


def clean_question(text: object) -> str:
    q = sanitize_text(text)
    if len(q.encode()) > MAX_QUESTION_BYTES:
        raise TextTooLong(f"question larger than {MAX_QUESTION_BYTES} bytes (UTF-8)", max_bytes=MAX_QUESTION_BYTES)
    return q


def plan_request(question: str, catalog: QaCatalog, budget: LlmBudget) -> LlmRequest:
    return LlmRequest(
        system_prompt=system_prompt(catalog, question),
        user_content=user_envelope(question),
        output_schema=output_schema(),
        schema_name=SCHEMA_NAME,
        max_output_tokens=budget.max_output_tokens,
        prompt_version=PROMPT_VERSION,
        text_sha256=hashlib.sha256(question.encode()).hexdigest(),
    )


def rephrase_request(language: str, composed: Composed, budget: LlmBudget) -> LlmRequest:
    payload = rephrase_payload(language, composed.sentences)
    return LlmRequest(
        system_prompt=REPHRASE_RULES,
        user_content=payload,
        output_schema=rephrase_schema(),
        schema_name=REPHRASE_SCHEMA_NAME,
        max_output_tokens=budget.max_output_tokens,
        prompt_version=REPHRASE_PROMPT_VERSION,
        text_sha256=hashlib.sha256(payload.encode()).hexdigest(),
    )


def _add_usage(total: dict[str, int], r: LlmResponse) -> None:
    for k, v in r.usage.as_json().items():
        total[k] = total.get(k, 0) + int(v)
    total["requests"] = total.get("requests", 0) + 1


def _sub_key(key: str, suffix: str) -> str:
    return hashlib.sha256(f"{key}:{suffix}".encode()).hexdigest()


async def answer_question(
    inp: AskInput,
    *,
    catalog: QaCatalog,
    backends: Backends,
    drafters: Sequence[ScenarioDrafter] | None,
    ledger: LlmLedger | None,
    budget: LlmBudget | None,
) -> AskResult:
    t0 = time.perf_counter()
    question = clean_question(inp.question)
    qsha = hashlib.sha256(question.encode()).hexdigest()
    warnings: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "requests": 0}
    response: LlmResponse | None = None
    planner = "keyword_fallback"
    prompt_sha = None
    if drafters:
        assert ledger is not None and budget is not None
        request = plan_request(question, catalog, budget)
        prompt_sha = prompt_digest(catalog, question)
        try:
            response, failures = await call_providers(request, drafters, ledger, inp.operation_key)
        except (BudgetExhausted, LlmProviderError, LlmUnavailable) as e:
            # The planner is unavailable (budget, provider, ledger): the keyword planner answers instead,
            # labelled; nothing is guessed and no further LLM call is made for this question.
            response, failures = None, []
            reason = e.details.get("reason")
            warnings.append(
                f"llm_planner_unavailable: {e.code}" + (f" ({reason})" if reason else "") + "; keyword plan used"
            )
            inp.rephrase = inp.rephrase and not isinstance(e, BudgetExhausted)
        for f in failures:
            warnings.append(f"fallback_provider: {f['provider']} failed ({f['reason']})")
        if response is not None:
            _add_usage(usage, response)
            planner = "llm"
    if response is not None and response.refusal:
        warnings.append("llm_refused: the planner model refused; keyword plan used")
        raw = keyword_plan(question, catalog)
        planner = "keyword_fallback"
    elif response is not None:
        raw = response.output
    else:
        raw = keyword_plan(question, catalog)
    try:
        plan = validate_plan(raw, catalog, question, planner=planner)
    except ValueError as e:
        warnings.append(f"llm_plan_invalid: {str(e)[:120]}; keyword plan used")
        plan = validate_plan(keyword_plan(question, catalog), catalog, question, planner="keyword_fallback")
    if inp.lang in ("fr", "en"):
        plan.language = inp.lang
    m = _INSTRUCTION_RULE.pattern.search(question)
    if m:
        warnings.append("instruction_in_question: instructions in the question were treated as data")
        plan.unsupported.append(
            {
                "text": m.group(0)[:200],
                "reason": "instruction in question ignored",
                "source": "server_guard",
                "in_question": True,
            }
        )
    warnings.extend(plan.warnings)
    execution = await asyncio.to_thread(Executor(backends, catalog).run, plan)
    warnings.extend(execution.warnings)
    warnings.extend(backends.notes)
    composed = compose(execution, question, catalog)
    result = AskResult(
        question_sha256=qsha,
        plan=plan,
        restatement=restate(plan, catalog),
        answer_text=composed.text,
        composed=composed,
        evidence=execution.evidence.items,
        planner=plan.planner,
        provider=response.provider if response else None,
        model=response.model if response else None,
        model_requested=response.model_requested if response else None,
        prompt_sha256=prompt_sha,
        catalog_digest=catalog.digest(),
        usage=usage,
        warnings=warnings,
    )
    if inp.rephrase:
        await _rephrase(result, inp, drafters, ledger, budget)
    result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return result


async def _rephrase(
    result: AskResult,
    inp: AskInput,
    drafters: Sequence[ScenarioDrafter] | None,
    ledger: LlmLedger | None,
    budget: LlmBudget | None,
) -> None:
    rep: dict[str, Any] = {"requested": True}
    result.rephrase = rep
    # the "Sources et licences" list is a deterministic inventory of the evidence: never rephrased
    sentences = [s for s in result.composed.sentences if s.kind != "sources"]
    if not drafters or ledger is None or budget is None:
        rep["status"] = "skipped_llm_disabled"
        return
    if len(sentences) > MAX_REPHRASE_SENTENCES or len(result.answer_text) > MAX_REPHRASE_CHARS:
        rep["status"] = "skipped_too_long"
        return
    request = rephrase_request(result.plan.language, Composed(sentences), budget)
    try:
        response, _ = await call_providers(request, drafters, ledger, _sub_key(inp.operation_key, "rephrase"))
    except (LlmProviderError, BudgetExhausted, LlmUnavailable) as e:
        rep["status"] = "provider_error" if isinstance(e, LlmProviderError) else e.code
        rep["reason"] = e.details.get("reason")
        result.warnings.append("rephrase_failed: template text returned")
        return
    _add_usage(result.usage, response)
    rep["model"] = response.model
    if response.refusal:
        rep["status"] = "refused"
        return
    evidence = {e["id"]: e for e in result.evidence}
    report, texts = check_faithful(sentences, response.output, evidence)
    rep["check"] = {"ok": report.ok, "problems": report.problems}
    if not report.ok:
        rep["status"] = "rejected_unfaithful"
        result.warnings.append(
            "rephrase_rejected: the rephrased text failed the evidence check; template text returned"
        )
        return
    for s in sentences:
        rep.setdefault("template_text", {})[s.id] = s.text
        s.text = texts[s.id]
    result.answer_text = result.composed.text
    rep["status"] = "accepted"
