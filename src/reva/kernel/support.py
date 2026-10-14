"""Supports: where, when, over whom and at what scale a statement applies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from reva.kernel.ids import logical_hash
from reva.kernel.time import TimeExtent

Aggregation = Literal[
    "point_in_time_stock",  # e.g. population on 1 January
    "period_average_stock",  # e.g. mid-year / annual-average population
    "period_flow_total",  # e.g. annual import value
    "period_rate",
    "category_cell",
    "not_applicable",
]


class SpatialRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scheme: str  # e.g. "provider_reporter_territory", "naturalearth_display"
    key: str  # provider key, e.g. "WB:FRA", "COMTRADE:251"
    authority: str
    note: str | None = None


class PopulationRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    universe: str  # e.g. "de_facto_resident_population", "economy", "not_applicable"
    definition_source: str | None = None


class Support(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    temporal: TimeExtent
    spatial: SpatialRef
    population: PopulationRef
    aggregation: Aggregation
    resolution: dict[str, str] = {}

    def digest(self) -> str:
        return logical_hash(self.model_dump(mode="json"))

    def comparable_with(self, other: Support) -> bool:
        """Strict support equality for aggregation/comparison purposes."""
        return (
            self.aggregation == other.aggregation
            and self.population.universe == other.population.universe
            and self.temporal.kind == other.temporal.kind
            and self.temporal.start == other.temporal.start
            and self.temporal.end == other.temporal.end
        )
