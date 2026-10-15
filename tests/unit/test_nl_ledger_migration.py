"""The LLM ledger functions shipped in migration 0008 equal the copy the NL service documents."""

import importlib.util
from pathlib import Path

from reva.nl.budget import LEDGER_FUNCTIONS_SQL


def test_migration_0008_carries_the_llm_ledger_functions() -> None:
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0008_v2.py"
    spec = importlib.util.spec_from_file_location("m0008", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SQL_LLM_LEDGER == LEDGER_FUNCTIONS_SQL
