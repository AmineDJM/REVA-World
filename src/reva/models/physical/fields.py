"""P8 physical-field and asset port contracts (interfaces only).

These typed descriptors and protocols fix the semantics a future CF/COG/Zarr
field adapter and STAC-style asset catalogue must supply: CF standard name and
units, CRS, CF calendar, named axes, cell support and source kind. No reader,
catalogue or field store is implemented in this build; the reserved entry
points return/raise ``capability_not_available`` for ``physical.field_adapter``.

This module has no DB/HTTP imports (import-linter contract for ``reva.models``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from reva.capabilities import CapabilityNotAvailable, not_available, require

# CF-1.x calendar attribute values (CF conventions §4.4.1). "standard" == "gregorian" (mixed Julian/Gregorian).
CFCalendar = Literal[
    "standard", "gregorian", "proleptic_gregorian", "julian", "noleap", "365_day", "all_leap", "366_day",
    "360_day", "none",
]
AxisKind = Literal["latitude", "longitude", "time", "vertical", "projection_x", "projection_y", "ensemble", "other"]
CellSupport = Literal["point", "cell_mean", "cell_total", "cell_max", "cell_min"]
SourceKind = Literal["observation", "reanalysis", "forecast", "projection", "analytic_verification"]
AssetFormat = Literal["netcdf", "zarr", "cog", "grib2", "parquet"]


@dataclass(frozen=True)
class CRSSpec:
    """Coordinate reference system of a field.

    ``identifier`` is an authority code (``OGC:CRS84``, ``EPSG:4326``, ``EPSG:3035``…).
    ``axis_order`` is explicit because EPSG:4326 is lat/lon while CRS84 is lon/lat.
    """

    identifier: str
    axis_order: tuple[str, ...]
    datum: str | None = None
    longitude_convention: Literal["-180..180", "0..360", "not_applicable"] = "-180..180"
    ellipsoid_or_sphere: str | None = None

    def __post_init__(self) -> None:
        if ":" not in self.identifier:
            raise ValueError("CRS identifier must be an authority code such as 'OGC:CRS84'")
        if not self.axis_order:
            raise ValueError("CRS axis order must be explicit")

    @property
    def is_geographic(self) -> bool:
        return self.identifier.upper() in {"OGC:CRS84", "EPSG:4326", "EPSG:4979", "OGC:CRS84H"}


@dataclass(frozen=True)
class AxisSpec:
    name: str
    kind: AxisKind
    units: str
    size: int
    standard_name: str | None = None
    positive: Literal["up", "down"] | None = None
    bounds_variable: str | None = None
    """CF ``bounds``: cell edges are required for conservative aggregation."""

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError(f"axis {self.name!r} must have size >= 1")
        if not self.units:
            raise ValueError(f"axis {self.name!r} needs explicit units")


@dataclass(frozen=True)
class FieldMetadata:
    """CF-style description of one gridded variable. Units are never inferred from names."""

    variable: str
    standard_name: str | None
    long_name: str | None
    units: str
    crs: CRSSpec
    calendar: CFCalendar
    axes: tuple[AxisSpec, ...]
    support: CellSupport
    cell_methods: str | None
    source_kind: SourceKind
    conventions: str = "CF-1.11"
    includes_later_information: bool = False
    """True for reanalyses: they assimilate observations after the valid time and are not a
    real-time forecast vintage (architecture §26)."""
    fill_value: float | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.units:
            raise ValueError("field units must be explicit")
        names = [a.name for a in self.axes]
        if len(set(names)) != len(names):
            raise ValueError("axis names must be unique")
        if self.fill_value is not None and math.isinf(self.fill_value):
            raise ValueError("an infinite fill value is not allowed; use NaN or a finite sentinel")
        if self.source_kind == "reanalysis" and not self.includes_later_information:
            raise ValueError("a reanalysis includes later information; declare includes_later_information=True")
        if self.support in ("cell_mean", "cell_total") and self.cell_methods is None:
            raise ValueError("cell_mean/cell_total support requires CF cell_methods")

    def axis(self, kind: AxisKind) -> AxisSpec | None:
        return next((a for a in self.axes if a.kind == kind), None)


@dataclass(frozen=True)
class FieldAsset:
    """An immutable stored field asset (STAC-item-like)."""

    asset_id: str
    format: AssetFormat
    media_type: str
    sha256: str
    metadata: FieldMetadata
    licence: str | None
    provider: str
    stac_item: Mapping[str, object] | None = None


@dataclass(frozen=True)
class FieldWindow:
    """A bounded read request; ``bbox`` is (west, south, east, north) in the field CRS."""

    bbox: tuple[float, float, float, float] | None
    time_start: str | None
    time_end: str | None
    variables: tuple[str, ...]


@runtime_checkable
class FieldCatalogue(Protocol):
    """STAC-like discovery of field assets."""

    def search(self, *, standard_name: str, bbox: tuple[float, float, float, float] | None,
               time_range: tuple[str, str] | None) -> Sequence[FieldAsset]: ...

    def describe(self, asset_id: str) -> FieldAsset: ...


@runtime_checkable
class FieldReader(Protocol):
    """Reads a bounded window of an asset as named numpy arrays plus validated metadata."""

    def metadata(self, asset: FieldAsset) -> FieldMetadata: ...

    def read(self, asset: FieldAsset, window: FieldWindow) -> Mapping[str, object]: ...


def field_adapter_not_available() -> CapabilityNotAvailable:
    """Typed result for any request to read or catalogue a real field in this build."""
    gap = not_available("physical.field_adapter")
    assert gap is not None, "physical.field_adapter must not be marked implemented without a reader"
    return gap


def open_field(asset: FieldAsset) -> FieldReader:
    """Reserved entry point: raises ``CapabilityGap`` (physical.field_adapter)."""
    require("physical.field_adapter", asset_id=asset.asset_id, format=asset.format)
    raise AssertionError("unreachable: physical.field_adapter is not implemented")  # pragma: no cover
