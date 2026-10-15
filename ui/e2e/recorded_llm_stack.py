#!/usr/bin/env python3
"""The e2e stack (scripts/e2e_stack.py) with the *recorded* LLM drafter wired in.

Same throwaway database, fixtures, API and worker as ``scripts/e2e_stack.py``; the only
difference is that ``POST /v1/scenario-drafts`` replays the recorded provider responses in
``tests/fixtures/nl/recorded`` (``reva.nl.recorded.RecordedDrafter``), exactly as the API
integration tests do. No live LLM is ever called (CI and tests never call an LLM). Without
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


if __name__ == "__main__":
    mod = _load_stack_module()
    _with_llm_ledger(mod)
    sys.argv[0] = str(ROOT / "scripts" / "e2e_stack.py")
    sys.exit(mod.main())
