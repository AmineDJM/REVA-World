"""Export stored source observations as normalised observation records.

The published contract is ``contracts/observation.schema.json`` (schema_version
1.0.0, the binding appendix). :func:`export_observation` reads one row of
``evidence.observation`` (joined to its acquisition for ``retrieved_at`` and
``raw_sha256``) and maps the kernel/DB representation onto the contract
*faithfully*:

* nothing is invented: no defaults are filled in, values are not rounded,
  re-scaled or re-formatted, decimal strings are copied byte-for-byte;
* nothing is silently dropped: a stored representation the v1 contract cannot
  express raises :class:`ObservationNotExportable` instead of being truncated.

Representations that are refused (each is a typed, explicit refusal):

* value kinds other than ``decimal`` and ``missing`` (the scalar contract has no
  float, category, text, entity, array, distribution or geometry shape);
* decimal strings outside the contract grammar (``1E+5``, ``+1``, ``1_000``,
  ``.5``, ``NaN``): they are not rewritten, because rewriting changes the
  reported representation of the source value;
* uncertainty of kind ``probability``: observation records are source reports
  of a scalar quantity; probability of an *event* is a model-output type
  (forecast/belief modality), not a source-report uncertainty kind. A source
  distribution over the reported value is kind ``distribution``;
* uncertainty carrying kernel fields the contract has no place for (``level``,
  ``lower``, ``upper``, ``conditioning`` or any later field with a non-default
  value): exporting without them would lose the interval/assumption semantics;
* quantified uncertainty without a content-addressed artifact (the contract
  requires ``artifact_sha256`` for ``status=quantified``);
* any record that, after mapping, still fails the JSON Schema (defence in depth).

Timestamps are exported as RFC 3339 UTC strings (``...Z``); the instant is
unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, text

from reva.kernel.errors import RevaError
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.kernel.values import Missing, ScalarDecimal, parse_value

OBSERVATION_SCHEMA_VERSION = "1.0.0"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "contracts" / "observation.schema.json"

#: The decimal grammar of contracts/observation.schema.json (value.value).
DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
#: Uncertainty fields the v1 observation contract carries.
CONTRACT_UNCERTAINTY_FIELDS = (
    "status",
    "kind",
    "description",
    "artifact_sha256",
    "dependence_group",
    "method_version",
)
#: Kernel uncertainty kinds a source-report observation record may not carry.
NON_OBSERVATION_KINDS = frozenset({UncertaintyKind.probability})

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


class ObservationNotFound(RevaError):
    code = "observation_not_found"
    http_status = 404


class ObservationNotExportable(RevaError):
    """The stored observation cannot be expressed in the observation contract v1."""

    code = "observation_not_exportable"
    http_status = 422


_SELECT = text(
    """SELECT o.id, o.source_id, o.connector_version_id, o.acquisition_id, o.subject_id,
              o.concept_version_id, o.support_id, o.logical_key, o.source_revision_id, o.value,
              o.uncertainty, o.observed_at, o.source_published_at, o.source_available_at,
              o.availability_basis, o.source_flags, o.raw_selector, o.provenance_activity_id,
              a.retrieved_at, a.raw_sha256
         FROM evidence.observation o
         JOIN evidence.acquisition a
           ON a.id = o.acquisition_id
          AND a.source_id = o.source_id
          AND a.connector_version_id = o.connector_version_id
        WHERE o.id = :id"""
)


def export_observation(conn: Connection, observation_id: UUID | str) -> dict[str, Any]:
    """The contract record (observation.schema.json v1.0.0) for one stored observation."""
    oid = UUID(str(observation_id))
    row = conn.execute(_SELECT, {"id": oid}).mappings().one_or_none()
    if row is None:
        raise ObservationNotFound(f"observation {oid} does not exist", observation_id=str(oid))
    return observation_record(dict(row))


def observation_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map one joined observation/acquisition row to a validated contract record."""
    oid = str(row.get("id", "?"))

    def refuse(message: str, **details: Any) -> ObservationNotExportable:
        return ObservationNotExportable(message, observation_id=oid, **details)

    logical_key = row["logical_key"]
    if not isinstance(logical_key, str) or not logical_key:
        raise refuse("logical_key must be a non-empty string")
    srid = row["source_revision_id"]
    if srid is not None and not isinstance(srid, str):
        raise refuse("source_revision_id must be a string or null")
    raw_sha = row["raw_sha256"]
    if not isinstance(raw_sha, str) or not SHA256_PATTERN.match(raw_sha):
        raise refuse("acquisition raw_sha256 is not a lowercase hex SHA-256", raw_sha256=raw_sha)
    selector = _json_object(row["raw_selector"], "raw_selector", refuse)
    if selector.get("artifact_sha256") != raw_sha:
        raise refuse(
            "raw_selector.artifact_sha256 does not name the acquisition's raw artifact",
            selector_sha256=selector.get("artifact_sha256"),
            raw_sha256=raw_sha,
        )
    available_at = _timestamp(row["source_available_at"], "source_available_at", refuse)
    basis = row["availability_basis"]
    if basis is not None:
        basis = _json_object(basis, "availability_basis", refuse)
    if available_at is not None and not basis:
        raise refuse("source_available_at is set but availability_basis is empty")
    retrieved_at = _timestamp(row["retrieved_at"], "retrieved_at", refuse)
    if retrieved_at is None:
        raise refuse("acquisition retrieved_at is missing")

    record: dict[str, Any] = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "source_id": _uuid(row["source_id"]),
        "connector_version_id": _uuid(row["connector_version_id"]),
        "acquisition_id": _uuid(row["acquisition_id"]),
        "subject_id": _uuid(row["subject_id"]),
        "concept_version_id": _uuid(row["concept_version_id"]),
        "support_id": _uuid(row["support_id"]),
        "logical_key": logical_key,
        "source_revision_id": srid,
        "value": export_value(row["value"], refuse),
        "uncertainty": export_uncertainty(row["uncertainty"], refuse),
        "observed_at": _timestamp(row["observed_at"], "observed_at", refuse),
        "source_published_at": _timestamp(row["source_published_at"], "source_published_at", refuse),
        "source_available_at": available_at,
        "availability_basis": basis,
        "retrieved_at": retrieved_at,
        "source_flags": _json_object(row["source_flags"], "source_flags", refuse),
        "raw_selector": selector,
        "provenance_activity_id": _uuid(row["provenance_activity_id"]),
        "raw_sha256": raw_sha,
    }
    problems = contract_errors(record)
    if problems:
        raise refuse("exported record violates observation.schema.json", errors=problems[:10])
    return record


# --------------------------------------------------------------------------- value
def export_value(stored: Any, refuse: Any = None) -> dict[str, Any]:
    """Contract ``value``: decimal (exact string, unit, basis as stored) or typed missing."""
    refuse = refuse or _default_refuse
    if isinstance(stored, str):
        stored = _loads(stored, "value", refuse)
    if not isinstance(stored, Mapping):
        raise refuse("value is not a JSON object")
    try:
        parsed = parse_value(dict(stored))
    except ValidationError as e:
        raise refuse("stored value is not a valid kernel value", error=str(e)) from None
    if isinstance(parsed, Missing):
        return {"kind": "missing", "reason": parsed.reason.value}
    if isinstance(parsed, ScalarDecimal):
        if not DECIMAL_PATTERN.match(parsed.value):
            raise refuse(
                "decimal string is outside the contract grammar; it is not rewritten",
                value=parsed.value,
            )
        return {
            "kind": "decimal",
            "value": parsed.value,
            "unit_ref": parsed.unit_ref,
            "basis": _detach(stored["basis"]),
        }
    raise refuse(f"value kind {parsed.kind!r} has no representation in observation contract v1", kind=parsed.kind)


# --------------------------------------------------------------------------- uncertainty
def export_uncertainty(stored: Any, refuse: Any = None) -> dict[str, Any]:
    """Contract ``uncertainty``; refuses what v1 cannot carry instead of dropping it."""
    refuse = refuse or _default_refuse
    if isinstance(stored, str):
        stored = _loads(stored, "uncertainty", refuse)
    if not isinstance(stored, Mapping):
        raise refuse("uncertainty is not a JSON object")
    try:
        spec = UncertaintySpec.model_validate(dict(stored))
    except ValidationError as e:
        raise refuse("stored uncertainty is not a valid kernel UncertaintySpec", error=str(e)) from None
    if spec.kind in NON_OBSERVATION_KINDS:
        raise refuse(
            f"uncertainty kind {spec.kind.value!r} is a model-output type; observation records "
            "(source reports) do not carry it (observation.schema.json v1)",
            kind=spec.kind.value,
        )
    extra = {}
    for name, field in UncertaintySpec.model_fields.items():
        if name in CONTRACT_UNCERTAINTY_FIELDS:
            continue
        current = getattr(spec, name)
        if current != field.get_default(call_default_factory=True):
            extra[name] = current
    if extra:
        raise refuse(
            "uncertainty carries fields observation contract v1 cannot express; refusing to drop them",
            fields=sorted(extra),
        )
    if not spec.description:
        raise refuse("uncertainty description is empty")
    if spec.artifact_sha256 is not None and not SHA256_PATTERN.match(spec.artifact_sha256):
        raise refuse("uncertainty artifact_sha256 is not a lowercase hex SHA-256")
    if spec.status == UncertaintyStatus.quantified and spec.artifact_sha256 is None:
        raise refuse("quantified uncertainty must reference its artifact (artifact_sha256) in contract v1")
    return {
        "status": spec.status.value,
        "kind": spec.kind.value,
        "description": spec.description,
        "artifact_sha256": spec.artifact_sha256,
        "dependence_group": spec.dependence_group,
        "method_version": spec.method_version,
    }


# --------------------------------------------------------------------------- contract
@lru_cache(maxsize=1)
def observation_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema


def _format_checker() -> Any:
    from jsonschema import FormatChecker

    checker = FormatChecker()

    # jsonschema only checks date-time when the optional rfc3339-validator is
    # installed; enforce RFC 3339 here so the format is never silently ignored.
    @checker.checks("date-time", raises=ValueError)
    def _date_time(value: object) -> bool:
        if not isinstance(value, str):
            return True
        if not _RFC3339.match(value):
            return False
        datetime.fromisoformat(value.replace("z", "Z").replace("t", "T"))
        return True

    return checker


@lru_cache(maxsize=1)
def _validator() -> Any:
    from jsonschema import Draft202012Validator

    return Draft202012Validator(observation_schema(), format_checker=_format_checker())


def contract_errors(record: Any) -> list[str]:
    """JSON Schema (Draft 2020-12 + formats) violations of a record; empty when valid."""
    return [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in _validator().iter_errors(record)]


# --------------------------------------------------------------------------- helpers
def _default_refuse(message: str, **details: Any) -> ObservationNotExportable:
    return ObservationNotExportable(message, **details)


def _uuid(v: Any) -> str:
    return str(UUID(str(v)))


def _loads(raw: str, what: str, refuse: Any) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        raise refuse(f"{what} is not valid JSON") from None


def _detach(obj: Any) -> Any:
    """A JSON-only deep copy (rejects NaN/Infinity, which JSON cannot carry)."""
    return json.loads(json.dumps(obj, allow_nan=False))


def _json_object(v: Any, what: str, refuse: Any) -> dict[str, Any]:
    if isinstance(v, str):
        v = _loads(v, what, refuse)
    if not isinstance(v, Mapping):
        raise refuse(f"{what} must be a JSON object")
    try:
        out: dict[str, Any] = _detach(dict(v))
    except (TypeError, ValueError):
        raise refuse(f"{what} is not JSON-serialisable") from None
    return out


def _timestamp(v: Any, what: str, refuse: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, datetime):
        raise refuse(f"{what} is not a timestamp")
    if v.tzinfo is None or v.utcoffset() is None:
        raise refuse(f"{what} has no time zone; the instant is ambiguous")
    return v.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"
