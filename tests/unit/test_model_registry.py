"""Model registry helpers, port compatibility, applicability and pure evaluation logic."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from reva.evaluation.tournament_service import (
    LABEL,
    evaluate,
    finite_json,
    holdout_spec,
    panel_from_rows,
)
from reva.kernel.errors import ImmutableRecordViolation, ValidationProblem
from reva.models.applicability import (
    CAPABILITY_LEVELS,
    ApplicabilityDescriptor,
    ApplicabilityRequest,
    capability_status,
    check_applicability,
    descriptor_from_registry,
)
from reva.models.ports import (
    ApplicabilityReport,
    CapabilityLevel,
    PortSpec,
    TransformOperator,
    WorldModel,
    compatible,
    port_from_registry,
)
from reva.models.registry import (
    ModelDescriptor,
    PromotionEvent,
    assert_immutable_update,
    bump,
    current_pointer,
    index_descriptors,
    parse_ref,
    rollback_target,
)
from reva.ontology.registry import load_registry

POLICY = {"model_promotion": load_registry().policies["promotion"]["model_promotion"]}


def port(name: str, direction: str, **kw) -> PortSpec:
    base = dict(concept="trade.hs2709.import_value.world_total", concept_version="1.0.0", unit="unit.usd",
                basis="nominal_current", spatial_support="provider_reporter_territory:country",
                population_universe="economy", temporal_resolution="annual",
                temporal_aggregation="period_flow_total", interpretation="flow")
    base.update(kw)
    return PortSpec(name=name, direction=direction, **base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- ports


def test_identical_ports_are_compatible():
    c = compatible(port("M", "out"), port("M", "in"))
    assert c.ok and c.reasons == () and c.operators == ()


def test_annual_flow_cannot_feed_monthly_port_without_approved_disaggregation():
    out, inp = port("M", "out"), port("M_monthly", "in", temporal_resolution="monthly")
    c = compatible(out, inp)
    assert not c.ok
    assert any("approved disaggregation operator" in r for r in c.reasons)

    unapproved = TransformOperator("temporal.denton", "1.0.0", "temporal_disaggregation", "annual", "monthly",
                                   approved=False, method="denton", interpretations=("flow",))
    assert not compatible(out, inp, [unapproved]).ok

    approved = replace(unapproved, approved=True)
    # Approved, but the method is not permitted by the producing port.
    c2 = compatible(out, inp, [approved])
    assert not c2.ok and any("permitted_interpolation" in r for r in c2.reasons)

    out_ok = replace(out, permitted_interpolation=("denton",))
    c3 = compatible(out_ok, inp, [approved])
    assert c3.ok and c3.operators == ("temporal.denton@1.0.0",)

    # An operator restricted to stocks does not apply to a flow.
    stock_only = replace(approved, interpretations=("stock",))
    assert not compatible(out_ok, inp, [stock_only]).ok


def test_monthly_to_annual_needs_aggregation_operator():
    out = port("m", "out", temporal_resolution="monthly")
    inp = port("a", "in")
    assert not compatible(out, inp).ok
    agg = TransformOperator("temporal.sum", "1.0.0", "temporal_aggregation", "monthly", "annual", approved=True,
                            interpretations=("flow",))
    assert compatible(out, inp, [agg]).ok


def test_semantic_mismatches_are_all_reported():
    out = port("G", "out", unit="unit.eur", basis="real_2015", interpretation="stock",
               temporal_aggregation="point_in_time_stock", conservation_role="conserved_quantity")
    inp = port("M", "in", concept="economics.gdp.nominal_current_usd")
    c = compatible(out, inp)
    joined = " | ".join(c.reasons)
    for attr in ("concept", "unit", "basis", "interpretation", "temporal_aggregation", "conservation_role"):
        assert attr in joined, attr
    assert not c.ok and not bool(c)


def test_direction_dimensions_and_state_ownership():
    assert not compatible(port("a", "in"), port("b", "in")).ok
    assert not compatible(port("a", "out"), port("b", "in", dimensions=("entity", "time", "sector"))).ok
    writer = port("s", "in", state_owner="model.a", state_access="write")
    assert not compatible(port("x", "out", state_owner="model.b"), writer).ok
    assert compatible(port("x", "out", state_owner="model.a"), writer).ok
    with pytest.raises(ValueError):
        port("bad", "in", state_access="write")
    w = compatible(port("x", "out", dependence_group="g1"), port("y", "in", dependence_group="g2"))
    assert w.ok and w.warnings


def test_registry_ports_and_protocol():
    entry = load_registry().models
    ce = next(m for m in entry if m["model_key"] == "economic.crude_exposure")
    ports = [port_from_registry(p, "in") for p in ce["input_ports"]]
    assert all(p is not None for p in ports)
    fc = next(m for m in entry if m["model_key"] == "forecast.log_drift")
    assert port_from_registry(fc["input_ports"][0], "in") is None  # under-specified: never auto-compatible
    assert hasattr(WorldModel, "specification") and hasattr(WorldModel, "checkpoint")


# --------------------------------------------------------------------------- applicability


def test_out_of_envelope_requests_are_refused_with_reasons():
    reg = {m["model_key"]: m for m in load_registry().models}
    desc = descriptor_from_registry(reg["forecast.log_drift"])
    train = [(2000 + i, 100.0 * 1.02**i) for i in range(10)]
    ok = check_applicability(desc, ApplicabilityRequest(train=train, horizons=[1, 2, 3]))
    assert ok.applicable and "horizon" in ok.checked

    far = check_applicability(desc, ApplicabilityRequest(train=train, horizons=[5]))
    assert not far.applicable and any("no silent extrapolation" in r for r in far.reasons)

    neg = check_applicability(desc, ApplicabilityRequest(train=[*train[:-1], (2009, -1.0)], horizons=[1]))
    assert not neg.applicable and any(r.startswith("positivity") for r in neg.reasons)

    short = check_applicability(desc, ApplicabilityRequest(train=train[:2], horizons=[1]))
    assert any("min_train" in r for r in short.reasons)

    gap = check_applicability(desc, ApplicabilityRequest(train=[train[0], train[2], train[3]], horizons=[1]))
    assert any("non-consecutive" in r for r in gap.reasons)

    monthly = check_applicability(desc, ApplicabilityRequest(train=train, temporal_resolution="monthly"))
    assert any(r.startswith("support") for r in monthly.reasons)

    past = check_applicability(desc, ApplicabilityRequest(train=train, target_years=[2005]))
    assert any("not after the last training year" in r for r in past.reasons)

    scen = descriptor_from_registry(reg["economic.crude_exposure"])
    r = check_applicability(scen, ApplicabilityRequest(target_years=[2030], intervention="price_shock"))
    assert not r.applicable
    assert any(x.startswith("time_window") for x in r.reasons)
    assert any(x.startswith("intervention") for x in r.reasons)

    geo = ApplicabilityDescriptor("x@1.0.0", geographies=frozenset({"FRA"}))
    assert not check_applicability(geo, ApplicabilityRequest(geography="DZA")).applicable


def test_capability_levels_l3_l4_reserved():
    assert CAPABILITY_LEVELS[CapabilityLevel.L3].implemented is False
    for lv in ("L3", "L4"):
        rep = capability_status(lv, ["L0", "L1", "L2", "L3", "L4"])
        assert isinstance(rep, ApplicabilityReport) and not rep.applicable
        assert "reserved" in rep.reasons[0]
    assert capability_status("L1", ["L1"]).applicable
    assert not capability_status("L2", ["L1"]).applicable
    assert not capability_status("L9", ["L1"]).applicable


# --------------------------------------------------------------------------- registry


def test_versions_are_immutable_and_promotion_moves_pointers():
    entries = load_registry().models
    idx = index_descriptors(entries)
    d = idx["forecast.log_drift@1.0.0"]
    assert parse_ref(d.ref) == ("forecast.log_drift", "1.0.0")
    assert bump("1.0.0", "minor") == "1.1.0"
    with pytest.raises(ValidationProblem):
        parse_ref("forecast.log_drift@latest")
    changed = ModelDescriptor.from_registry_entry({**d.spec, "description": "silently edited"})
    with pytest.raises(ImmutableRecordViolation):
        assert_immutable_update(d, changed)
    with pytest.raises(ImmutableRecordViolation):
        index_descriptors([*entries, {**d.spec, "assumptions": ["changed"]}])
    assert_immutable_update(d, ModelDescriptor.from_registry_entry(dict(d.spec)))  # identical: fine

    naive, drift, ll = "forecast.last_value@1.0.0", "forecast.log_drift@1.0.0", "forecast.local_level_log@1.0.0"
    ev = [
        PromotionEvent(drift, "keep_incumbent", "t", 1),
        PromotionEvent(drift, "promote", "t", 2),
        PromotionEvent(ll, "reject", "t", 3),
        PromotionEvent(ll, "promote", "other-task", 4),
    ]
    assert current_pointer(ev, "t", naive) == drift
    assert current_pointer(ev, "unknown", naive) == naive
    # The naive baseline lacks the prediction_interval port: not a compatible rollback target.
    assert rollback_target(ev, "t", idx, naive) is None
    ev2 = [*ev, PromotionEvent(ll, "promote", "t", 5)]
    assert current_pointer(ev2, "t", naive) == ll
    assert rollback_target(ev2, "t", idx, naive) == drift
    ev3 = [*ev2, PromotionEvent(drift, "rollback", "t", 6)]
    assert current_pointer(ev3, "t", naive) == drift
    ev4 = [*ev3, PromotionEvent(drift, "retire", "t", 7)]
    assert current_pointer(ev4, "t", naive) == naive
    # Rollback never picks a retired version or one with different output semantics.
    assert rollback_target(ev4, "t", idx, naive) == ll
    assert idx[drift].spec_digest == ModelDescriptor.from_registry_entry(dict(idx[drift].spec)).spec_digest


# --------------------------------------------------------------------------- pure evaluation


def synthetic_growth_panel(n_series: int = 5, years: tuple[int, int] = (2004, 2024), seed: int = 7):
    """Smooth exponential growth with small multiplicative noise (log drift should win)."""
    rng = np.random.default_rng(seed)
    out = {}
    for i in range(n_series):
        g = 0.03 + 0.01 * i
        level = 100.0 * (i + 1)
        obs = []
        for y in range(years[0], years[1] + 1):
            level *= math.exp(g + rng.normal(0, 0.002))
            obs.append((y, level))
        out[f"S{i}:test.concept"] = obs
    return out


def test_pure_evaluation_promotes_only_after_locked_holdout_and_blocks_reuse():
    panel = synthetic_growth_panel()
    kw = dict(incumbent_key="forecast.last_value", horizons=(1, 2, 3), seed=1, policy=POLICY,
              holdout_years=2, max_holdout_accesses=1)
    first = evaluate(panel, prior_holdout_accesses={}, **kw)
    assert first.holdout.years == (2023, 2024)
    assert max(r.target_year for r in first.tournament.records) <= 2022  # selection never saw the holdout
    assert first.tournament.decisions["forecast.log_drift"].decision == "promote"
    assert first.holdout_status == "scored"
    assert first.decisions["forecast.log_drift"]["decision"] == "promote"
    assert first.decisions["forecast.log_drift"]["reasons"][0] == LABEL
    assert any(r.startswith("locked_holdout_confirmed") for r in first.decisions["forecast.log_drift"]["reasons"])
    assert first.status == "promoted"
    assert first.access_after["forecast.log_drift@1.0.0"] == 1
    assert first.access_after["forecast.last_value@1.0.0"] == 1
    assert first.access_after["forecast.local_level_log@1.0.0"] == 0  # not a promotion candidate

    second = evaluate(panel, prior_holdout_accesses=first.access_after, **kw)
    assert second.holdout_status == "blocked" and second.holdout_scores == {}
    d = second.decisions["forecast.log_drift"]
    assert d["development_decision"] == "promote" and d["decision"] == "keep_incumbent"
    assert any(r.startswith("locked_holdout_reuse_blocked") for r in d["reasons"])
    assert second.access_after == second.access_before


def test_pure_evaluation_insufficient_evidence_keeps_incumbent():
    panel = {"S0:test.concept": [(2010 + i, 100.0 * 1.05**i) for i in range(8)]}
    out = evaluate(panel, incumbent_key="forecast.last_value", horizons=(1, 2, 3), seed=1, policy=POLICY,
                   holdout_years=2, max_holdout_accesses=1, prior_holdout_accesses={})
    assert out.status == "insufficient_evaluation"
    assert all(d["decision"] == "keep_incumbent" for d in out.decisions.values())
    assert out.holdout_status == "locked_unopened"
    assert "forecast.last_value" in out.tournament.metrics  # naive baseline always evaluated


def test_panel_rows_drop_missing_and_exclude_non_positive_series():
    def row(iso, year, v, kind="decimal"):
        return {"concept_id": "c", "spatial_spec": {"key": f"WB:{iso}"}, "aggregation": "period_flow_total",
                "temporal_extent": {"kind": "interval", "start": f"{year}-01-01", "end": f"{year + 1}-01-01"},
                "value": {"kind": kind, "value": v, "unit_ref": "unit.usd"}}

    rows = [row("AAA", 2000, "1"), row("AAA", 2001, None, kind="missing"), row("AAA", 2002, "3"),
            row("BBB", 2000, "5"), row("BBB", 2001, "0"), row("CCC", 2000, "2")]
    series, meta, dropped = panel_from_rows(rows, concepts=["c"], countries=["AAA", "BBB"], years=(2000, 2024))
    assert set(series) == {"AAA:c"} and meta["AAA:c"]["gaps"] == [2001]
    reasons = {d["reason"] for d in dropped}
    assert {"value_kind_missing", "non_positive_values"} <= reasons
    spec = holdout_spec(series, 2, 1)
    assert spec.years == (2001, 2002) and spec.cells == ("AAA:c:2002",)
    assert finite_json({"x": [math.inf, 1.0]}) == {"x": ["Infinity", 1.0]}
