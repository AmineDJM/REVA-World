"""Forecasting contracts shared by all annual univariate forecasters.

Conventions
-----------
* An :data:`Observation` is ``(year, value)``. Values may be ``int``, ``float`` or
  ``Decimal``; decimals are converted explicitly with ``float(Decimal)`` after a
  finiteness check. Booleans are rejected.
* ``fit`` sees only the training window it is given. All preprocessing (log
  transforms, differencing, parameter estimation) happens inside ``fit`` on that
  window; there are no global scalers or panel-wide statistics.
* ``horizon`` is measured in calendar years from the last training year
  (``target_year = origin_year + horizon``).
* Data problems that make a model inapplicable (too short, non-positive values
  for log models, gaps, non-finite values, failed fits) are reported as an
  :class:`IneligibleReport`, never silently repaired (no clipping, no imputation).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus

Observation = tuple[int, float]
"""``(year, value)``. ``value`` may also be a ``Decimal`` or ``int`` on input."""

IntervalKind = Literal["prediction_interval"]


@dataclass(frozen=True)
class Forecast:
    model_key: str
    model_version: str
    origin_year: int
    target_year: int
    horizon: int
    point: float
    interval: tuple[float, float] | None
    interval_kind: IntervalKind | None
    level: float | None
    uncertainty: UncertaintySpec
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.horizon < 1:
            raise ValueError("horizon must be >= 1")
        if self.target_year != self.origin_year + self.horizon:
            raise ValueError("target_year must equal origin_year + horizon")
        if not math.isfinite(self.point):
            raise ValueError("point forecast must be finite")
        if self.interval is None:
            if self.interval_kind is not None or self.level is not None:
                raise ValueError("interval_kind/level given without an interval")
            if self.uncertainty.status == UncertaintyStatus.quantified:
                raise ValueError("quantified uncertainty requires an interval")
        else:
            lo, hi = self.interval
            if not (math.isfinite(lo) and math.isfinite(hi)) or lo > hi:
                raise ValueError("interval bounds must be finite with lower <= upper")
            if self.interval_kind != "prediction_interval" or self.level is None:
                raise ValueError("an interval must be a prediction_interval with a level")
            if (
                self.uncertainty.status != UncertaintyStatus.quantified
                or self.uncertainty.kind != UncertaintyKind.prediction_interval
                or self.uncertainty.level != self.level
            ):
                raise ValueError("uncertainty must be a quantified prediction_interval")


@dataclass(frozen=True)
class IneligibleReport:
    model_key: str
    version: str
    reason: str
    n_train: int


@runtime_checkable
class Fitted(Protocol):
    @property
    def model_key(self) -> str: ...
    @property
    def model_version(self) -> str: ...
    @property
    def origin_year(self) -> int: ...

    def predict(self, horizons: Sequence[int], level: float = 0.8) -> list[Forecast]: ...


@runtime_checkable
class ForecastModel(Protocol):
    key: str
    version: str
    min_train: int

    def fit(self, train: Sequence[Observation]) -> Fitted | IneligibleReport: ...


@dataclass(frozen=True)
class PreparedSeries:
    years: tuple[int, ...]
    values: tuple[float, ...]
    problems: tuple[str, ...] = field(default=())

    @property
    def n(self) -> int:
        return len(self.years)

    @property
    def consecutive(self) -> bool:
        return all(b - a == 1 for a, b in itertools.pairwise(self.years))


def to_float(value: object) -> float:
    """Explicit numeric conversion; Decimal is converted via ``float()``."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a numeric observation")
    if isinstance(value, Decimal):
        if not value.is_finite():
            return math.nan
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"unsupported observation value type: {type(value).__name__}")


def prepare(train: Sequence[tuple[int, object]]) -> PreparedSeries:
    """Validate ordering and convert values. Raises on structural errors.

    Years must be strictly increasing integers (an unsorted or duplicated
    window is a caller bug). Non-finite values are recorded in ``problems`` so
    models can return an :class:`IneligibleReport`.
    """
    years: list[int] = []
    values: list[float] = []
    for obs in train:
        year, raw = obs
        if isinstance(year, bool) or not isinstance(year, int):
            raise TypeError("observation year must be an int")
        if years and year <= years[-1]:
            raise ValueError("training years must be strictly increasing")
        years.append(year)
        values.append(to_float(raw))
    problems: list[str] = []
    if any(not math.isfinite(v) for v in values):
        problems.append("non_finite_values")
    return PreparedSeries(tuple(years), tuple(values), tuple(problems))


def check_horizons(horizons: Sequence[int]) -> tuple[int, ...]:
    hs = tuple(horizons)
    for h in hs:
        if isinstance(h, bool) or not isinstance(h, int) or h < 1:
            raise ValueError(f"horizons must be positive integers, got {h!r}")
    return hs


def check_level(level: float) -> float:
    if not (0.0 < level < 1.0):
        raise ValueError("level must be in (0, 1)")
    return float(level)


def prediction_interval_spec(
    *,
    method_version: str,
    level: float,
    lower: float,
    upper: float,
    description: str,
    conditioning: tuple[str, ...] = (),
) -> UncertaintySpec:
    return UncertaintySpec(
        status=UncertaintyStatus.quantified,
        kind=UncertaintyKind.prediction_interval,
        description=description,
        method_version=method_version,
        level=level,
        lower=repr(lower),
        upper=repr(upper),
        conditioning=conditioning,
    )
