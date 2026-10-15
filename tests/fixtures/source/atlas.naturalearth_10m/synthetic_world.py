"""A small synthetic Natural Earth 1:10m world for offline tests (agent A-GEO).

The real 1:10m layers are 16 MB, too large for committed fixtures; the two small real layers
(``disputed_areas``, ``boundary_lines_disputed``) are captured in this directory's manifest. This module
writes the four large layers as tiny shapefile ZIPs with the same field names, built from axis-aligned boxes
whose edges are subdivided every 0.5 degree so that shared edges coincide vertex by vertex (as they do in
Natural Earth). Layout (lon x lat, degrees):

* ``FRA`` box [0,4]x[0,4] with two holes: ``VAT`` [1,2]x[1,2] (an enclave country) and ``ESP`` [2.5,3.5]x
  [2.5,3.5] (an enclosed Spanish exclave, like Llivia); plus a coastal FRA exclave [8,9]x[0,1] touching ESP.
* ``ESP`` box [4,8]x[0,4] (main), an island [10,11]x[0,1]; a boundary lake [6,7]x[1,2] in the
  "without lakes" layer.
* ``DEU`` [0,2]x[4,6], ``CHE`` [2,6]x[4,6] (landlocked: FRA/ESP south, DEU west, ITA east, AUT north),
  ``ITA`` [6,8]x[4,6], ``AUT`` [0,8]x[6,8].
* unmapped feature ``SOL`` (ISO -99) [4,8]x[-2,0] south of ESP.
* ``KAZ`` [47,48]x[40,41]: only Caspian shores (landlocked by convention).
* ``RUS`` split at the antimeridian: [178,180]x[60,62] and [-180,-178]x[60,62] (one component).
* ``GBR`` as two map units ENG [20,21]x[0,1] and SCT [20,21]x[1,2] (ISO -99, EH GBR): one country.
* ``UMI`` map unit with ISO -99, ADM0 UMI, EH USA [30,31]x[0,1]; ``KOS`` -> XKX [40,41]x[0,1].
* filler: 250 tiny ``FJI`` islands and 420 short unrelated boundary lines so the layers pass the
  connector's minimum-feature checks.

Boundary lines: FRA-ESP "International boundary (verify)" (Land), CHE-FRA "Disputed (please verify)",
ESP-CHE "International boundary (verify)" as "Water Indicator"; DEU-AUT has no line (untyped).
"""

from __future__ import annotations

import io
import itertools
import zipfile

import shapefile

MU_FIELDS = ("featurecla", "ADM0_A3", "GU_A3", "ISO_A3", "ISO_A3_EH", "TYPE", "NAME", "SOV_A3")
LINE_FIELDS = ("FEATURECLA", "TYPE", "ADM0_A3_L", "ADM0_A3_R", "ne_id")
STEP = 0.5


def _dense(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [pts[0]]
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        n = max(1, round(max(abs(x1 - x0), abs(y1 - y0)) / STEP))
        for i in range(1, n + 1):
            out.append((x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n))
    return out


def box_cw(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """Clockwise (outer ring in the shapefile convention), closed, densified."""
    return _dense([(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)])


def box_ccw(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """Counter-clockwise (hole), closed, densified."""
    return _dense([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])


def _zip(name: str, write) -> bytes:  # type: ignore[no-untyped-def]
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf)
    write(w)
    w.close()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"{name}.shp", shp.getvalue())
        z.writestr(f"{name}.shx", shx.getvalue())
        z.writestr(f"{name}.dbf", dbf.getvalue())
        z.writestr(f"{name}.cpg", "UTF-8")
        z.writestr(f"{name}.VERSION.txt", "9.9.9-synthetic")
    return out.getvalue()


def _units(lakes: bool) -> list[tuple[dict[str, str], list[list[tuple[float, float]]]]]:
    def rec(
        adm: str,
        gu: str | None = None,
        iso: str | None = None,
        eh: str | None = None,
        typ: str = "Sovereign country",
        name: str | None = None,
    ) -> dict[str, str]:
        return {
            "featurecla": "Admin-0 map unit",
            "ADM0_A3": adm,
            "GU_A3": gu or adm,
            "ISO_A3": iso or adm,
            "ISO_A3_EH": eh or iso or adm,
            "TYPE": typ,
            "NAME": name or adm,
            "SOV_A3": adm,
        }

    esp_main = [box_cw(4, 0, 8, 4)] + ([box_ccw(6, 1, 7, 2)] if lakes else [])
    units = [
        (rec("FRA"), [box_cw(0, 0, 4, 4), box_ccw(1, 1, 2, 2), box_ccw(2.5, 2.5, 3.5, 3.5), box_cw(8, 0, 9, 1)]),
        (rec("VAT"), [box_cw(1, 1, 2, 2)]),
        (rec("ESP"), [*esp_main, box_cw(2.5, 2.5, 3.5, 3.5), box_cw(10, 0, 11, 1)]),
        (rec("DEU"), [box_cw(0, 4, 2, 6)]),
        (rec("CHE"), [box_cw(2, 4, 6, 6)]),
        (rec("ITA"), [box_cw(6, 4, 8, 6)]),
        (rec("AUT"), [box_cw(0, 6, 8, 8)]),
        (rec("SOL", iso="-99", eh="-99", typ="Sovereign country", name="Somaliland"), [box_cw(4, -2, 8, 0)]),
        (rec("KAZ"), [box_cw(47, 40, 48, 41)]),
        (rec("RUS"), [box_cw(178, 60, 180, 62), box_cw(-180, 60, -178, 62)]),
        (rec("GBR", gu="ENG", iso="-99", eh="GBR", typ="Geo unit", name="England"), [box_cw(20, 0, 21, 1)]),
        (rec("GBR", gu="SCT", iso="-99", eh="GBR", typ="Geo unit", name="Scotland"), [box_cw(20, 1, 21, 2)]),
        (rec("UMI", gu="JQI", iso="-99", eh="USA", typ="Geo unit", name="Johnston Atoll"), [box_cw(30, 0, 31, 1)]),
        (rec("KOS", iso="-99", eh="-99", typ="Disputed", name="Kosovo"), [box_cw(40, 0, 41, 1)]),
    ]
    fji = [
        box_cw(100 + (i % 50) * 0.1, -20 + (i // 50) * 0.1, 100 + (i % 50) * 0.1 + 0.05, -20 + (i // 50) * 0.1 + 0.05)
        for i in range(250)
    ]
    units.append((rec("FJI"), fji[:1]))
    for i, r in enumerate(fji[1:], start=1):
        units.append((rec("FJI"), [r]))
    return units


def _write_polys(units, fields=MU_FIELDS):  # type: ignore[no-untyped-def]
    def write(w: shapefile.Writer) -> None:
        w.shapeType = shapefile.POLYGON
        for f in fields:
            w.field(f, "C", size=80)
        for rec, rings in units:
            w.poly(rings)
            w.record(*[rec.get(f, "") for f in fields])

    return write


def _lines() -> list[tuple[dict[str, str], list[tuple[float, float]]]]:
    land = {"TYPE": "Land"}
    out = [
        (
            {
                **land,
                "FEATURECLA": "International boundary (verify)",
                "ADM0_A3_L": "FRA",
                "ADM0_A3_R": "ESP",
                "ne_id": "1",
            },
            _dense([(4, 0), (4, 4)]),
        ),
        (
            {**land, "FEATURECLA": "Disputed (please verify)", "ADM0_A3_L": "FRA", "ADM0_A3_R": "CHE", "ne_id": "2"},
            _dense([(2, 4), (4, 4)]),
        ),
        (
            {
                "TYPE": "Water Indicator",
                "FEATURECLA": "International boundary (verify)",
                "ADM0_A3_L": "ESP",
                "ADM0_A3_R": "CHE",
                "ne_id": "3",
            },
            _dense([(4, 4), (6, 4)]),
        ),
    ]
    for i in range(420):  # filler lines far away from every polygon
        x = -100 + (i % 40) * 0.2
        y = -60 + (i // 40) * 0.2
        out.append(
            (
                {
                    **land,
                    "FEATURECLA": "International boundary (verify)",
                    "ADM0_A3_L": "ZZA",
                    "ADM0_A3_R": "ZZB",
                    "ne_id": str(1000 + i),
                },
                [(x, y), (x + 0.1, y)],
            )
        )
    return out


def _write_lines(write_lines):  # type: ignore[no-untyped-def]
    def write(w: shapefile.Writer) -> None:
        w.shapeType = shapefile.POLYLINE
        for f in LINE_FIELDS:
            w.field(f, "C", size=80)
        for rec, pts in write_lines:
            w.line([pts])
            w.record(*[rec.get(f, "") for f in LINE_FIELDS])

    return write


def layers() -> dict[str, bytes]:
    """item key -> ZIP bytes for the four large layers."""
    units = _units(lakes=False)
    return {
        "map_units": _zip("ne_10m_admin_0_map_units", _write_polys(units)),
        "countries": _zip("ne_10m_admin_0_countries", _write_polys(_merge_countries(units))),
        "countries_lakes": _zip("ne_10m_admin_0_countries_lakes", _write_polys(_merge_countries(_units(lakes=True)))),
        "boundary_lines_land": _zip("ne_10m_admin_0_boundary_lines_land", _write_lines(_lines())),
    }


def _merge_countries(units):  # type: ignore[no-untyped-def]
    """Countries layer: one feature per ADM0_A3 (map units of one ADM0 merged; rings concatenated)."""
    merged: dict[str, tuple[dict[str, str], list]] = {}
    for rec, rings in units:
        adm = rec["ADM0_A3"]
        if adm not in merged:
            r = dict(rec)
            r["GU_A3"] = adm
            merged[adm] = (r, [])
        merged[adm][1].extend(rings)
    return list(merged.values())
