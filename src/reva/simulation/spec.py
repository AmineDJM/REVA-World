"""ScenarioSpec v1: JSON Schema validation followed by mandatory scope checks.

Contract bounds are implementation/applicability limits, not empirical claims
about plausible economics. The example's fixture baseline UUID is not a real
release; a real published baseline must be resolved before execution.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import logical_hash

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
FIXTURE_BASELINE = "00000000-0000-4000-8000-000000000001"
SUPPORTED_FIDELITY = ("L0", "L1", "L2")
RESERVED_FIDELITY = ("L3", "L4")
IMPORT_ELASTICITY_ENVELOPE = (-2.0, 1.0)
EXPORT_ELASTICITY_ENVELOPE = (-1.0, 2.0)


@lru_cache(maxsize=1)
def scenario_schema() -> dict[str, Any]:
    return json.loads((CONTRACTS / "scenario.schema.json").read_text())


class ElasticityPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    import_elasticity: float
    export_elasticity: float
    basis: Literal["scenario_assumption"]


class Intervention(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["set_exogenous_border_price_multiplier"]
    import_multiplier: float = Field(gt=0, le=3)
    export_multiplier: float = Field(gt=0, le=3)
    unit: Literal["ratio"]


class Assumptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    quantity_response: Literal["fixed", "elasticity_sensitivity"]
    elasticity_grid: tuple[ElasticityPoint, ...]
    no_general_equilibrium: Literal[True]


class Horizon(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    periods: Literal[1]
    unit: Literal["annual_accounting_period"]


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1.0.0"]
    title: str
    baseline_release_id: str
    knowledge_cutoff: str
    model_key: Literal["economic.crude_exposure"]
    model_version: Literal["1.0.0"]
    countries: tuple[Literal["FRA", "DZA", "CHN", "USA"], ...]
    reference_year: int
    horizon: Horizon
    intervention: Intervention
    assumptions: Assumptions
    fidelity: Literal["L0", "L1", "L2"]
    missing_input_policy: Literal["fail"]
    outputs: tuple[str, ...]
    seed: int

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        """Plain sorted JSON; the DB verifies these bytes encode the stored spec."""
        return json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def digest(self) -> str:
        return logical_hash(self.as_json())


def validate_scenario_spec(data: dict[str, Any], *, allow_fixture_baseline: bool = False) -> ScenarioSpec:
    """JSON Schema validation, then scope checks (still required after schema)."""
    if isinstance(data, dict) and data.get("fidelity") in RESERVED_FIDELITY:
        raise CapabilityGap(
            f"fidelity {data['fidelity']} is reserved and not available in V1",
            capability=f"fidelity.{data['fidelity']}",
            reason="L3 requires justified parameter distributions; L4 requires research execution",
        )
    if isinstance(data, dict) and isinstance(data.get("horizon"), dict) and data["horizon"].get("periods") != 1:
        raise CapabilityGap(
            "only a one annual accounting period horizon is supported; dynamic multi-year effects are unsupported",
            capability="horizon.multi_period",
        )
    unsupported = {"gdp_loss", "unemployment", "consumer_inflation", "household_welfare"} & set(
        data.get("outputs", []) if isinstance(data, dict) else []
    )
    if unsupported:
        raise CapabilityGap(
            f"outputs {sorted(unsupported)} are not produced by an accounting exposure model",
            capability="outputs.macroeconomic",
        )
    validator = jsonschema.Draft202012Validator(scenario_schema(), format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        raise ValidationProblem(
            "scenario does not satisfy ScenarioSpec v1",
            errors=[{"path": "/" + "/".join(map(str, e.path)), "message": e.message} for e in errors[:20]],
        )
    spec = ScenarioSpec.model_validate(data)
    if spec.baseline_release_id == FIXTURE_BASELINE and not allow_fixture_baseline:
        raise ValidationProblem(
            "baseline_release_id is the illustrative fixture UUID; resolve a real published baseline release"
        )
    for p in spec.assumptions.elasticity_grid:
        if not IMPORT_ELASTICITY_ENVELOPE[0] <= p.import_elasticity <= IMPORT_ELASTICITY_ENVELOPE[1]:
            raise ValidationProblem("import elasticity outside registered envelope")
        if not EXPORT_ELASTICITY_ENVELOPE[0] <= p.export_elasticity <= EXPORT_ELASTICITY_ENVELOPE[1]:
            raise ValidationProblem("export elasticity outside registered envelope")
    if spec.fidelity == "L1" and (spec.assumptions.quantity_response != "fixed" or spec.assumptions.elasticity_grid):
        raise ValidationProblem("L1 requires fixed quantities and an empty elasticity grid")
    if spec.fidelity == "L2" and (
        spec.assumptions.quantity_response != "elasticity_sensitivity" or not spec.assumptions.elasticity_grid
    ):
        raise ValidationProblem("L2 requires an elasticity_sensitivity grid")
    if not 2020 <= spec.reference_year <= 2024:
        raise CapabilityGap("reference year outside the model's registered applicability (2020-2024)")
    return spec
