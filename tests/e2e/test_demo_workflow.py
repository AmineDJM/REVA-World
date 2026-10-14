"""End-to-end: the §17.3 demonstration workflow in fixture mode (offline, origin test_fixture).

Runs ``scripts/demo_workflow.py`` against its own freshly created database and
asserts that every step that could run passed (a blocked step must carry a
reason and is never counted as a pass), and that the negative demonstration
(all four countries -> explicit Algerian ``missing_input``) holds.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.db, pytest.mark.e2e]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_workflow.py"


def _demo_module() -> Any:
    name = "reva_demo_workflow_script"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _require_database(demo: Any) -> None:
    base = demo._base_settings()
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    eng = create_engine(base.migration_url().set(database="postgres"))
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        pytest.skip("PostgreSQL not reachable")
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def demo_result(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    demo = _demo_module()
    _require_database(demo)
    out = tmp_path_factory.mktemp("demo") / "demo_fixtures.json"
    doc = demo.run_demo("fixtures", out_path=out, keep=False)
    assert json.loads(out.read_text())["steps"] == doc["steps"]
    return doc


def _failures(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"step": s["id"], "title": s["title"], "error": s["error"],
             "failed_checks": [c for c in s["checks"] if c["status"] != "pass"]}
            for s in doc["steps"] if s["status"] == "fail"]


def test_environment_was_set_up(demo_result: dict[str, Any]) -> None:
    assert not demo_result.get("setup_error"), demo_result.get("setup_traceback_tail")
    assert demo_result["mode"] == "fixtures" and demo_result["origin"] == "test_fixture"
    assert demo_result["database"]["fresh"] is True
    assert [s["id"] for s in demo_result["steps"]] == [*map(str, range(1, 13)), "N"]


def test_all_non_blocked_steps_pass(demo_result: dict[str, Any]) -> None:
    assert not _failures(demo_result), json.dumps(_failures(demo_result), indent=1, default=str)[:6000]
    for s in demo_result["steps"]:
        assert s["status"] in ("pass", "blocked")
        if s["status"] == "blocked":
            assert s["blocked_reason"], s  # never silently skipped
        else:
            assert s["checks"] and all(c["status"] == "pass" for c in s["checks"]), s["id"]


def test_offline_core_steps_are_never_blocked(demo_result: dict[str, Any]) -> None:
    # In fixture mode nothing external can block ingestion, beliefs, graph, simulation or provenance.
    blocked = {s["id"]: s["blocked_reason"] for s in demo_result["steps"]
               if s["id"] in {"1", "2", "3", "4", "5", "6", "7", "N"} and s["status"] == "blocked"}
    assert not blocked, blocked


def test_negative_demo_reports_the_algerian_blocker(demo_result: dict[str, Any]) -> None:
    neg = next(s for s in demo_result["steps"] if s["id"] == "N")
    assert neg["status"] == "pass", neg
    assert demo_result["negative_demo"]["holds"] is True
    names = {c["name"] for c in neg["checks"]}
    assert "only Algeria is reported missing" in names
    assert "no numbers were produced for the blocked run (no imputation, no zero fill)" in names
    assert neg["evidence"]["result_ref"]["status"] == "missing_input"
