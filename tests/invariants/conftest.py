"""Shared fixtures for the §24.1 invariant tests.

``fixture_release`` replays the captured WDI and Comtrade responses through the
real ingest path (``FixtureHttp``, origin=test_fixture, offline) and publishes one
belief release with ``Origin.test_fixture``. It is session scoped and lives on the
session ``db`` fixture, so DB-backed invariant tests share one baseline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "reva"


@pytest.fixture(scope="session")
def fixture_release(db) -> dict[str, Any]:
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.belief.publication import publish_release
    from reva.evidence.runner import FixtureHttp, run_connector
    from reva.kernel.context import Origin
    from reva.kernel.time import utc_now

    async def ingest() -> None:
        for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
            c = builtin_connectors()[name]
            await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)

    asyncio.run(ingest())
    rel = publish_release(db.worker, db.store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
    return {"release_id": rel.release_id, "cutoff": rel.knowledge_cutoff}

