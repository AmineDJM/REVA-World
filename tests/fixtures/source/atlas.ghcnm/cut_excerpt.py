"""Cut the derived excerpts of tests/fixtures/source/atlas.ghcnm from the real provider files (V4CLIMATE).

usage: python -I cut_excerpt.py <ghcnm.tavg.latest.qcf.tar.gz> <ne_10m_admin_1_states_provinces.zip> <out_dir>

* GHCN-M: the inventory and data lines of the stations whose id starts with one of STATION_PREFIXES (real lines,
  unchanged), in a tar.gz with the provider's directory layout plus a REVA_EXCERPT.json member;
* Natural Earth admin-1: the features whose iso_a2 is in NE_ISO_A2 or whose iso_3166_2 is in NE_CODES (real
  geometries and attributes, unchanged) in a ZIP with a REVA_EXCERPT.json member.
The connector accepts an excerpt only from a test_fixture acquisition.
"""

import hashlib
import io
import json
import sys
import tarfile
import zipfile

import shapefile

STATION_PREFIXES = ("SW0", "ACW", "RQW", "IC0", "LU", "SUM")
NE_ISO_A2 = ("SE", "IS", "LU", "SD", "SS")
NE_CODES = ("US-PR",)


def main(tar_path: str, ne_path: str, out: str) -> None:
    tb = open(tar_path, "rb").read()
    tf = tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz")
    mem = {m.name: m for m in tf.getmembers() if m.isfile()}
    inv = next(n for n in mem if n.endswith(".inv"))
    dat = next(n for n in mem if n.endswith(".dat"))
    keep = lambda ln: ln.startswith(STATION_PREFIXES)  # noqa: E731
    inv_b = "".join(ln for ln in tf.extractfile(mem[inv]).read().decode("latin-1").splitlines(True) if keep(ln))
    dat_b = "".join(ln for ln in tf.extractfile(mem[dat]).read().decode("latin-1").splitlines(True) if keep(ln))
    note = {
        "kind": "derived_excerpt",
        "by": "tests/fixtures/source/atlas.ghcnm/cut_excerpt.py",
        "source_sha256": hashlib.sha256(tb).hexdigest(),
        "station_prefixes": list(STATION_PREFIXES),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as w:
        for name, data in (
            (inv, inv_b.encode("latin-1")),
            (dat, dat_b.encode("latin-1")),
            (inv.rsplit("/", 1)[0] + "/REVA_EXCERPT.json", json.dumps(note).encode()),
        ):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = 0
            w.addfile(ti, io.BytesIO(data))
    open(f"{out}/ghcnm_excerpt.tar.gz", "wb").write(buf.getvalue())

    nb = open(ne_path, "rb").read()
    z = zipfile.ZipFile(io.BytesIO(nb))
    names = z.namelist()
    get = lambda ext: io.BytesIO(z.read(next(n for n in names if n.endswith(ext))))  # noqa: E731
    r = shapefile.Reader(shp=get(".shp"), dbf=get(".dbf"), shx=get(".shx"), encoding="utf-8")
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=r.shapeType, encoding="utf-8")
    w.fields = r.fields[1:]
    n = 0
    for sr in r.iterShapeRecords():
        a = sr.record.as_dict()
        if a["iso_a2"] in NE_ISO_A2 or a["iso_3166_2"] in NE_CODES:
            w.shape(sr.shape)
            w.record(*sr.record)
            n += 1
    w.close()
    base = "ne_10m_admin_1_states_provinces"
    note2 = {
        "kind": "derived_excerpt",
        "by": "tests/fixtures/source/atlas.ghcnm/cut_excerpt.py",
        "source_sha256": hashlib.sha256(nb).hexdigest(),
        "iso_a2": list(NE_ISO_A2),
        "codes": list(NE_CODES),
        "features": n,
    }
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as zo:
        for ext, b in ((".shp", shp), (".shx", shx), (".dbf", dbf)):
            zi = zipfile.ZipInfo(base + ext, date_time=(2026, 10, 17, 0, 0, 0))
            zo.writestr(zi, b.getvalue(), compress_type=zipfile.ZIP_DEFLATED)
        zo.writestr(zipfile.ZipInfo("REVA_EXCERPT.json", date_time=(2026, 10, 17, 0, 0, 0)), json.dumps(note2))
    open(f"{out}/ne_admin1_excerpt.zip", "wb").write(zb.getvalue())
    print(len(inv_b.splitlines()), "stations;", n, "NE features")


if __name__ == "__main__":
    main(*sys.argv[1:4])
