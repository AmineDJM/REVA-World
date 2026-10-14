"""Analytic checks of decision EVSI and the separation from the heuristic proxy."""

from __future__ import annotations

import pytest

from reva.evaluation.voi import (
    DecisionProblem,
    KnowledgeGap,
    estimate_value,
    evpi,
    evsi_exact,
    evsi_nested_mc,
    evsi_nested_mc_expectation,
    prior_decision_value,
    uncertainty_reduction_proxy,
)
from reva.kernel.errors import MissingInput

# utility[action][state]; action 0 = do nothing, action 1 = intervene.
U = ((0.0, -100.0), (-20.0, -20.0))
PROBLEM = DecisionProblem(prior=0.3, utility=U, sensitivity=0.9, specificity=0.8, cost=2.0)


def test_evsi_exact_hand_check():
    # Prior: E[U(0)] = 0.7*0 + 0.3*(-100) = -30; E[U(1)] = -20  -> prior value -20.
    # P(+) = 0.9*0.3 + 0.2*0.7 = 0.41; P(theta=1|+) = 0.27/0.41
    #   E[U(0)|+] = -100*0.27/0.41 = -65.85 ; E[U(1)|+] = -20 -> choose 1, value -20
    # P(-) = 0.59; P(theta=1|-) = 0.03/0.59
    #   E[U(0)|-] = -100*0.03/0.59 = -5.085 ; E[U(1)|-] = -20 -> choose 0, value -3/0.59
    # Preposterior = 0.41*(-20) + 0.59*(-3/0.59) = -8.2 - 3.0 = -11.2
    # Gross EVSI = -11.2 - (-20) = 8.8 ; net of cost 2 -> 6.8
    assert prior_decision_value(PROBLEM) == pytest.approx(-20.0)
    assert evsi_exact(PROBLEM) == pytest.approx(6.8, abs=1e-12)
    # EVPI = E[max_a U] - prior value = (0.7*0 + 0.3*(-20)) + 20 = 14
    assert evpi(PROBLEM) == pytest.approx(14.0)


def test_perfect_information_equals_evpi_minus_cost():
    perfect = DecisionProblem(prior=0.3, utility=U, sensitivity=1.0, specificity=1.0, cost=2.0)
    assert evsi_exact(perfect) == pytest.approx(evpi(perfect) - 2.0, abs=1e-12)
    assert evsi_exact(perfect) == pytest.approx(12.0)


def test_uninformative_test_has_value_minus_cost():
    useless = DecisionProblem(prior=0.3, utility=U, sensitivity=0.6, specificity=0.4, cost=1.5)
    assert evsi_exact(useless) == pytest.approx(-1.5, abs=1e-12)


@pytest.mark.parametrize("problem", [PROBLEM, DecisionProblem(0.15, U, 0.95, 0.9, 0.5)])
def test_nested_mc_agrees_with_exact(problem):
    est = evsi_nested_mc(problem, n_outer=20000, n_inner=2000, seed=12345)
    exact = evsi_exact(problem)
    assert est.std_error > 0
    # Inner-loop bias is O(1/n_inner) and negligible at n_inner=2000.
    assert abs(est.value - exact) <= 3 * est.std_error
    again = evsi_nested_mc(problem, n_outer=20000, n_inner=2000, seed=12345)
    assert again == est


# Review R3-13 problem (verified with exact rational arithmetic: net EVSI = 7.2).
R313 = DecisionProblem(prior=0.3, utility=((0.0, 0.0), (-20.0, 100.0)), sensitivity=0.9,
                       specificity=0.8, cost=1.0)


def test_nested_mc_inner_bias_sign_is_not_determined():
    """The nested estimator's inner-loop bias can be upward or downward (R3-13)."""
    exact = evsi_exact(R313)
    assert exact == pytest.approx(7.2, abs=1e-12)
    # Exact expectation of the estimator (finite sum over the inner Binomial count).
    e1, e2, e64 = (evsi_nested_mc_expectation(R313, n) for n in (1, 2, 64))
    assert e1 == pytest.approx(13.0, abs=1e-9)  # upward at n_inner = 1
    assert e2 == pytest.approx(5.4629, abs=1e-3)  # downward at n_inner = 2
    assert e64 < exact - 0.25  # still downward at n_inner = 64
    assert abs(evsi_nested_mc_expectation(R313, 5000) - exact) < 0.02  # vanishes as n_inner grows
    # The Monte Carlo estimator agrees with its exact expectation, not with the EVSI:
    # at n_inner=2 the bias is ~20 outer standard errors and std_error does not include it.
    mc = evsi_nested_mc(R313, n_outer=200_000, n_inner=2, seed=2)
    assert abs(mc.value - e2) <= 4 * mc.std_error
    assert exact - mc.value > 10 * mc.std_error


def test_nested_mc_expectation_matches_exact_in_the_limit_for_both_problems():
    for problem in (PROBLEM, DecisionProblem(0.15, U, 0.95, 0.9, 0.5)):
        assert evsi_nested_mc_expectation(problem, 4000) == pytest.approx(evsi_exact(problem), abs=0.05)
    with pytest.raises(ValueError):
        evsi_nested_mc_expectation(PROBLEM, 0)


def test_estimate_value_without_decision_spec_is_unitless_proxy():
    v = estimate_value(KnowledgeGap("gap-1", prior_sd=2.0, expected_posterior_sd=1.5))
    assert v.kind == "uncertainty_reduction_proxy"
    assert v.value is not None and float(v.value) == pytest.approx(0.25)
    assert v.unit is not None and v.unit.upper() not in {"USD", "EUR", "GBP", "UNIT.USD"}
    assert "usd" not in v.unit.lower() and "eur" not in v.unit.lower()
    assert any("NOT money" in a for a in v.assumptions)
    assert v.method_version
    assert v.uncertainty["status"] == "unquantified"
    assert v == uncertainty_reduction_proxy(2.0, 1.5)
    with pytest.raises(MissingInput):
        estimate_value({"gap_id": "g"})


def test_estimate_value_with_decision_spec_is_decision_evsi():
    v = estimate_value({"gap_id": "g"}, decision_spec=PROBLEM)
    assert v.kind == "decision_evsi"
    assert float(v.value) == pytest.approx(6.8)
    assert v.unit == "utility"
