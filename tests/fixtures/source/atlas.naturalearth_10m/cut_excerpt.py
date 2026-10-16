"""Cut the real Natural Earth 1:10m Admin-0 layers to a small *derived excerpt* (fixture mode, agent FIX2).

The four large layers fetched by ``atlas.naturalearth_10m`` (map units, countries, countries without boundary
lakes, land boundary lines; ~16 MB) are too large to commit. This script keeps, byte for byte from the real
release-5.1.1 shapefiles, only the features of a few **closed** groups of countries — groups whose land
borders are all inside the group, so every area, coastline, border length and exclave computed from the
excerpt equals the value computed from the whole world (``--verify`` checks it):

* Ireland + United Kingdom (4 GBR map units; the Irish land border);
* Hispaniola: Haiti + Dominican Republic;
* Cyprus: Cyprus, Northern Cyprus (``CYN``, not a crosswalk country), the UN buffer zone (``CNM``),
  the British sovereign base areas Akrotiri (``WSB``) and Dhekelia (``ESB``, with Cypriot enclaves);
* Saint-Martin: Saint-Martin (``MAF``) + Sint Maarten (``SXM``).

Land boundary lines are kept when both sides are in the selection. Fields, records, geometry, ``.prj``,
``.cpg`` and ``VERSION.txt`` are copied unchanged (the README is dropped); each ZIP gets a
``REVA_EXCERPT.json`` member naming the source file, its sha256 and the selection, and the connector accepts a
layer carrying that member only from a ``test_fixture`` acquisition (it then waives the whole-world minimum
feature counts and labels every row ``ne_excerpt``). The two small layers (disputed areas, disputed boundary
lines) stay the full real captures.

Reproduce (writes the four ZIPs here and updates ``manifest.json``; the output is deterministic)::

    mkdir -p /tmp/ne10m && for n in map_units countries countries_lakes boundary_lines_land; do
      curl -sSo /tmp/ne10m/ne_10m_admin_0_$n.zip https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_$n.zip
    done
    uv run python tests/fixtures/source/atlas.naturalearth_10m/cut_excerpt.py /tmp/ne10m --verify

The script refuses source ZIPs whose sha256 differs from ``SOURCE_SHA256`` (the files cut for the committed
fixture, retrieved 2026-10-16) unless ``--accept-new-sources`` is given.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import shapefile  # pyshp

HERE = Path(__file__).resolve().parent
BASE_URL = "https://naciscdn.org/naturalearth/10m/cultural/"
#: Closed groups of countries (ADM0_A3 codes): every land border of a member is with another member.
GROUPS = {
    "ireland_uk": ("IRL", "GBR"),
    "hispaniola": ("HTI", "DOM"),
    "cyprus": ("CYP", "CYN", "CNM", "WSB", "ESB"),
    "saint_martin": ("MAF", "SXM"),
}
SELECTION = tuple(sorted({c for g in GROUPS.values() for c in g}))
LAYERS = (
    "ne_10m_admin_0_map_units",
    "ne_10m_admin_0_countries",
    "ne_10m_admin_0_countries_lakes",
    "ne_10m_admin_0_boundary_lines_land",
)
#: sha256 of the release files the committed excerpt was cut from (naciscdn.org, retrieved 2026-10-16).
SOURCE_SHA256 = {
    "ne_10m_admin_0_map_units": "45bebe2aaf8bf42b9daf4428594925bcb11eabd32fe9dc6e0acf681438053eb5",
    "ne_10m_admin_0_countries": "ce1ac7036499a0edd641fbc093cd209a98f96a49d2eca8480aaacad35138a7f6",
    "ne_10m_admin_0_countries_lakes": "3f1596b05b4a19bd97f3a54731497cffbb4dbed4cda494084e73dd6dd3d37264",
    "ne_10m_admin_0_boundary_lines_land": "16ead035f539c8b6c23650c5845d86ad3556553e7456bdf9b4730210f26aacbe",
}
RETRIEVED_AT = "2026-10-16T00:16:00+00:00"
ZIP_TIME = (2022, 5, 9, 0, 0, 0)  # fixed member timestamps (deterministic archives)


def _members(body: bytes) -> dict[str, bytes]:
    zf = zipfile.ZipFile(io.BytesIO(body))
    out = {}
    for info in zf.infolist():
        name = info.filename
        if name.upper().endswith("VERSION.TXT"):
            out["VERSION.txt"] = zf.read(info)
        else:
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in ("shp", "shx", "dbf", "cpg", "prj"):
                out[ext] = zf.read(info)
    return out


def _keep(layer: str, rec: dict) -> bool:
    if layer.endswith("boundary_lines_land"):
        return rec.get("ADM0_A3_L") in SELECTION and rec.get("ADM0_A3_R") in SELECTION
    return rec.get("ADM0_A3") in SELECTION


def cut(layer: str, body: bytes, source_sha256: str) -> tuple[bytes, int]:
    m = _members(body)
    r = shapefile.Reader(shp=io.BytesIO(m["shp"]), shx=io.BytesIO(m["shx"]), dbf=io.BytesIO(m["dbf"]), encoding="utf-8")
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=r.shapeType, encoding="utf-8")
    for f in r.fields[1:]:
        w.field(*f)
    kept = []
    for i, sr in enumerate(r.iterShapeRecords()):
        rec = sr.record.as_dict()
        if _keep(layer, rec):
            w.shape(sr.shape)
            w.record(*list(sr.record))
            kept.append(
                {"record": i, "ADM0_A3": rec.get("ADM0_A3"), "GU_A3": rec.get("GU_A3")}
                if not layer.endswith("lines_land")
                else {"record": i, "sides": [rec.get("ADM0_A3_L"), rec.get("ADM0_A3_R")]}
            )
    w.close()
    dbf_bytes = bytearray(dbf.getvalue())
    dbf_bytes[1:4] = m["dbf"][1:4]  # the source's "last update" date, not today's (reproducible bytes)
    note = {
        "kind": "derived_excerpt",
        "derived_by": "tests/fixtures/source/atlas.naturalearth_10m/cut_excerpt.py",
        "source_url": f"{BASE_URL}{layer}.zip",
        "source_sha256": source_sha256,
        "source_features": len(r),
        "selection_adm0_a3": list(SELECTION),
        "groups": {k: list(v) for k, v in GROUPS.items()},
        "kept": kept,
        "note": "real Natural Earth features copied unchanged; a test fixture, not the whole layer",
    }
    files = {
        f"{layer}.shp": shp.getvalue(),
        f"{layer}.shx": shx.getvalue(),
        f"{layer}.dbf": bytes(dbf_bytes),
        f"{layer}.prj": m.get("prj", b""),
        f"{layer}.cpg": m.get("cpg", b"UTF-8"),
        f"{layer}.VERSION.txt": m.get("VERSION.txt", b""),
        "REVA_EXCERPT.json": json.dumps(note, indent=1, sort_keys=True).encode(),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name], compresslevel=9)
    return out.getvalue(), len(kept)


def _fingerprint(url: str) -> str:
    from reva.evidence.runner import _fixture_fingerprint

    return str(_fixture_fingerprint(url))


def verify(src: dict[str, bytes], cut_bodies: dict[str, bytes]) -> list[str]:
    """Values computed from the excerpt equal the whole-world values for every selected country."""
    from reva.adapters.connectors import atlas_naturalearth10m as ne

    keys = ne._crosswalk_keys()

    def comp(bodies: dict[str, bytes]):  # type: ignore[no-untyped-def]
        layers = {
            k: ne.read_layer(bodies[ne.LAYERS[k][0]], ne.LAYERS[k][0]) for k in ne.LAYERS if ne.LAYERS[k][0] in bodies
        }
        return ne.compute(layers, keys)

    full, part = comp(src), comp(cut_bodies)
    problems = []
    sel_keys = set(part.area_m2)
    for k in sorted(sel_keys):
        for what, a, b in (
            ("area", full.area_m2.get(k), part.area_m2.get(k)),
            ("coast", full.coast_m.get(k, 0.0), part.coast_m.get(k, 0.0)),
            ("lakes", full.lakes_m2.get(k, 0.0), part.lakes_m2.get(k, 0.0)),
            ("components", len(full.components.get(k, [])), len(part.components.get(k, []))),
        ):
            if a != b:
                problems.append(f"{k} {what}: whole world {a} != excerpt {b}")
        fn, pn = full.neighbours(k), part.neighbours(k)
        if fn != pn:
            problems.append(f"{k} neighbours: whole world {fn} != excerpt {pn}")
    for pair, pb in part.borders.items():
        fb = full.borders[pair]
        if (fb.length_m, dict(fb.by_class_m), fb.water_m) != (pb.length_m, dict(pb.by_class_m), pb.water_m):
            problems.append(f"border {pair}: whole world {fb} != excerpt {pb}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", type=Path, help="directory holding the four original ne_10m_admin_0_*.zip files")
    ap.add_argument("--out", type=Path, default=HERE)
    ap.add_argument("--verify", action="store_true", help="compare excerpt and whole-world computations")
    ap.add_argument("--accept-new-sources", action="store_true")
    a = ap.parse_args(argv)
    src = {n: (a.src / f"{n}.zip").read_bytes() for n in LAYERS}
    shas = {n: hashlib.sha256(b).hexdigest() for n, b in src.items()}
    for n, sha in shas.items():
        if SOURCE_SHA256[n] and sha != SOURCE_SHA256[n] and not a.accept_new_sources:
            print(f"{n}.zip sha256 {sha} differs from the committed source {SOURCE_SHA256[n]}", file=sys.stderr)
            return 2
    cut_bodies, counts = {}, {}
    for n in LAYERS:
        cut_bodies[n], counts[n] = cut(n, src[n], shas[n])
    if a.verify:
        problems = verify(src, cut_bodies)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        print("verify: excerpt values equal whole-world values for", ", ".join(SELECTION))
    manifest_path = a.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    urls = {f"{BASE_URL}{n}.zip" for n in LAYERS}
    manifest = [e for e in manifest if e["url"] not in urls]
    for n in LAYERS:
        url = f"{BASE_URL}{n}.zip"
        fp = _fingerprint(url)
        body = cut_bodies[n]
        fname = f"{fp[:16]}.zip"
        (a.out / fname).write_bytes(body)
        manifest.append(
            {
                "request_fingerprint": fp,
                "url": url,
                "file": fname,
                "sha256": hashlib.sha256(body).hexdigest(),
                "status": 200,
                "headers": {"content-type": "application/zip"},
                "retrieved_at": RETRIEVED_AT,
                "licence": "Public domain",
                "derived": {
                    "kind": "derived_excerpt",
                    "by": "tests/fixtures/source/atlas.naturalearth_10m/cut_excerpt.py",
                    "source_sha256": shas[n],
                    "features_kept": counts[n],
                    "selection_adm0_a3": list(SELECTION),
                    "note": "excerpt of the real layer (closed country groups), not a provider response",
                },
            }
        )
        print(f"{fname}: {n} {counts[n]} features, {len(body)} bytes")
    manifest.sort(key=lambda e: e["url"])
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
