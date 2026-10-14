"""VERIFICATION-ONLY conservative aggregation of analytic fields on a sphere.

This is the numerical core needed for the P8 acceptance gate "known analytic
field integrates correctly", made available in V0 as a test utility
(capability ``verification.analytic_field_aggregation``). It is **not** a
weather, climate or hazard capability: it reads no dataset, supports only a
regular latitude-longitude grid on a sphere and rectangular (lat/lon-bounded)
regions, and its results are labelled ``verification_only``.

Method (``conservative_area_weighted_spherical_v1``)
---------------------------------------------------
Grid cell values are cell means with respect to area. The exact spherical area
of the intersection of the cell [φ1, φ2] × [λ1, λ2] with a region
[φs, φn] × [λw, λe] is

    A = R² · Δλ_overlap · (sin min(φ2, φn) − sin max(φ1, φs)),

with Δλ_overlap measured in radians on the circle (so a region that crosses the
antimeridian, e.g. west=170°, east=−170°, is one 20° interval). The region
integral is Σ v·A and the region mean is Σ v·A / Σ A. Because overlap areas of
a partition of the sphere sum to the cell areas, the global integral is
conserved exactly up to floating-point rounding. When region edges coincide
with cell edges and the cell values are exact cell means, the region integral
of the analytic field is exact; otherwise sub-cell variation produces an
interpolation error that this helper does not estimate.

The default radius is the IUGG mean Earth radius (6 371 008.8 m); the sphere is
an approximation of the ellipsoid and the choice is part of the method.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from reva.kernel.errors import CapabilityGap, ValidationProblem

METHOD = "conservative_area_weighted_spherical_v1"
MEAN_EARTH_RADIUS_M = 6_371_008.8
SUPPORTED_CRS = ("OGC:CRS84", "EPSG:4326")
_TOL_DEG = 1e-9

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class RegularLatLonGrid:
    """Cell edges in degrees. Latitude ascending within [-90, 90]; longitude ascending,
    spanning at most 360° (``-180..180``, ``0..360`` or regional)."""

    lat_edges: tuple[float, ...]
    lon_edges: tuple[float, ...]
    crs: str = "OGC:CRS84"
    radius_m: float = MEAN_EARTH_RADIUS_M

    def __post_init__(self) -> None:
        if self.crs.upper() not in SUPPORTED_CRS:
            raise CapabilityGap(
                f"verification aggregation supports geographic CRS {SUPPORTED_CRS} only; "
                f"reprojection of {self.crs!r} is a reserved P8 capability",
                capability="physical.field_to_region", crs=self.crs,
            )
        lat = np.asarray(self.lat_edges, dtype=np.float64)
        lon = np.asarray(self.lon_edges, dtype=np.float64)
        if lat.ndim != 1 or lat.size < 2 or lon.ndim != 1 or lon.size < 2:
            raise ValidationProblem("grid needs at least two latitude and two longitude edges")
        if not (np.all(np.isfinite(lat)) and np.all(np.isfinite(lon))):
            raise ValidationProblem("grid edges must be finite")
        if np.any(np.diff(lat) <= 0) or np.any(np.diff(lon) <= 0):
            raise ValidationProblem("grid edges must be strictly ascending")
        if lat[0] < -90 - _TOL_DEG or lat[-1] > 90 + _TOL_DEG:
            raise ValidationProblem("latitude edges must lie within [-90, 90]")
        if lon[-1] - lon[0] > 360 + _TOL_DEG:
            raise ValidationProblem("longitude edges may span at most 360 degrees")
        if not (math.isfinite(self.radius_m) and self.radius_m > 0):
            raise ValidationProblem("sphere radius must be positive and finite")

    @classmethod
    def global_regular(cls, n_lat: int, n_lon: int, *, lon0: float = -180.0,
                       radius_m: float = MEAN_EARTH_RADIUS_M) -> RegularLatLonGrid:
        if n_lat < 1 or n_lon < 1:
            raise ValidationProblem("grid sizes must be positive")
        lat = np.linspace(-90.0, 90.0, n_lat + 1)
        lon = np.linspace(lon0, lon0 + 360.0, n_lon + 1)
        return cls(tuple(float(x) for x in lat), tuple(float(x) for x in lon), radius_m=radius_m)

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.lat_edges) - 1, len(self.lon_edges) - 1)

    @property
    def is_global_longitude(self) -> bool:
        return abs((self.lon_edges[-1] - self.lon_edges[0]) - 360.0) <= _TOL_DEG

    def cell_areas(self) -> FloatArray:
        """Exact spherical cell areas in m², shape (n_lat, n_lon)."""
        sin_lat = np.sin(np.radians(np.asarray(self.lat_edges, dtype=np.float64)))
        dlon = np.radians(np.diff(np.asarray(self.lon_edges, dtype=np.float64)))
        return self.radius_m**2 * np.outer(np.diff(sin_lat), dlon)


@dataclass(frozen=True)
class LatLonRegion:
    """Rectangular region bounded by parallels and meridians, in degrees.

    ``west > east`` (after normalisation to the circle) means the region crosses the
    antimeridian. ``west == east`` is rejected as ambiguous; a full longitude band is
    written ``west=-180, east=180`` (or any pair exactly 360° apart).
    """

    region_id: str
    south: float
    north: float
    west: float
    east: float

    def __post_init__(self) -> None:
        vals = (self.south, self.north, self.west, self.east)
        if not all(math.isfinite(v) for v in vals):
            raise ValidationProblem(f"region {self.region_id!r} bounds must be finite")
        if not (-90 <= self.south < self.north <= 90):
            raise ValidationProblem(f"region {self.region_id!r} needs -90 <= south < north <= 90")
        if self.west == self.east:
            raise ValidationProblem(f"region {self.region_id!r} has zero or ambiguous longitude width")

    @property
    def lon_width_deg(self) -> float:
        width = (self.east - self.west) % 360.0
        return 360.0 if width == 0.0 else width

    @property
    def crosses_antimeridian(self) -> bool:
        w = ((self.west + 180.0) % 360.0) - 180.0
        return w + self.lon_width_deg > 180.0 + _TOL_DEG

    def area_m2(self, radius_m: float = MEAN_EARTH_RADIUS_M) -> float:
        return radius_m**2 * math.radians(self.lon_width_deg) * (
            math.sin(math.radians(self.north)) - math.sin(math.radians(self.south))
        )


@dataclass(frozen=True)
class RegionAggregate:
    region_id: str
    region_area_m2: float
    covered_area_m2: float
    """Area of the region covered by grid cells."""
    valid_area_m2: float
    """Covered area whose cell values are not missing (NaN)."""
    integral: float | None
    """Σ value·area in (field unit)·m²; ``None`` unless the region is completely and validly covered."""
    mean: float | None
    """Area-weighted mean over the valid covered area; ``None`` if nothing valid is covered."""
    coverage_fraction: float
    complete: bool
    crosses_antimeridian: bool


@dataclass(frozen=True)
class AggregationReport:
    method: str
    verification_only: Literal[True]
    label: str
    radius_m: float
    grid_shape: tuple[int, int]
    grid_integral: float | None
    regions: tuple[RegionAggregate, ...]

    def by_id(self, region_id: str) -> RegionAggregate:
        return next(r for r in self.regions if r.region_id == region_id)


def _lon_overlap_deg(cell_w: FloatArray, cell_e: FloatArray, w: float, width: float, lon0: float) -> FloatArray:
    """Overlap (degrees) of each cell [cell_w, cell_e] with the circular interval [w, w+width]."""
    start = lon0 + ((w - lon0) % 360.0)
    end = start + width
    total = np.zeros_like(cell_w)
    for k in (-1, 0, 1):
        lo = np.maximum(cell_w, start + 360.0 * k)
        hi = np.minimum(cell_e, end + 360.0 * k)
        total += np.clip(hi - lo, 0.0, None)
    return total


def overlap_areas(grid: RegularLatLonGrid, region: LatLonRegion) -> FloatArray:
    """Exact spherical intersection areas (m²) of every cell with ``region``; shape (n_lat, n_lon)."""
    lat = np.asarray(grid.lat_edges, dtype=np.float64)
    lon = np.asarray(grid.lon_edges, dtype=np.float64)
    lo = np.maximum(lat[:-1], region.south)
    hi = np.minimum(lat[1:], region.north)
    dsin = np.where(hi > lo, np.sin(np.radians(hi)) - np.sin(np.radians(lo)), 0.0)
    dlon = _lon_overlap_deg(lon[:-1], lon[1:], region.west, region.lon_width_deg, float(lon[0]))
    return grid.radius_m**2 * np.outer(dsin, np.radians(dlon))


def aggregate_to_regions(
    grid: RegularLatLonGrid,
    values: FloatArray | Sequence[Sequence[float]],
    regions: Sequence[LatLonRegion],
    *,
    nan_policy: Literal["reject", "exclude"] = "reject",
    label: str = "analytic verification",
) -> AggregationReport:
    """Conservative area-weighted aggregation of cell-mean ``values`` to rectangular regions.

    VERIFICATION ONLY — see module docstring.
    """
    v = np.asarray(values, dtype=np.float64)
    if v.shape != grid.shape:
        raise ValidationProblem(f"values shape {v.shape} does not match grid shape {grid.shape}")
    if np.any(np.isinf(v)):
        raise ValidationProblem("field values must be finite or NaN (missing)")
    missing = np.isnan(v)
    if missing.any() and nan_policy == "reject":
        raise ValidationProblem("field has missing (NaN) cells; pass nan_policy='exclude' to aggregate valid area")
    ids = [r.region_id for r in regions]
    if len(set(ids)) != len(ids):
        raise ValidationProblem("region ids must be unique")

    filled = np.where(missing, 0.0, v)
    cell_area = grid.cell_areas()
    grid_integral = None if missing.any() else float(np.sum(filled * cell_area))

    out: list[RegionAggregate] = []
    for region in regions:
        ov = overlap_areas(grid, region)
        region_area = region.area_m2(grid.radius_m)
        covered = float(ov.sum())
        valid_w = np.where(missing, 0.0, ov)
        valid = float(valid_w.sum())
        weighted = float(np.sum(filled * valid_w))
        coverage = covered / region_area if region_area > 0 else 0.0
        complete = abs(coverage - 1.0) <= 1e-9 and valid == covered
        out.append(
            RegionAggregate(
                region_id=region.region_id,
                region_area_m2=region_area,
                covered_area_m2=covered,
                valid_area_m2=valid,
                integral=weighted if complete else None,
                mean=weighted / valid if valid > 0 else None,
                coverage_fraction=coverage,
                complete=complete,
                crosses_antimeridian=region.crosses_antimeridian,
            )
        )
    return AggregationReport(
        method=METHOD, verification_only=True, label=label, radius_m=grid.radius_m,
        grid_shape=grid.shape, grid_integral=grid_integral, regions=tuple(out),
    )


def separable_cell_means(
    grid: RegularLatLonGrid,
    *,
    lat_primitive: Callable[[float], float] | None = None,
    lon_primitive: Callable[[float], float] | None = None,
) -> FloatArray:
    """Exact area-weighted cell means of a separable analytic field f(φ, λ) = g(φ)·h(λ).

    ``lat_primitive(φ)`` is an antiderivative of g(φ)·cos φ and ``lon_primitive(λ)`` an
    antiderivative of h(λ), both with arguments in **radians**. ``None`` means the factor
    is 1. The cell mean is [G(φ2) − G(φ1)] / (sin φ2 − sin φ1) · [H(λ2) − H(λ1)] / (λ2 − λ1).
    """
    lat = np.radians(np.asarray(grid.lat_edges, dtype=np.float64))
    lon = np.radians(np.asarray(grid.lon_edges, dtype=np.float64))
    if lat_primitive is None:
        g = np.ones(lat.size - 1)
    else:
        gp = np.array([lat_primitive(float(x)) for x in lat])
        g = np.diff(gp) / np.diff(np.sin(lat))
    if lon_primitive is None:
        h = np.ones(lon.size - 1)
    else:
        hp = np.array([lon_primitive(float(x)) for x in lon])
        h = np.diff(hp) / np.diff(lon)
    return np.outer(g, h)


def separable_region_integral(
    region: LatLonRegion,
    *,
    lat_primitive: Callable[[float], float] | None = None,
    lon_primitive: Callable[[float], float] | None = None,
    radius_m: float = MEAN_EARTH_RADIUS_M,
) -> float:
    """Closed-form ∬ g(φ)h(λ) dA over a rectangular region (antimeridian-aware), in (unit)·m².

    ``lon_primitive`` must be the antiderivative of a 2π-periodic h when the region crosses
    the antimeridian; the interval is evaluated on the unwrapped longitude [west, west+width].
    """
    s, n = math.radians(region.south), math.radians(region.north)
    w = math.radians(region.west)
    e = w + math.radians(region.lon_width_deg)
    lat_part = (lat_primitive(n) - lat_primitive(s)) if lat_primitive else (math.sin(n) - math.sin(s))
    lon_part = (lon_primitive(e) - lon_primitive(w)) if lon_primitive else (e - w)
    return radius_m**2 * lat_part * lon_part
