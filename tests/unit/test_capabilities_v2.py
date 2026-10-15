"""V2 honesty rule 5: implemented capabilities cite existing tests (and V2 ones a doc entry)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from reva import capabilities as caps

ROOT = Path(__file__).resolve().parents[2]
V2_RESERVED = ("demography.cohort_component", "economic.io_price", "social.retirement_age", "nl.scenario_drafts",
               "scenario.all_countries")


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    return {n for n in names if n.startswith("test_")}


@pytest.mark.parametrize("cap", [c for c in caps.catalogue() if c.status == "implemented"],
                         ids=lambda c: c.capability_id)
def test_implemented_capability_cites_existing_tests(cap: caps.Capability) -> None:
    assert cap.tests, f"{cap.capability_id} is implemented but cites no test"
    for ref in cap.tests:
        path, _, name = ref.partition("::")
        f = ROOT / path
        assert f.is_file() and f.name.startswith("test_"), f"{cap.capability_id}: {path} does not exist"
        assert name and name in _test_names(f), f"{cap.capability_id}: {ref} is not a test function"
    if cap.phase == "V2":
        assert cap.docs, f"{cap.capability_id}: an implemented V2 capability needs a doc entry"
        for d in cap.docs:
            assert (ROOT / d).is_file(), f"{cap.capability_id}: doc {d} does not exist"


@pytest.mark.parametrize("cid", V2_RESERVED)
def test_v2_model_capabilities_are_declared(cid: str) -> None:
    c = caps.get(cid)
    assert c is not None and c.phase == "V2"
    if c.status != "implemented":
        with pytest.raises(caps.CapabilityGap):
            caps.require(cid)


def test_cited_tests_appear_in_api_shape() -> None:
    d = {c["capability_id"]: c for c in caps.as_dict()["capabilities"]}
    assert d["fidelity.L1"]["tests"] and d["simulation.platform.dataset_inputs"]["docs"] == ["docs/dev/v2_eng.md"]
