"""Identifiers and versioned canonical hashing (property-based where it matters)."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from reva.kernel.ids import (
    CANONICAL_HASH_VERSION,
    canonical_bytes,
    external_id,
    is_sha256,
    logical_hash,
    sha256_bytes,
)
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import TimeExtent
from reva.ontology.registry import load_registry

# --------------------------------------------------------------------------- strategies
keys = st.text(min_size=0, max_size=8)
finite_floats = st.floats(allow_nan=False, allow_infinity=False)
decimals = st.decimals(allow_nan=False, allow_infinity=False, places=None)
scalars = st.one_of(
    st.none(), st.booleans(), st.integers(), finite_floats, decimals, st.text(max_size=12), st.uuids()
)
json_like = st.recursive(
    scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(keys, children, max_size=4)
    ),
    max_leaves=20,
)


def _shuffle_keys(obj, rnd):  # type: ignore[no-untyped-def]
    if isinstance(obj, dict):
        items = list(obj.items())
        rnd.shuffle(items)
        return {k: _shuffle_keys(v, rnd) for k, v in items}
    if isinstance(obj, list):
        return [_shuffle_keys(v, rnd) for v in obj]
    return obj


# --------------------------------------------------------------------------- canonical JSON
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(obj=json_like, rnd=st.randoms(use_true_random=False))
def test_canonical_bytes_deterministic_under_key_order(obj, rnd) -> None:  # type: ignore[no-untyped-def]
    a = canonical_bytes(obj)
    b = canonical_bytes(_shuffle_keys(obj, rnd))
    assert a == b
    assert logical_hash(obj) == logical_hash(_shuffle_keys(obj, rnd))
    # Output is strict JSON (no NaN tokens) and decodes.
    json.loads(a.decode("utf-8"))


@settings(max_examples=200)
@given(d=decimals)
def test_decimal_is_exact_string_in_canonical_form(d: Decimal) -> None:
    payload = json.loads(canonical_bytes({"v": d}))
    assert Decimal(payload["v"]["$decimal"]) == d


@settings(max_examples=200)
@given(f=finite_floats)
def test_float_round_trips_through_canonical_form(f: float) -> None:
    payload = json.loads(canonical_bytes({"v": f}))
    assert float(payload["v"]["$float"]) == f


@settings(max_examples=200)
@given(x=st.integers(min_value=-(10**6), max_value=10**6))
def test_float_and_decimal_of_same_number_hash_differently(x: int) -> None:
    # A measured float and an exact decimal are different logical records.
    assert logical_hash({"v": float(x)}) != logical_hash({"v": Decimal(x)})
    assert logical_hash({"v": float(x)}) != logical_hash({"v": x})
    assert logical_hash({"v": Decimal(x)}) != logical_hash({"v": str(x)})


def test_bool_int_and_string_are_not_conflated() -> None:
    hashes = {logical_hash({"v": v}) for v in (True, 1, "1", 1.0, Decimal(1), None, "true")}
    assert len(hashes) == 7


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, Decimal("NaN"), Decimal("-Infinity"),
                                 Decimal("sNaN")])
def test_non_finite_forbidden_in_canonical_json(bad: object) -> None:
    with pytest.raises(ValueError):
        canonical_bytes({"nested": [1, {"x": bad}]})
    with pytest.raises(ValueError):
        logical_hash(bad)


def test_naive_datetimes_and_non_string_keys_forbidden() -> None:
    with pytest.raises(ValueError):
        canonical_bytes({"t": datetime(2026, 1, 1)})
    with pytest.raises(TypeError):
        canonical_bytes({1: "x"})
    with pytest.raises(TypeError):
        canonical_bytes({"b": b"bytes"})
    with pytest.raises(TypeError):
        canonical_bytes({"o": object()})


def test_temporal_types_are_tagged_and_distinct() -> None:
    d = date(2023, 1, 1)
    t = datetime(2023, 1, 1, tzinfo=UTC)
    assert logical_hash(d) != logical_hash(t) != logical_hash("2023-01-01")
    # Same instant in a different offset is a different serialisation: callers must normalise to UTC.
    t2 = t.astimezone(timezone(timedelta(hours=2)))
    assert t == t2
    assert canonical_bytes(t) != canonical_bytes(t2)


def test_sets_are_order_independent_and_enums_use_values() -> None:
    class Colour(StrEnum):
        red = "red"

    assert canonical_bytes({"s": {3, 1, 2}}) == canonical_bytes({"s": frozenset([2, 3, 1])})
    assert canonical_bytes({"c": Colour.red}) == canonical_bytes({"c": "red"})


def test_logical_hash_is_versioned_and_hex() -> None:
    import hashlib

    payload = {"a": 1}
    raw = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    h = logical_hash(payload)
    assert is_sha256(h)
    # The version prefix is part of the hash, so a future serialisation cannot collide silently.
    assert h != raw
    assert h == hashlib.sha256(CANONICAL_HASH_VERSION.encode() + b"\x00" + canonical_bytes(payload)).hexdigest()


def test_logical_hash_separate_from_byte_hash() -> None:
    a = json.dumps({"a": 1, "b": 2}).encode()
    b = json.dumps({"b": 2, "a": 1}, indent=2).encode()
    assert sha256_bytes(a) != sha256_bytes(b)  # bytes differ
    assert logical_hash(json.loads(a)) == logical_hash(json.loads(b))  # logical record does not


def test_is_sha256() -> None:
    assert is_sha256("a" * 64)
    assert not is_sha256("A" * 64)
    assert not is_sha256("a" * 63)
    assert not is_sha256("g" * 64)


def test_support_digest_stable_and_sensitive_to_semantics() -> None:
    def sup(agg: str, universe: str) -> Support:
        return Support(
            temporal=TimeExtent.calendar_year(2023),
            spatial=SpatialRef(scheme="provider_reporter_territory", key="WB:FRA", authority="World Bank"),
            population=PopulationRef(universe=universe),
            aggregation=agg,  # type: ignore[arg-type]
        )

    a = sup("period_flow_total", "economy")
    assert a.digest() == sup("period_flow_total", "economy").digest()
    assert a.digest() != sup("period_average_stock", "economy").digest()
    assert a.digest() != sup("period_flow_total", "residents").digest()


# --------------------------------------------------------------------------- UUIDv5 identities
def test_external_id_is_deterministic_uuid5() -> None:
    a = external_id("comtrade_reporter", "251")
    assert a == external_id("comtrade_reporter", "251")
    assert a.version == 5
    assert isinstance(a, uuid.UUID)


def test_external_id_depends_on_provider_key_and_epoch() -> None:
    base = external_id("comtrade_reporter", "251", "1")
    assert base != external_id("un_m49", "251", "1")  # same code, different provider scope
    assert base != external_id("comtrade_reporter", "250", "1")
    assert base != external_id("comtrade_reporter", "251", "2")  # identity epoch change


def test_external_id_takes_no_display_name() -> None:
    import inspect

    params = set(inspect.signature(external_id).parameters)
    assert params == {"provider", "external_key", "identity_epoch"}
    with pytest.raises(ValueError):
        external_id("", "251")
    with pytest.raises(ValueError):
        external_id("worldbank", "")


def test_registry_entity_ids_are_label_independent() -> None:
    from reva.cli.seed import entity_id

    reg = load_registry()
    for e in reg.entities:
        eid = entity_id(e["key"])
        assert eid == external_id("reva.entity", e["key"])
        # Labels/names never enter the identity.
        label = e.get("label") or e.get("name") or ""
        if label:
            assert eid != external_id("reva.entity", label)
    assert len({entity_id(e["key"]) for e in reg.entities}) == len(reg.entities)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: external_id joins components with \\x1f without escaping/length-prefixing, so "
        "('a\\x1fb','c') and ('a','b\\x1fc') collide; identities must be injective"
    ),
)
def test_external_id_is_injective_over_components() -> None:
    assert external_id("a\x1fb", "c") != external_id("a", "b\x1fc")


@settings(max_examples=200)
@given(p=st.text(min_size=1, max_size=10, alphabet=st.characters(blacklist_characters="\x1f")),
       k=st.text(min_size=1, max_size=10, alphabet=st.characters(blacklist_characters="\x1f")),
       e=st.text(min_size=1, max_size=4, alphabet=st.characters(blacklist_characters="\x1f")))
def test_external_id_distinct_for_distinct_keys(p: str, k: str, e: str) -> None:
    assert external_id(p, k, e) != external_id(p, k + "x", e)
    assert external_id(p, k, e) != external_id(p + "x", k, e)
