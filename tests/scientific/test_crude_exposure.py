"""economic.crude_exposure 1.0.0: exact analytic checks and properties."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from reva.kernel.errors import CapabilityGap, IncompatibleSemantics, MissingInput, ValidationProblem
from reva.kernel.values import Missing, MissingReason
from reva.models.economic import crude_exposure as ce
from reva.models.economic.crude_exposure import (
    CountryInputs,
    MonetaryInput,
    evaluate_country,
    explain,
    exposure_l1,
    exposure_l2,
    validate_country_inputs,
)

D = Decimal


def test_hand_computed_l1_exact():
    r = exposure_l1(D(100), D(40), D(1000), 1.25, 1.25)
    assert r.import_value_change_usd == D(25)
    assert r.export_value_change_usd == D(10)
    assert r.net_external_cost_change_usd == D(15)
    assert r.exposure_percent_baseline_gdp == D("1.5")
    assert r.scenario_import_value_usd == D(125)
    assert r.scenario_export_value_usd == D(50)
    assert r.as_json()["exposure_percent_baseline_gdp"] == "1.5"


def test_unit_multipliers_give_zero():
    r = exposure_l1(D("172426221223"), D("1438103655"), D("27720709000000"), 1, 1)
    assert all(v == 0 for v in r.outputs().values())


def test_negative_net_cost_is_net_receipt():
    r = exposure_l1(D(10), D(100), D(1000), 1.25, 1.25)
    assert r.net_external_cost_change_usd == D("-22.5")
    assert "net receipt" in explain()["sign_convention"]


@pytest.mark.parametrize("p_m,p_x", [(1.25, 1.25), (0.5, 2.0), (1.1, 0.9), (2.999, 0.001), (1, 1)])
def test_zero_elasticity_l2_equals_l1_exactly(p_m, p_x):
    M, X, G = D("172426221223"), D("1438103655"), D("27720709000000.5")
    l1 = exposure_l1(M, X, G, p_m, p_x)
    for e in (0, 0.0, D("0"), D("0.000")):
        l2 = exposure_l2(M, X, G, p_m, p_x, e, e)
        for k in ce.OUTPUT_KEYS:
            a, b = getattr(l1, k), getattr(l2, k)
            assert a == b and str(a) == str(b), k  # identical Decimal, not merely close


def test_l2_matches_definition_and_is_correctly_rounded():
    M, X, G = D(100), D(40), D(1000)
    r = exposure_l2(M, X, G, 1.25, 1.25, -0.5, 0.5)
    with localcontext() as c:
        c.prec = 60
        m_prime = M * (D("0.5") * D("1.25").ln()).exp()
        x_prime = X * (D("1.5") * D("1.25").ln()).exp()
    assert abs(r.scenario_import_value_usd - m_prime) < D("1e-45")
    assert abs(r.scenario_export_value_usd - x_prime) < D("1e-45")
    with localcontext() as c:
        c.prec = 80
        assert abs(r.import_value_change_usd - (r.scenario_import_value_usd - M)) < D("1e-45")
    # integral exponents are exact: eps = 1 -> p^2
    r2 = exposure_l2(M, X, G, 1.25, 1.25, 1, 1)
    assert r2.import_value_change_usd == D("56.25")
    assert r2.export_value_change_usd == D("22.5")
    # eps = -1 -> p^0 = 1: no value change at all
    r3 = exposure_l2(M, X, G, 1.25, 1.25, -1, -1)
    assert r3.import_value_change_usd == 0 and r3.export_value_change_usd == 0


def test_l2_elasticity_direction():
    M, X, G = D(100), D(40), D(1000)
    base = exposure_l2(M, X, G, 1.25, 1.25, 0, 0)
    less_demand = exposure_l2(M, X, G, 1.25, 1.25, -0.5, 0)
    more_supply = exposure_l2(M, X, G, 1.25, 1.25, 0, 0.5)
    # price up + negative import elasticity -> smaller import value increase
    assert less_demand.import_value_change_usd < base.import_value_change_usd
    assert less_demand.import_value_change_usd > 0  # |eps| < 1: value still rises
    # positive export elasticity -> larger export gain, lower net cost
    assert more_supply.export_value_change_usd > base.export_value_change_usd
    assert more_supply.net_external_cost_change_usd < base.net_external_cost_change_usd
    # price down: negative elasticity cushions the import value fall
    down = exposure_l2(M, X, G, 0.8, 0.8, -0.5, 0)
    down0 = exposure_l1(M, X, G, 0.8, 0.8)
    assert down0.import_value_change_usd < down.import_value_change_usd < 0


def test_sensitivity_range_not_probabilistic():
    M, X, G = D(100), D(40), D(1000)
    grid = [exposure_l2(M, X, G, 1.25, 1.25, e, 0) for e in (-0.5, 0, 0.5)]
    lo, hi = ce.sensitivity_range(grid, "exposure_percent_baseline_gdp")
    assert lo < hi
    from reva.kernel.uncertainty import UncertaintySpec

    u = UncertaintySpec.sensitivity(str(lo), str(hi), "m@1", "grid")
    assert u.level is None and not u.is_probabilistic


@pytest.mark.parametrize("p", [0, -1, D("-0.01")])
def test_non_positive_multiplier_rejected(p):
    with pytest.raises(ValidationProblem):
        exposure_l1(D(1), D(1), D(1), p, 1)
    with pytest.raises(ValidationProblem):
        exposure_l2(D(1), D(1), D(1), 1, p, 0, 0)


def test_envelopes_and_horizon():
    with pytest.raises(CapabilityGap):
        exposure_l1(D(1), D(1), D(1), 3.5, 1)
    with pytest.raises(CapabilityGap):
        exposure_l2(D(1), D(1), D(1), 1.2, 1.2, -2.5, 0)
    with pytest.raises(CapabilityGap):
        ce.check_horizon(5, "annual_accounting_period")
    with pytest.raises(ValidationProblem):
        exposure_l1(D(1), D(1), D(0), 1.2, 1.2)  # GDP must be positive
    with pytest.raises(ValidationProblem):
        exposure_l1(D(-1), D(1), D(1), 1.2, 1.2)


def _inputs(country="FRA", m=D(100), x=D(40), g=D(1000), years=(2023, 2023, 2023), **over):
    def mk(name, v, y, valuation=None):
        kw = {"valuation": valuation}
        kw.update(over.get(name, {}))
        return MonetaryInput(name, v, y, **kw)

    return CountryInputs(country, mk("M", m, years[0], "CIF"), mk("X", x, years[1], "FOB"), mk("G", g, years[2]))


def test_typed_inputs_validate_and_evaluate():
    ci = _inputs()
    assert validate_country_inputs(ci, reference_year=2023) == (D(100), D(40), D(1000))
    [r] = evaluate_country(ci, reference_year=2023, import_multiplier=1.25, export_multiplier=1.25)
    assert r.exposure_percent_baseline_gdp == D("1.5")


@pytest.mark.parametrize("which", ["M", "X", "G"])
def test_missing_input_raises_never_zero(which):
    vals = {"m": D(100), "x": D(40), "g": D(1000)}
    vals[which.lower()] = Missing(reason=MissingReason.unavailable)
    ci = _inputs("DZA", **vals)
    with pytest.raises(MissingInput) as ei:
        validate_country_inputs(ci, reference_year=2023)
    assert "DZA 2023" in ei.value.message
    assert ei.value.details["missing"][0]["input"] == which
    with pytest.raises(MissingInput):
        evaluate_country(ci, reference_year=2023, import_multiplier=1.25, export_multiplier=1.25)
    ci_none = _inputs("DZA", **{**vals, which.lower(): None})
    with pytest.raises(MissingInput):
        validate_country_inputs(ci_none, reference_year=2023)


def test_mixed_reference_years_rejected():
    with pytest.raises(IncompatibleSemantics):
        validate_country_inputs(_inputs(years=(2023, 2023, 2022)), reference_year=2023)
    with pytest.raises(IncompatibleSemantics):
        validate_country_inputs(_inputs(years=(2022, 2022, 2022)), reference_year=2023)


@pytest.mark.parametrize(
    "over",
    [
        {"G": {"price_basis": "real"}},
        {"G": {"price_basis": "nominal", "price_reference_year": 2015}},
        {"M": {"currency": "EUR"}},
        {"X": {"unit_ref": "unit.kg"}},
        {"M": {"aggregation": "point_in_time_stock"}},
        {"G": {"price_basis": "ppp"}},
    ],
)
def test_mixed_basis_rejected(over):
    with pytest.raises(IncompatibleSemantics):
        validate_country_inputs(_inputs(**over), reference_year=2023)


def test_from_value_dict_reads_kernel_values():
    v = {"kind": "decimal", "value": "172426221223", "unit_ref": "unit.usd",
         "basis": {"currency": "USD", "price_basis": "nominal", "scale_factor": "1", "valuation": "CIF"}}
    m = MonetaryInput.from_value_dict("M", v, reference_year=2023, aggregation="period_flow_total",
                                      source_flags={"provider_derived_aggregate": True, "isReported": False})
    assert m.value == D("172426221223") and m.valuation == "CIF"
    miss = MonetaryInput.from_value_dict("X", {"kind": "missing", "reason": "unavailable"}, reference_year=2023,
                                         aggregation="period_flow_total")
    assert miss.is_missing
    ex = explain(CountryInputs("USA", m, m, m))
    assert ex["source_flags"]["M"]["provider_derived_aggregate"] is True
    assert "not a GDP loss" in ex["not_claimed"]
    assert "sensitivity range, not a confidence" in ex["uncertainty"]
    assert "CIF" in " ".join(ex["assumptions"])


_money = st.integers(min_value=0, max_value=10**13)
_mult = st.decimals(min_value=D("0.001"), max_value=D(3), places=3)


@settings(max_examples=200, deadline=None)
@given(m1=_money, m2=_money, x1=_money, x2=_money, p_m=_mult, p_x=_mult, a=st.integers(-5, 5))
def test_linearity_in_m_and_x_at_fixed_p(m1, m2, x1, x2, p_m, p_x, a):
    G = D("27720709000000")
    r1 = exposure_l1(D(m1), D(x1), G, p_m, p_x)
    r2 = exposure_l1(D(m2), D(x2), G, p_m, p_x)
    r12 = exposure_l1(D(m1 + m2), D(x1 + x2), G, p_m, p_x)
    with localcontext() as c:
        c.prec = 100  # the test's own arithmetic must not round
        for k in ("import_value_change_usd", "export_value_change_usd", "net_external_cost_change_usd"):
            assert getattr(r12, k) == getattr(r1, k) + getattr(r2, k)  # exact
        # exposure involves one division rounded at 50 significant digits
        tol = D("1e-48") * (abs(r1.exposure_percent_baseline_gdp) + abs(r2.exposure_percent_baseline_gdp))
        assert abs(r12.exposure_percent_baseline_gdp
                   - (r1.exposure_percent_baseline_gdp + r2.exposure_percent_baseline_gdp)) <= tol
        # homogeneity: scaling M and X by a scales every value change exactly
        if a >= 0:
            ra = exposure_l1(D(a * m1), D(a * x1), G, p_m, p_x)
            assert ra.net_external_cost_change_usd == a * r1.net_external_cost_change_usd
