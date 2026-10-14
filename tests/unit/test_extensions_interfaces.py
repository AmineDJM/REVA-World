"""P9-P12 extension interfaces: pure verifiers work, every capability entry point refuses honestly."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from reva.extensions import decision, feedback, io_sfc, population, runner
from reva.kernel.errors import CapabilityGap, ValidationProblem

D = Decimal


def P(sector, account, unit, amount):
    return io_sfc.Posting(sector, account, unit, D(amount))


def purchase(tx_id="t1", amount="100"):
    """Household buys goods from firms, paying with deposits."""
    return io_sfc.LedgerTransaction(tx_id, "transaction", (
        P("households", "deposits", "USD", f"-{amount}"), P("firms", "deposits", "USD", amount),
        P("households", "goods", "USD", amount), P("firms", "goods", "USD", f"-{amount}"),
    ))


# ----------------------------------------------------------------------------- P9 ledger verifier

def test_balanced_ledger_passes():
    rep = io_sfc.verify_double_entry([purchase()])
    assert rep.balanced and rep.findings == () and rep.verification_only


def test_imbalance_detected_with_residual():
    tx = io_sfc.LedgerTransaction("bad", "transaction", (
        P("households", "deposits", "USD", "-100"), P("firms", "deposits", "USD", "99.99")))
    rep = io_sfc.verify_double_entry([tx])
    assert not rep.balanced
    f = rep.findings[0]
    assert f.code == "unbalanced_transaction" and f.residual == D("-0.01") and f.transaction_id == "bad"


def test_money_never_balances_against_energy():
    tx = io_sfc.LedgerTransaction("mix", "transaction", (
        P("firms", "deposits", "USD", "-100"), P("firms", "fuel", "MJ", "100")))
    rep = io_sfc.verify_double_entry([tx])
    assert {f.key for f in rep.findings} == {("USD",), ("MJ",)}


def test_revaluation_rules_and_boundary_exchange():
    reval_ok = io_sfc.LedgerTransaction("rv", "revaluation", (
        P("banks", "foreign_bonds", "USD", "5"), P("banks", "net_worth", "USD", "-5")))
    reval_cp = io_sfc.LedgerTransaction("rv2", "revaluation", (
        P("banks", "foreign_bonds", "USD", "5"), P("government", "net_worth", "USD", "-5")))
    reval_phys = io_sfc.LedgerTransaction("rv3", "revaluation", (
        P("firms", "oil", "bbl", "5"), P("firms", "oil_adj", "bbl", "-5")))
    boundary = io_sfc.LedgerTransaction("imp", "boundary_exchange", (
        P("firms", "deposits", "USD", "-10"), P("households", "deposits", "USD", "10")))
    rep = io_sfc.verify_double_entry([reval_ok, reval_cp, reval_phys, boundary])
    codes = sorted(f.code for f in rep.findings)
    assert codes == ["boundary_without_rest_of_world", "revaluation_physical_unit", "revaluation_with_counterparty"]


def test_stock_flow_consistency_and_zero_sum_claims():
    opening = {("households", "deposits", "USD"): D(500), ("banks", "deposits", "USD"): D(-1000),
               ("firms", "deposits", "USD"): D(500), ("households", "goods", "USD"): D(0),
               ("firms", "goods", "USD"): D(300)}
    closing = {("households", "deposits", "USD"): D(400), ("banks", "deposits", "USD"): D(-1000),
               ("firms", "deposits", "USD"): D(600), ("households", "goods", "USD"): D(100),
               ("firms", "goods", "USD"): D(200)}
    ok = io_sfc.verify_stock_flow(opening, [purchase()], closing, financial_accounts=frozenset({"deposits"}))
    assert ok.balanced, ok.findings
    wrong = dict(closing)
    wrong[("firms", "deposits", "USD")] = D(601)
    rep = io_sfc.verify_stock_flow(opening, [purchase()], wrong, financial_accounts=frozenset({"deposits"}))
    assert {f.code for f in rep.findings} == {"stock_flow_mismatch", "instrument_not_zero_sum"}


def test_float_amounts_and_single_leg_rejected():
    with pytest.raises(ValidationProblem):
        io_sfc.Posting("a", "b", "USD", 0.1)
    with pytest.raises(ValidationProblem):
        io_sfc.LedgerTransaction("x", "transfer", (P("a", "b", "USD", "1"),))


def test_io_and_sfc_models_refuse():
    meta = io_sfc.IOTableMeta("t", ("FRA",), ("C19",), 2019, "basic_prices", "USD", 1_000_000, "src", "lic")
    with pytest.raises(CapabilityGap) as ei:
        io_sfc.run_input_output(meta, {})
    assert ei.value.details["capability"] == "economic.input_output"
    with pytest.raises(CapabilityGap) as ei:
        io_sfc.run_sfc_model("l1")
    assert ei.value.details["available_instead"] == ["verification.double_entry_balance"]


# ----------------------------------------------------------------------------- P9 feedback solver

def contract(**kw):
    base = dict(step_size=1.0, criterion="residual_inf_norm", tolerance=1e-8, damping=0.5, max_iterations=200,
                conservative_exchange=True, error_bounds={"price": 1e-6, "quantity": 1e-6})
    base.update(kw)
    return feedback.ConvergenceContract(**base)


def test_feedback_solve_refuses_without_contract():
    cfg = feedback.FeedbackSolverConfig("b1", ("m1", "m2"), ("price", "quantity"), None)
    with pytest.raises(CapabilityGap) as ei:
        feedback.solve(cfg)
    assert ei.value.details["refusal"] == "no_convergence_contract"
    assert ei.value.details["capability"] == "coupling.feedback"


def test_feedback_solve_refuses_even_with_valid_contract():
    cfg = feedback.FeedbackSolverConfig("b1", ("m1", "m2"), ("price", "quantity"), contract())
    with pytest.raises(CapabilityGap) as ei:
        feedback.solve(cfg)
    d = ei.value.details
    assert d["refusal"] == "not_implemented_in_v1" and d["status"] == "reserved" and d["phase"] == "P9"


@pytest.mark.parametrize("bad", [dict(damping=0.0), dict(damping=1.5), dict(conservative_exchange=False),
                                 dict(error_bounds={}), dict(max_iterations=0), dict(tolerance=float("nan"))])
def test_invalid_contracts_rejected(bad):
    with pytest.raises(ValidationProblem):
        contract(**bad)


def test_contract_must_bound_every_exchanged_variable():
    with pytest.raises(ValidationProblem):
        feedback.FeedbackSolverConfig("b", ("m1", "m2"), ("price", "stock"), contract())


def test_outcome_never_presents_last_iterate_as_equilibrium():
    with pytest.raises(ValidationProblem):
        feedback.FeedbackOutcome("converged", 200, 1e-3, 1e-8, 200, {"p": 1.0}, True)
    with pytest.raises(ValidationProblem):
        feedback.FeedbackOutcome("failed", 200, 1e-3, 1e-8, 200, {"p": 1.0}, False)
    with pytest.raises(ValidationProblem):
        feedback.FeedbackOutcome("qualified_partial", 200, 1e-3, 1e-8, 200, {"p": 1.0}, True, "partial")
    ok = feedback.FeedbackOutcome("failed", 200, 1e-3, 1e-8, 200, None, False)
    assert ok.values is None


# ----------------------------------------------------------------------------- P10 population / privacy

def test_dp_budget_requires_explicit_terms():
    b = population.DPBudget(1.0, 1e-6, "add_remove_household", "household", "zero_concentrated_dp", "release X")
    with pytest.raises(CapabilityGap) as ei:
        population.open_privacy_accountant(b)
    assert ei.value.details["capability"] == "population.privacy_accounting"
    for kw in (dict(epsilon=0.0), dict(epsilon=float("inf")), dict(delta=1.0), dict(unit_of_privacy="person"),
               dict(justification=" ")):
        args = dict(epsilon=1.0, delta=1e-6, neighbouring="add_remove_household", unit_of_privacy="household",
                    composition="basic_sequential", justification="j")
        args.update(kw)
        with pytest.raises(ValidationProblem):
            population.DPBudget(**args)


def test_household_synthesis_refuses_and_validates_sources():
    src = population.JointDataSource("census_sample", "licensed", "pending", ("age", "hh_size"), "household")
    with pytest.raises(ValidationProblem):
        population.HouseholdSynthesisSpec("FRA residents", "2021", ("wpp",), (src,), ("one head",), "bounds")
    with pytest.raises(ValidationProblem):
        population.HouseholdSynthesisSpec("FRA residents", "2021", ("wpp",), (), ("one head",), "bounds",
                                          sensitive_attributes_allowed=("religion",))
    spec = population.HouseholdSynthesisSpec("FRA residents", "2021", ("wpp",), (), ("one head",), "bounds")
    with pytest.raises(CapabilityGap) as ei:
        population.synthesize_households(spec, seed=1)
    assert ei.value.details["status"] == "reserved" and ei.value.details["phase"] == "P10"


# ----------------------------------------------------------------------------- P11 external runner

BUDGET = runner.RunnerResourceBudget(2, 600, 2**31, 2**32, 900, 2**24)
DEP = "numpy==2.1.3 --hash=sha256:" + "a" * 64


def test_runner_policy_defaults_are_safe():
    p = runner.ExternalRunPolicy("1", "firecracker_microvm", BUDGET, (DEP,))
    assert p.network_blocked and p.secrets_mounted is False and p.inputs_read_only and p.disposable


@pytest.mark.parametrize("kw", [dict(isolation="python_subprocess"), dict(isolation="docker_default"),
                                dict(secrets_mounted=True), dict(inputs_read_only=False),
                                dict(allowlisted_dependencies=("numpy",)), dict(egress_allowlist=("*",))])
def test_runner_policy_rejects_unsafe(kw):
    args = dict(policy_version="1", isolation="gvisor_vm", budget=BUDGET, allowlisted_dependencies=(DEP,))
    args.update(kw)
    with pytest.raises(ValidationProblem):
        runner.ExternalRunPolicy(**args)


def test_runner_budget_must_be_bounded():
    with pytest.raises(ValidationProblem):
        runner.RunnerResourceBudget(2, 600, 0, 1, 1, 1)


def test_run_request_refuses_secrets_and_unaddressed_inputs():
    h = "b" * 64
    with pytest.raises(ValidationProblem):
        runner.ExternalRunRequest(uuid.uuid4(), "c", h, h, (), {"REVA_SANDBOX_TOKEN": "x"})
    with pytest.raises(ValidationProblem):
        runner.RunInput("fixture", "not-a-hash", "application/json")
    runner.ExternalRunRequest(uuid.uuid4(), "c", h, h, (runner.RunInput("f", h, "application/json"),),
                              {"LANG": "C.UTF-8"})


def test_resolve_runner_blocked_or_reserved(monkeypatch):
    monkeypatch.delenv("REVA_SANDBOX_ENDPOINT", raising=False)
    with pytest.raises(CapabilityGap) as ei:
        runner.resolve_external_runner()
    assert ei.value.details["status"] == "blocked"
    cfg = SimpleNamespace(reva_sandbox_endpoint="https://vm-runner.example", reva_sandbox_token="tok-secret")
    with pytest.raises(CapabilityGap) as ei:
        runner.resolve_external_runner(cfg)
    assert ei.value.details["status"] == "reserved"
    blob = json.dumps(ei.value.problem())
    assert "tok-secret" not in blob and "vm-runner.example" not in blob


# ----------------------------------------------------------------------------- P12 decisions

def make_spec(**kw):
    args = dict(
        actor="FRA_government",
        actions=(decision.Action("fuel_tax", "fuel excise change", "continuous", -0.1, 0.1, unit="EUR/l",
                                 controllable_by="FRA_government"),),
        objectives=(decision.Objective("exposure", "economic.crude_exposure_pct_gdp", "minimise", "percent"),
                    decision.Objective("revenue", "fiscal.revenue", "maximise", "EUR",
                                       affected_groups=("bottom_quintile",))),
        distributional_constraints=(decision.DistributionalConstraint("dc1", "bottom_quintile", "cost_share",
                                                                      "<=", 0.02, "1"),),
        hard_constraints=(decision.HardConstraint("eu_min", "legal", "EU minimum excise", "Directive 2003/96/EC"),),
        risk_measure=decision.RiskMeasure("cvar", alpha=0.1),
        horizon_periods=1, period_unit="year",
        discounting=decision.Discounting("none", rationale="single period"),
        model_set=("economic.crude_exposure@1.0.0",), scenario_set=("s1", "s2"),
    )
    args.update(kw)
    return decision.DecisionSpec(**args)


def test_optimise_refuses_with_structured_gap():
    spec = make_spec()
    with pytest.raises(CapabilityGap) as ei:
        decision.optimise(spec)
    d = ei.value.details
    assert d["capability"] == "decision.search" and d["status"] == "reserved" and d["phase"] == "P12"
    assert d["context"]["spec_digest"] == spec.digest() and len(spec.digest()) == 64
    assert d["kind"] == "capability_not_available"


def test_decision_spec_validation():
    with pytest.raises(ValidationProblem):
        make_spec(hard_constraints=())
    make_spec(hard_constraints=(), no_hard_constraints_rationale="analytic test problem")
    with pytest.raises(ValidationProblem):
        make_spec(actions=(decision.Action("x", "d", "continuous", 0, 1, unit="u", controllable_by="CHN"),))
    with pytest.raises(ValidationProblem):
        make_spec(risk_measure=decision.RiskMeasure("cvar", alpha=1.5))
    with pytest.raises(ValidationProblem):
        decision.Discounting("constant_exponential", 0.03, rationale="")
    with pytest.raises(ValidationProblem):
        make_spec(model_set=())
    assert make_spec(risk_measure=decision.RiskMeasure("expectation")).single_model_mean_only


def test_multi_actor_spec_and_refusal():
    obj = (decision.Objective("o", "c", "maximise", "u"),)
    act = lambda who: (decision.Action("a", "d", "binary", controllable_by=who),)  # noqa: E731
    a = decision.ActorSpec("A", obj, act("A"), ("price",), "beliefs/a", 0)
    b = decision.ActorSpec("B", obj, act("B"), ("price",), "beliefs/b", 1)
    with pytest.raises(ValidationProblem):
        decision.MultiActorSpec((a,), "nash", ("r1",))
    with pytest.raises(ValidationProblem):
        decision.MultiActorSpec((a, b), "stackelberg", ("r1",))
    with pytest.raises(ValidationProblem):
        decision.ActorSpec("C", obj, act("C"), (), "beliefs/c", 0)
    spec = decision.MultiActorSpec((a, b), "stackelberg", ("r1", "r2"), leader="A")
    with pytest.raises(CapabilityGap) as ei:
        decision.solve_reactions(spec)
    assert ei.value.details["capability"] == "decision.multi_actor"


def test_frontier_points_are_model_conditioned():
    pt = decision.ParetoPoint({"fuel_tax": 0.05}, {"exposure": {"s1": 1.0, "s2": 1.2}}, {"dc1": 0.0}, {})
    fr = decision.ParetoFrontier("d" * 64, (pt,), {}, {}, {}, "test", "0")
    assert pt.model_conditioned is True and fr.caveats
