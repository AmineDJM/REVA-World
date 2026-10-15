"""Natural-language scenario drafts: ``POST /v1/scenario-drafts`` (V2 step 2).

The endpoint turns a sentence into a *draft* ScenarioSpec through an LLM and
returns it with a server-generated restatement, the unsupported parts (typed
capability gaps), the numbers to confirm, the provider/model that answered,
the prompt version and the token usage. **Nothing is executed or stored**: no
scenario row, no job. The user runs a draft only by submitting the reviewed
spec to ``POST /v1/scenarios`` (the confirmation step), where it is validated
again exactly like a hand-written spec.

Controls: scope ``simulate``; ``Idempotency-Key`` required; request body at
most 8 KiB and ``text`` at most 4096 UTF-8 bytes (413); LLM drafting must be
enabled (``REVA_LLM_ENABLED``, else 503 ``llm_unavailable``); every provider
request is charged to the persistent daily LLM budget first (429
``budget_exhausted``, nothing sent). One audit row per request stores a
sha256 and the length of the text, never the text itself (it may contain
personal data), nor the restatement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine

from reva.api.auth import Principal, require_scope
from reva.api.deps import get_engine, idempotency_key
from reva.api.errors import ApiProblem
from reva.kernel.errors import RevaError
from reva.nl.budget import DbLlmLedger, LlmBudget, load_llm_budget
from reva.nl.guards import MAX_TEXT_BYTES, sanitize_text
from reva.nl.ports import LlmUnavailable, ScenarioDrafter
from reva.nl.providers import DEFAULT_GEMINI_MODEL, DEFAULT_OPENAI_MODEL, GeminiDrafter, OpenAIDrafter
from reva.nl.service import DraftInput, draft_scenario

router = APIRouter(prefix="/v1", tags=["scenario-drafts"])

MAX_DRAFT_BODY_BYTES = 8192
FALLBACK_ENV = "REVA_LLM_FALLBACK"  # "gemini" (default), "openai" or "none"
FALLBACK_MODEL_ENV = "REVA_LLM_FALLBACK_MODEL"


class ScenarioDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., description="The request in plain French or English (at most 4096 UTF-8 bytes). "
                      "Treated as data: it never changes the system's instructions.")
    baseline_release_id: UUID | None = Field(
        None, description="Published belief release the draft is pinned to (default: the latest published one)")


class DraftGap(BaseModel):
    text: str
    capability_key: str
    kind: str = Field(description="capability_gap | policy_refusal")
    reason: str
    source: str = Field(description="llm | server_guard | server | validator | provider")
    llm_capability_key_rejected: bool | None = None
    blocks_draft: bool | None = Field(None, description="true when this item withholds the draft (needs_input)")


class DraftNumber(BaseModel):
    path: str
    value: Any
    provenance: str = Field(description="stated | converted_from_stated | not_in_text | server_default | "
                            "schema_constant")
    quote: str | None = None


class DraftAssumption(BaseModel):
    text: str
    source: str


class ScenarioDraftResponse(BaseModel):
    status: str = Field(description="draft_ready | needs_input | draft_invalid | draft_rejected | "
                        "nothing_draftable")
    confirmation_required: bool
    executed: bool
    draft_spec: dict[str, Any] | None = Field(description="Valid ScenarioSpec (status draft_ready only); runs "
                                              "only when submitted to POST /v1/scenarios")
    draft_spec_sha256: str | None
    candidate_spec: dict[str, Any] | None = Field(description="Incomplete or invalid spec, for review only")
    validation: dict[str, Any]
    restatement: str = Field(description="Server-generated restatement of the draft (input language)")
    language: str
    llm_restatement: str = Field(description="The LLM's paraphrase of the request (untrusted, for comparison)")
    unsupported: list[DraftGap]
    rejected_ids: list[dict[str, Any]]
    missing_fields: list[str]
    assumptions_added: list[DraftAssumption]
    numbers_to_confirm: list[DraftNumber]
    parameters_set: list[dict[str, Any]] = Field(description="Every parameter the LLM set, with its quote")
    provider: str
    model: str
    model_requested: str
    prompt_version: str
    prompt_sha256: str
    catalog_digest: str
    usage: dict[str, Any]
    latency_ms: float
    fallback_from: list[dict[str, Any]]
    warnings: list[str]
    next_step: str


class _DraftCache:
    """Per-process idempotency cache: (actor, key) -> (body hash, response). Drafts are never persisted."""

    def __init__(self, max_entries: int = 256, ttl_seconds: float = 86_400) -> None:
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()

    def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if time.monotonic() - item[0] > self.ttl:
                self._items.pop(key, None)
                return None
            return item[1], item[2]

    def put(self, key: str, body_hash: str, response: dict[str, Any]) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), body_hash, response)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


def default_drafters(settings: Any) -> list[ScenarioDrafter]:
    """Primary provider from settings (``REVA_LLM_PROVIDER``, default openai; ``REVA_LLM_MODEL``), plus the
    fallback provider (``REVA_LLM_FALLBACK``: gemini by default, ``none`` to disable)."""
    if not getattr(settings, "reva_llm_enabled", False):
        raise LlmUnavailable("natural-language drafting is disabled (REVA_LLM_ENABLED=false); write the "
                             "ScenarioSpec by hand or enable the LLM drafter", reason="llm_disabled")
    proxy_mode = getattr(settings, "reva_http_proxy_mode", None)
    primary = (getattr(settings, "reva_llm_provider", None) or "openai").lower()
    model = getattr(settings, "reva_llm_model", None)
    fallback = os.environ.get(FALLBACK_ENV, "gemini" if primary == "openai" else "openai").lower()
    fallback_model = os.environ.get(FALLBACK_MODEL_ENV) or None

    def make(provider: str, model_name: str | None) -> ScenarioDrafter:
        if provider == "openai":
            return OpenAIDrafter(model_name or DEFAULT_OPENAI_MODEL, proxy_mode=proxy_mode)
        if provider == "gemini":
            return GeminiDrafter(model_name or DEFAULT_GEMINI_MODEL, proxy_mode=proxy_mode)
        raise LlmUnavailable(f"unknown LLM provider {provider!r} (openai or gemini)", reason="unknown_provider")

    out = [make(primary, model)]
    if fallback not in ("none", "", primary):
        out.append(make(fallback, fallback_model))
    return out


def _resolve_baseline(engine: Engine, release_id: UUID | None) -> dict[str, Any]:
    from reva.belief.snapshots import release_as_of, release_by_id

    with engine.connect() as conn:
        rel = release_by_id(conn, release_id) if release_id is not None else release_as_of(conn, None)
    if rel is None or rel.get("sequence") is None:
        if release_id is not None:
            raise ApiProblem(422, "validation_error", "baseline_release_id is not a published belief release",
                             baseline_release_id=str(release_id))
        raise ApiProblem(409, "no_published_release",
                         "no published belief release exists: a draft must be pinned to a published baseline")
    return rel


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_DRAFT_BODY_BYTES):
        raise ApiProblem(413, "payload_too_large", f"request body larger than {MAX_DRAFT_BODY_BYTES} bytes",
                         max_bytes=MAX_DRAFT_BODY_BYTES)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_DRAFT_BODY_BYTES:
            raise ApiProblem(413, "payload_too_large", f"request body larger than {MAX_DRAFT_BODY_BYTES} bytes",
                             max_bytes=MAX_DRAFT_BODY_BYTES)
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(raw: bytes) -> ScenarioDraftRequest:
    try:
        return ScenarioDraftRequest.model_validate_json(raw)
    except ValidationError as e:
        # Never echo the submitted text in the error (it may contain personal data).
        errors = [{"loc": [str(p) for p in err.get("loc", ())], "type": str(err.get("type", ""))}
                  for err in e.errors()[:10]]
        raise ApiProblem(422, "validation_error", "request does not satisfy the contract", errors=errors) from None


def _audit(request: Request, principal: Principal, action: str, target: dict[str, Any],
           details: dict[str, Any]) -> None:
    request.app.state.auth.audit(principal.actor, action, target, details)


@router.post(
    "/scenario-drafts",
    response_model=ScenarioDraftResponse,
    status_code=200,
    responses={409: {"description": "Idempotency-Key reuse or no published release"},
               413: {"description": "Body larger than 8 KiB or text larger than 4096 bytes"},
               422: {"description": "Invalid request"},
               429: {"description": "Daily LLM budget exhausted (nothing was sent)"},
               502: {"description": "LLM provider failed; no draft"},
               503: {"description": "LLM drafting disabled or budget ledger unavailable"}},
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": ScenarioDraftRequest.model_json_schema()}}}},
)
async def create_scenario_draft(
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope("simulate")),
    key: str = Depends(idempotency_key),
) -> dict[str, Any]:
    """Draft a ScenarioSpec from natural language. **Nothing is executed or stored.**

    The draft is re-validated by the same validator as hand-written specs; numbers proposed for the
    intervention are listed in ``numbers_to_confirm``. Run it only after review, by submitting
    ``draft_spec`` to ``POST /v1/scenarios``.
    """
    state = request.app.state
    raw = await _read_body(request)
    body = _parse(raw)
    if len(body.text.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        raise ApiProblem(413, "payload_too_large", f"text is larger than {MAX_TEXT_BYTES} bytes (UTF-8)",
                         max_bytes=MAX_TEXT_BYTES)
    text = sanitize_text(body.text)
    text_sha = hashlib.sha256(text.encode()).hexdigest()
    body_hash = hashlib.sha256(json.dumps({"t": text, "b": str(body.baseline_release_id or "")}).encode()).hexdigest()
    op_key = hashlib.sha256(f"{principal.actor}\x00{key}".encode()).hexdigest()
    cache: _DraftCache = getattr(state, "nl_draft_cache", None) or _DraftCache()
    state.nl_draft_cache = cache
    cached = cache.get(op_key)
    if cached is not None:
        if cached[0] != body_hash:
            raise ApiProblem(409, "idempotency_conflict", "Idempotency-Key was already used for a different request")
        response.headers["Idempotent-Replayed"] = "true"
        return cached[1]

    factory: Callable[[Any], Sequence[ScenarioDrafter]] = getattr(state, "nl_drafter_factory", None) \
        or default_drafters
    target = {"kind": "scenario_draft", "text_sha256": text_sha}
    base_details = {"text_length": len(text), "idempotency_key_sha256": hashlib.sha256(key.encode()).hexdigest()}
    engine = get_engine(request)
    try:
        drafters = list(factory(state.settings))
        budget: LlmBudget = getattr(state, "nl_budget", None) or load_llm_budget()
        rel = await asyncio.to_thread(_resolve_baseline, engine, body.baseline_release_id)
        cutoff = rel["knowledge_cutoff"].astimezone(UTC).isoformat().replace("+00:00", "Z")
        result = await draft_scenario(
            DraftInput(text=text, baseline_release_id=str(rel["id"]), knowledge_cutoff=cutoff, operation_key=op_key),
            drafters=drafters, ledger=DbLlmLedger(engine, budget), budget=budget)
    except RevaError as e:
        await asyncio.to_thread(_audit, request, principal, "scenario_draft.refused", target,
                                {**base_details, "code": e.code, "reason": e.details.get("reason")})
        raise
    out = result.as_json()
    await asyncio.to_thread(_audit, request, principal, "scenario_draft.created", target, {
        **base_details, "status": result.status, "provider": result.provider, "model": result.model,
        "prompt_version": result.prompt_version, "prompt_sha256": result.prompt_sha256,
        "catalog_digest": result.catalog_digest, "usage": result.usage,
        "draft_spec_sha256": result.draft_spec_sha256, "baseline_release_id": str(rel["id"]),
        "unsupported_keys": sorted({u["capability_key"] for u in result.unsupported}),
        "fallback_from": [f.get("provider") for f in result.fallback_from]})
    cache.put(op_key, body_hash, out)
    return out
