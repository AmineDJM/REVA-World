"""Questions about the world: ``POST /v1/ask`` (V3, agent NLQA).

A question in French or English is translated into a typed query plan (by the LLM, or by a
deterministic keyword planner when the LLM is not enabled for questions), the plan is validated
against the allow-lists, executed against the KG, the atlas and the interconnection layer, and
the answer is composed **only** from what those sources return: every sentence cites evidence
(Wikidata statement id + revision, or atlas source/dataset/indicator/raw sha256/selector), missing
data is said as missing, a path is a connection and not a cause.

Controls: scope ``read`` (it reads sourced data and writes nothing but the LLM ledger and one
audit row); body ≤ 8 KiB, question ≤ 2048 UTF-8 bytes; a per-actor rate limit
(``REVA_NLQA_RATE_PER_MINUTE``, default 10/min → 429 ``rate_limited``); every LLM request is
charged to the persistent daily LLM budget *before* it is sent (``reva.nl.service.call_providers``:
ledger reserve → provider → settle).

LLM use is **opt-in**: it needs ``REVA_LLM_ENABLED`` *and* ``REVA_NLQA_LLM=1``. A dedicated
``llm.nlqa`` line is deferred (it needs a migration for a separate LLM ledger), so questions reuse the
V2 line ``llm.scenario_drafts`` and its ledger accounts. When the planner LLM is not enabled, or is
refused (budget exhausted, provider error, ledger unavailable), the deterministic keyword planner
answers instead (``planner: keyword_fallback`` + a warning naming the reason) — never an invented answer.
The audit row holds the question's sha256 and length, never its text.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from reva.api.auth import Principal, require_scope
from reva.api.deps import get_engine
from reva.api.errors import ApiProblem
from reva.kernel.errors import RevaError
from reva.nl.ports import ScenarioDrafter

router = APIRouter(prefix="/v1", tags=["ask"])

MAX_ASK_BODY_BYTES = 8192
NLQA_LLM_ENV = "REVA_NLQA_LLM"
RATE_ENV = "REVA_NLQA_RATE_PER_MINUTE"
DEFAULT_RATE_PER_MINUTE = 10
BACKENDS_TTL_SECONDS = 300.0


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(..., description="The question in French or English (≤ 2048 UTF-8 bytes). Treated as data.")
    lang: Literal["fr", "en"] | None = Field(None, description="Answer language (default: the question's)")
    rephrase: bool = Field(
        False,
        description="Let the LLM rephrase the composed answer; accepted only if every number, date and name "
        "is found in the cited evidence (else the template text is returned)",
    )


class AskSentence(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    text: str
    kind: str = Field(description="fact | path | identification | missing | unavailable | unsupported | note")
    citations: list[str] = Field(description="Evidence ids (E1, E2, …) this sentence rests on")
    citation_refs: list[str] = Field(description="Compact human-readable form of each citation")


class AskResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    question_sha256: str
    language: str
    plan: dict[str, Any] = Field(description="The validated typed query plan (planner, operations, rejected)")
    restatement: str = Field(description="Server restatement of the validated plan")
    llm_restatement: str = Field(description="The planner model's own restatement (untrusted, for comparison)")
    answer_text: str
    sentences: list[AskSentence]
    evidence: list[dict[str, Any]] = Field(description="Every cited record with its full provenance")
    unsupported: list[dict[str, Any]]
    planner: str = Field(description="llm | keyword_fallback")
    provider: str | None
    model: str | None = Field(description="Model as reported by the provider (e.g. gpt-4.1-2025-04-14)")
    model_requested: str | None
    prompt_version: str
    prompt_sha256: str | None
    catalog_digest: str
    usage: dict[str, int]
    rephrase: dict[str, Any]
    warnings: list[str]
    latency_ms: float


class _RateLimiter:
    """Sliding one-minute window per actor (per process)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check(self, actor: str) -> float | None:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(actor, deque())
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return 60 - (now - q[0])
            q.append(now)
            if len(self._hits) > 10_000:
                for k in [k for k, v in self._hits.items() if not v][:5_000]:
                    self._hits.pop(k, None)
            return None


def nlqa_llm_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "reva_llm_enabled", False)) and os.environ.get(NLQA_LLM_ENV, "").lower() in (
        "1",
        "true",
        "yes",
    )


def default_planners(settings: Any) -> list[ScenarioDrafter]:
    """The LLM providers for planning (same adapters and fallback rules as NL drafts), or ``[]`` when
    LLM questions are not enabled (keyword planner)."""
    if not nlqa_llm_enabled(settings):
        return []
    from reva.api.routes.nl import default_drafters

    return list(default_drafters(settings))


def _backends(request: Request) -> Any:
    return _backends_of(request.app.state, lambda: get_engine(request))


def _backends_of(state: Any, engine: Callable[[], Any]) -> Any:
    fixed = getattr(state, "nlqa_backends", None)
    if fixed is not None:
        return fixed
    cached = getattr(state, "nlqa_backends_cache", None)
    if cached is not None and time.monotonic() - cached[0] < BACKENDS_TTL_SECONDS:
        return cached[1]
    from reva.nlqa.adapters import build_backends

    b = build_backends(engine(), state.settings, atlas_store=getattr(state, "atlas_store", None))
    state.nlqa_backends_cache = (time.monotonic(), b)
    return b


#: ``REVA_NLQA_WARMUP`` (``1`` / ``0``): open the atlas query file in the background when the API starts, so
#: the first atlas question does not wait for its ATLAS-1 verification (default: on, except ``REVA_ENV=test``).
WARMUP_ENV = "REVA_NLQA_WARMUP"


def warmup_enabled(settings: Any) -> bool:
    v = os.environ.get(WARMUP_ENV)
    if v is not None:
        return v.strip().lower() in ("1", "true", "yes")
    return getattr(settings, "reva_env", "") != "test"


def warm_up(state: Any) -> dict[str, Any]:
    """Build the question backends and open the atlas (APIFIX). Returns what it did, for the log; never raises."""
    t0 = time.monotonic()
    out: dict[str, Any]
    try:
        b = _backends_of(state, state.engine)
        atlas: Any = getattr(b, "atlas", None)
        ready = atlas.warm() if hasattr(atlas, "warm") else atlas is not None
        out = {"atlas_ready": bool(ready), "seconds": round(time.monotonic() - t0, 2)}
    except Exception as e:  # a warm-up failure only means the first question pays the cost
        out = {"error": type(e).__name__, "seconds": round(time.monotonic() - t0, 2)}
    state.nlqa_warmup = out
    return out


def start_warm_up(state: Any) -> threading.Thread | None:
    if not warmup_enabled(getattr(state, "settings", None)):
        return None
    t = threading.Thread(target=warm_up, args=(state,), name="reva-nlqa-warmup", daemon=True)
    t.start()
    return t


def _catalog(request: Request) -> Any:
    """The allow-lists (atlas catalog via ATLASCORE's loader, crosswalk, KG properties); cached loaders."""
    fixed = getattr(request.app.state, "nlqa_catalog", None)
    if fixed is not None:
        return fixed
    from reva.nlqa.catalog import build_catalog

    return build_catalog()


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_ASK_BODY_BYTES):
        raise ApiProblem(
            413,
            "payload_too_large",
            f"request body larger than {MAX_ASK_BODY_BYTES} bytes",
            max_bytes=MAX_ASK_BODY_BYTES,
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ASK_BODY_BYTES:
            raise ApiProblem(
                413,
                "payload_too_large",
                f"request body larger than {MAX_ASK_BODY_BYTES} bytes",
                max_bytes=MAX_ASK_BODY_BYTES,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(raw: bytes) -> AskRequest:
    try:
        return AskRequest.model_validate_json(raw)
    except ValidationError as e:
        errors = [
            {"loc": [str(p) for p in err.get("loc", ())], "type": str(err.get("type", ""))} for err in e.errors()[:10]
        ]  # never echo the question
        raise ApiProblem(422, "validation_error", "request does not satisfy the contract", errors=errors) from None


@router.post(
    "/ask",
    response_model=AskResponse,
    status_code=200,
    responses={
        413: {"description": "Body larger than 8 KiB or question larger than 2048 bytes"},
        422: {"description": "Invalid request"},
        429: {"description": "Rate limited (per actor)"},
    },
    openapi_extra={
        "requestBody": {"required": True, "content": {"application/json": {"schema": AskRequest.model_json_schema()}}}
    },
)
async def ask(request: Request, principal: Principal = Depends(require_scope("read"))) -> dict[str, Any]:
    """Answer a question about the world **only from sourced data** (KG statements, atlas cells, derived
    links). Every sentence cites evidence; missing data is said as missing; paths are connections, not causes.
    """
    from reva.nl.budget import DbLlmLedger, LlmBudget, load_llm_budget
    from reva.nlqa.service import AskInput, answer_question

    state = request.app.state
    raw = await _read_body(request)
    body = _parse(raw)
    limiter: _RateLimiter | None = getattr(state, "nlqa_rate_limiter", None)
    if limiter is None:
        try:
            per_minute = int(os.environ.get(RATE_ENV) or DEFAULT_RATE_PER_MINUTE)
        except ValueError:
            per_minute = DEFAULT_RATE_PER_MINUTE
        limiter = state.nlqa_rate_limiter = _RateLimiter(max(1, per_minute))
    wait = limiter.check(principal.actor)
    if wait is not None:
        raise ApiProblem(429, "rate_limited", "too many questions; retry later", retry_after=int(wait) + 1)
    qsha = hashlib.sha256(body.question.encode("utf-8", "surrogatepass")).hexdigest()
    target = {"kind": "question", "question_sha256": qsha}
    base = {"question_length": len(body.question)}
    factory: Callable[[Any], Sequence[ScenarioDrafter]] = getattr(state, "nlqa_planner_factory", None) or (
        default_planners
    )
    try:
        drafters = list(factory(state.settings))
        ledger = budget = None
        if drafters:
            budget = getattr(state, "nlqa_budget", None) or load_llm_budget()
            assert isinstance(budget, LlmBudget)
            ledger = getattr(state, "nlqa_ledger", None) or DbLlmLedger(get_engine(request), budget)
        backends = await asyncio.to_thread(_backends, request)
        catalog = await asyncio.to_thread(_catalog, request)
        op_key = hashlib.sha256(f"nlqa\x00{principal.actor}\x00{uuid.uuid4().hex}".encode()).hexdigest()
        result = await answer_question(
            AskInput(question=body.question, operation_key=op_key, lang=body.lang, rephrase=body.rephrase),
            catalog=catalog,
            backends=backends,
            drafters=drafters,
            ledger=ledger,
            budget=budget,
        )
    except RevaError as e:
        await asyncio.to_thread(
            state.auth.audit,
            principal.actor,
            "ask.refused",
            target,
            {**base, "code": e.code, "reason": e.details.get("reason")},
        )
        raise
    out = result.as_json()
    if not drafters:
        out["warnings"].insert(
            0,
            "llm_not_enabled_for_questions: keyword planner used (set REVA_LLM_ENABLED and "
            "REVA_NLQA_LLM=1 for the LLM planner)",
        )
    await asyncio.to_thread(
        state.auth.audit,
        principal.actor,
        "ask.answered",
        target,
        {
            **base,
            "planner": result.planner,
            "provider": result.provider,
            "model": result.model,
            "prompt_version": result.prompt_version,
            "catalog_digest": result.catalog_digest,
            "usage": result.usage,
            "operations": [o.op for o in result.plan.operations],
            "rejected": len(result.plan.rejected),
            "sentences": len(result.composed.sentences),
            "evidence": len(result.evidence),
            "rephrase": result.rephrase.get("status"),
        },
    )
    return out
