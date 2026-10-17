"""Cut the fixtures of tests/fixtures/source/atlas.ucdp_ged from the real 2026-10-17 downloads (V6CONFLICT).

usage: python -I cut_fixture.py <download_dir> <out_dir>

<download_dir> holds the provider files as downloaded: downloads.html, ged261-csv.zip, ged261.pdf,
ucdp-candidate-codebook1.5.pdf, GEDEvent_v26_01_26_06.csv, GEDEvent_v26_0_7.csv, GEDEvent_v26_0_8.csv,
ucdp-nonstate-261-csv.zip, ucdp-onesided-261-csv.zip, actor.zip (ucdp-actor-261-csv.zip), ne_admin1.zip.

* CSV slices: selected records with the original header; field values unchanged (csv module re-quoting), the CSV
  inside a ZIP with the provider's member name where the provider zips it.
* Natural Earth admin-1: the features of NE_ISO_A2 (real geometries and attributes) in a ZIP with a
  REVA_EXCERPT.json member (accepted by the connector only from a test_fixture acquisition).
* downloads.html and the two codebook PDFs: the real bytes, unchanged.
* countrycode: the KGGEOPOL fixture slice (tests/fixtures/source/atlas.ucdp, same URL) is reused.
"""

import csv
import hashlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import shapefile

NE_ISO_A2 = ("AF", "IL", "PS", "SD", "SS", "BA", "AZ", "AM", "SY", "TR", "CO")
RETRIEVED = "2026-10-17T16:24:00+00:00"
LIC = "CC BY 4.0"
BASE = "https://ucdp.uu.se/downloads/"


def fp(url: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
    from reva.evidence.runner import _fixture_fingerprint

    return _fixture_fingerprint(url)


def read(text: str) -> tuple[list[str], list[dict[str, str]]]:
    r = csv.DictReader(io.StringIO(text))
    return list(r.fieldnames or []), list(r)


def write(fields: list[str], rows: list[dict[str, str]]) -> str:
    b = io.StringIO()
    w = csv.DictWriter(b, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return b.getvalue()


def zipped(member: str, data: bytes) -> bytes:
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo(member, date_time=(2026, 3, 30, 0, 0, 0)), data, compress_type=zipfile.ZIP_DEFLATED)
    return zb.getvalue()


def pick_ged(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: dict[str, dict[str, str]] = {}

    def take(pred, n):  # noqa: ANN001
        k = 0
        for r in rows:
            if k >= n:
                break
            if r["id"] not in out and pred(r):
                out[r["id"]] = r
                k += 1

    take(lambda r: r["country_id"] == "700" and r["year"] == "2020", 40)
    take(lambda r: r["country_id"] == "700" and r["type_of_violence"] == "2", 8)
    take(lambda r: r["country_id"] == "700" and r["type_of_violence"] == "3", 8)
    take(lambda r: r["country_id"] == "666" and r["year"] == "2024" and float(r["latitude"]) < 31.6, 20)
    take(lambda r: r["country_id"] == "625" and r["year"] == "2005", 20)
    take(lambda r: r["country_id"] == "365", 10)
    take(lambda r: r["country_id"] == "346" and r["year"] == "1993", 12)
    take(lambda r: r["country_id"] == "652" and r["year"] == "2016", 20)
    take(lambda r: r["country_id"] == "100", 10)
    for wp in ("5", "6", "7"):
        take(lambda r, wp=wp: r["where_prec"] == wp, 4)
    take(lambda r: r["date_start"][:7] != r["date_end"][:7], 8)
    return sorted(out.values(), key=lambda r: int(r["id"]))


def main(src: str, out: str) -> None:
    s, o = Path(src), Path(out)
    man: list[dict[str, object]] = []

    def add(url: str, body: bytes, ext: str, ctype: str, note: str, derived: dict[str, object] | None = None) -> None:
        sha = hashlib.sha256(body).hexdigest()
        name = f"{sha[:16]}{ext}"
        (o / name).write_bytes(body)
        e: dict[str, object] = {"request_fingerprint": fp(url), "url": url, "file": name, "sha256": sha, "status": 200,
                                "headers": {"content-type": ctype}, "retrieved_at": RETRIEVED, "licence": LIC,
                                "note": note}
        if derived:
            e["derived"] = derived
        man.append(e)

    # countrycode (KGGEOPOL slice, same URL)
    kg = Path(__file__).resolve().parents[1] / "atlas.ucdp"
    for e in json.loads((kg / "manifest.json").read_text()):
        if e["url"].startswith("https://cran.r-project.org/"):
            shutil.copy(kg / e["file"], o / e["file"])
            man.append(e)
    add(BASE, (s / "downloads.html").read_bytes(), ".html", "text/html", "real downloads page (2026-10-17)")
    add(BASE + "ged/ged261.pdf", (s / "ged261.pdf").read_bytes(), ".pdf", "application/pdf", "real GED 26.1 codebook")
    add(BASE + "candidateged/ucdp-candidate-codebook1.5.pdf", (s / "ucdp-candidate-codebook1.5.pdf").read_bytes(),
        ".pdf", "application/pdf", "real Candidate codebook 1.5")

    z = zipfile.ZipFile(s / "ged261-csv.zip")
    fields, rows = read(z.read("GEDEvent_v26_1.csv").decode("utf-8"))
    g = pick_ged(rows)
    add(BASE + "ged/ged261-csv.zip", zipped("GEDEvent_v26_1.csv", write(fields, g).encode()), ".zip",
        "application/x-zip-compressed", f"slice of ged261-csv.zip: {len(g)} of {len(rows)} events, values unchanged")
    conflicts = {r["conflict_new_id"] for r in g}

    cand = {}
    for name in ("GEDEvent_v26_01_26_06.csv", "GEDEvent_v26_0_7.csv", "GEDEvent_v26_0_8.csv"):
        cand[name] = read((s / name).read_text(encoding="utf-8"))
    ids = {n: {r["id"] for r in rs} for n, (_f, rs) in cand.items()}
    overlap = set()
    names = list(cand)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            overlap |= ids[a] & ids[b]
    for name, (f, rs) in cand.items():
        keep = [r for r in rs if r["id"] in overlap]
        keep += [r for r in rs if r["country_id"] in ("700", "666", "625", "652") and r["id"] not in overlap][:30]
        keep += [r for r in rs if r["date_start"][:4] == "2025"][:2]
        keep.sort(key=lambda r: int(r["id"]))
        add(BASE + "candidateged/" + name, write(f, keep).encode(), ".csv", "text/csv",
            f"slice of {name}: {len(keep)} of {len(rs)} events (all ids shared with another release kept)")

    used_actors: set[str] = set()
    for fname, member, key in (("ucdp-nonstate-261-csv.zip", "NonState_v26_1.csv", "nonstate"),
                               ("ucdp-onesided-261-csv.zip", "OneSided_v26_1.csv", "onesided")):
        zz = zipfile.ZipFile(s / fname)
        f, rs = read(zz.read(member).decode("utf-8-sig"))
        keep = [r for r in rs if r["conflict_id"] in conflicts]
        if key == "nonstate":
            keep += [r for r in rs if r["side_a_id"] == "3818" or r["side_b_id"] == "3818"]
            for r in keep:
                used_actors |= {r["side_a_id"], r["side_b_id"]}
        else:
            for r in keep:
                used_actors.add(r["actor_id"])
        add(BASE + "nsos/" + fname, zipped(member, write(f, keep).encode("utf-8")), ".zip",
            "application/x-zip-compressed", f"slice of {fname}: {len(keep)} of {len(rs)} rows, values unchanged")
    za = zipfile.ZipFile(s / "actor.zip")
    f, rs = read(za.read("Actor_v26_1.csv").decode("cp1252"))
    keep = [r for r in rs if r["ActorId"] in used_actors]
    add(BASE + "actor/ucdp-actor-261-csv.zip", zipped("Actor_v26_1.csv", write(f, keep).encode("cp1252")), ".zip",
        "application/x-zip-compressed", f"slice of ucdp-actor-261-csv.zip: {len(keep)} of {len(rs)} rows (cp1252)")

    nb = (s / "ne_admin1.zip").read_bytes()
    nz = zipfile.ZipFile(io.BytesIO(nb))
    get = lambda ext: io.BytesIO(nz.read(next(n for n in nz.namelist() if n.endswith(ext))))  # noqa: E731
    r = shapefile.Reader(shp=get(".shp"), dbf=get(".dbf"), shx=get(".shx"), encoding="utf-8")
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=r.shapeType, encoding="utf-8")
    w.fields = r.fields[1:]
    n = 0
    for sr in r.iterShapeRecords():
        if sr.record.as_dict()["iso_a2"] in NE_ISO_A2:
            w.shape(sr.shape)
            w.record(*sr.record)
            n += 1
    w.close()
    note = {"kind": "derived_excerpt", "by": "tests/fixtures/source/atlas.ucdp_ged/cut_fixture.py",
            "source_sha256": hashlib.sha256(nb).hexdigest(), "iso_a2": list(NE_ISO_A2), "features": n}
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as zo:
        for ext, b in ((".shp", shp), (".shx", shx), (".dbf", dbf)):
            zo.writestr(zipfile.ZipInfo("ne_10m_admin_1_states_provinces" + ext, date_time=(2026, 10, 17, 0, 0, 0)),
                        b.getvalue(), compress_type=zipfile.ZIP_DEFLATED)
        zo.writestr(zipfile.ZipInfo("REVA_EXCERPT.json", date_time=(2026, 10, 17, 0, 0, 0)), json.dumps(note))
    add("https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip", zb.getvalue(), ".zip",
        "application/zip", f"derived excerpt: {n} Natural Earth admin-1 features", derived=note)
    for e in man:
        if e["url"].startswith("https://naciscdn.org"):
            e["licence"] = "Public domain (Natural Earth)"
        if e["url"].startswith("https://cran"):
            continue
    (o / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    print(len(g), "GED events;", len(conflicts), "conflicts;", n, "NE features;", len(used_actors), "actors")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
