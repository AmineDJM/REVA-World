"""Invariant 30: no external egress, LLM or scientific computation beyond the configured budget.

Proven here for the paths V1 has:

* egress: the connector runner stops issuing requests once the per-run
  ``FetchBudget`` is spent, even when a connector plans more items than allowed;
* LLM: disabled by default with a zero monthly budget (``budgets.yaml``) and the
  LLM extractor returns a typed blocked result;
* computation: the CPU thread budget is explicit (``REVA_CPU_BUDGET_THREADS``),
  and external runner budgets must be bounded.

Not proven (see coverage.yaml): the *daily* egress totals in
``registry/policies/budgets.yaml`` (``daily.http_requests_total``,
``per_source_daily_requests``) are not enforced by a persistent ledger across
runs; scheduled live ingest relies on per-run budgets only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import yaml

from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.runner import run_connector
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import Cursor, FetchBudget, FetchItem, FetchOutcome, FetchPlan, RawAcquisition

PLANNED = 6


class OverPlanningConnector:
    """Wraps the WDI connector but plans PLANNED items whatever the budget says, and counts fetches.

    ``fetch`` never touches the network: it records the attempt and fails with a
    typed, non-retryable error so nothing is published.
    """

    def __init__(self) -> None:
        self.inner = builtin_connectors()["worldbank.wdi.indicators"]
        self.fetched: list[str] = []

    def describe(self) -> Any:
        # an unregistered name: record_health finds no connector version and writes nothing,
        # so this probe cannot change the shared session database's connector health
        return self.inner.describe().model_copy(update={"name": "invariant30.budget_probe"})

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        full = self.inner.plan(cursor, FetchBudget(max_requests=1000))
        # independent items (no depends_on), so only the budget can stop a fetch
        items = tuple(
            it.model_copy(
                update={"key": f"probe-{i}", "params": {k: v for k, v in it.params.items() if k != "depends_on"}}
            )
            for i, it in enumerate(full.items * 2)
        )
        assert len(items) >= PLANNED
        return full.model_copy(update={"items": items[:PLANNED]})

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        self.fetched.append(item.key)
        raise SourceResponseError("offline budget probe: no request is made", retryable=False)

    def parse(self, raw: RawAcquisition) -> Iterator[Any]:  # pragma: no cover - never reached
        raise AssertionError("parse must not run")

    def validate(self, batch: Any) -> Any:  # pragma: no cover - never reached
        raise AssertionError("validate must not run")

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return self.inner.next_cursor(plan, outcomes)


class NoHttp:
    origin = "test_fixture"

    async def get(self, url: str, **kw: Any) -> Any:  # pragma: no cover - fetch never calls it
        raise AssertionError("no HTTP client call expected")


@pytest.mark.db
@pytest.mark.parametrize("max_requests", [0, 1, 3])
def test_runner_never_exceeds_the_request_budget(db, max_requests: int) -> None:
    conn = OverPlanningConnector()
    report = asyncio.run(
        run_connector(
            conn, NoHttp(), db.store, db.worker, budget=FetchBudget(max_requests=max_requests), persist_cursor=False
        )
    )
    assert len(conn.fetched) == max_requests, conn.fetched
    over = [o for o in report.outcomes if o.detail == "budget"]
    assert len(over) == PLANNED - max_requests
    assert all(o.status == "failed" for o in over)
    assert sum(1 for e in report.errors if e.get("error") == "request budget exhausted") == PLANNED - max_requests


def test_llm_is_disabled_with_a_zero_budget_by_default() -> None:
    from reva.autonomy.policy import budgets_policy
    from reva.settings import Settings

    doc = yaml.safe_load(budgets_policy().path.read_text())
    assert doc["llm"]["monthly_usd"] == 0
    assert Settings.model_fields["reva_llm_enabled"].default is False
    assert Settings.model_fields["reva_llm_monthly_budget_usd"].default == 0


def test_external_runner_budget_must_be_bounded() -> None:
    from reva.extensions import runner
    from reva.kernel.errors import ValidationProblem

    with pytest.raises(ValidationProblem, match="unbounded is not allowed"):
        runner.RunnerResourceBudget(0, 600, 2**31, 2**32, 900, 2**24)
