"""P8 verification helpers: analytic conservative aggregation and BMI lifecycle conformance."""

from __future__ import annotations

import math

import numpy as np
import pytest

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.models.physical import adapter, fields
from reva.models.physical.verification import (
    MEAN_EARTH_RADIUS_M,
    LatLonRegion,
    RegularLatLonGrid,
    aggregate_to_regions,
    overlap_areas,
    separable_cell_means,
    separable_region_integral,
)

R = MEAN_EARTH_RADIUS_M
A, B = 288.15, -40.0  # f(φ) = A + Bφ, φ in radians (temperature-like magnitude; analytic only)


def lin_lat_primitive(phi: float) -> float:
    """Antiderivative of (A + Bφ)·cos φ."""
    return A * math.sin(phi) + B * (phi * math.sin(phi) + math.cos(phi))


def cos_lon_primitive(lam: float) -> float:
    """Antiderivative of h(λ) = 2 + cos λ (2π-periodic)."""
    return 2.0 * lam + math.sin(lam)


def rel(a: float, b: float) -> float:
    return abs(a - b) / abs(b)


def test_cell_areas_sum_to_sphere():
    g = RegularLatLonGrid.global_regular(90, 180)
    assert rel(float(g.cell_areas().sum()), 4 * math.pi * R**2) < 1e-12


def test_constant_field_exact_mean_for_unaligned_region():
    g = RegularLatLonGrid.global_regular(18, 36)
    v = np.full(g.shape, 3.7)
    region = LatLonRegion("r", south=12.3, north=47.9, west=-33.3, east=71.1)
    rep = aggregate_to_regions(g, v, [region])
    r = rep.by_id("r")
    assert rep.verification_only is True
    assert rep.method == "conservative_area_weighted_spherical_v1"
    assert r.complete and abs(r.coverage_fraction - 1) < 1e-12
    assert rel(r.mean, 3.7) < 1e-14
    assert rel(r.integral, 3.7 * region.area_m2()) < 1e-12


def test_linear_in_latitude_matches_closed_form_spherical_integral():
    g = RegularLatLonGrid.global_regular(180, 360)
    v = separable_cell_means(g, lat_primitive=lin_lat_primitive)
    region = LatLonRegion("band", south=-30.0, north=60.0, west=-10.0, east=40.0)
    r = aggregate_to_regions(g, v, [region]).by_id("band")
    exact = separable_region_integral(region, lat_primitive=lin_lat_primitive)
    assert rel(r.integral, exact) < 1e-9
    assert rel(r.mean, exact / region.area_m2()) < 1e-9


def test_separable_lat_lon_field_and_global_total():
    g = RegularLatLonGrid.global_regular(36, 72)
    v = separable_cell_means(g, lat_primitive=lin_lat_primitive, lon_primitive=cos_lon_primitive)
    region = LatLonRegion("box", south=-45.0, north=20.0, west=-90.0, east=35.0)
    rep = aggregate_to_regions(g, v, [region])
    exact = separable_region_integral(region, lat_primitive=lin_lat_primitive, lon_primitive=cos_lon_primitive)
    assert rel(rep.by_id("box").integral, exact) < 1e-9
    globe = LatLonRegion("globe", -90.0, 90.0, -180.0, 180.0)
    exact_global = separable_region_integral(globe, lat_primitive=lin_lat_primitive, lon_primitive=cos_lon_primitive)
    assert rel(rep.grid_integral, exact_global) < 1e-9


def test_partition_conserves_total_with_unaligned_edges_and_antimeridian():
    g = RegularLatLonGrid.global_regular(45, 90)
    v = separable_cell_means(g, lat_primitive=lin_lat_primitive, lon_primitive=cos_lon_primitive)
    lat_cuts = [-90.0, -33.3, 7.7, 51.1, 90.0]
    lon_cuts = [(-120.3, 17.9), (17.9, 120.3), (120.3, -120.3)]  # last sector crosses the antimeridian
    regions = [
        LatLonRegion(f"r{i}{j}", lat_cuts[i], lat_cuts[i + 1], w, e)
        for i in range(4)
        for j, (w, e) in enumerate(lon_cuts)
    ]
    rep = aggregate_to_regions(g, v, regions)
    assert all(r.complete for r in rep.regions)
    total = sum(r.integral for r in rep.regions)
    assert rel(total, rep.grid_integral) < 1e-12
    assert rel(sum(r.region_area_m2 for r in rep.regions), 4 * math.pi * R**2) < 1e-12
    assert sum(r.crosses_antimeridian for r in rep.regions) == 4
    # Overlap areas of the partition reproduce every cell area exactly (conservative weights).
    ov = sum(overlap_areas(g, reg) for reg in regions)
    np.testing.assert_allclose(ov, g.cell_areas(), rtol=1e-12)


@pytest.mark.parametrize("lon0", [-180.0, 0.0])
def test_antimeridian_region_on_both_longitude_conventions(lon0):
    g = RegularLatLonGrid.global_regular(180, 360, lon0=lon0)
    v = separable_cell_means(g, lon_primitive=cos_lon_primitive)
    region = LatLonRegion("pacific", south=-20.0, north=30.0, west=170.0, east=-170.0)
    assert region.crosses_antimeridian and region.lon_width_deg == pytest.approx(20.0)
    r = aggregate_to_regions(g, v, [region]).by_id("pacific")
    exact = separable_region_integral(region, lon_primitive=cos_lon_primitive)
    # h integrates to 2·Δλ + sin(190°) − sin(170°) = 2·Δλ − 2 sin(10°) over the unwrapped interval
    expected = (
        R**2
        * (math.sin(math.radians(30)) - math.sin(math.radians(-20)))
        * (2 * math.radians(20) - 2 * math.sin(math.radians(10)))
    )
    assert rel(exact, expected) < 1e-12
    assert rel(r.integral, exact) < 1e-9
    split = aggregate_to_regions(
        g, v, [LatLonRegion("e", -20.0, 30.0, 170.0, 180.0), LatLonRegion("w", -20.0, 30.0, -180.0, -170.0)]
    )
    assert rel(split.by_id("e").integral + split.by_id("w").integral, r.integral) < 1e-12


def test_missing_cells_are_never_zero_filled():
    g = RegularLatLonGrid.global_regular(18, 36)
    v = np.full(g.shape, 1.0)
    v[9, 18] = np.nan
    region = LatLonRegion("r", -20.0, 20.0, -20.0, 20.0)
    with pytest.raises(ValidationProblem):
        aggregate_to_regions(g, v, [region])
    rep = aggregate_to_regions(g, v, [region], nan_policy="exclude")
    r = rep.by_id("r")
    assert r.integral is None and not r.complete and rep.grid_integral is None
    assert r.mean == pytest.approx(1.0) and r.valid_area_m2 < r.covered_area_m2


def test_region_outside_regional_grid_is_incomplete():
    g = RegularLatLonGrid(tuple(np.linspace(30, 60, 31)), tuple(np.linspace(-10, 30, 41)))
    rep = aggregate_to_regions(g, np.ones(g.shape), [LatLonRegion("r", 20.0, 50.0, -5.0, 10.0)])
    r = rep.by_id("r")
    assert 0 < r.coverage_fraction < 1 and r.integral is None and r.mean == pytest.approx(1.0)


def test_invalid_inputs_rejected():
    with pytest.raises(CapabilityGap) as ei:
        RegularLatLonGrid((0.0, 1.0), (0.0, 1.0), crs="EPSG:3035")
    assert ei.value.details["capability"] == "physical.field_to_region"
    with pytest.raises(ValidationProblem):
        RegularLatLonGrid((0.0, -1.0), (0.0, 1.0))
    with pytest.raises(ValidationProblem):
        LatLonRegion("z", 0.0, 10.0, 5.0, 5.0)
    g = RegularLatLonGrid.global_regular(2, 2)
    with pytest.raises(ValidationProblem):
        aggregate_to_regions(g, np.ones((3, 2)), [])
    with pytest.raises(ValidationProblem):
        aggregate_to_regions(g, np.full((2, 2), np.inf), [])


def test_field_metadata_contracts_and_reserved_reader():
    crs = fields.CRSSpec("OGC:CRS84", ("lon", "lat"))
    axes = (
        fields.AxisSpec("lat", "latitude", "degrees_north", 180),
        fields.AxisSpec("lon", "longitude", "degrees_east", 360),
    )
    with pytest.raises(ValueError):  # a reanalysis must declare that it includes later information
        fields.FieldMetadata(
            "tas",
            "air_temperature",
            None,
            "K",
            crs,
            "proleptic_gregorian",
            axes,
            "cell_mean",
            "time: mean",
            "reanalysis",
        )
    meta = fields.FieldMetadata(
        "tas",
        "air_temperature",
        None,
        "K",
        crs,
        "proleptic_gregorian",
        axes,
        "cell_mean",
        "time: mean",
        "reanalysis",
        includes_later_information=True,
    )
    assert meta.axis("latitude").units == "degrees_north"
    asset = fields.FieldAsset("a1", "zarr", "application/vnd+zarr", "0" * 64, meta, None, "test")
    with pytest.raises(CapabilityGap) as ei:
        fields.open_field(asset)
    assert ei.value.details["capability"] == "physical.field_adapter"
    assert fields.field_adapter_not_available().status == "reserved"
    with pytest.raises(CapabilityGap):
        adapter.aggregate_field_to_regions(asset, [])


class DecayModel:
    """Toy exponential decay x' = -k x with exact update; used only to exercise the port."""

    def initialize(self, config):
        self.k, self.x, self.t, self.dt, self.t_end = config["k"], config["x0"], 0.0, 1.0, 10.0

    def update(self):
        self.x *= math.exp(-self.k * self.dt)
        self.t += self.dt

    def update_until(self, time):
        while self.t < time:
            self.update()

    def finalize(self):
        self.finalized = True

    def get_component_name(self):
        return "decay"

    def get_input_var_names(self):
        return ("decay_rate",)

    def get_output_var_names(self):
        return ("amount",)

    def get_var_units(self, name):
        return {"decay_rate": "d-1", "amount": "kg"}[name]

    def get_var_support(self, name):
        return "point_in_time_stock" if name == "amount" else "period_rate"

    def get_start_time(self):
        return 0.0

    def get_end_time(self):
        return self.t_end

    def get_current_time(self):
        return self.t

    def get_time_step(self):
        return self.dt

    def get_time_units(self):
        return "days since 2000-01-01"

    def get_calendar(self):
        return "proleptic_gregorian"

    def get_value(self, name):
        return np.array([self.x])

    def set_value(self, name, value):
        self.k = float(value[0])


def test_lifecycle_conformance_passes_for_conforming_model():
    m = DecayModel()
    assert isinstance(m, adapter.ExternalModelPort)
    rep = adapter.check_lifecycle_conformance(m, {"k": 0.1, "x0": 2.0})
    assert rep.outcome == "pass", rep.findings
    assert rep.steps_executed == 3 and rep.verification_only
    assert m.finalized and m.x == pytest.approx(2.0 * math.exp(-0.3))
    with pytest.raises(CapabilityGap):
        adapter.couple_external_model(m, scenario_digest="d")


def test_lifecycle_conformance_detects_violations():
    class Frozen(DecayModel):
        def update(self):  # time never advances
            pass

        def get_var_units(self, name):
            return ""

    rep = adapter.check_lifecycle_conformance(Frozen(), {"k": 0.1, "x0": 1.0})
    codes = {f.code for f in rep.findings}
    assert rep.outcome == "fail" and {"time_not_increasing", "units_missing"} <= codes

    class Crashes(DecayModel):
        def update(self):
            raise RuntimeError("boom")

    m = Crashes()
    rep = adapter.check_lifecycle_conformance(m, {"k": 0.1, "x0": 1.0})
    assert rep.outcome == "fail" and m.finalized
    assert adapter.check_lifecycle_conformance(object(), {}).findings[0].code == "missing_methods"
