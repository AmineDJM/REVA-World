"""Fixture-plan mode replays restricted-plan V2 fixtures offline without touching live plans."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reva.adapters.connectors import fixture_plans as fp
from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.runner import FixtureHttp, _fixture_fingerprint
from reva.ports.connectors import FetchBudget


async def _replay(conn: Any, http: Any) -> list[Any]:
    """fetch + parse every (captured) item and follow-up, as run_connector does, without a database."""
    queue, raws = list(conn.plan(None, FetchBudget(max_requests=10_000)).items), []
    while queue:
        raw = await conn.fetch(queue.pop(0), http)
        raws.append(raw)
        for b in conn.parse(raw):
            queue.extend(b.follow_up)
    return raws


@pytest.mark.parametrize("name", sorted(fp.CAPTURED_CONFIGURATIONS))
def test_default_plan_is_not_replayable_but_fixture_plan_is(name: str) -> None:
    captured = fp.captured_fingerprints(name)
    default = builtin_connectors()[name].plan(None, FetchBudget(max_requests=10_000)).items
    assert {_fixture_fingerprint(i.url) for i in default} - captured or name == "eurostat.figaro.iciot_ii"
    conn = fp.fixture_plan_connector(name)
    live = builtin_connectors()[name].describe()
    assert (conn.describe().name, conn.describe().version) == (live.name, live.version)  # same registered connector
    items = conn.plan(None, FetchBudget(max_requests=10_000)).items
    assert items and all(_fixture_fingerprint(i.url) in captured for i in items)
    raws = asyncio.run(_replay(conn, FixtureHttp(name)))
    assert raws and all(r.origin == "test_fixture" for r in raws)
    assert {r.request_fingerprint for r in raws} <= captured or len(raws) == len(captured)


def test_figaro_follow_ups_outside_the_capture_are_reported_not_fetched() -> None:
    conn = fp.fixture_plan_connector("eurostat.figaro.iciot_ii")
    raws = asyncio.run(_replay(conn, FixtureHttp("eurostat.figaro.iciot_ii")))
    assert len(raws) == 2  # content constraint + the captured DOM group
    assert conn.not_captured and {d["stage"] for d in conn.not_captured} == {"follow_up"}


def test_live_connectors_are_untouched() -> None:
    assert len(builtin_connectors()["un.comtrade.public_batch"].plan(None, FetchBudget(max_requests=10_000)).items) > 3
    assert not isinstance(builtin_connectors()["eurostat.demography"], fp.FixturePlanConnector)
