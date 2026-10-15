"""R7 property checks for the kernel (src/reva/kernel/**).

Canonical hashing (reva-cjson-2), the Value union, ScalarDecimal/decimal_str,
UncertaintySpec, TimeExtent and UUIDv5 external identities. Pure: no database.

Tests marked ``xfail(strict=True)`` encode a property the current code violates;
each carries its minimal counterexample (see docs/dev/property_check.md). They
turn into hard failures (XPASS) once the code is fixed, so the marker must then
be removed.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from reva.kernel.ids import (
    CANONICAL_HASH_VERSION,
    REVA_NAMESPACE,
    canonical_bytes,
    entity_id,
    external_id,
    logical_hash,
)
from reva.kernel.time import Precision, TimeExtent, UncertainBound
from reva.kernel.uncertainty import INTERVAL_KINDS, UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.kernel.values import (
    _DECIMAL_RE,
    ArrayValue,
    Category,
    DistributionValue,
    EntityRefValue,
    GeometryValue,
    MeasurementBasis,
    Missing,
    MissingReason,
    ScalarDecimal,
    ScalarFloat,
    Text,
    decimal_str,
    parse_value,
)

FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SLOW = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
TAGS = ("$float", "$decimal", "$datetime", "$date")

# --------------------------------------------------------------------------- strategies
_offsets = st.integers(min_value=-(24 * 60 - 1), max_value=24 * 60 - 1).map(lambda m: timezone(timedelta(minutes=m)))
# Instants away from datetime.min/max so every offset is representable (edge case tested below).
_utc_instants = st.datetimes(min_value=datetime(2, 1, 1), max_value=datetime(9998, 12, 31)).map(
    lambda d: d.replace(tzinfo=UTC)
)
_aware = st.builds(lambda d, tz: d.astimezone(tz), _utc_instants, _offsets)
_decimals = st.builds(
    lambda sign, digits, exp: Decimal((sign, tuple(int(c) for c in str(digits)), exp)),
    st.integers(0, 1),
    st.integers(0, 10**40),
    st.integers(-60, 60),
)
_keys = st.one_of(
    st.text(max_size=6),
    st.sampled_from(["$", "$$", "$float", "$$float", "$decimal", "$$decimal", "$datetime", "$date", "$x", "$$x"]),
    st.text(max_size=4).map(lambda s: "$" + s),
)
_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**30), max_value=10**30),
    st.floats(allow_nan=False, allow_infinity=False),
    _decimals,
    st.text(max_size=8),
    st.sampled_from(["$float", "$decimal", "1.5", "2020-01-01"]),
    st.dates(),
    _aware,
    st.uuids(),
)
json_like = st.recursive(
    _scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(_keys, inner, max_size=4)),
    max_leaves=12,
)


def _strict_key(obj: Any) -> Any:
    """Exact identity class of a canonical-JSON input, with only the *designed* collapses:
    tuple≡list, UUID≡its string, aware datetime≡its UTC instant, Decimal≡format(d, 'f')."""
    if obj is None or isinstance(obj, bool):
        return ("lit", obj)
    if isinstance(obj, int):
        return ("int", obj)
    if isinstance(obj, float):
        return ("float", repr(obj))
    if isinstance(obj, Decimal):
        return ("dec", format(obj, "f"))
    if isinstance(obj, datetime):
        return ("dt", obj.astimezone(UTC))
    if isinstance(obj, date):
        return ("date", obj)
    if isinstance(obj, uuid.UUID):
        return ("str", str(obj))
    if isinstance(obj, str):
        return ("str", obj)
    if isinstance(obj, (list, tuple)):
        return ("list", tuple(_strict_key(v) for v in obj))
    if isinstance(obj, dict):
        return ("map", frozenset((k, _strict_key(v)) for k, v in obj.items()))
    raise TypeError(type(obj))


def _decode(j: Any) -> Any:
    """Independent inverse of the reva-cjson-2 encoding (proves injectivity on the domain)."""
    if isinstance(j, list):
        return [_decode(v) for v in j]
    if isinstance(j, dict):
        if len(j) == 1:
            ((k, v),) = j.items()
            if k == "$float":
                return float(v)
            if k == "$decimal":
                return Decimal(v)
            if k == "$datetime":
                return datetime.fromisoformat(v)
            if k == "$date":
                return date.fromisoformat(v)
        out = {}
        for k, v in j.items():
            assert not (k.startswith("$") and not k.startswith("$$")), f"unescaped user $-key {k!r}"
            out[k[1:] if k.startswith("$") else k] = _decode(v)
        return out
    return j


def _shuffled(obj: Any, rnd: Any) -> Any:
    if isinstance(obj, dict):
        items = list(obj.items())
        rnd.shuffle(items)
        return {k: _shuffled(v, rnd) for k, v in items}
    if isinstance(obj, list):
        return [_shuffled(v, rnd) for v in obj]
    return obj


# --------------------------------------------------------------------------- canonical hashing
def test_hash_version_is_cjson2() -> None:
    assert CANONICAL_HASH_VERSION == "reva-cjson-2"


@SLOW
@given(json_like)
def test_hash_is_deterministic(obj: Any) -> None:
    import copy

    assert logical_hash(obj) == logical_hash(copy.deepcopy(obj))
    assert canonical_bytes(obj) == canonical_bytes(obj)


@SLOW
@given(json_like, st.randoms(use_true_random=False))
def test_hash_is_key_order_independent(obj: Any, rnd: Any) -> None:
    assert logical_hash(_shuffled(obj, rnd)) == logical_hash(obj)


@SLOW
@given(json_like)
def test_canonical_encoding_is_injective_via_round_trip(obj: Any) -> None:
    """decode(canonical(x)) has the same identity class as x, so canonical(a) == canonical(b)
    implies a ≡ b: $-key escaping can never make a user mapping look like a tagged scalar."""
    decoded = _decode(json.loads(canonical_bytes(obj)))
    assert _strict_key(decoded) == _strict_key(obj)


@FAST
@given(json_like, json_like)
def test_distinct_objects_have_distinct_hashes(a: Any, b: Any) -> None:
    assume(_strict_key(a) != _strict_key(b))
    assert logical_hash(a) != logical_hash(b)


@FAST
@given(st.sampled_from(TAGS), _decimals, st.floats(allow_nan=False, allow_infinity=False), st.dates(), _aware)
def test_user_mapping_never_hashes_like_a_tagged_scalar(tag: str, d: Decimal, f: float, dd: date, dt: datetime) -> None:
    tagged = {"$float": f, "$decimal": d, "$datetime": dt, "$date": dd}[tag]
    payload = json.loads(canonical_bytes(tagged))[tag]
    spoof = {tag: payload}
    assert logical_hash(spoof) != logical_hash(tagged)
    assert logical_hash({"$" + tag: payload}) != logical_hash(spoof)  # "$$x" vs "$x" stay distinct


@FAST
@given(_utc_instants, _offsets, _offsets)
def test_equal_instants_with_different_offsets_hash_equal(instant: datetime, tz1: Any, tz2: Any) -> None:
    a, b = instant.astimezone(tz1), instant.astimezone(tz2)
    assert a == b
    assert logical_hash({"t": a}) == logical_hash({"t": b}) == logical_hash({"t": instant})


@FAST
@given(_utc_instants, st.integers(min_value=1, max_value=10**6))
def test_different_instants_hash_differently(instant: datetime, us: int) -> None:
    assert logical_hash(instant) != logical_hash(instant + timedelta(microseconds=us))


@FAST
@given(st.datetimes(min_value=datetime(1, 1, 2), max_value=datetime(9999, 12, 30)), json_like)
def test_naive_datetimes_are_rejected_anywhere(naive: datetime, ctx: Any) -> None:
    for obj in (naive, [ctx, naive], {"k": [naive]}, {"$date": naive}):
        with pytest.raises(ValueError, match="naive"):
            canonical_bytes(obj)


def test_edge_of_calendar_aware_datetime_is_encoded_or_rejected_with_valueerror() -> None:
    # R7-K1: 0000-12-31T19:00Z and 10000-01-01T04:00Z are not representable in UTC.
    for edge in (
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5))),
        datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-5))),
    ):
        with pytest.raises(ValueError, match="representable UTC range"):
            canonical_bytes(edge)
    # Representable instants keep their reva-cjson-2 encoding (the fix changes no successful output).
    assert (
        canonical_bytes(datetime(1, 1, 1, 5, tzinfo=timezone(timedelta(hours=5))))
        == b'{"$datetime":"0001-01-01T00:00:00+00:00"}'
    )
    assert (
        canonical_bytes(datetime(2020, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))))
        == b'{"$datetime":"2020-01-01T00:00:00+00:00"}'
    )


def test_lone_surrogate_string_is_rejected_with_valueerror() -> None:
    # UnicodeEncodeError is a ValueError subclass: unencodable text is rejected, never hashed lossily.
    with pytest.raises(ValueError):
        canonical_bytes("\ud800")


# --------------------------------------------------------------------------- Value union / Missing
_dec_strings = st.from_regex(r"-?(0|[1-9][0-9]{0,20})(\.[0-9]{1,12})?", fullmatch=True)
_basis = st.builds(
    MeasurementBasis,
    currency=st.sampled_from([None, "USD", "EUR"]),
    price_basis=st.sampled_from([None, "nominal", "real", "ppp"]),
    price_reference_year=st.one_of(st.none(), st.integers(1900, 2100)),
    valuation=st.sampled_from([None, "CIF", "FOB"]),
    scale_factor=st.sampled_from(["1", "1000", "0.001"]),
    notes=st.one_of(st.none(), st.text(max_size=10)),
)
_sha = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
values = st.one_of(
    st.builds(ScalarDecimal, value=_dec_strings, unit_ref=st.text(min_size=1, max_size=8), basis=_basis),
    st.builds(
        ScalarFloat,
        value=st.floats(allow_nan=False, allow_infinity=False),
        unit_ref=st.text(min_size=1, max_size=8),
        basis=_basis,
    ),
    st.builds(Category, code=st.text(max_size=8), vocabulary_version=st.text(max_size=4)),
    st.builds(
        Text,
        language_tag=st.sampled_from(["en", "fr"]),
        text=st.one_of(st.none(), st.text(max_size=10)),
        artifact_sha256=st.one_of(st.none(), _sha),
    ),
    st.builds(EntityRefValue, entity_id=st.uuids()),
    st.builds(
        ArrayValue,
        artifact_sha256=_sha,
        dtype=st.sampled_from(["float64", "int32"]),
        shape=st.lists(st.integers(0, 9), max_size=3).map(tuple),
        axes=st.lists(st.text(max_size=3), max_size=3).map(tuple),
        unit_ref=st.text(max_size=5),
    ),
    st.builds(DistributionValue, uncertainty_ref=st.text(max_size=8)),
    st.builds(GeometryValue, spatial_support_ref=st.text(max_size=8)),
    st.builds(Missing, reason=st.sampled_from(list(MissingReason))),
)


@FAST
@given(values)
def test_value_json_round_trip_preserves_equality_and_hash(v: Any) -> None:
    wire = json.loads(json.dumps(v.model_dump(mode="json"), allow_nan=False))
    back = parse_value(wire)
    assert type(back) is type(v)
    assert back == v
    assert logical_hash(back) == logical_hash(v)
    assert back.model_dump(mode="json") == wire


@FAST
@given(st.sampled_from(list(MissingReason)))
def test_missing_never_parses_as_a_number(reason: MissingReason) -> None:
    from reva.kernel.errors import MissingInput
    from reva.kernel.values import require_number

    v = parse_value({"kind": "missing", "reason": reason.value})
    assert isinstance(v, Missing)
    with pytest.raises(MissingInput):
        require_number(v, "x")


# --------------------------------------------------------------------------- ScalarDecimal / decimal_str
def _is_canonical_plain_decimal(s: str) -> bool:
    """Independent statement: optional '-', integer part '0' or no leading zero, optional
    '.digits'; ASCII digits only; no '+', exponent, whitespace, underscore or trailing '.'."""
    body = s[1:] if s.startswith("-") else s
    intpart, dot, frac = body.partition(".")
    ascii_digits = set("0123456789")
    if not intpart or not set(intpart) <= ascii_digits:
        return False
    if len(intpart) > 1 and intpart[0] == "0":
        return False
    if dot and (not frac or not set(frac) <= ascii_digits):
        return False
    return True


def _accepts(s: str) -> bool:
    try:
        ScalarDecimal(value=s, unit_ref="u", basis=MeasurementBasis())
        return True
    except ValidationError:
        return False


@settings(max_examples=400, deadline=None)
@given(st.one_of(st.text(alphabet="0123456789-+.eE_ \n٣", max_size=7), _dec_strings))
@example("+1")
@example("01")
@example("1.")
@example(".5")
@example("1e5")
@example("1E+5")
@example("1_0")
@example(" 1")
@example("1\n")
@example("٣")
@example("-0")
@example("--1")
def test_scalar_decimal_accepts_exactly_plain_decimals(s: str) -> None:
    assert _accepts(s) == _is_canonical_plain_decimal(s), s


@FAST
@given(_decimals)
@example(Decimal("-0"))
@example(Decimal("-0E-5"))
@example(Decimal("1E+30"))
@example(Decimal("1.2300"))
@example(Decimal("12345678901234567890123456789012345678901234567890.123"))
def test_decimal_str_round_trips(d: Decimal) -> None:
    s = decimal_str(d)
    assert _DECIMAL_RE.fullmatch(s), s
    assert _accepts(s)
    assert Decimal(s) == d
    assert decimal_str(Decimal(s)) == s  # idempotent
    assert not ("." in s and s.endswith("0")), s  # no trailing fractional zeros
    assert s != "-0"


# --------------------------------------------------------------------------- UncertaintySpec
_bound_str = st.one_of(st.none(), _dec_strings)
_levels = st.one_of(st.none(), st.sampled_from([0.0, 0.5, 0.9, 0.95, 1.0, -0.1, 1.5]))


def _expected_valid(
    status: str, kind: str, level: float | None, lower: str | None, upper: str | None, method: str | None
) -> bool:
    if status == "unquantified" and kind != "unknown":
        return False
    if status == "exact_by_definition" and kind != "definition":
        return False
    if status == "quantified" and (kind in ("unknown", "definition") or not method):
        return False
    if kind == "sensitivity_range" and (level is not None or lower is None or upper is None):
        return False
    if kind == "bounds" and level is not None:
        return False
    if level is not None and not (0 < level < 1 and kind in {k.value for k in INTERVAL_KINDS}):
        return False
    if kind in {k.value for k in INTERVAL_KINDS} and level is None:
        return False
    if lower is not None and upper is not None and Decimal(lower) > Decimal(upper):
        return False
    return True


@settings(max_examples=400, deadline=None)
@given(
    st.sampled_from([s.value for s in UncertaintyStatus]),
    st.sampled_from([k.value for k in UncertaintyKind]),
    _levels,
    _bound_str,
    _bound_str,
    st.sampled_from([None, "", "m@1"]),
)
def test_uncertainty_spec_invariants(
    status: str, kind: str, level: float | None, lower: str | None, upper: str | None, method: str | None
) -> None:
    try:
        spec = UncertaintySpec(
            status=status, kind=kind, description="d", level=level, lower=lower, upper=upper, method_version=method
        )
        ok = True
    except ValidationError:
        ok = False
    assert ok == _expected_valid(status, kind, level, lower, upper, method)
    if ok:
        if spec.lower is not None and spec.upper is not None:
            assert Decimal(spec.lower) <= Decimal(spec.upper)
        if spec.kind == UncertaintyKind.sensitivity_range:
            assert spec.level is None and not spec.quantifies_measurement and not spec.is_probabilistic
        assert UncertaintySpec.model_validate(json.loads(spec.model_dump_json())) == spec


@FAST
@given(_dec_strings, _dec_strings, st.sampled_from([0.5, 0.9]))
def test_sensitivity_range_never_carries_a_level(a: str, b: str, level: float) -> None:
    lo, hi = sorted((a, b), key=Decimal)
    spec = UncertaintySpec.sensitivity(lo, hi, "m@1", "d")
    assert spec.level is None
    with pytest.raises(ValidationError):
        UncertaintySpec(**{**spec.model_dump(), "level": level})


@settings(max_examples=50, deadline=None)
@given(st.sampled_from([" 1", "1_0", "1e5", "+1", "01"]))
def test_uncertainty_bounds_must_be_plain_decimals(bad: str) -> None:
    with pytest.raises(ValidationError):
        UncertaintySpec(
            status="quantified", kind="bounds", description="d", method_version="m", lower=bad, upper="999999"
        )


# --------------------------------------------------------------------------- TimeExtent
_dates = st.dates(min_value=date(1, 1, 2), max_value=date(9998, 12, 30))


@FAST
@given(_dates, _dates)
def test_interval_is_half_open(start: date, end: date) -> None:
    try:
        TimeExtent(kind="interval", precision=Precision.day, start=start, end=end)
        ok = True
    except ValidationError:
        ok = False
    assert ok == (end > start)  # [start, end): empty and reversed intervals are rejected


@FAST
@given(st.integers(1, 9997))
def test_calendar_years_tile_without_overlap(y: int) -> None:
    a, b = TimeExtent.calendar_year(y), TimeExtent.calendar_year(y + 1)
    assert a.end == b.start  # half-open: Dec 31 belongs to y, Jan 1 to y+1
    assert a.start == date(y, 1, 1) and a.reference_year() == y


@FAST
@given(_dates, _dates)
def test_instant_never_has_end(start: date, end: date) -> None:
    with pytest.raises(ValidationError):
        TimeExtent(kind="instant", precision=Precision.day, start=start, end=end)


@FAST
@given(_dates, _dates, _dates, st.booleans())
def test_bound_lies_within_its_uncertain_bound(x: date, e: date, l_: date, end_side: bool) -> None:
    try:
        ub = UncertainBound(earliest=e, latest=l_)
    except ValidationError:
        assert l_ < e
        return
    if end_side:
        kw = {"start": date(1, 1, 1), "end": x, "end_uncertainty": ub}
        assume(x > date(1, 1, 1))
    else:
        kw = {"start": x, "start_uncertainty": ub}
    try:
        TimeExtent(kind="interval", precision=Precision.day, **kw)
        ok = True
    except ValidationError:
        ok = False
    assert ok == (e <= x <= l_)


@settings(max_examples=50, deadline=None)
@given(_dates, st.integers(1, 3650), st.integers(0, 3650), st.booleans())
@example(date(2020, 1, 1), 365, 0, False)
def test_interval_whose_end_precedes_every_admissible_start_is_rejected(
    s: date, gap: int, width: int, end_side: bool
) -> None:
    assume(s.toordinal() - (gap + width) > 1)  # ordinal arithmetic: date subtraction below year 1 overflows
    before = UncertainBound(earliest=s - timedelta(days=gap + width), latest=s - timedelta(days=gap))
    if end_side:  # every admissible end is before the start
        kw = {"start": s, "end_uncertainty": before}
    else:  # end is before every admissible start
        kw = {
            "start": None,
            "start_uncertainty": UncertainBound(earliest=s, latest=s + timedelta(days=width)),
            "end": s - timedelta(days=gap),
        }
    with pytest.raises(ValidationError):
        TimeExtent(kind="interval", precision=Precision.day, **kw)


def test_instant_never_has_an_end_bound() -> None:
    with pytest.raises(ValidationError):
        TimeExtent(
            kind="instant",
            precision=Precision.day,
            start=date(2020, 1, 1),
            end_uncertainty=UncertainBound(earliest=date(2020, 1, 2), latest=date(2020, 2, 1)),
        )


@FAST
@given(_dates, st.integers(0, 400), st.integers(0, 400), st.integers(-400, 400))
def test_uncertain_interval_accepted_iff_some_admissible_pair_is_non_empty(s: date, sw: int, ew: int, off: int) -> None:
    assume(date(1, 1, 1) + timedelta(days=sw + ew + 800) < s < date(9998, 1, 1))
    su = UncertainBound(earliest=s, latest=s + timedelta(days=sw))
    e0 = s + timedelta(days=off)
    eu = UncertainBound(earliest=e0, latest=e0 + timedelta(days=ew))
    try:
        TimeExtent(kind="interval", precision=Precision.day, start=None, start_uncertainty=su, end_uncertainty=eu)
        ok = True
    except ValidationError:
        ok = False
    assert ok == (eu.latest > su.earliest)


# --------------------------------------------------------------------------- external_id
def test_external_id_is_pinned_uuidv5() -> None:
    fra = entity_id("FRA")
    assert fra == uuid.uuid5(REVA_NAMESPACE, "reva.entity\x1fFRA\x1f1")
    assert fra.version == 5
    assert str(REVA_NAMESPACE) == "5d0f8f1c-3f61-5c5b-9a8e-6f2b7d2a9c11"


_component = st.text(alphabet=["a", "\x1f", "\\", "u", "0", "1", "f"], max_size=6)


@FAST
@given(st.text(min_size=1, max_size=10), st.text(min_size=1, max_size=10), st.text(max_size=3))
def test_external_id_is_deterministic(p: str, k: str, ep: str) -> None:
    a = external_id(p, k, ep)
    assert a == external_id(p, k, ep) and a.version == 5
    if "\\" not in p + k + ep and "\x1f" not in p + k + ep:  # ordinary keys: plain join (stable since v1)
        assert a == uuid.uuid5(REVA_NAMESPACE, f"{p}\x1f{k}\x1f{ep}")


@settings(max_examples=300, deadline=None)
@given(
    st.tuples(_component.filter(bool), _component.filter(bool), _component),
    st.tuples(_component.filter(bool), _component.filter(bool), _component),
)
@example(("a\x1fb", "c", "1"), ("a", "b\x1fc", "1"))
@example(("a\\u001f", "b", "1"), ("a\x1f", "b", "1"))
def test_external_id_is_injective_over_components(t1: tuple[str, str, str], t2: tuple[str, str, str]) -> None:
    assume(t1 != t2)
    assert external_id(*t1) != external_id(*t2)


@pytest.mark.parametrize("p,k", [("", "x"), ("x", "")])
def test_external_id_requires_provider_and_key(p: str, k: str) -> None:
    with pytest.raises(ValueError):
        external_id(p, k)
