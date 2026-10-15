"""Live planner check (deselected by default: ``-m live``). Makes ONE real gpt-4.1 call through the
environment proxy, charged to an in-memory per-run cap of one request (no persistent ledger, no database).

When the provider account cannot serve requests (HTTP 402/429 quota or billing), the test is skipped with
the provider's typed reason — as happened on 2026-10-15 (see docs/dev/v3_nlqa.md, "Live run").
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from reva.nl.budget import load_llm_budget
from reva.nl.providers import DEFAULT_OPENAI_MODEL, OpenAIDrafter
from reva.nlqa.service import AskInput, answer_question

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "nlqa"))
import nlqa_fakes as F

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


def test_live_gpt41_plans_the_minimum_wage_question() -> None:
    model = os.environ.get("REVA_LLM_MODEL") or DEFAULT_OPENAI_MODEL
    r = asyncio.run(
        answer_question(
            AskInput("Quel est le salaire minimum au Sénégal ?", "6" * 64),
            catalog=F.catalog(),
            backends=F.backends(),
            drafters=[OpenAIDrafter(model)],
            ledger=OneRequestLedger(),
            budget=load_llm_budget(),
        )
    )
    print(f"live planner={r.planner} model={r.model} usage={r.usage} warnings={r.warnings}")
    if r.planner != "llm":
        quota = [w for w in r.warnings if "llm_planner_unavailable" in w]
        pytest.skip(f"provider unavailable: {quota}")
    assert r.model.startswith(model)
    ops = [(o.op, o.args.get("concept"), o.args.get("country")) for o in r.plan.operations]
    assert ("atlas_value", "labour.minimum_wage_monthly", "SEN") in ops
    assert "58 900" in r.answer_text
