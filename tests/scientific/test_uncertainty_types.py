"""Acceptance: uncertainty kinds stay distinct types in the kernel contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reva.kernel.uncertainty import (
    PROBABILISTIC_KINDS,
    UncertaintyKind,
    UncertaintySpec,
    UncertaintyStatus,
)
from reva.models.metrics import aggregate_coverage

Q = UncertaintyStatus.quantified


def spec(kind: UncertaintyKind, level: float | None = None) -> UncertaintySpec:
    return UncertaintySpec(
        status=Q,
        kind=kind,
        description="t",
        method_version="m@1",
        level=level,
        lower="1",
        upper="2",
    )


def test_sensitivity_range_cannot_carry_a_level():
    with pytest.raises(ValidationError):
        spec(UncertaintyKind.sensitivity_range, level=0.9)
    s = UncertaintySpec.sensitivity("1", "2", "m@1", "scenario")
    assert s.level is None and not s.is_probabilistic


def test_bounds_are_non_probabilistic_and_levelless():
    with pytest.raises(ValidationError):
        spec(UncertaintyKind.bounds, level=0.95)
    b = spec(UncertaintyKind.bounds)
    assert not b.is_probabilistic


@pytest.mark.parametrize(
    "kind",
    [
        UncertaintyKind.prediction_interval,
        UncertaintyKind.credible_interval,
        UncertaintyKind.confidence_interval,
    ],
)
def test_interval_kinds_require_a_level(kind):
    with pytest.raises(ValidationError):
        spec(kind)
    s = spec(kind, level=0.8)
    assert s.is_probabilistic and s.level == 0.8


def test_probability_kind_is_probabilistic():
    p = UncertaintySpec(
        status=Q, kind=UncertaintyKind.probability, description="p", method_version="m@1"
    )
    assert p.is_probabilistic


def test_kinds_are_distinct_values_and_not_interchangeable():
    kinds = {
        UncertaintyKind.probability,
        UncertaintyKind.credible_interval,
        UncertaintyKind.prediction_interval,
        UncertaintyKind.bounds,
        UncertaintyKind.sensitivity_range,
    }
    assert len({k.value for k in kinds}) == 5
    pi = spec(UncertaintyKind.prediction_interval, level=0.8)
    ci = spec(UncertaintyKind.credible_interval, level=0.8)
    assert pi != ci and pi.kind != ci.kind
    assert UncertaintyKind.sensitivity_range not in PROBABILISTIC_KINDS
    assert UncertaintyKind.bounds not in PROBABILISTIC_KINDS


def test_quantified_requires_method_version_and_concrete_kind():
    with pytest.raises(ValidationError):
        UncertaintySpec(
            status=Q, kind=UncertaintyKind.prediction_interval, description="x", level=0.8
        )
    with pytest.raises(ValidationError):
        UncertaintySpec(
            status=Q, kind=UncertaintyKind.unknown, description="x", method_version="m@1"
        )
    with pytest.raises(ValidationError):
        UncertaintySpec(
            status=UncertaintyStatus.unquantified,
            kind=UncertaintyKind.prediction_interval,
            description="x",
            level=0.8,
        )


def test_specs_are_immutable():
    s = spec(UncertaintyKind.prediction_interval, level=0.8)
    with pytest.raises(ValidationError):
        s.level = 0.5  # type: ignore[misc]


def test_only_prediction_intervals_have_coverage():
    assert aggregate_coverage([(UncertaintyKind.prediction_interval, True)]) == 1.0
    for kind in (
        UncertaintyKind.sensitivity_range,
        UncertaintyKind.bounds,
        UncertaintyKind.credible_interval,
        UncertaintyKind.probability,
    ):
        with pytest.raises(ValueError):
            aggregate_coverage([(kind, True)])
