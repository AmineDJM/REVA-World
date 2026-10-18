"""The decision start is binding for the retirement-age plugin (V6 FIXPOP).

Regression: ``france_retirement_65_2027`` (start 2027Q1) ran the retirement model with
``first_birth_year = 2027 - 65 = 1962``. Cohorts 1962-1964 reach their reference legal age (62y6m-62y9m,
law in force 2026) in 2024-2026, so the scenario reported +191,209 employed in 2025 and -8.3 bn EUR of
pension spending in 2025, before the decision starts. The engine now derives the first affected cohort
from the start quarter and the reference legal-age path, refuses an explicit cohort that would act before
the start, refuses any pre-start non-zero output, and reports only the calendar years of the horizon.
"""

from __future__ import annotations

from typing import Any

from reva.predict import propagators as pp
from reva.predict.context import NoBaseline, NoClaims, PluginData, StaticEffectSource
from reva.predict.decision import Decision, reference_decisions
from reva.predict.engine import LABEL_UNVALIDATED, predict
from reva.simulation import plugins

from .fixture_inputs import resolved_inputs

OUT = "plugin_output:social.retirement_age.{}@FRA"


def fra65(**opts: Any) -> Decision:
    d = reference_decisions()["france_retirement_65_2027"]
    if not opts:
        return d
    po = {**d.plugin_options, pp.RETIREMENT_KEY: {**d.plugin_options[pp.RETIREMENT_KEY], **opts}}
    return d.model_copy(update={"plugin_options": po})


def fixture_runner(seen: list[dict[str, Any]]) -> Any:
    """The real social.retirement_age plugin on captured Eurostat fixtures (same steps as predict.db)."""

    def run(spec: dict[str, Any]) -> dict[str, Any]:
        seen.append(spec)
        model = plugins.get(spec["model_key"], spec.get("model_version"))
        model.validate(spec)
        compiled = model.compile(resolved_inputs(model.requirements(spec)), spec)
        if getattr(compiled, "missing", None):
            return {"status": "missing_input", "problems": list(compiled.missing)}
        rows = model.compute(compiled, "L1")
        return {
            "status": "succeeded",
            "rows": [
                {"output_key": r.output_key, "variant": r.variant, "value": r.value, "year": r.period[0].year}
                for r in rows
            ],
            "provenance": {"release_id": "fixture-release"},
        }

    return run


def var(r: dict[str, Any], vid: str) -> dict[str, Any]:
    return next(v for v in r["variables"] if v["id"] == vid)


def test_first_cohort_is_derived_from_start_and_reference_path() -> None:
    fc = pp.retirement_first_cohort("FRA", "law_in_force_2026", 780, 2027, 1)
    assert fc.first_birth_year == 1965 and fc.problem is None
    assert fc.derivation["first_cohort"]["first_eligible_month"] >= "2027-01"
    assert fc.derivation["previous_cohort_excluded"]["birth_year"] == 1964  # reaches 62y9m from 2026-10
    # lowering the level to 60 binds through the decided level: cohort 1966 would be eligible in 2026
    assert pp.retirement_first_cohort("FRA", "law_in_force_2026", 720, 2027, 1).first_birth_year == 1967
    bad = pp.retirement_first_cohort("ZZZ", "law_in_force_2026", 780, 2027, 1)
    assert bad.first_birth_year is None and bad.problem


def test_reference_decision_real_model_has_no_effect_before_start_and_reports_horizon_years() -> None:
    seen: list[dict[str, Any]] = []
    run = fixture_runner(seen)
    r = predict(fra65(), StaticEffectSource([]), NoClaims(), NoBaseline(), PluginData(retirement_runner=run), draws=200)
    spec = seen[0]
    assert spec["intervention"]["first_birth_year"] == 1965
    assert spec["horizon"] == {"periods": 4, "unit": "calendar_year"}  # 2025..2028 computed, 2027-2028 reported
    assert r["label"] == LABEL_UNVALIDATED
    assert not [g for g in r["gaps"] if g["code"] in ("effect_before_start", "decision_start_conflict")]
    emp = var(r, OUT.format("employment_change_55_69"))
    rec = var(r, OUT.format("pension_recipients_change"))
    eur = var(r, OUT.format("pension_expenditure_change_approx_eur"))
    for v in (emp, rec, eur):
        assert [p["period"] for p in v["periods"]] == ["2027", "2028"] and v["frequency"] == "year"
        assert any("annual calendar-year values 2027-2028" in n for n in v["notes"])
    assert emp["periods"][0]["p50"] > 0 and rec["periods"][0]["p50"] < 0 and eur["periods"][0]["p50"] < 0
    # the model itself: the pre-start years it computes are exactly zero under the derived cohort
    rows = run(spec)["rows"]
    pre = [
        r_ for r_ in rows if r_["year"] < 2027 and r_["variant"] == "L1" and r_["output_key"] in pp.RETIREMENT_OUTPUTS
    ]
    assert pre and all(float(r_["value"]["value"]) == 0 for r_ in pre)
    hop = next(h for h in r["hops"] if h["kind"] == "plugin")
    assert hop["provenance"]["first_birth_year_derivation"]["used"] == 1965
    assert hop["provenance"]["reported_years"]["years"] == [2027, 2028]


def test_former_cohort_1962_acts_before_start_and_is_refused() -> None:
    seen: list[dict[str, Any]] = []
    r = predict(
        fra65(first_birth_year=1962),
        StaticEffectSource([]),
        NoClaims(),
        NoBaseline(),
        PluginData(retirement_runner=fixture_runner(seen)),
        draws=200,
    )
    assert not seen and not r["variables"]  # the model is not run, nothing is reported (never zero)
    g = next(g for g in r["gaps"] if g["code"] == "decision_start_conflict")
    assert "1965" in g["message"]
    # a later stated cohort is allowed (it cannot act earlier)
    r2 = predict(
        fra65(first_birth_year=1966),
        StaticEffectSource([]),
        NoClaims(),
        NoBaseline(),
        PluginData(retirement_runner=fixture_runner(seen)),
        draws=200,
    )
    assert seen[-1]["intervention"]["first_birth_year"] == 1966 and r2["variables"]


def test_pre_start_nonzero_output_is_refused_and_out_of_horizon_years_dropped() -> None:
    def runner(spec: dict[str, Any]) -> dict[str, Any]:
        rows = [
            {"output_key": "employment_change_55_69", "variant": "L1", "value": {"value": v}, "year": y}
            for y, v in ((2025, "0"), (2026, "0"), (2027, "10"), (2028, "20"), (2029, "30"))
        ]
        rows += [
            {"output_key": "pension_recipients_change", "variant": "L1", "value": {"value": v}, "year": y}
            for y, v in ((2026, "-5"), (2027, "-7"))
        ]
        return {"status": "succeeded", "rows": rows, "provenance": {}}

    r = predict(
        fra65(), StaticEffectSource([]), NoClaims(), NoBaseline(), PluginData(retirement_runner=runner), draws=200
    )
    emp = var(r, OUT.format("employment_change_55_69"))
    assert [(p["period"], p["p50"]) for p in emp["periods"]] == [("2027", 10.0), ("2028", 20.0)]
    assert not [v for v in r["variables"] if v["id"] == OUT.format("pension_recipients_change")]
    g = next(g for g in r["gaps"] if g["code"] == "effect_before_start")
    assert g["output"] == "pension_recipients_change" and g["years"] == [2026]
