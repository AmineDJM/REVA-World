"""Kernel value algebra, uncertainty, time and context contracts.

These tests exercise behaviour (what is accepted/rejected and what survives a
round trip), not the literal values of constants.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from reva.kernel.context import Context, ContextMode, Origin, assert_publishable
from reva.kernel.errors import MissingInput, PolicyDenied
from reva.kernel.time import Calendar, DeepTimeExtent, Precision, TimeExtent, UncertainBound
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.kernel.values import (
    VALUE_ADAPTER,
    MeasurementBasis,
    Missing,
    MissingReason,
    ScalarDecimal,
    ScalarFloat,
    decimal_str,
    parse_value,
    require_number,
)

USD = MeasurementBasis(currency="USD", price_basis="nominal", valuation="CIF")


def dec(v: str, unit: str = "unit.usd", basis: MeasurementBasis = USD) -> ScalarDecimal:
    return ScalarDecimal(value=v, unit_ref=unit, basis=basis)


# --------------------------------------------------------------------------- Missing
@pytest.mark.parametrize("reason", list(MissingReason))
def test_missing_can_never_be_used_as_a_number(reason: MissingReason) -> None:
    m = Missing(reason=reason)
    with pytest.raises(MissingInput) as ei:
        require_number(m, "imports_2023_DZA")
    err = ei.value
    assert err.code == "missing_input"
    assert err.details["reason"] == reason
    assert err.problem()["status"] == 422
    # Missing has no numeric coercion path at all.
    with pytest.raises(TypeError):
        float(m)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _ = m + 0  # type: ignore[operator]


@pytest.mark.parametrize("bad", [None, 0, "0", {"kind": "missing", "reason": "unavailable"}, Decimal("0")])
def test_require_number_rejects_non_value_inputs_instead_of_defaulting(bad: object) -> None:
    # A raw None/0/dict is not a typed numeric value: no silent zero.
    with pytest.raises(MissingInput):
        require_number(bad, "x")


def test_missing_rejects_a_smuggled_value_and_unknown_reasons() -> None:
    with pytest.raises(ValidationError):
        parse_value({"kind": "missing", "reason": "unavailable", "value": "0"})
    with pytest.raises(ValidationError):
        parse_value({"kind": "missing", "reason": "zero"})
    with pytest.raises(ValidationError):
        parse_value({"kind": "missing"})


def test_require_number_extracts_exact_decimals() -> None:
    assert require_number(dec("0.1"), "x") + require_number(dec("0.2"), "y") == Decimal("0.3")
    f = ScalarFloat(value=0.1, unit_ref="unit.ratio", basis=MeasurementBasis())
    assert require_number(f, "f") == Decimal("0.1")  # repr-based, not binary expansion


# --------------------------------------------------------------------------- NaN / Infinity
@pytest.mark.parametrize("v", ["NaN", "nan", "sNaN", "Infinity", "-Infinity", "inf"])
def test_non_finite_decimal_strings_rejected(v: str) -> None:
    with pytest.raises(ValidationError):
        dec(v)


@pytest.mark.parametrize("v", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_rejected(v: float) -> None:
    with pytest.raises(ValidationError):
        ScalarFloat(value=v, unit_ref="unit.ratio", basis=MeasurementBasis())


def test_float_overflow_from_json_is_rejected() -> None:
    # "1e400" parses to +inf as a float: it must not become a value.
    with pytest.raises(ValidationError):
        VALUE_ADAPTER.validate_json('{"kind":"float","value":1e400,"unit_ref":"u","basis":{}}')


@pytest.mark.parametrize("v", ["abc", "", "0x10", "1,5"])
def test_garbage_decimal_strings_rejected(v: str) -> None:
    with pytest.raises(ValidationError):
        dec(v)


@pytest.mark.parametrize("v", ["1_000", " 1.5 ", "1E+5", "+1", "1."])
def test_decimal_strings_are_canonical_plain_notation(v: str) -> None:
    with pytest.raises(ValidationError):
        dec(v)


# --------------------------------------------------------------------------- precision
@pytest.mark.parametrize(
    "v",
    [
        "12345678901234567890.123456789012345678901",
        "0.000000000000000000000000000001",
        "-98765432109876543210",
        "0.1",
        "1.10",  # trailing zero is source precision, kept
    ],
)
def test_decimal_survives_json_round_trip_exactly(v: str) -> None:
    sd = dec(v)
    wire = sd.model_dump_json()
    # On the wire the number is a string, so no JSON parser can round it.
    assert json.loads(wire)["value"] == v
    back = parse_value(json.loads(wire))
    assert isinstance(back, ScalarDecimal)
    assert back == sd
    assert back.as_decimal() == Decimal(v)
    # Going through float would lose it (shows the test is not vacuous for long values).
    if len(Decimal(v).as_tuple().digits) > 17:
        assert Decimal(repr(float(v))) != Decimal(v)


def test_decimal_str_is_plain_and_has_no_negative_zero() -> None:
    assert decimal_str(Decimal("1E+3")) == "1000"
    assert decimal_str(Decimal("-0.000")) == "0"
    assert decimal_str(Decimal("2.500")) == "2.5"
    with pytest.raises(ValueError):
        decimal_str(Decimal("NaN"))


def test_values_are_frozen_and_discriminated() -> None:
    sd = dec("1")
    with pytest.raises(ValidationError):
        sd.value = "2"  # type: ignore[misc]
    assert isinstance(parse_value({"kind": "category", "code": "C00", "vocabulary_version": "cm-1"}), object)
    with pytest.raises(ValidationError):
        parse_value({"kind": "decimal", "value": "1"})  # no unit/basis
    with pytest.raises(ValidationError):
        parse_value({"kind": "number", "value": "1"})


# --------------------------------------------------------------------------- MeasurementBasis
def test_measurement_basis_is_explicit_and_closed() -> None:
    assert USD != MeasurementBasis(currency="USD", price_basis="real", valuation="CIF")
    assert USD != MeasurementBasis(currency="USD", price_basis="nominal", valuation="FOB")
    with pytest.raises(ValidationError):
        MeasurementBasis(currency="USD", price_basis="constant-ish")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        MeasurementBasis(currency="USD", fx="x")  # type: ignore[call-arg]
    # CIF and FOB of the same number are different values, not duplicates.
    assert dec("10", basis=USD) != dec("10", basis=USD.model_copy(update={"valuation": "FOB"}))


# --------------------------------------------------------------------------- UncertaintySpec
def test_unknown_uncertainty_is_valid_and_not_zero_width() -> None:
    u = UncertaintySpec.unknown()
    assert u.status == UncertaintyStatus.unquantified
    assert u.kind == UncertaintyKind.unknown
    assert u.lower is None and u.upper is None and u.level is None
    assert not u.is_probabilistic


@pytest.mark.parametrize("kind", [k for k in UncertaintyKind if k != UncertaintyKind.unknown])
def test_unquantified_must_be_kind_unknown(kind: UncertaintyKind) -> None:
    with pytest.raises(ValidationError):
        UncertaintySpec(status="unquantified", kind=kind, description="d")


def test_exact_by_definition_requires_definition_kind() -> None:
    UncertaintySpec.by_definition("accounting identity")
    with pytest.raises(ValidationError):
        UncertaintySpec(status="exact_by_definition", kind="bounds", description="d")


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("bounds", {"lower": "1", "upper": "2"}),
        ("sensitivity_range", {"lower": "1", "upper": "2"}),
        ("confidence_interval", {"lower": "1", "upper": "2", "level": 0.9}),
        ("distribution", {}),
    ],
)
def test_quantified_requires_method(kind: str, extra: dict) -> None:
    with pytest.raises(ValidationError):
        UncertaintySpec(status="quantified", kind=kind, description="d", **extra)
    ok = UncertaintySpec(status="quantified", kind=kind, description="d", method_version="m@1", **extra)
    assert ok.status == UncertaintyStatus.quantified


@pytest.mark.parametrize("kind", ["unknown", "definition"])
def test_quantified_needs_concrete_kind(kind: str) -> None:
    with pytest.raises(ValidationError):
        UncertaintySpec(status="quantified", kind=kind, description="d", method_version="m")


def test_sensitivity_range_and_bounds_have_no_coverage_level() -> None:
    s = UncertaintySpec.sensitivity("-1", "2", "elasticity-grid@1", "assumption grid")
    assert s.level is None and not s.is_probabilistic
    for kind in ("sensitivity_range", "bounds"):
        with pytest.raises(ValidationError):
            UncertaintySpec(
                status="quantified", kind=kind, description="d", method_version="m", lower="0", upper="1", level=0.9
            )


@pytest.mark.parametrize("kind", ["confidence_interval", "credible_interval", "prediction_interval"])
def test_interval_kinds_need_a_level_in_open_unit_interval(kind: str) -> None:
    base = dict(status="quantified", kind=kind, description="d", method_version="m", lower="0", upper="1")
    with pytest.raises(ValidationError):
        UncertaintySpec(**base)
    for level in (0.0, 1.0, 1.5, -0.1):
        with pytest.raises(ValidationError):
            UncertaintySpec(**base, level=level)
    assert UncertaintySpec(**base, level=0.95).is_probabilistic


def test_interval_kinds_are_distinct_types() -> None:
    kw = dict(status="quantified", description="d", method_version="m", lower="0", upper="1", level=0.9)
    ci = UncertaintySpec(kind="confidence_interval", **kw)
    cr = UncertaintySpec(kind="credible_interval", **kw)
    pi = UncertaintySpec(kind="prediction_interval", **kw)
    assert len({ci, cr, pi}) == 3
    assert ci != cr != pi


def test_uncertainty_bounds_must_be_ordered_finite_decimals() -> None:
    kw = dict(status="quantified", kind="bounds", description="d", method_version="m")
    with pytest.raises(ValidationError):
        UncertaintySpec(**kw, lower="2", upper="1")
    with pytest.raises(ValidationError):
        UncertaintySpec(**kw, lower="abc", upper="1")
    with pytest.raises(ValidationError):
        UncertaintySpec(**kw, lower="-Infinity", upper="1")


def test_uncertainty_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        UncertaintySpec(status="unquantified", kind="unknown", description="d", sigma="0")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- TimeExtent
def test_calendar_year_is_half_open() -> None:
    y = TimeExtent.calendar_year(2023)
    assert (y.start, y.end) == (date(2023, 1, 1), date(2024, 1, 1))
    nxt = TimeExtent.calendar_year(2024)
    # Adjacent years share a boundary but do not overlap: [a,b) and [b,c).
    assert y.end == nxt.start
    assert y.reference_year() == 2023


def test_interval_end_must_be_after_start() -> None:
    with pytest.raises(ValidationError):
        TimeExtent(kind="interval", precision="day", start=date(2020, 1, 2), end=date(2020, 1, 2))
    with pytest.raises(ValidationError):
        TimeExtent(kind="interval", precision="day", start=date(2020, 1, 2), end=date(2020, 1, 1))


def test_instants_have_no_end_and_require_a_start() -> None:
    i = TimeExtent.on_date(date(2023, 7, 1), label="midyear")
    assert i.kind == "instant" and i.end is None
    with pytest.raises(ValidationError):
        TimeExtent(kind="instant", precision="day", start=date(2023, 7, 1), end=date(2023, 7, 2))
    with pytest.raises(ValidationError):
        TimeExtent(kind="instant", precision="day", start=None)


def test_open_ended_and_uncertain_bounds() -> None:
    # Unknown end stays unknown (open interval), never inferred.
    open_ = TimeExtent(kind="interval", precision="year", start=date(1973, 10, 1))
    assert open_.end is None
    ub = UncertainBound(earliest=date(1973, 10, 1), latest=date(1973, 10, 31))
    fuzzy = TimeExtent(kind="interval", precision="month", start=None, start_uncertainty=ub, end=date(1974, 4, 1))
    assert fuzzy.start is None and fuzzy.reference_year() is None
    with pytest.raises(ValidationError):
        TimeExtent(kind="interval", precision="year", start=None)
    with pytest.raises(ValidationError):
        UncertainBound(earliest=date(2000, 2, 1), latest=date(2000, 1, 1))


def test_calendar_and_precision_are_closed_vocabularies() -> None:
    TimeExtent(kind="instant", precision=Precision.day, start=date(1582, 10, 4), calendar=Calendar.julian)
    with pytest.raises(ValidationError):
        TimeExtent(kind="instant", precision="fortnight", start=date(2000, 1, 1))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TimeExtent(kind="instant", precision="day", start=date(2000, 1, 1), calendar="lunar")  # type: ignore[arg-type]


def test_deep_time_is_a_separate_type() -> None:
    d = DeepTimeExtent(epoch="BP1950", unit="kiloyear", start_offset=-66_000.0, scale_reference="ICS-2023/09")
    with pytest.raises(ValidationError):
        TimeExtent.model_validate(d.model_dump())


# --------------------------------------------------------------------------- Context
def test_context_cutoff_must_be_timezone_aware() -> None:
    kw = dict(context_id=uuid4(), mode=ContextMode.belief, origin=Origin.live, manifest_sha256="a" * 64)
    Context(**kw, knowledge_cutoff=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValidationError):
        Context(**kw, knowledge_cutoff=datetime(2026, 1, 1))


def test_fixture_origin_never_publishable_in_production() -> None:
    assert_publishable(Origin.live, "production")
    assert_publishable(Origin.test_fixture, "test")
    with pytest.raises(PolicyDenied):
        assert_publishable(Origin.test_fixture, "production")
