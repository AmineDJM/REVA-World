"""ScenarioSpec v2.0.0: discriminated union on model_key; V1 crude specs keep validating."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.simulation import plugins
from reva.simulation.plugins import InputRequirement, ResolvedInputs, ResultRow
from reva.simulation.scenario_schema import SCHEMA_FILE, build_schema, render
from reva.simulation.spec import GenericScenarioSpec, ScenarioSpec, validate_scenario_spec

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
V1 = json.loads((CONTRACTS / "scenario.v1.schema.json").read_text())
V2 = json.loads((CONTRACTS / "scenario.schema.json").read_text())
EXAMPLE = json.loads((CONTRACTS / "scenario.example.json").read_text())


def errs(schema: dict[str, Any], doc: Any) -> list[str]:
    return [e.message for e in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(doc)]


def test_exported_schema_is_generated_from_the_registered_plugins() -> None:
    assert SCHEMA_FILE.read_text() == render(build_schema()), (
        "contracts/scenario.schema.json is stale: run `uv run python -m reva.simulation.scenario_schema`"
    )
    assert "economic.crude_exposure" in V2["$defs"]


def test_crude_branch_accepts_every_v1_spec() -> None:
    """The crude branch may only widen V1 (e.g. V2 step 1: more countries, an explicit skip policy);
    it never narrows it: every V1-valid spec stays valid. Keywords V1 constrains and the branch
    does not widen keep V1's exact value."""
    branch = V2["$defs"]["economic.crude_exposure"]
    assert "1.0.0" in branch["properties"]["schema_version"]["enum"]
    assert set(branch["properties"]) == set(V1["properties"]) and branch["required"] == V1["required"]
    widened = {"schema_version", "countries", "missing_input_policy"}
    for k in set(V1["properties"]) - widened:
        assert branch["properties"][k] == V1["properties"][k], k


V1_VALID_VARIANTS = [
    lambda d: None,
    lambda d: d.update(countries=["FRA", "DZA", "CHN", "USA"]),
    lambda d: d.update(
        fidelity="L2",
        assumptions={
            "quantity_response": "elasticity_sensitivity",
            "no_general_equilibrium": True,
            "elasticity_grid": [{"import_elasticity": -0.5, "export_elasticity": 0.25, "basis": "scenario_assumption"}],
        },
    ),
    lambda d: d.update(fidelity="L0", outputs=["exposure_percent_baseline_gdp"], reference_year=2020),
]


@pytest.mark.parametrize("variant", V1_VALID_VARIANTS)
def test_v1_valid_specs_validate_under_v2(variant: Any) -> None:
    doc = copy.deepcopy(EXAMPLE)
    variant(doc)
    assert errs(V1, doc) == [] and errs(V2, doc) == []


def test_v1_spec_identity_is_unchanged_and_2_0_0_is_accepted() -> None:
    spec = validate_scenario_spec({**EXAMPLE, "baseline_release_id": str(uuid.uuid4())})
    assert isinstance(spec, ScenarioSpec) and spec.as_json()["schema_version"] == "1.0.0"
    assert spec.canonical_bytes() == json.dumps(spec.as_json(), sort_keys=True, separators=(",", ":")).encode()
    v2 = validate_scenario_spec({**spec.as_json(), "schema_version": "2.0.0"})
    assert isinstance(v2, ScenarioSpec) and v2.digest() != spec.digest()  # a distinct, explicit identity


def test_unknown_model_is_a_capability_gap() -> None:
    with pytest.raises(CapabilityGap) as ei:
        validate_scenario_spec({**EXAMPLE, "model_key": "economic.unknown_model"})
    assert ei.value.details["capability"] == "model.economic.unknown_model"
    assert errs(V2, {**EXAMPLE, "model_key": "economic.unknown_model"}) != []


class _Multi:
    """A minimal multi-period plugin used to exercise the union and the generic spec path."""

    model_key = "test.multi_period"
    model_version = "0.1.0"
    fidelities = frozenset({"L1"})
    missing_input_policies = frozenset({"fail"})

    def spec_schema(self) -> dict[str, Any]:
        return {
            "schema_version": {"const": "2.0.0"},
            "horizon": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "periods": {"type": "integer", "minimum": 1, "maximum": 30},
                    "unit": {"const": "calendar_year"},
                },
                "required": ["periods", "unit"],
            },
            "intervention": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"kind": {"const": "scale_series"}, "factor": {"type": "number"}},
                "required": ["kind", "factor"],
            },
            "assumptions": {"type": "object", "additionalProperties": False, "properties": {}},
            "outputs": {"type": "array", "minItems": 1, "items": {"enum": ["scaled"]}},
        }

    def validate(self, spec: dict[str, Any]) -> None:
        if spec["intervention"]["factor"] <= 0:
            raise ValidationProblem("factor must be > 0")

    def requirements(self, spec: dict[str, Any]) -> list[InputRequirement]:
        return []

    def compile(self, inputs: ResolvedInputs, spec: dict[str, Any]) -> Any:
        return None

    def compute(self, compiled: Any, fidelity: str) -> list[ResultRow]:
        return []

    def manifest(self) -> dict[str, Any]:
        return {}


def multi_spec(**over: Any) -> dict[str, Any]:
    doc = {
        **copy.deepcopy(EXAMPLE),
        "schema_version": "2.0.0",
        "model_key": "test.multi_period",
        "model_version": "0.1.0",
        "baseline_release_id": str(uuid.uuid4()),
        "countries": ["DEU", "FRA"],
        "horizon": {"periods": 10, "unit": "calendar_year"},
        "intervention": {"kind": "scale_series", "factor": 2},
        "assumptions": {},
        "outputs": ["scaled"],
    }
    doc.update(over)
    return doc


@pytest.fixture
def multi() -> Any:
    m = plugins.register(_Multi())
    yield m
    plugins.unregister(m.model_key)


def test_plugin_branch_validates_generic_specs(multi: Any) -> None:
    spec = validate_scenario_spec(multi_spec())
    assert isinstance(spec, GenericScenarioSpec)
    assert spec.model_key == "test.multi_period" and spec.horizon == {"periods": 10, "unit": "calendar_year"}
    assert spec.canonical_bytes() == json.dumps(spec.as_json(), sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(AttributeError):
        spec.title = "x"  # type: ignore[misc]
    schema = build_schema(dict(plugins.REGISTRY))
    assert set(schema["properties"]["model_key"]["enum"]) >= {"economic.crude_exposure", "test.multi_period"}
    for bad, exc in [
        (multi_spec(extra=1), ValidationProblem),
        (multi_spec(intervention={"kind": "scale_series", "factor": 2, "x": 1}), ValidationProblem),
        (multi_spec(intervention={"kind": "scale_series", "factor": -1}), ValidationProblem),  # plugin validate
        (multi_spec(outputs=["gdp_loss"]), CapabilityGap),  # honesty rule: no macro outputs
        (multi_spec(missing_input_policy="skip_missing_countries"), CapabilityGap),  # not supported by plugin
        (multi_spec(fidelity="L2"), CapabilityGap),  # plugin computes L1 only
        (multi_spec(fidelity="L3"), CapabilityGap),
        (multi_spec(model_version="9.9.9"), ValidationProblem),
        (multi_spec(baseline_release_id="00000000-0000-4000-8000-000000000001"), ValidationProblem),
    ]:
        with pytest.raises(exc):
            validate_scenario_spec(bad)
