"""Invariant 33: no dynamic economic effect inferred solely from the MVP accounting identity.

The crude-exposure model is a one-period border-value accounting identity. A
request for a multi-period horizon, for macroeconomic outputs (GDP loss,
unemployment, inflation, welfare) or without the explicit "no general
equilibrium" assumption is refused with a typed result before anything runs;
the only outputs are accounting changes, and every result carries the
not-claimed statement.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.models.economic import crude_exposure as ce
from reva.simulation.spec import CONTRACTS, validate_scenario_spec

BASE: dict[str, Any] = json.loads((CONTRACTS / "scenario.example.json").read_text())
BASE["baseline_release_id"] = "8b4f5d4e-1111-4222-8333-944455556666"  # any non-illustrative UUID


def _spec(**over: Any) -> dict[str, Any]:
    s = copy.deepcopy(BASE)
    s.update(over)
    return s


def test_positive_control_one_period_accounting_spec_is_valid() -> None:
    assert validate_scenario_spec(_spec()).horizon.periods == 1


@pytest.mark.parametrize("periods", [2, 5, 20])
def test_multi_period_dynamics_are_a_typed_capability_gap(periods: int) -> None:
    with pytest.raises(CapabilityGap) as ei:
        validate_scenario_spec(_spec(horizon={"periods": periods, "unit": "annual_accounting_period"}))
    assert ei.value.details.get("capability_id", ei.value.details.get("capability")) == "horizon.multi_period"


@pytest.mark.parametrize("output", ["gdp_loss", "unemployment", "consumer_inflation", "household_welfare"])
def test_macroeconomic_outputs_are_refused(output: str) -> None:
    with pytest.raises(CapabilityGap):
        validate_scenario_spec(_spec(outputs=[*BASE["outputs"], output]))


def test_general_equilibrium_cannot_be_switched_on() -> None:
    with pytest.raises(ValidationProblem):
        validate_scenario_spec(_spec(assumptions={**BASE["assumptions"], "no_general_equilibrium": False}))


def test_unknown_outputs_and_time_units_are_refused() -> None:
    with pytest.raises((ValidationProblem, CapabilityGap)):
        validate_scenario_spec(_spec(outputs=["oil_demand_elasticity"]))
    with pytest.raises((ValidationProblem, CapabilityGap)):
        validate_scenario_spec(_spec(horizon={"periods": 1, "unit": "quarter"}))


def test_model_outputs_are_accounting_changes_only_and_say_what_is_not_claimed() -> None:
    assert set(ce.OUTPUT_KEYS) == {"import_value_change_usd", "export_value_change_usd",
                                   "net_external_cost_change_usd", "exposure_percent_baseline_gdp"}
    for word in ("GDP loss", "welfare", "inflation", "unemployment", "general-equilibrium"):
        assert word in ce.NOT_CLAIMED
    with pytest.raises(CapabilityGap):
        ce.check_horizon(3, "annual_accounting_period")
    ex = ce.explain()
    assert ex["modality"] == "assumption_conditioned_scenario"
    assert "one annual accounting period; no dynamics" in ex["assumptions"]
