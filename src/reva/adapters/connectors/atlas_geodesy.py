"""Small pure-numpy geodesy on the WGS 84 ellipsoid (no shapely/pyproj dependency).

Used by the Natural Earth 1:10m atlas connector (agent A-GEO). Two operations only:

* **Polygon area** (``ring_signed_area_m2`` / ``polygon_area_m2``): ellipsoidal area on WGS 84.
  Method: the authalic (equal-area) latitude beta maps the ellipsoid onto the authalic sphere of
  radius ``R_q`` preserving area exactly, so for a region bounded by a closed ring
  ``A = R_q^2 * |closed-integral of -sin(beta) d(lambda)|`` (Green's theorem in the
  (lambda, beta) plane, where the area element is ``R_q^2 cos(beta) d(beta) d(lambda)``).
  Edges are interpreted as straight lines in geographic coordinates (the Natural Earth data model:
  vertices in EPSG:4326, edges linear in lon/lat), and each edge integral is evaluated with
  Simpson's rule (exact along parallels and meridians; at 1:10m vertex spacing the remaining
  quadrature error is far below the precision of the source geometry). Rings must not cross the
  antimeridian (Natural Earth splits them at +/-180) and may run along the poles (Antarctica).
* **Segment length** (``vincenty_inverse_m``): geodesic distance by Vincenty's inverse formula,
  vectorised; segments are short (km scale), so the nearly-antipodal non-convergence case cannot
  occur in practice and is reported (NaN) rather than hidden.

Everything is validated in ``tests/unit/test_atlas_geodesy.py`` against closed forms (total
ellipsoid area 510,065,621.7 km^2, latitude zones) and Vincenty's published test line.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np

WGS84_A = 6_378_137.0
WGS84_F = 1.0 / 298.257223563
WGS84_B = WGS84_A * (1.0 - WGS84_F)
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_E = math.sqrt(WGS84_E2)

AREA_METHOD = (
    "WGS 84 ellipsoidal area via authalic-latitude equal-area transform; edges straight in lon/lat "
    "(Natural Earth data model); Simpson rule per edge (reva.adapters.connectors.atlas_geodesy)"
)
LENGTH_METHOD = "sum of WGS 84 geodesic segment lengths, Vincenty inverse (reva.adapters.connectors.atlas_geodesy)"


def _q(sin_phi: np.ndarray) -> np.ndarray:
    e, e2 = WGS84_E, WGS84_E2
    return (1.0 - e2) * (
        sin_phi / (1.0 - e2 * sin_phi**2) - (1.0 / (2.0 * e)) * np.log((1.0 - e * sin_phi) / (1.0 + e * sin_phi))
    )


_QP = float(_q(np.array([1.0]))[0])
#: Radius of the authalic sphere (same total area as the WGS 84 ellipsoid).
AUTHALIC_RADIUS = WGS84_A * math.sqrt(_QP / 2.0)


def sin_authalic(lat_deg: np.ndarray) -> np.ndarray:
    return _q(np.sin(np.radians(lat_deg))) / _QP


def ring_signed_area_m2(lon: Sequence[float] | np.ndarray, lat: Sequence[float] | np.ndarray) -> float:
    """Signed ellipsoidal area of a closed ring (counter-clockwise in lon/lat positive)."""
    x = np.asarray(lon, dtype=float)
    y = np.asarray(lat, dtype=float)
    if x.shape != y.shape or x.size < 3:
        return 0.0
    if x[0] != x[-1] or y[0] != y[-1]:
        x = np.append(x, x[0])
        y = np.append(y, y[0])
    dl = np.radians(np.diff(x))
    s1 = sin_authalic(y[:-1])
    s2 = sin_authalic(y[1:])
    sm = sin_authalic((y[:-1] + y[1:]) / 2.0)
    integral = float(np.sum(-(s1 + 4.0 * sm + s2) / 6.0 * dl))
    return AUTHALIC_RADIUS**2 * integral


def polygon_area_m2(rings: Iterable[tuple[Sequence[float] | np.ndarray, Sequence[float] | np.ndarray]]) -> float:
    """Net area of a set of rings using the shapefile convention (outer rings clockwise, holes
    counter-clockwise): the signed areas are summed, so holes subtract automatically."""
    total = 0.0
    for lon, lat in rings:
        total += ring_signed_area_m2(lon, lat)
    return -total


def vincenty_inverse_m(
    lon1: np.ndarray, lat1: np.ndarray, lon2: np.ndarray, lat2: np.ndarray, *, max_iter: int = 200
) -> np.ndarray:
    """Geodesic distance (m) between point arrays on WGS 84 (Vincenty 1975 inverse)."""
    a, b, f = WGS84_A, WGS84_B, WGS84_F
    lon1, lat1, lon2, lat2 = (np.atleast_1d(np.asarray(v, dtype=float)) for v in (lon1, lat1, lon2, lat2))
    big_l = np.radians(lon2 - lon1)
    big_l = (big_l + np.pi) % (2.0 * np.pi) - np.pi
    u1 = np.arctan((1.0 - f) * np.tan(np.radians(lat1)))
    u2 = np.arctan((1.0 - f) * np.tan(np.radians(lat2)))
    su1, cu1, su2, cu2 = np.sin(u1), np.cos(u1), np.sin(u2), np.cos(u2)
    lam = big_l.copy()
    converged = np.zeros(lam.shape, dtype=bool)
    sin_sigma = cos_sigma = sigma = cos2_alpha = cos_2sm = np.zeros_like(lam)
    for _ in range(max_iter):
        sl, cl = np.sin(lam), np.cos(lam)
        sin_sigma = np.sqrt((cu2 * sl) ** 2 + (cu1 * su2 - su1 * cu2 * cl) ** 2)
        cos_sigma = su1 * su2 + cu1 * cu2 * cl
        sigma = np.arctan2(sin_sigma, cos_sigma)
        with np.errstate(invalid="ignore", divide="ignore"):
            sin_alpha = np.where(sin_sigma == 0, 0.0, cu1 * cu2 * sl / sin_sigma)
            cos2_alpha = 1.0 - sin_alpha**2
            cos_2sm = np.where(cos2_alpha == 0, 0.0, cos_sigma - 2.0 * su1 * su2 / cos2_alpha)
        c = f / 16.0 * cos2_alpha * (4.0 + f * (4.0 - 3.0 * cos2_alpha))
        lam_new = big_l + (1.0 - c) * f * sin_alpha * (
            sigma + c * sin_sigma * (cos_2sm + c * cos_sigma * (-1.0 + 2.0 * cos_2sm**2))
        )
        done = np.abs(lam_new - lam) < 1e-12
        lam = np.where(converged, lam, lam_new)
        converged |= done
        if converged.all():
            break
    u_sq = cos2_alpha * (a * a - b * b) / (b * b)
    big_a = 1.0 + u_sq / 16384.0 * (4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq)))
    big_b = u_sq / 1024.0 * (256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq)))
    d_sigma = (
        big_b
        * sin_sigma
        * (
            cos_2sm
            + big_b
            / 4.0
            * (
                cos_sigma * (-1.0 + 2.0 * cos_2sm**2)
                - big_b / 6.0 * cos_2sm * (-3.0 + 4.0 * sin_sigma**2) * (-3.0 + 4.0 * cos_2sm**2)
            )
        )
    )
    s = b * big_a * (sigma - d_sigma)
    return np.where(converged, s, np.nan)


def line_length_m(lon: Sequence[float] | np.ndarray, lat: Sequence[float] | np.ndarray) -> float:
    x = np.asarray(lon, dtype=float)
    y = np.asarray(lat, dtype=float)
    if x.size < 2:
        return 0.0
    return float(np.sum(vincenty_inverse_m(x[:-1], y[:-1], x[1:], y[1:])))


def ellipsoid_zone_area_m2(lat1_deg: float, lat2_deg: float, dlon_deg: float = 360.0) -> float:
    """Closed-form area of the zone between two parallels over ``dlon_deg`` of longitude
    (independent check for the polygon routine)."""
    e = WGS84_E

    def g(phi_deg: float) -> float:
        s = math.sin(math.radians(phi_deg))
        return s / (2.0 * (1.0 - WGS84_E2 * s * s)) + (1.0 / (4.0 * e)) * math.log((1.0 + e * s) / (1.0 - e * s))

    return math.radians(dlon_deg) * WGS84_B**2 * (g(lat2_deg) - g(lat1_deg))
