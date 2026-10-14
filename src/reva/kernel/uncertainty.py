"""Uncertainty contract. Unknown uncertainty is valid and never silently zero.

Interval kinds are distinct types: a sensitivity range is not a confidence
interval and a prediction interval is not a credible interval.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class UncertaintyStatus(StrEnum):
    unquantified = "unquantified"
    exact_by_definition = "exact_by_definition"
    quantified = "quantified"


class UncertaintyKind(StrEnum):
    unknown = "unknown"
    definition = "definition"
    distribution = "distribution"
    bounds = "bounds"
    confidence_interval = "confidence_interval"
    credible_interval = "credible_interval"
    prediction_interval = "prediction_interval"
    sensitivity_range = "sensitivity_range"
    probability = "probability"


INTERVAL_KINDS = frozenset(
    {
        UncertaintyKind.confidence_interval,
        UncertaintyKind.credible_interval,
        UncertaintyKind.prediction_interval,
    }
)

PROBABILISTIC_KINDS = frozenset(
    {
        UncertaintyKind.distribution,
        UncertaintyKind.confidence_interval,
        UncertaintyKind.credible_interval,
        UncertaintyKind.prediction_interval,
        UncertaintyKind.probability,
    }
)


def _dec(v: str | None, name: str) -> object:
    from decimal import Decimal, InvalidOperation

    if v is None:
        return None
    try:
        d = Decimal(v)
    except InvalidOperation as e:
        raise ValueError(f"{name} must be a decimal string") from e
    if not d.is_finite():
        raise ValueError(f"{name} must be finite")
    return d


class UncertaintySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: UncertaintyStatus
    kind: UncertaintyKind
    description: str
    artifact_sha256: str | None = None
    dependence_group: str | None = None
    method_version: str | None = None
    level: float | None = None  # nominal coverage for interval kinds only
    lower: str | None = None  # decimal strings
    upper: str | None = None
    conditioning: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> UncertaintySpec:
        if self.status == UncertaintyStatus.unquantified and self.kind != UncertaintyKind.unknown:
            raise ValueError("unquantified uncertainty must have kind=unknown")
        if (
            self.status == UncertaintyStatus.exact_by_definition
            and self.kind != UncertaintyKind.definition
        ):
            raise ValueError("exact_by_definition must have kind=definition")
        if self.status == UncertaintyStatus.quantified:
            if self.kind in (UncertaintyKind.unknown, UncertaintyKind.definition):
                raise ValueError("quantified uncertainty needs a concrete kind")
            if not self.method_version:
                raise ValueError("quantified uncertainty requires method_version")
        if self.kind == UncertaintyKind.sensitivity_range and self.level is not None:
            raise ValueError("a sensitivity range has no probabilistic coverage level")
        if self.kind == UncertaintyKind.bounds and self.level is not None:
            raise ValueError("bounds have no probabilistic coverage level")
        if self.level is not None and not (0.0 < self.level < 1.0):
            raise ValueError("level must be in (0, 1)")
        if self.level is not None and self.kind not in INTERVAL_KINDS:
            raise ValueError(f"a nominal level is only meaningful for interval kinds, not {self.kind}")
        lo = _dec(self.lower, "lower")
        hi = _dec(self.upper, "upper")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("lower bound exceeds upper bound")
        if self.kind in (
            UncertaintyKind.confidence_interval,
            UncertaintyKind.credible_interval,
            UncertaintyKind.prediction_interval,
        ) and self.level is None:
            raise ValueError(f"{self.kind} requires a nominal level")
        return self

    @property
    def is_probabilistic(self) -> bool:
        return self.kind in PROBABILISTIC_KINDS

    @classmethod
    def unknown(cls, description: str = "Source reports no uncertainty") -> UncertaintySpec:
        return cls(
            status=UncertaintyStatus.unquantified,
            kind=UncertaintyKind.unknown,
            description=description,
        )

    @classmethod
    def by_definition(cls, description: str) -> UncertaintySpec:
        return cls(
            status=UncertaintyStatus.exact_by_definition,
            kind=UncertaintyKind.definition,
            description=description,
        )

    @classmethod
    def sensitivity(
        cls, lower: str, upper: str, method_version: str, description: str
    ) -> UncertaintySpec:
        return cls(
            status=UncertaintyStatus.quantified,
            kind=UncertaintyKind.sensitivity_range,
            description=description,
            method_version=method_version,
            lower=lower,
            upper=upper,
        )
