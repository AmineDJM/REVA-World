"""V2 step 1: crude scenario with countries ["ALL"] and missing_input_policy skip_missing_countries.

Baseline: a test_fixture release from the captured all-country fixtures (subset FRA, DZA, CHN,
USA, CHE, IRQ, NLD, ATG for Comtrade 2023; ten World Bank economies incl. XKX and PRI).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from reva.adapters.connectors.comtrade_batch import ComtradeBatchConnector
from reva.adapters.connectors.worldbank_all import WorldBankAllConnector
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.errors import ValidationProblem
from reva.simulation.engine import model_manifest, plan_run
from reva.simulation.spec import CONTRACTS, validate_scenario_spec

pytestmark = pytest.mark.db

CT_SUBSET = {"FRA": 251, "DZA": 12, "CHN": 156, "USA": 842, "CHE": 757, "IRQ": 368, "NLD": 528, "ATG": 28}
WB_SUBSET = ["FRA", "DZA", "CHN", "USA", "CHE", "IRQ", "NLD", "ATG", "XKX", "PRI"]


def fixture_comtrade() -> ComtradeBatchConnector:
    return ComtradeBatchConnector(CT_SUBSET, total_years=(2023,), bilateral_years=(2023,), bilateral_batch=4)


def fixture_worldbank() -> WorldBankAllConnector:
    return WorldBankAllConnector(WB_SUBSET)


@pytest.fixture(scope="module")
def baseline(db) -> dict[str, Any]:
    import asyncio

    from reva.belief.publication import publish_release
    from reva.kernel.context import Origin
    from reva.kernel.time import utc_now

    async def ingest() -> None:
        for c in (fixture_worldbank(), fixture_comtrade()):
            await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)

    asyncio.run(ingest())
    rel = publish_release(db.worker, db.store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
    return {"release_id": rel.release_id, "cutoff": rel.knowledge_cutoff}


def spec(baseline: dict[str, Any], **over: Any) -> dict[str, Any]:
    s = json.loads((CONTRACTS / "scenario.example.json").read_text())
    s.update(
        {
            "schema_version": "2.0.0",
            "baseline_release_id": str(baseline["release_id"]),
            "knowledge_cutoff": baseline["cutoff"].isoformat(),
            "title": "ALLC test",
        }
    )
    s.update(over)
    return s


def run(db, s: dict[str, Any], fidelity: str = "L1") -> Any:
    validate_scenario_spec(s)
    with db.sim.connect() as conn:
        return plan_run(conn, {"id": uuid.uuid4(), "spec": s, "spec_digest": "test"}, fidelity, model_manifest(conn))


def test_all_countries_with_skip_policy_runs_complete_countries_and_lists_the_rest(db, baseline) -> None:
    plan = run(db, spec(baseline, countries=["ALL"], missing_input_policy="skip_missing_countries"))
    assert plan.status == "succeeded", plan.problems
    app = plan.applicability
    sel = app["country_selection"]
    assert sel["requested"] == ["ALL"] and {"FRA", "CHN", "USA", "CHE", "DZA", "IRQ", "XKX"} <= set(sel["resolved"])
    computed = set(app["computed_countries"])
    assert {"FRA", "CHN", "USA", "CHE"} <= computed
    skipped = {s["country"]: s for s in app["skipped_countries"]}
    assert not computed & set(skipped) and computed | set(skipped) == set(sel["resolved"])
    # DZA: typed Missing (provider returned no rows); IRQ: exports reported under HS H2 (ineligible);
    # XKX: no Comtrade reporter at all. Every reason is a typed missing_input, never a zero.
    for iso, inp in (("DZA", "M"), ("IRQ", "X"), ("XKX", "M")):
        assert skipped[iso]["status"] == "missing_input"
        assert any(r["code"] == "missing_input" and r["input"] == inp for r in skipped[iso]["reasons"]), skipped[iso]
    assert {r.iso3 for r in plan.results} == computed
    # Crosswalk aliases are labelled as provider-declared, reviewed ones as reviewed.
    assert app["baseline_inputs"]["CHE"]["subject_resolution"] == "provider_declared_alias:iso3166_alpha3"
    assert app["baseline_inputs"]["FRA"]["subject_resolution"] == "reviewed_alias:iso3166_alpha3"
    # A skipped country shows which inputs are present, absent or typed Missing.
    assert skipped["DZA"]["inputs"]["M"]["value"] is None and skipped["DZA"]["inputs"]["G"]["value"]
    assert all(r.value["kind"] == "decimal" for r in plan.results)


def test_all_countries_with_fail_policy_refuses_listing_every_missing_input(db, baseline) -> None:
    plan = run(db, spec(baseline, countries=["ALL"]))
    assert plan.status == "missing_input" and plan.results == []
    assert {"DZA", "IRQ", "XKX"} <= {p["country"] for p in plan.problems}
    assert "skipped_countries" not in plan.applicability


def test_explicit_list_with_skip_policy_and_all_missing_is_refused(db, baseline) -> None:
    plan = run(db, spec(baseline, countries=["FRA", "DZA"], missing_input_policy="skip_missing_countries"))
    assert plan.status == "succeeded"
    assert [s["country"] for s in plan.applicability["skipped_countries"]] == ["DZA"]
    assert {r.iso3 for r in plan.results} == {"FRA"}
    only_missing = run(db, spec(baseline, countries=["DZA"], missing_input_policy="skip_missing_countries"))
    assert only_missing.status == "missing_input" and only_missing.results == []


def test_skip_policy_gives_the_same_numbers_as_fail_for_complete_countries(db, baseline) -> None:
    a = run(db, spec(baseline, countries=["FRA", "CHN", "USA"]))
    b = run(db, spec(baseline, countries=["FRA", "CHN", "USA", "DZA"], missing_input_policy="skip_missing_countries"))
    assert a.status == b.status == "succeeded"
    assert a.output_digest == b.output_digest


def test_v1_specs_keep_the_v1_contract(baseline) -> None:
    with pytest.raises(ValidationProblem):
        validate_scenario_spec(spec(baseline, schema_version="1.0.0", missing_input_policy="skip_missing_countries"))
    with pytest.raises(ValidationProblem):
        validate_scenario_spec(spec(baseline, schema_version="1.0.0", countries=["CHE"]))
    with pytest.raises(ValidationProblem):
        validate_scenario_spec(spec(baseline, countries=["ALL", "FRA"]))
    assert validate_scenario_spec(spec(baseline, schema_version="1.0.0")).countries == ("FRA", "CHN", "USA")
