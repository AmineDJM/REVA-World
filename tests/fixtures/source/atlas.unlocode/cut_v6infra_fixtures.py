"""Cut the V6INFRA test fixtures from captured provider bytes (maintainer tool, not run by tests).

usage: python -I cut_v6infra_fixtures.py <capture_dir> <repo_root>

<capture_dir> holds: ne.zip (Natural Earth 10m admin-1), unlocode-<tag>.zip (official release), pub.html, terms.html,
and cache/<16 hex>{,.json} files (one per URL, ``{"url": ...}`` metadata) of an IMF PortWatch run.

Every fixture is a **derived excerpt** of real bytes (rows/features filtered, nothing rewritten); ZIP excerpts carry a
REVA_EXCERPT.json member (refused by the parsers outside fixture mode) and each manifest entry says how it was cut and
the sha256 of the source bytes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from datetime import UTC, datetime
from typing import Any

NE_ISO_A2 = ("DZ", "NL", "SG", "EG", "PA", "MA", "AD", "MC", "TV")
COUNTRIES2 = ("DZ", "NL", "SG", "EG", "PA", "MA", "AD", "XZ")
COUNTRIES3 = ("FRA", "DZA", "NLD", "SGP", "EGY", "PAN", "MAR")
CHOKEPOINTS = ("chokepoint1", "chokepoint2", "chokepoint6")
WHEN = datetime(2026, 10, 17, 12, 0, tzinfo=UTC).isoformat()
DT = (2026, 10, 17, 0, 0, 0)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fp(url: str) -> str:
    from reva.adapters.http.safe_client import SECRET_QUERY_PARAMS, request_fingerprint

    return str(request_fingerprint("GET", url, secret_params=tuple(SECRET_QUERY_PARAMS)))


def entry(url: str, body: bytes, ctype: str, ext: str, licence: str, note: str, source: bytes | None) -> tuple[dict[str, Any], bytes]:  # fmt: skip
    f = fp(url)
    e = {"request_fingerprint": f, "url": url, "file": f"{f[:16]}.{ext}", "sha256": sha(body), "status": 200,
         "headers": {"content-type": ctype}, "retrieved_at": WHEN, "licence": licence, "note": note}  # fmt: skip
    if source is not None:
        e["derived"] = {"kind": "derived_excerpt", "by": "tests/fixtures/source/atlas.unlocode/cut_v6infra_fixtures.py",
                        "source_sha256": sha(source)}  # fmt: skip
    return e, body


def ne_excerpt(nb: bytes) -> bytes:
    import shapefile  # type: ignore[import-untyped]

    z = zipfile.ZipFile(io.BytesIO(nb))
    names = z.namelist()

    def get(ext: str) -> io.BytesIO:
        return io.BytesIO(z.read(next(n for n in names if n.endswith(ext))))

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
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as zo:
        for ext, b in ((".shp", shp), (".shx", shx), (".dbf", dbf)):
            zo.writestr(zipfile.ZipInfo("ne_10m_admin_1_states_provinces" + ext, date_time=DT), b.getvalue(),
                        compress_type=zipfile.ZIP_DEFLATED)  # fmt: skip
        zo.writestr(zipfile.ZipInfo("REVA_EXCERPT.json", date_time=DT),
                    json.dumps({"kind": "derived_excerpt", "source_sha256": sha(nb), "iso_a2": NE_ISO_A2, "features": n}))  # fmt: skip
    return zb.getvalue()


def unlocode_excerpt(rb: bytes) -> bytes:
    z = zipfile.ZipFile(io.BytesIO(rb))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in z.namelist():
            if re.match(r"^release/csv/UNLOCODE CodeListPart\d\.csv$", n) or n == "release/csv/SubdivisionCodes.csv":
                rows = list(csv.reader(io.StringIO(z.read(n).decode("utf-8"))))
                col = 1 if "CodeList" in n else 0
                keep = [r for r in rows if len(r) > col and r[col] in COUNTRIES2]
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\r\n").writerows(keep)
                zo.writestr(zipfile.ZipInfo(n, date_time=DT), buf.getvalue().encode("utf-8"))
            elif n == "release/UNLOCODE.ttl":
                ttl = z.read(n).decode("utf-8")
                head = ttl[: ttl.index("# === Countries")]  # prefixes, concept schemes, function codes (verbatim)
                zo.writestr(zipfile.ZipInfo(n, date_time=DT), head.encode("utf-8"))
        zo.writestr(zipfile.ZipInfo("REVA_EXCERPT.json", date_time=DT),
                    json.dumps({"kind": "derived_excerpt", "source_sha256": sha(rb), "countries": COUNTRIES2}))  # fmt: skip
    return out.getvalue()


def write(d: str, entries: list[tuple[dict[str, Any], bytes]], merge: bool = False) -> None:
    os.makedirs(d, exist_ok=True)
    man: list[dict[str, Any]] = []
    if merge and os.path.exists(f"{d}/manifest.json"):
        man = [e for e in json.load(open(f"{d}/manifest.json")) if e["url"] not in {x["url"] for x, _ in entries}]
    for e, b in entries:
        open(f"{d}/{e['file']}", "wb").write(b)
        man.append(e)
    with open(f"{d}/manifest.json", "w") as f:
        json.dump(man, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main(cap: str, repo: str) -> None:
    from reva.adapters.connectors import atlas_portwatch as pw
    from reva.adapters.connectors import atlas_unlocode as un
    from reva.adapters.connectors import v6infra_common as vc

    src = f"{repo}/tests/fixtures/source"
    nb = open(f"{cap}/ne.zip", "rb").read()
    ne = entry(vc.NE_URL, ne_excerpt(nb), "application/zip", "zip", "Public domain (Natural Earth)",
               f"EXCERPT: admin-1 features with iso_a2 in {list(NE_ISO_A2)}", nb)  # fmt: skip
    # UN/LOCODE
    pub, terms = open(f"{cap}/pub.html", "rb").read(), open(f"{cap}/terms.html", "rb").read()
    tag = un.check_pages(pub, terms)
    rel = open(f"{cap}/unlocode-{tag}.zip", "rb").read()
    lic = "UN/LOCODE: conflicting statements (CC BY 4.0 footer vs non-commercial site terms)"
    write(f"{src}/atlas.unlocode", [
        entry(un.PUBLICATIONS_URL, pub, "text/html", "html", lic, "verbatim", None),
        entry(un.TERMS_URL, terms, "text/html", "html", lic, "verbatim", None),
        entry(un.release_url(tag), unlocode_excerpt(rel), "application/zip", "zip", lic,
              f"EXCERPT: code list and subdivision rows of {list(COUNTRIES2)}; SKOS file cut before the countries", rel),
        ne,
    ])  # fmt: skip
    # WPI / OurAirports: add the Natural Earth excerpt
    write(f"{src}/atlas.nga_wpi", [ne], merge=True)
    write(f"{src}/atlas.ourairports", [ne], merge=True)
    # PortWatch
    cache: dict[str, bytes] = {}
    for f in os.listdir(f"{cap}/cache"):
        if f.endswith(".json"):
            cache[json.load(open(f"{cap}/cache/{f}"))["url"]] = open(f"{cap}/cache/{f[:-5]}", "rb").read()
    plic = "IMF terms (licence review pending)"
    ents = [entry(pw.item_url(i), cache[pw.item_url(i)], "application/json", "json", plic, "verbatim", None)
            for i in (pw.ITEM_CHOKE_DAILY, pw.ITEM_PORTS_DAILY, pw.ITEM_PORTS_DB, pw.ITEM_CHOKE_DB)]  # fmt: skip
    cb = cache[pw.choke_csv_url()]
    lines = cb.decode("utf-8-sig").splitlines(True)
    keep = [lines[0]] + [ln for ln in lines[1:] if ln.split(",")[4] in CHOKEPOINTS and ln.startswith(("2024/01/0", "2026/10/0"))]
    ents.append(entry(pw.choke_csv_url(), ("﻿" + "".join(keep)).encode("utf-8"), "text/csv", "csv", plic,
                      f"EXCERPT: rows of {list(CHOKEPOINTS)} on 2024-01-01..09 and 2026-10-01..09", cb))  # fmt: skip
    rng = cache[pw.date_range_url()]
    ents.append(entry(pw.date_range_url(), rng, "application/json", "json", plic, "verbatim", None))
    last = int(json.loads(rng)["features"][0]["attributes"]["max"][:4])

    def trim(body: bytes, key: str, values: tuple[str, ...], extra: Any = None) -> bytes:
        d = json.loads(body)
        d["features"] = [x for x in d["features"] if x["attributes"].get(key) in values or (extra and extra(x))]
        d["exceededTransferLimit"] = False
        return json.dumps(d, separators=(",", ":")).encode()

    for y in range(pw.FIRST_YEAR, last + 1):
        u = pw.monthly_url(y, 0)
        pages = [cache[pw.monthly_url(y, o)] for o in range(0, 10_000, pw.PAGE) if pw.monthly_url(y, o) in cache]
        merged = json.loads(pages[0])
        for p in pages[1:]:
            merged["features"] += json.loads(p)["features"]
        body = json.dumps(merged).encode()
        ents.append(entry(u, trim(body, "ISO3", COUNTRIES3), "application/json", "json", plic,
                          f"EXCERPT: groups of {list(COUNTRIES3)} from all pages of the year", body))  # fmt: skip
    pages = [cache[pw.ports_url("PortWatch_ports_database", o)] for o in range(0, 10_000, pw.PAGE)
             if pw.ports_url("PortWatch_ports_database", o) in cache]  # fmt: skip
    merged = json.loads(pages[0])
    for p in pages[1:]:
        merged["features"] += json.loads(p)["features"]
    body = json.dumps(merged).encode()
    fso = {x["attributes"]["portid"] for x in merged["features"] if str(x["attributes"]["portid"]).startswith("fso")}
    first_fso = tuple(sorted(fso)[:2])
    ents.append(entry(pw.ports_url("PortWatch_ports_database", 0),
                      trim(body, "ISO3", COUNTRIES3, lambda x: x["attributes"]["portid"] in first_fso),
                      "application/json", "json", plic, f"EXCERPT: ports of {list(COUNTRIES3)} + {list(first_fso)}", body))  # fmt: skip
    cdb = cache[pw.ports_url("PortWatch_chokepoints_database", 0)]
    ents.append(entry(pw.ports_url("PortWatch_chokepoints_database", 0), trim(cdb, "portid", CHOKEPOINTS),
                      "application/json", "json", plic, f"EXCERPT: {list(CHOKEPOINTS)}", cdb))  # fmt: skip
    ents.append(ne)
    write(f"{src}/atlas.imf_portwatch", ents)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
