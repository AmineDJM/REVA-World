"""Live LLM drafting check (deselected by default: ``-m live``). Makes ONE real provider call.

Charged to an in-memory per-run cap of one request (no persistent ledger: like the
operator live-check scripts, it never touches a database).
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from reva.nl.budget import load_llm_budget
from reva.nl.providers import DEFAULT_OPENAI_MODEL, OpenAIDrafter
from reva.nl.service import DraftInput, draft_scenario

pytestmark = pytest.mark.live


class OneRequestLedger:
    def __init__(self) -> None:
        self.reserved = 0

    def reserve(self, operation_id: str, *, requests: int, tokens: int) -> None:
        from reva.operations.budgets import BudgetExhausted

        if self.reserved + requests > 1:
            raise BudgetExhausted("live test cap: one request", reason="request_limit")
        self.reserved += requests

    def settle(self, operation_id: str, *, requests: int, tokens: int) -> None:
        pass


def test_live_openai_drafts_the_basic_request() -> None:
    model = os.environ.get("REVA_LLM_MODEL") or DEFAULT_OPENAI_MODEL
    text = "What happens to France and the USA if crude oil import prices rise by 20% in 2023?"
    inp = DraftInput(
        text=text,
        baseline_release_id="11111111-1111-4111-8111-111111111111",
        knowledge_cutoff="2026-10-01T00:00:00Z",
        operation_key=hashlib.sha256(text.encode()).hexdigest(),
    )
    r = asyncio.run(
        draft_scenario(inp, drafters=[OpenAIDrafter(model)], ledger=OneRequestLedger(), budget=load_llm_budget())
    )
    print(f"live model={r.model} latency_ms={r.latency_ms} usage={r.usage} status={r.status}")
    assert r.provider == "openai" and r.model.startswith(model)
    assert r.status in ("draft_ready", "needs_input")
    spec = r.draft_spec or r.candidate_spec
    assert spec["model_key"] == "economic.crude_exposure" and set(spec["countries"]) == {"FRA", "USA"}
    assert spec["intervention"]["import_multiplier"] == pytest.approx(1.2)
