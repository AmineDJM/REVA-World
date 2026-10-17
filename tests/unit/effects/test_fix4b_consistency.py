"""FIX4B (audit AUDIT3 M3): the effect loader refuses level-vs-growth inconsistencies (curated identifier links in
``registry/effects/variables.yaml``; never a name match). Synthetic rows only."""

from __future__ import annotations

import pytest
from test_effects_model import _est, _ok, _reason  # type: ignore[import-not-found]

from reva.effects import Uncertainty
from reva.effects.consistency import side_problem
from reva.effects.variables import load_variables


def _resp(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        cause_kind="event_type",
        cause_id="sanction_episode",
        cause_definition="synthetic onset",
        cause_transform="occurrence",
        measure="response",
        unit="percent",
        shock_size=1,
        shock_unit="occurrence",
        point=-0.4,
        as_printed="-0.4",
        quote="effect -0.4",
        uncertainty=Uncertainty(status="not_reported"),
    )
    base.update(kw)
    return _est(**base)


def test_growth_links_are_curated_ids() -> None:
    v = load_variables()
    assert v.items["output.real_gdp_per_capita"].growth_variable == "output.real_gdp_per_capita_growth"
    assert v.level_of_growth_concept("wb.ny.gdp.pcap.kd.zg") == ["output.real_gdp_per_capita"]
    assert v.level_of_growth_concept("wb.ny.gdp.mktp.kd.zg") == ["output.real_gdp_level"]
    assert "wb.ny.gdp.pcap.kd.zg" in v.growth_ids()["atlas_concept"]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        (
            dict(effect_kind="atlas_concept", effect_id="wb.ny.gdp.pcap.kd.zg", effect_transform="percent_change"),
            "effect_level_transform_on_growth_concept",
        ),
        (
            dict(effect_kind="atlas_concept", effect_id="wb.ny.gdp.mktp.kd.zg", effect_transform="growth_rate"),
            "effect_percent_response_on_growth_concept",
        ),
        (
            dict(
                effect_kind="variable",
                effect_id="output.real_gdp_growth",
                effect_transform="growth_rate",
                unit="percentage_points",
                effect_definition="cumulative sum of growth rates from t to t+h",
            ),
            "effect_cumulative_lhs_on_growth_concept",
        ),
        (
            dict(
                effect_kind="variable",
                effect_id="output.real_gdp_per_capita",
                effect_transform="growth_rate",
                unit="percentage_points",
            ),
            "effect_growth_rate_on_level_variable",
        ),
    ],
)
def test_inconsistent_level_growth_rows_are_rejected(kw, reason) -> None:  # type: ignore[no-untyped-def]
    assert _reason(_resp(**kw)) == reason


def test_consistent_rows_pass() -> None:
    _ok(_resp(effect_kind="variable", effect_id="output.real_gdp_per_capita", effect_transform="percent_change"))
    _ok(
        _resp(
            effect_kind="variable",
            effect_id="output.real_gdp_per_capita_growth",
            effect_transform="growth_rate",
            unit="percentage_points",
        )
    )


def test_side_problem_ignores_unlinked_variables() -> None:
    # agricultural value added has no curated growth variable: a growth_rate transform stays allowed
    assert side_problem(transform="growth_rate", is_growth=False, growth_variable=None) is None
    assert side_problem(transform="occurrence", is_growth=True, growth_variable=None) is None


def test_event_study_cumsum_runs_are_written_on_the_level_variable() -> None:
    from reva.estimation.run import SHOCK_PROFILE, effect_target
    from reva.kernel.errors import ValidationProblem

    kind, vid, note = effect_target("wb.ny.gdp.pcap.kd.zg", "cumsum")
    assert (kind, vid) == ("variable", "output.real_gdp_per_capita") and "not a growth rate" in note
    assert effect_target("wb.sl.uem.totl.zs", "diff")[:2] == ("atlas_concept", "wb.sl.uem.totl.zs")
    with pytest.raises(ValidationProblem):
        effect_target("wb.sl.uem.totl.zs", "cumsum")  # no curated level variable: refused, not mislabelled
    assert SHOCK_PROFILE == "one_time_impulse"
