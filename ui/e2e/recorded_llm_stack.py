#!/usr/bin/env python3
"""The e2e stack (scripts/e2e_stack.py) with the *recorded* LLM drafter wired in.

Same throwaway database, fixtures, API and worker as ``scripts/e2e_stack.py``; the only
differences are that ``POST /v1/scenario-drafts`` replays the recorded provider responses in
``tests/fixtures/nl/recorded`` (``reva.nl.recorded.RecordedDrafter``), exactly as the API
integration tests do, and that V2 dataset inputs are added offline (``--no-v2-fixtures`` to skip):
the recorded Eurostat labour/pensions fixture and the SYNTHETIC 3x2 FIGARO-format table of
tests/integration/test_io_price_pipeline.py (so economic.io_price runs end to end; its results are
test_fixture data, not FIGARO figures). No live LLM and no live provider is ever called (CI and tests never call an LLM). Without
this wrapper the stack runs with LLM drafting disabled and the composer e2e tests check the
typed ``llm_unavailable`` refusal instead.

Usage (from the repository root; every argument is passed to scripts/e2e_stack.py)::

    uv run python ui/e2e/recorded_llm_stack.py --stack-file <scratch>/stack.json --static-dir <scratch>/ui-build
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import reva.api.app as app_module  # noqa: E402
from reva.nl.recorded import RecordedDrafter  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "nl" / "recorded"
_original_create_app = app_module.create_app


def create_app(*args, **kwargs):  # type: ignore[no-untyped-def]
    app = _original_create_app(*args, **kwargs)
    drafter = RecordedDrafter(FIXTURES)
    app.state.nl_drafter_factory = lambda settings: [drafter]
    app.state.nl_drafter_mode = "recorded"
    return app


app_module.create_app = create_app  # e2e_stack imports it at call time


def _load_stack_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("reva_e2e_stack", ROOT / "scripts" / "e2e_stack.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reva_e2e_stack"] = mod
    spec.loader.exec_module(mod)
    return mod


def _with_llm_ledger(stack_module):  # type: ignore[no-untyped-def]
    """Until migration 0008_v2 ships the LLM budget ledger functions, install them in the throwaway
    e2e database exactly as tests/integration/test_nl_drafts_api.py does (no-op once present)."""
    original = stack_module.Stack.prepare_data

    def prepare_data(self, settings):  # type: ignore[no-untyped-def]
        out = original(self, settings)
        from sqlalchemy import text

        from reva.adapters.postgres.engine import make_engine
        from reva.nl.budget import LEDGER_FUNCTIONS_SQL

        owner = make_engine("migration", settings)
        try:
            with owner.begin() as c:
                present = c.execute(text("SELECT to_regprocedure('ops.reserve_llm_budget(text,integer,bigint,integer,"
                                         "bigint)') IS NOT NULL")).scalar_one()
                if not present:
                    c.execute(text(LEDGER_FUNCTIONS_SQL))
            out["llm_ledger"] = "migration" if present else "installed_for_e2e"
        finally:
            owner.dispose()
        out["llm_drafter"] = "recorded"
        return out

    stack_module.Stack.prepare_data = prepare_data


#: V2 recorded provider fixtures (tests/fixtures/source/<name>) also ingested offline, when the connector
#: is registered: all-country WDI/Comtrade (ALLC), Eurostat demography (POP), FIGARO IO tables (IO) and
#: labour/pensions (PEN). Their datasets are then pinned by the stack's test_fixture release.
V2_FIXTURE_CONNECTORS = (
    "eurostat.labour_pensions",
)
# Not ingested with their default (full) plans: their recorded fixtures were captured for restricted
# plans, so most requests have no captured response (worldbank.wdi.all_economies,
# un.comtrade.public_batch, eurostat.demography, eurostat.figaro.iciot_ii). FIGARO is replaced by the
# synthetic table below.


def _with_v2_fixtures(stack_module):  # type: ignore[no-untyped-def]
    from reva.adapters.connectors.registry import builtin_connectors

    known = builtin_connectors()
    extra = tuple(n for n in V2_FIXTURE_CONNECTORS if n in known and n not in stack_module.FIXTURE_CONNECTORS)
    stack_module.FIXTURE_CONNECTORS = tuple(stack_module.FIXTURE_CONNECTORS) + extra


def _load_synthetic_io():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("io_synthetic", ROOT / "tests" / "fixtures" / "io_tables" / "synthetic.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _with_synthetic_io(stack_module):  # type: ignore[no-untyped-def]
    """Ingest the SYNTHETIC 3-economy x 2-industry FIGARO-format table used by
    tests/integration/test_io_price_pipeline.py (tests/fixtures/io_tables/synthetic.py) through the
    real connector (FixtureHttp), identity checks and dataset registration, just before the stack
    publishes its test_fixture release, so economic.io_price can run end to end. The recorded
    FIGARO source fixture holds one data item only and cannot form a complete table."""
    import asyncio
    import json
    import tempfile

    import reva.belief.publication as pub

    original_prepare = stack_module.Stack.prepare_data
    original_publish = pub.publish_release
    state: dict = {}

    def prepare_data(self, settings):  # type: ignore[no-untyped-def]
        state["settings"] = settings
        out = original_prepare(self, settings)
        out["io_dataset"] = state.get("io_dataset")
        return out

    def publish_release(worker, store, **kwargs):  # type: ignore[no-untyped-def]
        settings = state.get("settings")
        if settings is not None and "io_dataset" not in state:
            from reva.adapters.connectors import io_tables as it
            from reva.adapters.postgres import repositories as repo
            from reva.adapters.postgres.engine import make_engine
            from reva.cli.seed import register_connector_version
            from reva.evidence.runner import FixtureHttp, _fixture_fingerprint, run_connector
            from reva.kernel.ids import sha256_bytes

            syn = _load_synthetic_io()
            dims = it.TableDims(countries=syn.COUNTRIES, industries=syn.INDUSTRIES)
            root = Path(tempfile.mkdtemp(prefix="reva-e2e-io-"))
            d = root / it.NAME
            d.mkdir(parents=True)
            conn = it.FigaroConnector(dims=dims)
            bodies = {it.constraint_url("NAIO_10_FCP_II4"): syn.constraint_xml()}
            for item in conn._data_items(syn.YEAR, "NAIO_10_FCP_II4"):
                bodies[item.url] = syn.body(tuple(item.params["origins"]), year=syn.YEAR)
            entries = []
            for url, body in bodies.items():
                fp = _fixture_fingerprint(url)
                (d / f"{fp[:16]}.bin").write_bytes(body)
                entries.append({"request_fingerprint": fp, "url": url, "file": f"{fp[:16]}.bin",
                                "sha256": sha256_bytes(body), "status": 200,
                                "headers": {"content-type": "application/vnd.sdmx.data+csv"},
                                "retrieved_at": "2026-10-15T00:00:00+00:00", "licence": it.LICENCE})
            (d / "manifest.json").write_text(json.dumps(entries))
            owner = make_engine("migration", settings)
            try:
                with owner.begin() as c:
                    act = repo.simple_activity("registry.seed", "review", "maintainer:registry-seed", {"e2e": it.NAME})
                    repo.record_activity(c, act)
                    register_connector_version(c, store, it.FigaroConnector(dims=dims).describe(), act.id)
            finally:
                owner.dispose()
            rep = asyncio.run(run_connector(it.FigaroConnector(syn.YEAR, dims=dims), FixtureHttp(it.NAME, root=root),
                                            store, worker, persist_cursor=False))
            if rep.errors:
                state["io_dataset"] = {"errors": rep.errors}
            else:
                state["io_dataset"] = it.publish_io_dataset(worker, store, year=syn.YEAR, origin="test_fixture", dims=dims)
            # The stack computed its cutoff before this hook ran: the release must see the dataset.
            from datetime import UTC, datetime

            kwargs["cutoff"] = datetime.now(UTC)
        return original_publish(worker, store, **kwargs)

    stack_module.Stack.prepare_data = prepare_data
    pub.publish_release = publish_release


if __name__ == "__main__":
    mod = _load_stack_module()
    _with_llm_ledger(mod)
    if "--no-v2-fixtures" in sys.argv:
        sys.argv.remove("--no-v2-fixtures")
    else:
        _with_v2_fixtures(mod)
        _with_synthetic_io(mod)
    sys.argv[0] = str(ROOT / "scripts" / "e2e_stack.py")
    sys.exit(mod.main())
