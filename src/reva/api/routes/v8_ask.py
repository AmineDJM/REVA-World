"""V8 « Demander » (agent V8ASK): cited answers as page bundles, and the « Brainstormer ».

* ``POST /v1/v8/ask/answer`` — a French or English question → the NLQA answer (same pipeline as ``POST /v1/ask``:
  keyword planner, plan validation, executor, deterministic composer; every sentence cites evidence) plus the
  page bundle: short answer (exact number or sentence, or « REVA ne sait pas » + what is missing), evidence view
  (time series / ranked bars / forecast bands / relations graph / causal hops / estimates / scenario), « Comment
  c'est calculé » (operations, tables, periods, as-of), sources with licences, deep-link targets, follow-ups and
  the pin reference. No LLM is called here (no network); nothing is written (no audit row, no ledger).
* ``GET /v1/v8/ask/brainstorm?target=…`` — drivers with attributed effect estimates and causal claims, which have
  loaded series (freshness) and which are missing (catalog candidates, queued loads quoted from the reports),
  existing sealed forecasts and their judged status vs official forecasters, ranked next steps. Pure retrieval
  and rules over REVA's registries.
* ``GET /v1/v8/ask/examples`` — example questions and brainstorm targets for the empty page.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from reva.api.auth import Principal, require_scope
from reva.api.errors import ApiProblem
from reva.api.routes.ask import _backends, _catalog

router = APIRouter(prefix="/v1/v8/ask", tags=["v8"])

RATE_ENV = "REVA_V8_ASK_RATE_PER_MINUTE"
DEFAULT_RATE = 30


class _M(BaseModel):
    model_config = ConfigDict(extra="allow")


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(..., min_length=1, max_length=2048, description="Question (fr|en), treated as data")
    lang: Literal["fr", "en"] | None = Field(None, description="Answer language (default: the question's)")


class Headline(_M):
    kind: str = Field(description="number | sentence | partial | unknown")
    text: str
    citations: list[str] = []


class AnswerBundle(_M):
    question: str
    language: str | None
    status: str = Field(description="answered | partial | unknown (« REVA ne sait pas »)")
    headline: Headline
    missing: list[str] = Field(description="What is missing when REVA does not know (cause classes)")
    viz: dict[str, Any] | None = Field(description="Evidence view chosen from the evidence shape, or null")
    how: dict[str, Any] = Field(description="Operations, tables, periods, as-of, planner, latency")
    sources: list[dict[str, Any]]
    targets: list[dict[str, Any]] = Field(
        description="Deep-link targets (fiche, monde, comparer, liens, decision, scores)"
    )
    followups: list[dict[str, Any]]
    pin: dict[str, Any]
    sentences: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    answer_text: str | None
    warnings: list[str]
    latency_ms: float | None
    cached: bool = False


class BrainstormBundle(_M):
    target: dict[str, Any]
    known: bool
    anchors: dict[str, Any]
    target_series: list[dict[str, Any]]
    drivers: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    forecasts: list[dict[str, Any]]
    verdicts: list[dict[str, Any]]
    next_steps: list[dict[str, Any]]
    method: str
    as_of: str
    latency_ms: float | None = None


EXAMPLES = {
    "questions": [
        "Causes de décès en France",
        "Quelle est la prévision d'inflation pour la France ?",
        "Que se passe-t-il si la BCE monte ses taux de 50 pb ?",
        "Quel est l'impact d'une hausse des taux directeurs sur le PIB ?",
        "Que mange le loup ?",
        "Qui possède la mine KOV ?",
        "Quelles sont les parties du cœur ?",
        "Quel est le prix du Brent ?",
        "Which coal power plants are in India?",
        "Quelles maladies causées par Mycobacterium tuberculosis ?",
    ],
    "brainstorms": [
        "prévoir l'inflation française à 4 trimestres",
        "prévoir le prix du Brent",
        "prévoir le chômage allemand",
    ],
}


class _Limiter:
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
            return None


def _limit(request: Request, actor: str) -> None:
    st = request.app.state
    lim: _Limiter | None = getattr(st, "v8_ask_limiter", None)
    if lim is None:
        try:
            n = int(os.environ.get(RATE_ENV) or DEFAULT_RATE)
        except ValueError:
            n = DEFAULT_RATE
        lim = st.v8_ask_limiter = _Limiter(max(1, n))
    wait = lim.check(actor)
    if wait is not None:
        raise ApiProblem(429, "rate_limited", "too many questions; retry later", retry_after=int(wait) + 1)


def _service(request: Request) -> Any:
    from reva.v8.ask.service import AskService

    backends = _backends(request)
    st = request.app.state
    held = getattr(st, "v8_ask_service", None)
    if held is not None and held[0] is backends:
        return held[1]
    svc = AskService(backends, _catalog(request))
    st.v8_ask_service = (backends, svc)
    return svc


@router.post("/answer", response_model=AnswerBundle)
async def answer(
    body: AnswerRequest, request: Request, principal: Principal = Depends(require_scope("read"))
) -> dict[str, Any]:
    """Answer a question only from sourced data, as a page bundle (answer first, then evidence)."""
    _limit(request, principal.actor)
    svc = await asyncio.to_thread(_service, request)
    return await svc.answer(body.question, lang=body.lang)  # type: ignore[no-any-return]


@router.get("/brainstorm", response_model=BrainstormBundle)
async def brainstorm(
    request: Request,
    target: str = Query(..., min_length=2, max_length=300, description="e.g. « prévoir le prix du Brent »"),
    principal: Principal = Depends(require_scope("read")),
) -> dict[str, Any]:
    """What REVA's registries say about forecasting a target: drivers, data, forecasts, verdicts, next steps."""
    _limit(request, principal.actor)
    t0 = time.perf_counter()
    svc = await asyncio.to_thread(_service, request)
    out: dict[str, Any] = await asyncio.to_thread(svc.brainstorm, target)
    out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


@router.get("/examples", dependencies=[Depends(require_scope("read"))])
async def examples() -> dict[str, Any]:
    """Example questions and brainstorm targets (curated from the golden set; each answers on the dev data)."""
    return EXAMPLES
