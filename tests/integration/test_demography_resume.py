"""eurostat.demography resumes an unfinished cycle across budgeted runs through the real runner.

Regression (V6 FIXPOP): ``reva.evidence.runner.run_connector`` persists a cursor only when its
state says ``complete: true``. Connector 1.0.0 returned ``complete: false`` for an unfinished
cycle, so its checkpoint was never saved and every 100-request live run restarted at the first
country (AT) and stopped at FI: France was never fetched. 1.1.0 always returns a savable
checkpoint (cycle progress in ``cycle_done`` / ``cycle_finished``).

Offline (fixture mode): the recorded Eurostat fixtures (LI, LU since 2008 + EUROPOP2023, 15
requests) fetched 6 requests per run, cursor persisted as in a live run, in a throwaway database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from reva.adapters.connectors import eurostat_demography as ed
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.evidence.runner import FixtureHttp, load_cursor, run_connector
from reva.ports.connectors import FetchBudget
from reva.settings import Settings

pytestmark = pytest.mark.db


def test_cycle_resumes_across_budgeted_runs_with_persisted_cursor(tmp_path: Path) -> None:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import seed

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    root = tmp_path / "objects"
    with throwaway_database(base, prefix="reva_test_demog_resume") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "worker")}
        try:
            seed(engines["migration"], store)
            conn_ = ed.EurostatDemographyConnector(
                countries=("LI", "LU"), since_year=2008, window_years=ed.WINDOW_YEARS
            )
            all_keys = [i.key for i in conn_.all_items()]
            assert len(all_keys) == 15
            assert load_cursor(engines["worker"], ed.NAME) is None

            def run() -> dict[str, Any]:
                rep = asyncio.run(
                    run_connector(
                        conn_,
                        FixtureHttp(ed.NAME),
                        store,
                        engines["worker"],
                        budget=FetchBudget(max_requests=6),
                        persist_cursor=True,  # what a live run does
                        post_publish=ed.post_publish_datasets,
                    )
                )
                return rep.as_dict()

            def datasets() -> set[tuple[str, str]]:
                with engines["migration"].connect() as c:
                    rows = c.execute(
                        text("SELECT dataset_key, subject_key FROM evidence.dataset WHERE origin = 'test_fixture'")
                    ).all()
                return {(r[0], r[1]) for r in rows}

            fetched: list[str] = []
            r1 = run()
            fetched += [o["item_key"] for o in r1["outcomes"]]
            assert r1["errors"] == [] and r1["cursor_advanced"] is True
            saved = load_cursor(engines["worker"], ed.NAME)
            assert saved is not None and saved.state["cycle_done"] == sorted(fetched)
            assert not saved.state["cycle_finished"]
            assert fetched == all_keys[:6]  # LI: 3 tables x 2 windows
            assert ("eurostat.demography.demo_pjan", "LIE") in datasets()  # LI complete after run 1
            assert not any(s == "LUX" for _, s in datasets())

            r2 = run()  # resumes at LU (1.0.0 restarted at LI here)
            k2 = [o["item_key"] for o in r2["outcomes"]]
            assert k2 == all_keys[6:12] and not set(k2) & set(fetched)
            fetched += k2
            assert {("eurostat.demography.demo_pjan", "LUX"), ("eurostat.demography.demo_magec", "LUX")} <= datasets()

            r3 = run()  # the three EUROPOP2023 tables finish the cycle
            k3 = [o["item_key"] for o in r3["outcomes"]]
            assert k3 == all_keys[12:]
            fetched += k3
            assert fetched == all_keys  # every item exactly once across the three runs
            saved = load_cursor(engines["worker"], ed.NAME)
            assert saved is not None and saved.state["cycle_finished"] and saved.state["cycle_done"] == []

            r4 = run()  # a finished cycle: the next run starts a new one from the first item
            assert [o["item_key"] for o in r4["outcomes"]] == all_keys[:6]
            assert all(o["status"] in ("published", "unchanged") for o in r4["outcomes"])
        finally:
            for e in engines.values():
                e.dispose()
