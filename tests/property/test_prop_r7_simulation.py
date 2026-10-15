"""R7 property checks for the crude-exposure model and the pure simulation layer.

Covers ``reva.models.economic.crude_exposure`` and the DB-free parts of
``reva.simulation`` (``engine.compute`` / ``output_digest`` over a hand-built
``CompiledScenario``, ``compile.build_coupling`` / ``compile_intervention``,
``rng``). No database: ``CompiledScenario`` is assembled directly.

Tests marked ``xfail(strict=True)`` encode a property the current code violates
(see docs/dev/property_check.md).
"""

from __future__ import annotations

import random
from decimal import Context, Decimal, localcontext
from typing import Any
from uuid import uuid5

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from reva.kernel.errors import MissingInput, RevaError
from reva.kernel.ids import REVA_NAMESPACE
from reva.kernel.values import _DECIMAL_RE, Missing, MissingReason, ScalarDecimal, parse_value
from reva.models.economic import crude_exposure as ce
from reva.simulation import engine, rng
from reva.simulation.compile import CompiledScenario, CountryPlan, build_coupling, compile_intervention
from reva.simulation.spec import ScenarioSpec

WIDE = Context(prec=400)  # test-side arithmetic must not round at the 28-digit thread default
SET = settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
FEW = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
NUMERIC_FIELDS = (
    "import_multiplier", "export_multiplier", "import_elasticity", "export_elasticity",
    "baseline_import_value_usd", "baseline_export_value_usd", "baseline_gdp_usd",
    "scenario_import_value_usd", "scenario_export_value_usd", *ce.OUTPUT_KEYS,
)
COUNTRIES = ("FRA", "DZA", "CHN", "USA")
YEAR = 2023


# --------------------------------------------------------------------------- strategies
def _dec(max_int_digits: int, max_places: int, min_value: int = 0) -> st.SearchStrategy[Decimal]:
    return st.builds(
        lambda i, places, frac: Decimal(i) + Decimal(frac).scaleb(-places) if places else Decimal(i),
        st.integers(min_value, 10**max_int_digits), st.integers(0, max_places), st.integers(0, 10**max_places - 1),
    ).filter(lambda d: d >= min_value)


money = _dec(15, 4)
gdp = _dec(15, 4).filter(lambda d: d > 0)
multiplier = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal(3), places=6, allow_nan=False,
                         allow_infinity=False)
float_multiplier = st.floats(min_value=0.01, max_value=3.0, allow_nan=False).map(lambda f: round(f, 6))
e_import = st.sampled_from([-2.0, -1.5, -1.0, -0.5, -0.25, -0.1, 0.0, 0.1, 0.5, 1.0])
e_export = st.sampled_from([-1.0, -0.5, -0.1, 0.0, 0.1, 0.3, 1.0, 1.5, 2.0])


# --------------------------------------------------------------------------- crude_exposure core
@SET
@given(money, money, gdp, multiplier, multiplier, st.sampled_from([0, Decimal(0), Decimal("0.0"), Decimal("-0"),
                                                                   0.0, "0", "0.000"]))
def test_zero_elasticity_l2_is_bit_identical_to_l1(M, X, G, pm, px, zero) -> None:
    l1 = ce.exposure_l1(M, X, G, pm, px)
    l2 = ce.exposure_l2(M, X, G, pm, px, zero, zero)
    for f in NUMERIC_FIELDS:
        if f.endswith("elasticity"):
            assert getattr(l2, f) == 0
            continue
        a, b = getattr(l1, f), getattr(l2, f)
        assert a.as_tuple() == b.as_tuple(), f  # bit-identical representation, not merely equal
    assert {k: v for k, v in l1.as_json().items() if "elasticity" not in k and k != "variant"} == {
        k: v for k, v in l2.as_json().items() if "elasticity" not in k and k != "variant"}


@SET
@given(money, money, gdp, st.decimals(min_value=Decimal("0.5001"), max_value=Decimal(2), places=6),
       st.decimals(min_value=Decimal("0.5001"), max_value=Decimal(2), places=6))
def test_l1_is_linear_in_the_price_shock(M, X, G, pm, px) -> None:
    """Doubling (multiplier − 1) doubles ΔM, ΔX and Δnet exactly (and the % up to 1 ulp at 50 digits)."""
    base = ce.exposure_l1(M, X, G, pm, px)
    doubled = ce.exposure_l1(M, X, G, 1 + 2 * (pm - 1), 1 + 2 * (px - 1))
    with localcontext(WIDE):
        for k in ("import_value_change_usd", "export_value_change_usd", "net_external_cost_change_usd"):
            assert getattr(doubled, k) == 2 * getattr(base, k), k
        a, b = doubled.exposure_percent_baseline_gdp, 2 * base.exposure_percent_baseline_gdp
        assert abs(a - b) <= abs(b) * Decimal("1e-48") + Decimal("1e-60")  # one 50-digit division ulp


@SET
@given(money, money, gdp, multiplier, multiplier, e_import, e_export)
def test_sign_convention_net_is_import_change_minus_export_change(M, X, G, pm, px, em, ex) -> None:
    for r in (ce.exposure_l1(M, X, G, pm, px), ce.exposure_l2(M, X, G, pm, px, em, ex)):
        # Δnet is ΔM − ΔX rounded once in the model's 50-digit context; never X − M or a sum.
        assert r.net_external_cost_change_usd == ce._CTX.subtract(r.import_value_change_usd,
                                                                   r.export_value_change_usd)
        assert r.scenario_import_value_usd == ce._CTX.add(r.baseline_import_value_usd, r.import_value_change_usd)
        assert r.scenario_export_value_usd == ce._CTX.add(r.baseline_export_value_usd, r.export_value_change_usd)
    l1 = ce.exposure_l1(M, X, G, pm, px)
    # higher import price, unchanged export price: net cost has the sign of (pm − 1) when M > 0
    only_m = ce.exposure_l1(M, X, G, pm, 1)
    assert (only_m.net_external_cost_change_usd > 0) == (M > 0 and pm > 1)
    # higher export price, unchanged import price: net receipt (negative net cost)
    only_x = ce.exposure_l1(M, X, G, 1, px)
    assert (only_x.net_external_cost_change_usd < 0) == (X > 0 and px > 1)
    assert (l1.exposure_percent_baseline_gdp > 0) == (l1.net_external_cost_change_usd > 0)


@SET
@given(money, money, gdp, multiplier, multiplier, e_import, e_export)
def test_outputs_are_decimals_never_floats(M, X, G, pm, px, em, ex) -> None:
    for args in ((M, X, G, pm, px), (float(M), float(X), float(G), float(pm), float(px))):
        for r in (ce.exposure_l1(*args), ce.exposure_l2(*args, em, ex)):
            for f in NUMERIC_FIELDS:
                assert type(getattr(r, f)) is Decimal, f
            for k, v in r.as_json().items():
                assert isinstance(v, str)
                if k != "variant":
                    assert _DECIMAL_RE.fullmatch(v), (k, v)


@SET
@given(st.integers(10**25, 10**30), st.integers(1, 10**15), st.integers(1, 15), gdp)
def test_full_decimal_precision_is_kept(M_int, frac, places, G) -> None:
    """30-digit values × 16-digit multipliers keep every digit (a float would keep 17)."""
    M = Decimal(M_int)
    pm = 1 + Decimal(frac).scaleb(-places - 15)  # e.g. 1.000000000000000xxxxx
    assume(pm <= 3)
    r = ce.exposure_l1(M, Decimal(0), G, pm, Decimal(1))
    exact = Context(prec=400).multiply(M, Context(prec=400).subtract(pm, Decimal(1)))
    assert r.import_value_change_usd == exact
    # and the published string carries all of it
    assert Decimal(ce.dstr(r.import_value_change_usd)) == exact


@SET
@given(money, money, gdp, multiplier, multiplier, e_import, e_export, st.sampled_from([3, 9, 28, 400]))
def test_results_do_not_depend_on_the_callers_decimal_context(M, X, G, pm, px, em, ex, prec) -> None:
    ref = (ce.exposure_l1(M, X, G, pm, px), ce.exposure_l2(M, X, G, pm, px, em, ex))
    with localcontext(Context(prec=prec)):
        got = (ce.exposure_l1(M, X, G, pm, px), ce.exposure_l2(M, X, G, pm, px, em, ex))
        assert [r.as_json() for r in got] == [r.as_json() for r in ref]


# --------------------------------------------------------------------------- missing inputs
def _input(name: str, value: Any) -> ce.MonetaryInput:
    return ce.MonetaryInput(name, value, YEAR)


_maybe_missing = st.one_of(
    money.map(lambda d: ("value", d)),
    st.just(("none", None)),
    st.sampled_from(list(MissingReason)).map(lambda r: ("missing", Missing(reason=r))),
)


@SET
@given(_maybe_missing, _maybe_missing, _maybe_missing.filter(lambda t: t[0] != "value" or t[1] > 0))
def test_missing_input_yields_missing_input_never_a_number(m, x, g) -> None:
    ci = ce.CountryInputs("FRA", _input("M", m[1]), _input("X", x[1]), _input("G", g[1]))
    missing_names = [n for n, t in zip("MXG", (m, x, g), strict=True) if t[0] != "value"]
    if not missing_names:
        out = ce.evaluate_country(ci, reference_year=YEAR, import_multiplier=1.1, export_multiplier=1.1)
        assert len(out) == 1
        return
    for grid in (None, ((0.0, 0.0), (-0.5, 0.5))):
        with pytest.raises(MissingInput) as ei:
            ce.evaluate_country(ci, reference_year=YEAR, import_multiplier=1.1, export_multiplier=1.1,
                                elasticity_grid=grid)
        assert ei.value.code == "missing_input"
        assert [d["input"] for d in ei.value.details["missing"]] == missing_names
        for d, t in zip(ei.value.details["missing"], [t for t in (m, x, g) if t[0] != "value"], strict=True):
            assert d["reason"] == (t[1].reason.value if t[0] == "missing" else "unavailable")


@FEW
@given(st.sampled_from(list(MissingReason)))
def test_missing_value_dict_becomes_missing_input(reason: MissingReason) -> None:
    inp = ce.MonetaryInput.from_value_dict("M", {"kind": "missing", "reason": reason.value}, reference_year=YEAR,
                                           aggregation="period_flow_total")
    assert inp.is_missing and isinstance(inp.value, Missing)
    ci = ce.CountryInputs("DZA", inp, _input("X", Decimal(1)), _input("G", Decimal(10)))
    with pytest.raises(MissingInput):
        ce.validate_country_inputs(ci, reference_year=YEAR)


@FEW
@given(st.sampled_from(list(MissingReason)), st.sampled_from(["M", "X", "G"]))
@example(MissingReason.not_collected, "M")
def test_direct_model_call_with_missing_raises_missing_input(reason: MissingReason, which: str) -> None:
    args = {"M": Decimal(10), "X": Decimal(5), "G": Decimal(100)}
    args[which] = Missing(reason=reason)
    with pytest.raises(MissingInput):
        ce.exposure_l1(args["M"], args["X"], args["G"], 1.1, 1.1)


@FEW
@given(st.sampled_from(list(MissingReason)), st.sampled_from(["M", "X", "G"]))
def test_direct_model_call_with_missing_never_returns_a_number(reason: MissingReason, which: str) -> None:
    args = {"M": Decimal(10), "X": Decimal(5), "G": Decimal(100)}
    for bad in (Missing(reason=reason), None):
        args[which] = bad
        with pytest.raises(RevaError):
            ce.exposure_l1(args["M"], args["X"], args["G"], 1.1, 1.1)
        with pytest.raises(RevaError):
            ce.exposure_l2(args["M"], args["X"], args["G"], 1.1, 1.1, 0, 0)


# --------------------------------------------------------------------------- engine.compute (no DB)
def _spec(countries: tuple[str, ...], fidelity: str, pm: float, px: float,
          grid: list[tuple[float, float]]) -> ScenarioSpec:
    return ScenarioSpec.model_validate({
        "schema_version": "1.0.0", "title": "r7 property", "knowledge_cutoff": "2026-10-14T21:00:00Z",
        "baseline_release_id": "00000000-0000-4000-8000-0000000000aa",
        "model_key": "economic.crude_exposure", "model_version": "1.0.0", "countries": list(countries),
        "reference_year": YEAR, "horizon": {"periods": 1, "unit": "annual_accounting_period"},
        "intervention": {"kind": "set_exogenous_border_price_multiplier", "import_multiplier": pm,
                         "export_multiplier": px, "unit": "ratio"},
        "assumptions": {"quantity_response": "fixed" if fidelity == "L1" else "elasticity_sensitivity",
                        "elasticity_grid": [{"import_elasticity": a, "export_elasticity": b,
                                             "basis": "scenario_assumption"} for a, b in grid],
                        "no_general_equilibrium": True},
        "fidelity": fidelity, "missing_input_policy": "fail", "outputs": list(ce.OUTPUT_KEYS), "seed": 7,
    })


def _plan(iso3: str, vals: tuple[Any, Any, Any]) -> CountryPlan:
    ci = ce.CountryInputs(iso3, *(_input(n, v) for n, v in zip("MXG", vals, strict=True)))
    plan = CountryPlan(iso3, uuid5(REVA_NAMESPACE, iso3), "deterministic_entity_id", ci)
    try:
        plan.values = ce.validate_country_inputs(ci, reference_year=YEAR)
    except MissingInput as e:
        plan.status = "missing_input"
        plan.problems = [{"code": "missing_input", **m} for m in e.details["missing"]]
    return plan


def _compiled(spec: ScenarioSpec, plans: list[CountryPlan]) -> CompiledScenario:
    change = compile_intervention(spec.intervention.model_dump(mode="json"), spec.assumptions.quantity_response)
    return CompiledScenario(spec=spec, release={"id": "00000000-0000-4000-8000-0000000000aa"}, change=change,
                            countries=plans, schedule=build_coupling(plans))


@st.composite
def scenarios(draw: Any, fidelity: str | None = None) -> tuple[ScenarioSpec, list[CountryPlan]]:
    fid = fidelity or draw(st.sampled_from(["L1", "L2"]))
    isos = draw(st.lists(st.sampled_from(COUNTRIES), min_size=1, max_size=4, unique=True))
    grid = [] if fid == "L1" else draw(st.lists(st.tuples(e_import, e_export), min_size=1, max_size=4))
    spec = _spec(tuple(isos), fid, draw(float_multiplier), draw(float_multiplier), grid)
    plans = []
    for iso in isos:
        vals = (draw(money), draw(money), draw(gdp))
        if draw(st.integers(0, 4)) == 0:  # sometimes a typed Missing input
            vals = (Missing(reason=MissingReason.unavailable), vals[1], vals[2])
        plans.append(_plan(iso, vals))
    return spec, plans


def _walk_no_float(obj: Any, path: str = "") -> None:
    assert not isinstance(obj, float), f"float at {path}: {obj!r}"
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_no_float(v, f"{path}/{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk_no_float(v, f"{path}/{i}")


@FEW
@given(scenarios(), st.randoms(use_true_random=False))
def test_results_are_invariant_to_country_order(sc: Any, rnd: random.Random) -> None:
    spec, plans = sc
    fid = spec.fidelity
    shuffled = plans[:]
    rnd.shuffle(shuffled)
    spec2 = spec.model_copy(update={"countries": tuple(p.iso3 for p in shuffled)})
    a = _compiled(spec, plans)
    b = _compiled(spec2, shuffled)
    ra, rb = engine.compute(a, fid), engine.compute(b, fid)
    assert [r.digest_view() for r in ra] == [r.digest_view() for r in rb]
    assert engine.output_digest(ra) == engine.output_digest(rb)
    assert a.schedule == b.schedule
    assert a.baseline_digest() == b.baseline_digest()
    assert rng.seed_plan(7, "m", [p.iso3 for p in plans], 1) == rng.seed_plan(7, "m", [p.iso3 for p in shuffled], 1)


@FEW
@given(scenarios())
def test_missing_country_produces_no_numbers(sc: Any) -> None:
    spec, plans = sc
    rows = engine.compute(_compiled(spec, plans), spec.fidelity)
    missing = {p.iso3 for p in plans if p.status == "missing_input"}
    assert not {r.iso3 for r in rows} & missing  # never a zero or partial value
    for p in plans:
        if p.status == "missing_input":
            assert p.values is None and p.problems and all(q["code"] == "missing_input" for q in p.problems)
    ready = {p.iso3 for p in plans if p.status == "ready"}
    assert {r.iso3 for r in rows} == ready


@FEW
@given(scenarios(fidelity="L2"))
def test_l2_sensitivity_range_contains_l1(sc: Any) -> None:
    spec, plans = sc
    rows = engine.compute(_compiled(spec, plans), "L2")
    l1 = {(r.output_key, r.iso3): Decimal(r.value["value"]) for r in rows if r.variant == "L1"}
    rng_rows = {(r.output_key, r.iso3): r for r in rows if r.variant == engine.SENSITIVITY_VARIANT}
    assert l1.keys() == rng_rows.keys()
    for key, v in l1.items():
        r = rng_rows[key]
        lo, hi = Decimal(r.value["lower"]), Decimal(r.value["upper"])
        assert lo <= v <= hi, (key, lo, v, hi)
        grid_vals = [Decimal(x.value["value"]) for x in rows
                     if (x.output_key, x.iso3) == key and x.variant.startswith("L2:eM=")]
        assert all(lo <= g <= hi for g in grid_vals)
        assert r.uncertainty["kind"] == "sensitivity_range" and r.uncertainty["level"] is None
        assert (r.uncertainty["lower"], r.uncertainty["upper"]) == (r.value["lower"], r.value["upper"])
    for r in rows:
        if r.variant == "L1":
            assert r.uncertainty["status"] == "unquantified"  # the range never upgrades the L1 point
    # no duplicate result identities
    ids = [(r.output_key, r.iso3, r.variant) for r in rows]
    assert len(ids) == len(set(ids))


@FEW
@given(scenarios())
def test_result_values_are_decimal_strings_without_floats(sc: Any) -> None:
    spec, plans = sc
    compiled = _compiled(spec, plans)
    rows = engine.compute(compiled, spec.fidelity)
    for r in rows:
        _walk_no_float(r.value, r.variant)
        _walk_no_float(r.uncertainty, r.variant)
        if r.value["kind"] == "decimal":
            v = parse_value(r.value)
            assert isinstance(v, ScalarDecimal)
            assert type(v.as_decimal()) is Decimal
        else:
            assert _DECIMAL_RE.fullmatch(r.value["lower"]) and _DECIMAL_RE.fullmatch(r.value["upper"])
    # L1 row strings reproduce the model's Decimal exactly (no float or 28-digit rounding on the way)
    for p in plans:
        if p.status != "ready":
            continue
        m, x, g = p.values
        l1 = ce.exposure_l1(m, x, g, compiled.change.import_multiplier, compiled.change.export_multiplier)
        for r in rows:
            if r.iso3 == p.iso3 and r.variant == "L1":
                assert Decimal(r.value["value"]) == getattr(l1, r.output_key)
    _walk_no_float(compiled.change.as_json())
    assert type(compiled.change.import_multiplier) is Decimal


# --------------------------------------------------------------------------- rng
@FEW
@given(st.text(max_size=4), st.text(max_size=4), st.text(max_size=4), st.integers(0, 2**32))
@example("a", "b", "c", 0)
def test_rng_stream_names_are_injective(a: str, b: str, c: str, seed: int) -> None:
    assert rng.stream_key(seed, f"{a}\x1f{b}", c, 0) != rng.stream_key(seed, a, f"{b}\x1f{c}", 0)


@pytest.mark.parametrize(("args", "key"), [
    ((7, "economic.crude_exposure", "FRA", 0), "6c829630437ee667e71268b31101765287f6c6b438aa56337beae86b8273f3c0"),
    ((0, "", " ", 3), "5f9f8dd00d5fe4861ccaffe6a3716e4ac9487627ca9a9243b623613f8e129989"),
    ((123, "économie", "日本", 1), "6cad2dd89121af279fe88c92b427571b7d72c0e77d954ce3cbbbec51ca9863b4"),
])
def test_rng_ordinary_names_keep_their_pre_escaping_stream_keys(args: tuple[Any, ...], key: str) -> None:
    # Pinned before the R7-S2 escaping fix: names without '\\' or U+001F are unaffected by it.
    assert rng.stream_key(*args) == key


@FEW
@given(st.integers(0, 2**64 - 1), st.text(max_size=6), st.text(max_size=6), st.integers(0, 50))
def test_rng_streams_are_deterministic_by_name(seed: int, model: str, entity: str, i: int) -> None:
    a = rng.stream(seed, model, entity, i).integers(0, 2**63, size=4).tolist()
    _ = rng.stream(seed, model, entity + "x", i).integers(0, 2**63, size=7)  # creating other streams is irrelevant
    b = rng.stream(seed, model, entity, i).integers(0, 2**63, size=4).tolist()
    assert a == b
    assert rng.stream_key(seed, model, entity, i) == rng.stream_key(seed, model, entity, i)
