"""Pure (no DB) properties of the observation export: exact decimals, explicit refusals.

* A contract-grammar decimal string survives export → JSON text → parse byte-for-byte,
  and the record validates against observation.schema.json.
* Representations the v1 contract cannot carry are refused with a typed error,
  never rewritten or truncated: non-canonical decimal strings, the kernel
  ``probability`` uncertainty kind (D-8), inline interval fields, quantified
  uncertainty without an artifact, non-scalar value kinds, naive timestamps.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from reva.evidence.export import (
    DECIMAL_PATTERN,
    ObservationNotExportable,
    contract_errors,
    export_uncertainty,
    observation_record,
)
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec

SHA = "a" * 64
OTHER_SHA = "b" * 64
CONTRACT_DECIMAL = r"\A-?(0|[1-9][0-9]{0,60})(\.[0-9]{1,60})?\Z"


def row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid4(),
        "source_id": uuid4(),
        "connector_version_id": uuid4(),
        "acquisition_id": uuid4(),
        "subject_id": uuid4(),
        "concept_version_id": uuid4(),
        "support_id": uuid4(),
        "logical_key": "wb:FRA:SP.POP.TOTL:2023",
        "source_revision_id": None,
        "value": {"kind": "decimal", "value": "68170228", "unit_ref": "unit.person", "basis": {"scale_factor": "1"}},
        "uncertainty": json.loads(UncertaintySpec.unknown("WDI publishes no uncertainty").model_dump_json()),
        "observed_at": None,
        "source_published_at": datetime(2024, 7, 1, tzinfo=UTC),
        "source_available_at": datetime(2024, 7, 1, 12, 30, 5, 120000, tzinfo=UTC),
        "availability_basis": {"kind": "provider_lastupdated"},
        "source_flags": {"obs_status": None, "footnote": "x", "n": 1.5},
        "raw_selector": {"artifact_sha256": SHA, "path": [1, 0]},
        "provenance_activity_id": uuid4(),
        "retrieved_at": datetime(2026, 10, 14, 9, 0, tzinfo=timezone(timedelta(hours=2))),
        "raw_sha256": SHA,
    }
    base.update(over)
    return base


def decimal_value(s: str) -> dict[str, Any]:
    return {
        "kind": "decimal",
        "value": s,
        "unit_ref": "unit.usd",
        "basis": {"currency": "USD", "price_basis": "nominal", "scale_factor": "1"},
    }


# --------------------------------------------------------------------------- round trip
@settings(max_examples=300, deadline=None)
@given(st.from_regex(CONTRACT_DECIMAL, fullmatch=True))
def test_contract_decimal_strings_round_trip_exactly(s: str) -> None:
    rec = observation_record(row(value=decimal_value(s)))
    text = json.dumps(rec, allow_nan=False)
    back = json.loads(text)
    assert back["value"]["value"] == s  # byte-for-byte, not re-formatted
    assert Decimal(back["value"]["value"]) == Decimal(s)
    assert back == rec
    assert contract_errors(back) == []


@settings(max_examples=300, deadline=None)
@given(st.decimals(allow_nan=False, allow_infinity=False))
def test_any_finite_decimal_in_plain_notation_round_trips(d: Decimal) -> None:
    s = format(d, "f")
    if s.startswith("+"):  # pragma: no cover - format never emits '+'
        s = s[1:]
    if not DECIMAL_PATTERN.match(s):  # e.g. leading-zero forms are outside the grammar
        return
    back = json.loads(json.dumps(observation_record(row(value=decimal_value(s)))))
    assert Decimal(back["value"]["value"]) == d
    assert back["value"]["value"] == s


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet="0123456789+-.eE_ ", min_size=1, max_size=12))
def test_non_contract_decimal_strings_are_refused_not_rewritten(s: str) -> None:
    if DECIMAL_PATTERN.match(s):
        return
    with pytest.raises(ObservationNotExportable):
        observation_record(row(value=decimal_value(s)))


@pytest.mark.parametrize("s", ["1E+5", "+1", "1_000", ".5", "5.", "01", "NaN", "Infinity", "-", "1e3"])
def test_named_non_canonical_decimals_are_refused(s: str) -> None:
    with pytest.raises(ObservationNotExportable) as e:
        observation_record(row(value=decimal_value(s)))
    assert e.value.problem()["status"] == 422


def test_exported_record_shape_and_formats() -> None:
    r = row()
    rec = observation_record(r)
    assert contract_errors(rec) == []
    assert rec["schema_version"] == "1.0.0"
    assert rec["retrieved_at"] == "2026-10-14T07:00:00Z"  # same instant, normalised to UTC
    assert rec["source_available_at"] == "2024-07-01T12:30:05.120000Z"
    assert rec["observed_at"] is None
    assert rec["source_flags"] == r["source_flags"]
    assert rec["value"]["basis"] == {"scale_factor": "1"}  # as stored; no defaults filled in
    assert rec["uncertainty"] == {
        "status": "unquantified",
        "kind": "unknown",
        "description": "WDI publishes no uncertainty",
        "artifact_sha256": None,
        "dependence_group": None,
        "method_version": None,
    }
    for f in ("source_id", "acquisition_id", "provenance_activity_id"):
        assert UUID(rec[f]) == r[f]


def test_missing_value_is_exported_as_typed_missing() -> None:
    rec = observation_record(row(value={"kind": "missing", "reason": "unavailable"}))
    assert rec["value"] == {"kind": "missing", "reason": "unavailable"}
    assert contract_errors(rec) == []


# --------------------------------------------------------------------------- uncertainty
def test_probability_kind_is_refused_explicitly() -> None:
    spec = UncertaintySpec(
        status="quantified",
        kind=UncertaintyKind.probability,
        description="p",
        method_version="m@1",
        artifact_sha256=SHA,
    )
    with pytest.raises(ObservationNotExportable, match="probability"):
        export_uncertainty(json.loads(spec.model_dump_json()))


def test_contract_documents_the_probability_restriction() -> None:
    from reva.evidence.export import observation_schema

    kind = observation_schema()["properties"]["uncertainty"]["properties"]["kind"]
    assert "probability" not in kind["enum"]
    assert "probability" in kind["description"] and "D-8" in kind["description"]
    assert {k.value for k in UncertaintyKind} - set(kind["enum"]) == {"probability"}


def test_quantified_distribution_with_artifact_is_exported() -> None:
    spec = UncertaintySpec(
        status="quantified",
        kind="distribution",
        description="posterior draws",
        method_version="m@1",
        artifact_sha256=SHA,
        dependence_group="wdi-family",
    )
    rec = observation_record(row(uncertainty=json.loads(spec.model_dump_json())))
    assert rec["uncertainty"]["artifact_sha256"] == SHA
    assert rec["uncertainty"]["dependence_group"] == "wdi-family"
    assert contract_errors(rec) == []


def test_exact_by_definition_is_exported() -> None:
    spec = UncertaintySpec.by_definition("count defined by statute")
    rec = observation_record(row(uncertainty=json.loads(spec.model_dump_json())))
    assert (rec["uncertainty"]["status"], rec["uncertainty"]["kind"]) == ("exact_by_definition", "definition")


@pytest.mark.parametrize(
    "spec",
    [
        # inline interval fields the v1 contract cannot carry
        UncertaintySpec(
            status="quantified",
            kind="confidence_interval",
            description="95% CI",
            method_version="m@1",
            artifact_sha256=SHA,
            level=0.95,
            lower="1",
            upper="2",
        ),
        UncertaintySpec(
            status="quantified",
            kind="bounds",
            description="b",
            method_version="m@1",
            artifact_sha256=SHA,
            lower="1",
            upper="2",
        ),
        UncertaintySpec.sensitivity("1", "2", "m@1", "assumption spread", conditioning=("elasticity",)),
        # quantified without the artifact the contract requires
        UncertaintySpec(status="quantified", kind="distribution", description="d", method_version="m@1"),
    ],
    ids=["ci_level_bounds", "inline_bounds", "sensitivity_conditioning", "quantified_no_artifact"],
)
def test_uncertainty_the_contract_cannot_express_is_refused(spec: UncertaintySpec) -> None:
    with pytest.raises(ObservationNotExportable):
        observation_record(row(uncertainty=json.loads(spec.model_dump_json())))


def test_invalid_stored_uncertainty_is_refused() -> None:
    bad = {"status": "unquantified", "kind": "confidence_interval", "description": "x"}
    with pytest.raises(ObservationNotExportable):
        observation_record(row(uncertainty=bad))
    with pytest.raises(ObservationNotExportable):
        observation_record(row(uncertainty={**bad, "kind": "unknown", "description": ""}))


# --------------------------------------------------------------------------- other refusals
@pytest.mark.parametrize(
    "value",
    [
        {"kind": "float", "value": 1.5, "unit_ref": "unit.usd", "basis": {}},
        {"kind": "category", "code": "A", "vocabulary_version": "v1"},
        {"kind": "decimal", "value": "1", "unit_ref": "unit.usd", "basis": {"bogus": 1}},
        {"kind": "missing", "reason": "zero"},
        {"kind": "missing"},
    ],
)
def test_values_outside_the_scalar_contract_are_refused(value: dict[str, Any]) -> None:
    with pytest.raises(ObservationNotExportable):
        observation_record(row(value=value))


@pytest.mark.parametrize(
    "over",
    [
        {"retrieved_at": datetime(2026, 10, 14, 9, 0)},  # naive: ambiguous instant
        {"retrieved_at": None},
        {"raw_selector": {"artifact_sha256": OTHER_SHA}},  # selector names another artifact
        {"raw_selector": {"path": [0]}},
        {"raw_sha256": "ABC"},
        {"availability_basis": None},  # available_at without its basis
        {"availability_basis": {}},
        {"logical_key": ""},
        {"source_flags": ["not", "an", "object"]},
    ],
)
def test_inconsistent_rows_are_refused(over: dict[str, Any]) -> None:
    with pytest.raises(ObservationNotExportable):
        observation_record(row(**over))


def test_rfc3339_output_is_checked_by_the_contract_validator() -> None:
    rec = observation_record(row())
    rec["retrieved_at"] = "2026-10-14 07:00:00"
    assert any("date-time" in e for e in contract_errors(rec))
    rec["retrieved_at"] = "2026-13-14T07:00:00Z"
    assert any("date-time" in e for e in contract_errors(rec))
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", observation_record(row())["retrieved_at"])
