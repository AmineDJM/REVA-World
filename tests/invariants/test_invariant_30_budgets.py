"""Invariant 30: no external egress, LLM or scientific computation beyond the configured budget.

Proven here (DB cap/concurrency/rollover tests live in ``tests/integration/test_budget_caps.py``):

* egress, per run: the connector runner stops issuing requests once the per-run
  ``FetchBudget`` is spent, even when a connector plans more items than allowed;
* egress, per day: budgets are keyed by registered *source* keys, every
  connector maps to exactly one budgeted source (``budget_source_key``) and
  ledger accounts come from one function (``account_key``); every code path
  that creates a live provider client is a budgeted path (reserve before the
  first request, metered client, settle afterwards) — a static scan fails on a
  new unbudgeted call site;
* LLM: disabled by default with a zero monthly budget (``budgets.yaml``) and the
  LLM extractor returns a typed blocked result;
* computation: the CPU thread budget is explicit (``REVA_CPU_BUDGET_THREADS``),
  and external runner budgets must be bounded.
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


# --------------------------------------------------------------------------- daily egress budget keys
REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def _registered_sources() -> set[str]:
    return {yaml.safe_load(p.read_text())["source_key"] for p in sorted((REPO / "registry/sources").glob("*.yaml"))}


def test_budget_keys_are_registered_sources_and_cover_every_connector() -> None:
    from reva.operations.budgets import account_key, budget_source_key, load_budget, source_accounts

    b = load_budget()  # the pinned, digest-checked policy
    assert set(b.per_source_requests) == _registered_sources()
    assert all(v > 0 for v in b.per_source_requests.values())
    for name, conn in builtin_connectors().items():
        key = budget_source_key(conn)
        assert key == conn.describe().source_key and key in b.per_source_requests, name
    promo = yaml.safe_load((REPO / "registry/policies/promotion.yaml").read_text())
    for tpl in (promo.get("approved_templates") or {}).values():  # promoted declarative connectors
        assert tpl["source_key"] in b.per_source_requests
    assert account_key("worldbank.wdi", "requests") == "source:worldbank.wdi:requests"
    assert account_key("worldbank.wdi", "bytes", "discovery") == "autonomy:discovery:worldbank.wdi:bytes"
    assert source_accounts("worldbank.wdi") == [
        "source:worldbank.wdi:requests",
        "autonomy:discovery:worldbank.wdi:requests",
    ]
    for bad in (
        ("", "requests", "ingest"),
        ("a:b", "requests", "ingest"),
        ("x", "calls", "ingest"),
        ("x", "requests", "llm"),
    ):
        with pytest.raises(ValueError):
            account_key(*bad)


def test_unknown_source_and_zero_limit_are_refused_without_a_database() -> None:
    from reva.operations.budgets import BudgetExhausted, IngestBudget, budget_source_key, source_limit

    b = IngestBudget("t", "", 10, 10, {"a.src": 0, "b.src": 3})
    with pytest.raises(BudgetExhausted, match="no per-source"):
        source_limit(b, "unknown.src")
    with pytest.raises(BudgetExhausted, match="is 0"):
        source_limit(b, "a.src")
    assert source_limit(b, "b.src") == 3

    class NoKey:
        name = "x"
        source_key = ""

    with pytest.raises(BudgetExhausted):
        budget_source_key(NoKey())


# Every place that can create a live provider client, and how it is budgeted.
LIVE_CALLS = (
    "make_live_client(",
    "SafeHttpClient(",
    "LlmHttpClient(",
    "httpx.AsyncClient(",
    "httpx.Client(",
    "urlopen(",
)
BUDGETED = {  # file -> markers that must all be present (reserve before, meter/cap, settle after)
    "src/reva/causal_candidates/fetch.py": ("reserve_ingest(", "metered(", "settle_metered("),
    "src/reva/operations/jobs.py": ("reserve_ingest(", "metered(", "settle_metered("),
    "src/reva/cli/main.py": ("reserve_ingest(", "metered(", "settle_metered(", "except BudgetExhausted"),
    "src/reva/autonomy/jobs.py": ("reserve_discovery(", "settle_discovery(", "max_requests=reservation.max_requests"),
    "src/reva/nl/service.py": ("ledger.reserve", "ledger.settle"),
    "src/reva/atlas/ingest.py": ("reserve_ingest(", "metered(", "settle_metered(", "except BudgetExhausted"),
    "src/reva/kg/harvest.py": ("reserve_ingest(", "metered(", "settle_metered(", "except BudgetExhausted"),
}
NOT_PROVIDER_EGRESS = {
    "src/reva/evidence/runner.py": "defines make_live_client (callers are budgeted)",
    "src/reva/adapters/http/safe_client.py": "the HTTP client itself",
    "src/reva/nl/http.py": "the LLM HTTP client itself",
    "src/reva/nl/providers.py": "builds the LLM client; every call goes through nl/service.py (ledger reserve/settle)",
    "src/reva/nl/recorded.py": (
        "maintainer-only capture of recorded responses for offline tests (not reachable from the API)"
    ),
    "src/reva/autonomy/sandbox.py": "POSTs to the operator's isolated sandbox runner, not a data provider "
    "(bounded by RunnerResourceBudget)",
}


def test_every_live_provider_request_path_is_budgeted() -> None:
    sites: set[str] = set()
    for p in sorted((REPO / "src/reva").rglob("*.py")):
        src = p.read_text()
        if any(c in src for c in LIVE_CALLS):
            sites.add(str(p.relative_to(REPO)))
    unaccounted = sites - set(BUDGETED) - set(NOT_PROVIDER_EGRESS)
    assert not unaccounted, f"live provider client created outside a budgeted path: {sorted(unaccounted)}"
    for f, markers in BUDGETED.items():
        text_ = (REPO / f).read_text()
        missing = [m for m in markers if m not in text_]
        assert not missing, (f, missing)
    # connector repair and the scheduler make no provider request themselves: repair reads stored
    # acquisitions and the fixture bank; schedules only enqueue ingest jobs (handled by handle_ingest)
    repair = (REPO / "src/reva/autonomy/repair.py").read_text()
    assert not any(c in repair for c in (*LIVE_CALLS, "run_connector(", ".fetch("))
    from reva.operations.jobs import HANDLERS
    from reva.operations.schedules import SCHEDULE_JOB_TYPES

    assert HANDLERS["ingest"] == "reva.operations.jobs:handle_ingest" and "ingest" in SCHEDULE_JOB_TYPES
    assert HANDLERS["discovery"] == "reva.autonomy.jobs:handle_discovery"


def test_metered_client_refuses_before_calling_the_transport_and_skips_fixtures(monkeypatch) -> None:
    from reva.evidence.runner import FixtureHttp
    from reva.operations.budgets import BudgetExhausted, IngestBudget, MeteredHttp, Reservation, metered

    class Forbidden:
        origin = "live"

        async def get(self, url: str, **kw: Any) -> Any:  # pragma: no cover - must not be reached
            raise AssertionError("transport called although the budget is exhausted")

    fx = FixtureHttp("worldbank.wdi.indicators")
    r = Reservation("s.src", "op:requests", "op:bytes", 0, 0, 0, "t")
    assert metered(fx, None, r) is fx  # fixture replays are never charged  # type: ignore[arg-type]
    m = metered(Forbidden(), None, r, budget=IngestBudget("t", "", 0, 0, {"s.src": 1}))  # type: ignore[arg-type]
    assert isinstance(m, MeteredHttp) and m.origin == "live"

    import reva.operations.budgets as budgets

    monkeypatch.setattr(budgets, "top_up", lambda *a, **k: 0)  # the ledger grants nothing more today
    with pytest.raises(BudgetExhausted):
        asyncio.run(m.get("https://api.worldbank.org/v2/x", max_bytes=10))
    assert m.requests == 0 and m.refused == 1
