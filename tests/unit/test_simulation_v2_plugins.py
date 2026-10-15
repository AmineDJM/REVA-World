"""V2 simulation platform: plugin protocol, registry, resolved inputs, crude plugin identity.

The crude plugin must reproduce the V1 engine bit for bit. Goldens:

* ``tests/fixtures/simulation/crude_v1_golden.json`` — full result rows (value,
  uncertainty, applicability), output digests, L0 cache keys, baseline digests and
  coupling schedules computed by the V1 (pre-plugin) engine;
* ``var/acceptance/demo_live.json`` — the recorded live acceptance demo (real WDI +
  Comtrade inputs): the plugin reproduces its values and its recorded output digest.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pyarrow as pa
import pytest

from reva.cli.seed import entity_id
from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.simulation import engine, plugins
from reva.simulation.crude_plugin import MODEL as CRUDE
from reva.simulation.plugins import (
    InputRequirement,
    ResolvedInputs,
    ResultRow,
    ScalarInput,
    SimulationModel,
    SubjectRef,
)

ROOT = Path(__file__).parents[2]
GOLDEN = json.loads((ROOT / "tests/fixtures/simulation/crude_v1_golden.json").read_text())
DEMO = ROOT / "var/acceptance/demo_live.json"


def _value(v: str) -> dict[str, Any]:
    return {"kind": "decimal", "value": v, "unit_ref": "unit.usd",
            "basis": {"currency": "USD", "price_basis": "nominal"}}


def resolved(spec: dict[str, Any], inputs: dict[str, dict[str, str]]) -> ResolvedInputs:
    """What the platform resolver would hand the plugin for these baseline values."""
    reqs = CRUDE.requirements(spec)
    subjects = {iso: SubjectRef(iso, entity_id(iso), "reviewed_alias:iso3166_alpha3") for iso in spec["countries"]}
    scalars = {}
    for r in reqs:
        (y,) = r.years
        vd = _value(inputs[r.subject][r.name])
        from reva.kernel.values import parse_value

        scalars[(r.name, r.subject, y)] = ScalarInput(
            r, y, subjects[r.subject].entity_id, parse_value(vd), vd, f"est-{r.subject}-{r.name}",
            {"kind": "interval", "precision": "year", "start": f"{y}-01-01", "end": f"{y + 1}-01-01"},
            "period_flow_total", {}, "selected_report")
    return ResolvedInputs(release={"id": "00000000-0000-4000-8000-0000000000aa"}, requirements=tuple(reqs),
                          subjects=subjects, scalars=scalars, datasets={})


def run_plugin(spec: dict[str, Any], inputs: dict[str, dict[str, str]]) -> tuple[Any, list[ResultRow]]:
    CRUDE.validate(spec)
    compiled = CRUDE.compile(resolved(spec, inputs), spec)
    assert compiled.missing == [] and compiled.incompatible == []
    return compiled, CRUDE.compute(compiled, spec["fidelity"])


# --------------------------------------------------------------------------- identity
@pytest.mark.parametrize("case", sorted(GOLDEN["cases"]))
def test_crude_plugin_is_bit_identical_to_v1_engine(case: str) -> None:
    g = GOLDEN["cases"][case]
    compiled, rows = run_plugin(g["spec"], GOLDEN["inputs"])
    got = [{"output_key": r.output_key, "iso3": r.iso3, "subject_id": str(r.subject_id), "variant": r.variant,
            "value": r.value, "uncertainty": r.uncertainty, "applicability": r.applicability} for r in rows]
    assert json.loads(json.dumps(got)) == g["rows"]
    assert engine.output_digest(rows) == g["output_digest"]
    assert engine.cache_key(g["spec"]) == g["cache_key"]
    assert compiled.baseline_digest() == g["baseline_digest"]
    assert json.loads(json.dumps(compiled.baseline_inputs())) == g["baseline_inputs"]
    assert compiled.schedule == g["schedule"]
    assert compiled.change.as_json() == g["change"]
    assert all(r.period is None for r in rows)


def test_crude_plugin_explanations_unchanged() -> None:
    for f, want in GOLDEN["explain"].items():
        assert json.loads(json.dumps(CRUDE.explanation(f))) == want


@pytest.mark.skipif(not DEMO.exists(), reason="recorded live demo not present")
def test_crude_plugin_reproduces_recorded_live_demo() -> None:
    demo = json.loads(DEMO.read_text())
    steps = {s["key"]: s for s in demo["steps"]}
    l1 = steps["scenario_l1"]["evidence"]
    res = l1["results"]
    inputs = {iso: {n: res[iso][f"input_{n}"] for n in "MXG"} for iso in res}
    spec = json.loads((ROOT / "contracts/scenario.example.json").read_text())
    spec.update(countries=["FRA", "CHN", "USA"], fidelity="L1")
    spec["intervention"].update(import_multiplier=1.25, export_multiplier=1.25)
    _, rows = run_plugin(spec, inputs)
    for r in rows:
        assert r.value["value"] == res[r.iso3][r.output_key], (r.iso3, r.output_key)
    fra = {r.output_key: r.value["value"] for r in rows if r.iso3 == "FRA"}
    assert fra["exposure_percent_baseline_gdp"] == "0.25756760879252203773441363615596620341591545855751"
    assert engine.output_digest(rows) == l1["job"]["result_ref"]["output_digest"]

    l2 = steps["l2_sensitivity"]["evidence"]
    spec2 = {**spec, "fidelity": "L2", "assumptions": {"quantity_response": "elasticity_sensitivity",
                                                        "no_general_equilibrium": True,
                                                        "elasticity_grid": l2["grid"]}}
    _, rows2 = run_plugin(spec2, inputs)
    for iso, ex in l2["exposure"].items():
        rng = next(r for r in rows2 if r.iso3 == iso and r.variant == engine.SENSITIVITY_VARIANT
                   and r.output_key == "exposure_percent_baseline_gdp")
        assert [rng.value["lower"], rng.value["upper"]] == ex["sensitivity_range"]
        l1row = next(r for r in rows2 if r.iso3 == iso and r.variant == "L1"
                     and r.output_key == "exposure_percent_baseline_gdp")
        assert l1row.value["value"] == ex["exposure_percent_L1"]


# --------------------------------------------------------------------------- registry
def test_registry_loads_crude_plugin_and_refuses_unknown_models() -> None:
    reg = plugins.load_all()
    assert reg["economic.crude_exposure"] is CRUDE
    assert isinstance(CRUDE, SimulationModel)
    assert plugins.get("economic.crude_exposure") is CRUDE
    with pytest.raises(CapabilityGap) as ei:
        plugins.get("economic.does_not_exist")
    assert ei.value.details["capability"] == "model.economic.does_not_exist"


class _Toy:
    model_key = "test.toy"
    model_version = "0.0.1"
    fidelities = frozenset({"L1"})

    def validate(self, spec: dict[str, Any]) -> None: ...

    def requirements(self, spec: dict[str, Any]) -> list[InputRequirement]:
        return []

    def compile(self, inputs: ResolvedInputs, spec: dict[str, Any]) -> Any:
        return None

    def compute(self, compiled: Any, fidelity: str) -> list[ResultRow]:
        return []

    def manifest(self) -> dict[str, Any]:
        return {}


def test_register_rules() -> None:
    toy = _Toy()
    try:
        plugins.register(toy)
        plugins.register(toy)  # same object: idempotent
        with pytest.raises(ValueError):
            plugins.register(_Toy())  # another object for a taken key
        plugins.register(_Toy(), replace=True)
        bad = _Toy()
        bad.model_key = "test.bad"
        bad.fidelities = frozenset({"L0"})  # L0 is the platform cache, never a model fidelity
        with pytest.raises(ValueError):
            plugins.register(bad)
        with pytest.raises(TypeError):
            plugins.register(object())  # type: ignore[arg-type]
    finally:
        plugins.unregister("test.toy")


def test_load_all_skips_only_absent_plugin_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "KNOWN_MODEL_MODULES", ("reva.simulation.crude_plugin", "reva.models.nope.absent"))
    assert "economic.crude_exposure" in plugins.load_all()

    def broken(name: str) -> Any:  # an existing plugin whose own dependency is missing
        raise ModuleNotFoundError("No module named 'missing_dependency'", name="missing_dependency")

    monkeypatch.setattr(plugins.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        plugins.load_all()


# --------------------------------------------------------------------------- resolved inputs / rows
def test_resolved_inputs_are_typed_read_only_and_never_zero_fill() -> None:
    reqs = [InputRequirement("M", "c.m", "FRA", (2022, 2023), "scalar"),
            InputRequirement("io", "d.io", "EU27", (2021,), "dataset"),
            InputRequirement("pop", "d.pop", "FRA", (2023,), "dataset")]
    table = pa.table({"row": ["A", "B"], "value": [1.5, 2.5]})
    ri = ResolvedInputs.from_values(
        reqs, scalars={("M", "FRA", 2023): _value("10"),
                       ("M", "FRA", 2022): {"kind": "missing", "reason": "suppressed"}},
        datasets={("io", "EU27"): (table, {"dataset_id": "d1", "artifact_sha256": "ab" * 32, "row_count": 2})})
    assert isinstance(ri.scalars, MappingProxyType) and isinstance(ri.datasets, MappingProxyType)
    with pytest.raises(TypeError):
        ri.scalars[("M", "FRA", 2023)] = None  # type: ignore[index]
    assert ri.scalar("M", "FRA", 2023).value.as_decimal() == Decimal(10)
    assert ri.scalar("M", "FRA", 2022).is_missing
    ds = ri.dataset("io", "EU27")
    frame = ds.frame()
    assert frame.shape == (2, 2) and ds.manifest["row_count"] == 2
    frame = frame.with_columns(frame["value"] * 0)  # a model's frame is its own copy
    assert ri.dataset("io", "EU27").frame()["value"].to_list() == [1.5, 2.5]
    problems = ri.missing_problems()
    assert [(p["input"], p.get("reference_year"), p["reason"]) for p in problems] == [
        ("M", 2022, "suppressed"), ("pop", None, "unavailable")]
    assert ri.missing_problems(subjects=["EU27"]) == []
    with pytest.raises(ValidationProblem):
        ri.scalar("X", "FRA", 2023)
    with pytest.raises(ValidationProblem):
        ri.dataset("pop", "FRA").frame()
    assert ri.provenance()["datasets"][0]["artifact_sha256"] == "ab" * 32


def test_result_row_period_is_half_open_and_extends_digest_only_when_set() -> None:
    sid = entity_id("FRA")
    v = {"kind": "decimal", "value": "1"}
    single = ResultRow("k", "FRA", sid, "L1", v, {"status": "unknown"}, {})
    assert "period" not in single.digest_view()
    p = ResultRow("k", "FRA", sid, "L1", v, {"status": "unknown"}, {}, (date(2024, 1, 1), date(2025, 1, 1)))
    assert p.digest_view()["period"] == ["2024-01-01", "2025-01-01"]
    assert engine.output_digest([single]) != engine.output_digest([p])
    with pytest.raises(ValidationProblem):
        ResultRow("k", "FRA", sid, "L1", v, {}, {}, (date(2025, 1, 1), date(2025, 1, 1)))
    rows = [ResultRow("k", "FRA", sid, "L1", v, {}, {}, (date(2025 - i, 1, 1), date(2026 - i, 1, 1)))
            for i in range(3)]
    assert engine.output_digest(rows) == engine.output_digest(list(reversed(rows)))
