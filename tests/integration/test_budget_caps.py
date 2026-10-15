"""Invariant 30: persistent daily egress caps (``ops.budget_ledger``), offline.

* a daily cap that is reached refuses the next live run with a typed
  ``budget_exhausted`` outcome, and no request is made (the transport fails the
  test if it is called) — through the real worker handler and the CLI path;
* unknown sources and a limit of 0 are refused;
* requests beyond the reservation are topped up per request under the lock or
  refused without calling the transport; actual usage is settled afterwards;
* concurrent reservations (ingest/ingest and ingest/discovery) can never exceed
  a shared cap: every egress reservation takes the same advisory lock;
* the day boundary is the UTC midnight of the database clock, independent of
  the session time zone; fixture runs are never charged.

Every test uses its own unique source key with a test budget, so the shared
session database's ledger cannot interfere.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.http.safe_client import HttpResult
from reva.kernel.errors import SourceResponseError
from reva.operations import budgets
from reva.operations.budgets import (
    BUDGET_LOCK_KEY,
    BudgetExhausted,
    IngestBudget,
    account_key,
    metered,
    reserve_egress,
    reserve_ingest,
    settle_metered,
    usage_today,
)
from reva.ports.connectors import Cursor, FetchBudget, FetchItem, FetchOutcome, FetchPlan, RawAcquisition

pytestmark = pytest.mark.db

HUGE = 10**12


def _src() -> str:
    return f"test.caps.{uuid.uuid4().hex[:10]}"


def _budget(src: str, limit: int, *, total_requests: int = HUGE, total_bytes: int = HUGE) -> IngestBudget:
    return IngestBudget(
        "budgets-test", "", total_requests=total_requests, total_bytes=total_bytes, per_source_requests={src: limit}
    )


def _prefill(db: Any, account: str, amount: int, *, at_sql: str | None = None) -> None:
    """Owner-side ledger row (only the table owner may choose ``recorded_at``)."""
    cols, vals = "account_key, operation_id, unit, reserved, consumed", ":a, :o, :u, :n, :n"
    if at_sql:
        cols, vals = cols + ", recorded_at", vals + f", {at_sql}"
    with db.owner.begin() as c:
        c.execute(
            text(f"INSERT INTO ops.budget_ledger({cols}) VALUES ({vals})"),
            {"a": account, "o": f"test-prefill-{uuid.uuid4().hex}", "u": account.rsplit(":", 1)[1], "n": amount},
        )


def _rows(db: Any, src: str) -> list[Any]:
    with db.owner.connect() as c:
        return c.execute(
            text(
                "SELECT account_key, unit, reserved, consumed FROM ops.budget_ledger "
                "WHERE account_key = ANY(:a) ORDER BY recorded_at"
            ),
            {"a": [account_key(src, u, p) for u in ("requests", "bytes") for p in ("ingest", "discovery")]},
        ).all()


class Transport:
    """Fake live client: counts calls, or fails the test when ``forbidden``."""

    origin = "live"
    max_bytes = 4096

    def __init__(self, *, forbidden: bool = False, body: bytes = b'{"ok": true}') -> None:
        self.forbidden = forbidden
        self.body = body
        self.calls: list[tuple[str, int | None]] = []

    async def get(self, url: str, **kw: Any) -> HttpResult:
        if self.forbidden:
            raise AssertionError(f"no provider request may be made (budget exhausted): {url}")
        self.calls.append((url, kw.get("max_bytes")))
        return HttpResult(url, url, 200, {"content-type": "application/json"}, self.body, datetime.now(UTC), 0.0, (), 1)

    async def aclose(self) -> None:
        return None


class ProbeConnector:
    """Unregistered connector charged to a test source; fetch requests then fails (nothing published)."""

    def __init__(self, source_key: str, n_items: int) -> None:
        self.inner = builtin_connectors()["worldbank.wdi.indicators"]
        self.source_key = source_key
        self.n_items = n_items
        self.fetched: list[str] = []

    def describe(self) -> Any:
        # an unregistered name: record_health and the failure streak find nothing to update
        return self.inner.describe().model_copy(
            update={"name": "invariant30.caps_probe", "source_key": self.source_key}
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        items = tuple(
            FetchItem(key=f"probe-{i}", url=f"https://api.worldbank.org/v2/probe/{i}", max_bytes=1000)
            for i in range(self.n_items)
        )
        return FetchPlan(connector="invariant30.caps_probe", items=items, pagination="none")

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        self.fetched.append(item.key)
        await http.get(item.url, max_bytes=item.max_bytes)
        raise SourceResponseError("offline budget probe: response discarded", retryable=False)

    def parse(self, raw: RawAcquisition) -> Iterator[Any]:  # pragma: no cover - never reached
        raise AssertionError("parse must not run")

    def validate(self, batch: Any) -> Any:  # pragma: no cover - never reached
        raise AssertionError("validate must not run")

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return Cursor(state={"complete": False})


def _ctx(db: Any) -> Any:
    from reva.operations.context import JobContext

    return JobContext(
        job_id=uuid.uuid4(),
        job_type="ingest",
        worker_id="budget-test",
        fencing_token=0,
        attempt=1,
        settings=db.settings,
        worker_engine=db.worker,
        sim_engine=None,
        store=db.store,
    )


@pytest.fixture
def probe_ingest(db, monkeypatch):
    """Route ``handle_ingest`` to a probe connector, a test budget and a fake live transport."""
    from reva.evidence import runner
    from reva.operations import jobs

    state: dict[str, Any] = {}

    def setup(limit: int | None, n_items: int, *, forbidden: bool) -> tuple[str, ProbeConnector, Transport]:
        src = _src()
        conn = ProbeConnector(src, n_items)
        transport = Transport(forbidden=forbidden)
        b = IngestBudget("budgets-test", "", HUGE, HUGE, {src: limit} if limit is not None else {})
        monkeypatch.setattr(jobs, "_all_connectors", lambda ctx: {"budget.probe": conn})
        monkeypatch.setattr(budgets, "load_budget", lambda policy=None: b)

        def live_client(spec: Any, settings: Any) -> Any:
            if forbidden:
                raise AssertionError("a live client must not be created when the budget refuses the run")
            return transport

        monkeypatch.setattr(runner, "make_live_client", live_client)
        state.update(src=src, conn=conn, transport=transport)
        return src, conn, transport

    return setup


# --------------------------------------------------------------------------- cap reached → typed refusal
def test_worker_ingest_refused_with_budget_exhausted_when_daily_cap_reached(db, probe_ingest) -> None:
    from reva.operations import jobs

    src, conn, transport = probe_ingest(5, 2, forbidden=True)
    _prefill(db, account_key(src, "requests"), 5)  # today's cap already consumed
    with pytest.raises(BudgetExhausted) as ei:
        jobs.handle_ingest({"connector": "budget.probe", "mode": "live"}, _ctx(db))
    assert ei.value.code == "budget_exhausted" and ei.value.details["reason"] == "source_limit"
    assert ei.value.problem()["status"] == 429
    assert conn.fetched == [] and transport.calls == []
    assert len(_rows(db, src)) == 1  # the refusal recorded nothing


def test_worker_ingest_charges_and_settles_actual_usage(db, probe_ingest) -> None:
    from reva.operations import jobs

    src, conn, transport = probe_ingest(10, 3, forbidden=False)
    # the probe discards every response, so the job fails (IngestFailed) after settling its usage
    with pytest.raises(jobs.IngestFailed):
        jobs.handle_ingest({"connector": "budget.probe", "mode": "live"}, _ctx(db))
    assert len(transport.calls) == 3
    rows = {r.unit: r for r in _rows(db, src)}
    assert rows["requests"].reserved == 3 and rows["requests"].consumed == 3
    assert rows["bytes"].consumed == 3 * len(transport.body)
    with db.worker.connect() as c:
        assert usage_today(c, src)["source_requests"] == 3
    # 7 left today: a run planning 8 is refused before any request
    conn.n_items = 8
    transport.forbidden = True
    with pytest.raises(BudgetExhausted):
        jobs.handle_ingest({"connector": "budget.probe", "mode": "live"}, _ctx(db))
    assert len(transport.calls) == 3


def test_fixture_ingest_is_never_charged(db, probe_ingest) -> None:
    from reva.operations import jobs

    src, conn, _ = probe_ingest(0, 2, forbidden=True)  # even a zero budget does not matter for fixtures
    with pytest.raises(jobs.IngestFailed):  # no captured fixtures for the probe: every item fails offline
        jobs.handle_ingest({"connector": "budget.probe", "mode": "fixture"}, _ctx(db))
    assert len(conn.fetched) == 2
    assert _rows(db, src) == []  # nothing reserved or charged


def test_cli_live_ingest_reports_typed_refusal_without_requests(db, monkeypatch) -> None:
    from reva.adapters.objects import factory
    from reva.adapters.postgres import engine as engine_mod
    from reva.cli import main as cli
    from reva.evidence import runner

    src = _src()
    conn = ProbeConnector(src, 2)
    monkeypatch.setattr(budgets, "load_budget", lambda policy=None: _budget(src, 4))
    monkeypatch.setattr("reva.adapters.connectors.registry.builtin_connectors", lambda *a, **k: {"budget.probe": conn})
    monkeypatch.setattr(engine_mod, "get_engine", lambda kind="worker", *a, **k: db.worker)
    monkeypatch.setattr(factory, "get_store", lambda *a, **k: db.store)
    monkeypatch.setattr("reva.autonomy.jobs.promoted_connectors", lambda *a, **k: {})

    def no_client(*a: Any, **k: Any) -> Any:
        raise AssertionError("a live client must not be created when the budget refuses the run")

    monkeypatch.setattr(runner, "make_live_client", no_client)
    _prefill(db, account_key(src, "requests"), 4)
    reports = cli._run_ingest(["budget.probe"], live=True, capture=False)
    assert reports == [
        {**reports[0], "connector": "invariant30.caps_probe", "status": "refused", "code": "budget_exhausted"}
    ]
    assert conn.fetched == []


# --------------------------------------------------------------------------- unknown source / zero limit
def test_unknown_source_and_zero_limit_are_refused(db) -> None:
    src = _src()
    for b, reason in ((_budget(src + ".other", 10), "unknown_source"), (_budget(src, 0), "zero_limit")):
        with pytest.raises(BudgetExhausted) as ei:
            reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=0, budget=b)
        assert ei.value.details["reason"] == reason
        with pytest.raises(BudgetExhausted):
            reserve_egress(
                db.worker,
                purpose="discovery",
                source_key=src,
                operation_id=f"t:{uuid.uuid4()}",
                want_requests=1,
                want_bytes=1,
                budget=b,
            )
    assert _rows(db, src) == []


def test_worker_ingest_of_unbudgeted_source_is_refused(db, probe_ingest) -> None:
    from reva.operations import jobs

    _, conn, _ = probe_ingest(None, 1, forbidden=True)
    with pytest.raises(BudgetExhausted) as ei:
        jobs.handle_ingest({"connector": "budget.probe", "mode": "live"}, _ctx(db))
    assert ei.value.details["reason"] == "unknown_source" and conn.fetched == []


# --------------------------------------------------------------------------- in-run cap
def test_requests_beyond_the_reservation_are_topped_up_then_refused_without_a_request(db) -> None:
    from reva.evidence.runner import run_connector

    src = _src()
    b = _budget(src, 5)
    conn = ProbeConnector(src, 8)
    r = reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=2, budget=b)
    assert r.max_requests == 5
    transport = Transport()
    http = metered(transport, db.worker, r, budget=b)
    # the per-run FetchBudget allows all 8: only the persistent daily ledger stops the run
    report = asyncio.run(
        run_connector(conn, http, db.store, db.worker, budget=FetchBudget(max_requests=100), persist_cursor=False)
    )
    assert len(transport.calls) == 5 and len(conn.fetched) == 8
    assert all(mb is not None and mb <= 1000 for _, mb in transport.calls)
    refused = [e for e in report.errors if e.get("code") == "budget_exhausted"]
    assert len(refused) == 3 and http.refused == 3
    with db.worker.connect() as c:
        assert usage_today(c, src)["source_requests"] == 5  # reservation grew to exactly the cap
    info = settle_metered(db.worker, http)
    assert info is not None and info["consumed_requests"] == 5 and info["refused_requests"] == 3
    with pytest.raises(BudgetExhausted):  # the cap is reached: the next run is refused
        reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=0, budget=b)


def test_byte_cap_limits_response_size_and_refuses_when_used_up(db) -> None:
    src = _src()
    with db.worker.connect() as c:
        used_bytes = usage_today(c, src)["total_bytes"]
    b = _budget(src, 100, total_bytes=used_bytes + 1500)
    r = reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=3, budget=b)
    transport = Transport(body=b"x" * 1000)
    http = metered(transport, db.worker, r, budget=b)

    async def go() -> None:
        await http.get("https://api.worldbank.org/v2/a", max_bytes=1000)
        await http.get("https://api.worldbank.org/v2/b", max_bytes=1000)  # only 500 bytes left: capped
        with pytest.raises(BudgetExhausted):  # nothing left: no request
            await http.get("https://api.worldbank.org/v2/c", max_bytes=1000)

    asyncio.run(go())
    assert [mb for _, mb in transport.calls] == [1000, 500]
    settle_metered(db.worker, http)


# --------------------------------------------------------------------------- concurrency
def _race(fns: list[Any]) -> list[Any]:
    barrier = threading.Barrier(len(fns))
    out: list[Any] = [None] * len(fns)

    def run(i: int) -> None:
        barrier.wait()
        try:
            out[i] = fns[i]()
        except BudgetExhausted as e:
            out[i] = e

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


def test_racing_ingest_reservations_cannot_exceed_the_cap(db) -> None:
    src = _src()
    b = _budget(src, 10)
    fns = [
        lambda: reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=4, budget=b)
        for _ in range(6)
    ]
    out = _race(fns)
    ok = [o for o in out if not isinstance(o, BudgetExhausted)]
    assert len(ok) == 2 and sum(isinstance(o, BudgetExhausted) for o in out) == 4
    with db.worker.connect() as c:
        assert usage_today(c, src)["source_requests"] == 8 <= 10


def test_ingest_and_discovery_share_one_lock_and_one_cap(db) -> None:
    src = _src()
    b = _budget(src, 6)
    results: dict[str, Any] = {}

    def ingest() -> None:
        try:
            results["ingest"] = reserve_ingest(
                db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=4, budget=b
            )
        except BudgetExhausted as e:
            results["ingest"] = e

    def discovery() -> None:
        try:
            results["discovery"] = reserve_egress(
                db.worker,
                purpose="discovery",
                source_key=src,
                operation_id=f"discovery:{uuid.uuid4()}",
                want_requests=5,
                want_bytes=10,
                budget=b,
            )
        except BudgetExhausted as e:
            results["discovery"] = e

    # hold the single egress lock: both paths must wait for it (one lock, not one per path)
    holder = db.owner.connect()
    tx = holder.begin()
    holder.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": BUDGET_LOCK_KEY})
    threads = [threading.Thread(target=ingest), threading.Thread(target=discovery)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    assert all(t.is_alive() for t in threads) and results == {}
    tx.rollback()
    holder.close()
    for t in threads:
        t.join(30)
    granted = [v for v in results.values() if not isinstance(v, BudgetExhausted)]
    assert granted, results
    with db.worker.connect() as c:
        used = usage_today(c, src)["source_requests"]
    assert used <= 6  # 4 (ingest) + 5 (discovery) never both granted in full
    if len(granted) == 2:  # discovery came second and got only what was left
        assert results["discovery"].requests == 2 and used == 6


def test_in_run_top_ups_race_with_new_reservations_without_exceeding_the_cap(db) -> None:
    src = _src()
    b = _budget(src, 12)
    r = reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=1, budget=b)
    fns: list[Any] = [lambda: budgets.top_up(db.worker, r, unit="requests", want=5, budget=b) for _ in range(3)]
    fns += [
        lambda: reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=3, budget=b)
        for _ in range(3)
    ]
    _race(fns)
    with db.worker.connect() as c:
        assert usage_today(c, src)["source_requests"] <= 12


# --------------------------------------------------------------------------- day rollover (DB clock, UTC)
def test_day_rollover_uses_the_database_utc_midnight(db) -> None:
    src = _src()
    acct = account_key(src, "requests")
    midnight = "(date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
    _prefill(db, acct, 1000, at_sql=f"{midnight} - interval '1 second'")  # yesterday (UTC): not counted
    with db.worker.connect() as c:
        for tz in ("UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago"):  # +14h / -11h session time zones
            c.execute(text(f"SET TIME ZONE '{tz}'"))
            assert usage_today(c, src)["source_requests"] == 0, tz
        c.rollback()
    b = _budget(src, 5)
    r = reserve_ingest(db.worker, job_id=uuid.uuid4(), attempt=1, source_key=src, planned_requests=5, budget=b)
    assert r.max_requests == 5
    _prefill(db, acct, 1, at_sql=midnight)  # exactly today's UTC midnight: counted
    with db.worker.connect() as c:
        c.execute(text("SET TIME ZONE 'Pacific/Kiritimati'"))
        assert usage_today(c, src)["source_requests"] == 6
        c.rollback()
