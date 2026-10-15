"""Temporal extents.

Modern computational timestamps (UTC ``datetime``) are kept separate from
statistical/historical extents (``TimeExtent``) and deep-time extents
(``DeepTimeExtent``). Unknown endpoints remain unknown; a publication time is
never inferred from a reference period.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class Precision(StrEnum):
    year = "year"
    month = "month"
    day = "day"
    instant = "instant"


class Calendar(StrEnum):
    gregorian = "gregorian"
    proleptic_gregorian = "proleptic_gregorian"
    julian = "julian"


class UncertainBound(BaseModel):
    """A bound that may be uncertain: earliest/latest admissible values."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    earliest: date
    latest: date

    @model_validator(mode="after")
    def _order(self) -> UncertainBound:
        if self.latest < self.earliest:
            raise ValueError("uncertain bound latest < earliest")
        return self


class TimeExtent(BaseModel):
    """A half-open interval [start, end) or an instant on a named calendar."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["interval", "instant"]
    calendar: Calendar = Calendar.gregorian
    precision: Precision
    start: date | None
    end: date | None = None
    start_uncertainty: UncertainBound | None = None
    end_uncertainty: UncertainBound | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _check(self) -> TimeExtent:
        if self.kind == "instant":
            if self.start is None:
                raise ValueError("instant requires start")
            if self.end is not None:
                raise ValueError("instant must not have end")
        else:
            if self.start is None and self.start_uncertainty is None:
                raise ValueError("interval requires a start or an uncertain start")
            if self.start is not None and self.end is not None and self.end <= self.start:
                raise ValueError("interval end must be after start (half-open)")
        if self.start is not None and self.start_uncertainty is not None and not (
            self.start_uncertainty.earliest <= self.start <= self.start_uncertainty.latest
        ):
            raise ValueError("start lies outside its own uncertain bound")
        if self.end is not None and self.end_uncertainty is not None and not (
            self.end_uncertainty.earliest <= self.end <= self.end_uncertainty.latest
        ):
            raise ValueError("end lies outside its own uncertain bound")
        if self.kind == "instant" and self.end_uncertainty is not None:
            raise ValueError("instant must not have an end bound")
        # R7-K4: the interval must be non-empty for at least one admissible (start, end) pair,
        # i.e. the latest admissible end must lie after the earliest admissible start (half-open).
        # An uncertain bound widens the admissible set; an unknown endpoint (None) leaves it open.
        earliest_start = (
            self.start_uncertainty.earliest if self.start_uncertainty is not None else self.start
        )
        latest_end = self.end_uncertainty.latest if self.end_uncertainty is not None else self.end
        if earliest_start is not None and latest_end is not None and latest_end <= earliest_start:
            raise ValueError(
                "interval end must be after start (half-open): every admissible end precedes "
                "or equals every admissible start"
            )
        return self

    @classmethod
    def calendar_year(cls, year: int) -> TimeExtent:
        return cls(
            kind="interval",
            precision=Precision.year,
            start=date(year, 1, 1),
            end=date(year + 1, 1, 1),
            label=str(year),
        )

    @classmethod
    def on_date(cls, d: date, label: str | None = None) -> TimeExtent:
        return cls(kind="instant", precision=Precision.day, start=d, label=label)

    def reference_year(self) -> int | None:
        return self.start.year if self.start is not None else None


class DeepTimeExtent(BaseModel):
    """Geological/astronomical extents: numeric offsets from a named epoch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    epoch: str  # e.g. "BP1950", "J2000"
    unit: Literal["year", "kiloyear", "megayear", "julian_day"]
    start_offset: float
    end_offset: float | None = None
    scale_reference: str  # e.g. "ICS-2023/09" chronostratigraphic chart version


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_aware(ts: datetime | None, field: str) -> datetime | None:
    if ts is not None and ts.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    return ts
