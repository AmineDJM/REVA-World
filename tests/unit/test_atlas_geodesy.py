"""A-GEO: pure-numpy WGS 84 geodesy (area via authalic latitude, Vincenty lengths) against closed forms."""

from __future__ import annotations

import math

import numpy as np
import pytest

from reva.adapters.connectors.atlas_geodesy import (
    AUTHALIC_RADIUS,
    ellipsoid_zone_area_m2,
    line_length_m,
    polygon_area_m2,
    ring_signed_area_m2,
    vincenty_inverse_m,
)

WGS84_SURFACE_KM2 = 510_065_621.7  # published total surface of the WGS 84 ellipsoid


def _box(x0: float, y0: float, x1: float, y1: float, *, cw: bool = True) -> tuple[list[float], list[float]]:
    pts = [(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)] if cw else [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [p[0] for p in pts], [p[1] for p in pts]


def test_total_ellipsoid_area_matches_published_value() -> None:
    assert ellipsoid_zone_area_m2(-90, 90) / 1e6 == pytest.approx(WGS84_SURFACE_KM2, abs=0.1)
    assert 4 * math.pi * AUTHALIC_RADIUS**2 / 1e6 == pytest.approx(WGS84_SURFACE_KM2, abs=0.1)
    assert AUTHALIC_RADIUS == pytest.approx(6_371_007.181, abs=0.01)


@pytest.mark.parametrize("box", [(0, 0, 1, 1), (2.5, 44, 7.5, 49), (-70, -55, -60, -20), (170, 60, 180, 75)])
def test_lon_lat_box_equals_closed_form_zone(box: tuple[float, float, float, float]) -> None:
    x0, y0, x1, y1 = box
    want = ellipsoid_zone_area_m2(y0, y1, x1 - x0)
    assert polygon_area_m2([_box(*box)]) == pytest.approx(want, rel=1e-12)


def test_orientation_sign_and_holes() -> None:
    outer = _box(0, 0, 4, 4, cw=True)
    hole = _box(1, 1, 2, 2, cw=False)
    assert ring_signed_area_m2(*outer) < 0 < ring_signed_area_m2(*hole)
    net = polygon_area_m2([outer, hole])
    assert net == pytest.approx(ellipsoid_zone_area_m2(0, 4, 4) - ellipsoid_zone_area_m2(1, 2, 1), rel=1e-12)


def test_polar_cap_split_at_antimeridian_like_antarctica() -> None:
    lon, lat = [-180, -180, 180, 180, -180], [-90, -60, -60, -90, -90]
    assert polygon_area_m2([(lon, lat)]) == pytest.approx(ellipsoid_zone_area_m2(-90, -60), rel=1e-12)


def test_unclosed_ring_is_closed_and_degenerate_ring_is_zero() -> None:
    lon, lat = _box(0, 0, 1, 1)
    assert ring_signed_area_m2(lon[:-1], lat[:-1]) == pytest.approx(ring_signed_area_m2(lon, lat))
    assert ring_signed_area_m2([0, 1], [0, 1]) == 0.0


def test_vincenty_published_test_line() -> None:
    # Vincenty (1975) / Geoscience Australia example: Flinders Peak -> Buninyong, 54 972.271 m (GRS80;
    # the WGS 84 flattening differs by 1e-9 relative, far below a millimetre here).
    lat1 = -(37 + 57 / 60 + 3.72030 / 3600)
    lon1 = 144 + 25 / 60 + 29.52440 / 3600
    lat2 = -(37 + 39 / 60 + 10.15610 / 3600)
    lon2 = 143 + 55 / 60 + 35.38390 / 3600
    d = vincenty_inverse_m(np.array([lon1]), np.array([lat1]), np.array([lon2]), np.array([lat2]))
    assert float(d[0]) == pytest.approx(54_972.271, abs=0.001)


def test_vincenty_meridian_and_equator_closed_forms() -> None:
    # Equator: arc length a * dlon; one degree of the equator is 111 319.491 m.
    d = vincenty_inverse_m(np.array([0.0]), np.array([0.0]), np.array([1.0]), np.array([0.0]))
    assert float(d[0]) == pytest.approx(6_378_137.0 * math.radians(1.0), abs=1e-3)
    # Meridian quadrant (equator -> pole) on WGS 84: 10 001 965.729 m.
    d = vincenty_inverse_m(np.array([0.0]), np.array([0.0]), np.array([0.0]), np.array([90.0]))
    assert float(d[0]) == pytest.approx(10_001_965.729, abs=0.01)
    # zero length and symmetry
    z = vincenty_inverse_m(np.array([3.0]), np.array([4.0]), np.array([3.0]), np.array([4.0]))
    assert float(z[0]) == 0.0
    a = vincenty_inverse_m(np.array([2.0]), np.array([48.0]), np.array([13.0]), np.array([52.0]))
    b = vincenty_inverse_m(np.array([13.0]), np.array([52.0]), np.array([2.0]), np.array([48.0]))
    assert float(a[0]) == pytest.approx(float(b[0]), abs=1e-6)


def test_line_length_sums_segments() -> None:
    lon, lat = [0.0, 0.5, 1.0], [0.0, 0.0, 0.0]
    assert line_length_m(lon, lat) == pytest.approx(6_378_137.0 * math.radians(1.0), abs=1e-3)
    assert line_length_m([0.0], [0.0]) == 0.0
