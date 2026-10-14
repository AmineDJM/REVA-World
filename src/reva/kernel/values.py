"""Value algebra: a discriminated union with typed missingness.

Null is never overloaded. A Missing value carries a reason and can never be
used as a number. NaN/Infinity are forbidden. Decimal values travel as strings
so JSON round-trips preserve precision.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from reva.kernel.errors import MissingInput


class MissingReason(StrEnum):
    not_collected = "not_collected"
    unavailable = "unavailable"
    not_applicable = "not_applicable"
    suppressed = "suppressed"
    source_error = "source_error"
    unmatched_semantics = "unmatched_semantics"
    outside_support = "outside_support"


class MeasurementBasis(BaseModel):
    """Explicit valuation/measurement basis. Required for currency values."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    currency: str | None = None  # ISO 4217
    price_basis: Literal["nominal", "real", "ppp"] | None = None
    price_reference_year: int | None = None
    exchange_rate_transform: str | None = None  # e.g. "provider_converted_to_usd"
    valuation: str | None = None  # e.g. "CIF", "FOB", "primaryValue"
    scale_factor: str = "1"  # multiplier already applied to the value
    notes: str | None = None


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScalarDecimal(_Base):
    kind: Literal["decimal"] = "decimal"
    value: str
    unit_ref: str
    basis: MeasurementBasis

    @field_validator("value")
    @classmethod
    def _decimal(cls, v: str) -> str:
        try:
            d = Decimal(v)
        except InvalidOperation as e:
            raise ValueError(f"not a decimal string: {v!r}") from e
        if not d.is_finite():
            raise ValueError("non-finite decimal forbidden")
        return v

    def as_decimal(self) -> Decimal:
        return Decimal(self.value)


class ScalarFloat(_Base):
    kind: Literal["float"] = "float"
    value: float
    unit_ref: str
    basis: MeasurementBasis

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("NaN/Infinity forbidden")
        return v


class Category(_Base):
    kind: Literal["category"] = "category"
    code: str
    vocabulary_version: str


class Text(_Base):
    kind: Literal["text"] = "text"
    language_tag: str
    text: str | None = None
    artifact_sha256: str | None = None


class EntityRefValue(_Base):
    kind: Literal["entity"] = "entity"
    entity_id: UUID


class ArrayValue(_Base):
    kind: Literal["array"] = "array"
    artifact_sha256: str
    dtype: str
    shape: tuple[int, ...]
    axes: tuple[str, ...]
    unit_ref: str


class DistributionValue(_Base):
    kind: Literal["distribution"] = "distribution"
    uncertainty_ref: str


class GeometryValue(_Base):
    kind: Literal["geometry"] = "geometry"
    spatial_support_ref: str


class Missing(_Base):
    kind: Literal["missing"] = "missing"
    reason: MissingReason


Value = Annotated[
    ScalarDecimal
    | ScalarFloat
    | Category
    | Text
    | EntityRefValue
    | ArrayValue
    | DistributionValue
    | GeometryValue
    | Missing,
    Field(discriminator="kind"),
]

VALUE_ADAPTER: TypeAdapter[Any] = TypeAdapter(Value)


def parse_value(data: Any) -> Any:
    return VALUE_ADAPTER.validate_python(data)


def require_number(value: Any, what: str) -> Decimal:
    """Extract a Decimal; missing values raise instead of becoming zero."""
    if isinstance(value, Missing):
        raise MissingInput(f"{what} is missing ({value.reason})", input=what, reason=value.reason)
    if isinstance(value, ScalarDecimal):
        return value.as_decimal()
    if isinstance(value, ScalarFloat):
        return Decimal(repr(value.value))
    raise MissingInput(f"{what} is not a numeric value", input=what)


def decimal_str(d: Decimal) -> str:
    """Plain (non-exponent) decimal string with no trailing noise."""
    if not d.is_finite():
        raise ValueError("non-finite decimal")
    import decimal

    ctx = decimal.Context(prec=max(28, len(d.as_tuple().digits)))
    s = format(d.normalize(ctx), "f")
    return "0" if s in ("-0", "") else s
