"""V6INFRA transport & logistics connectors, offline (derived excerpts of real 2026-10-17 provider bytes).

Fixtures: tests/fixtures/source/atlas.{unlocode,imf_portwatch} and the Natural Earth admin-1 excerpt added to
atlas.{nga_wpi,ourairports}; cut by tests/fixtures/source/atlas.unlocode/cut_v6infra_fixtures.py (each manifest
entry says how). Counts are re-derived here independently from the raw bytes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

import pytest

from reva.adapters.connectors import atlas_flights as fl
from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors import atlas_ports as po
from reva.adapters.connectors import atlas_portwatch as pw
from reva.adapters.connectors import atlas_unlocode as un
from reva.adapters.connectors import v6infra_common as vc
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FIXTURE_ROOT
from reva.kernel.errors import SourceResponseError

QIDS = {"DZA": "Q262", "NLD": "Q29999", "MAR": "Q1028", "SGP": "Q334", "EGY": "Q79", "PAN": "Q804", "AND": "Q228",
        "FRA": "Q142"}  # fmt: skip


def _fx(conn: str, url: str, name: str = "f") -> ic.Fetched:
    d = FIXTURE_ROOT / conn
    for e in json.loads((d / "manifest.json").read_text()):
        if e["url"] == url:
            body = (d / e["file"]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == e["sha256"]
            return ic.Fetched(name, url, body, e["sha256"], datetime.fromisoformat(e["retrieved_at"]))
    raise KeyError(url)


@pytest.fixture(scope="module")
def ne() -> vc.Adm1Index:
    return vc.Adm1Index.from_fetched(_fx("atlas.unlocode", vc.NE_URL, vc.NE_NAME), origin="test_fixture")


def _sts(kb: vc.KgBuilder, subject: str) -> dict[str, Any]:
    return {s.property_pid: s for s in kb.statements if s.subject_qid == subject}


# --------------------------------------------------------------------------- coordinates
def test_coordinate_conversions() -> None:
    assert vc.dms_to_decimal("30°20'00\"N") == "30.333333"
    assert vc.dms_to_decimal("16°60'00\"S") == "-17"  # the WPI's 60 minutes are arithmetically exact
    assert vc.dms_to_decimal("72°07'00\"W") == "-72.116667"
    assert vc.dms_to_decimal("91°00'00\"N") is None and vc.dms_to_decimal("garbage") is None
    assert vc.locode_coordinates("4230N 00131E") == ("42.5", "1.516667")
    assert vc.locode_coordinates("3345S 07040W") == ("-33.75", "-70.666667")
    assert vc.locode_coordinates("") is None and vc.locode_coordinates("4275N 00131E") is None


def test_natural_earth_excerpt_is_refused_live() -> None:
    f = _fx("atlas.unlocode", vc.NE_URL)
    with pytest.raises(SourceResponseError):
        vc.Adm1Index.from_fetched(f, origin="live")


# --------------------------------------------------------------------------- UN/LOCODE
@pytest.fixture(scope="module")
def unlo() -> tuple[un.Release, ic.Fetched, ic.Fetched, ic.Fetched]:
    pub = _fx("atlas.unlocode", un.PUBLICATIONS_URL, "publications.html")
    terms = _fx("atlas.unlocode", un.TERMS_URL, "terms.html")
    tag = un.check_pages(pub.body, terms.body)
    raw = _fx("atlas.unlocode", un.release_url(tag), "release.zip")
    return un.read_release(raw.body, tag, min_locations=10), raw, terms, pub


def test_unlocode_licence_markers_and_release_tag() -> None:
    pub = _fx("atlas.unlocode", un.PUBLICATIONS_URL).body
    terms = _fx("atlas.unlocode", un.TERMS_URL).body
    assert un.check_pages(pub, terms) == "2025-1"
    with pytest.raises(SourceResponseError, match="CC BY"):
        un.check_pages(pub.replace(b"free to use under CC By 4.0", b"free"), terms)
    with pytest.raises(SourceResponseError, match="non-commercial"):
        un.check_pages(pub, terms.replace(b"non-commercial", b"any"))


def test_unlocode_counts_are_exact(unlo: tuple[un.Release, ic.Fetched, ic.Fetched, ic.Fetched]) -> None:
    rel, raw, terms, pub = unlo
    assert rel.function_labels["1"] == "Maritime transport (sea port)"
    assert rel.function_labels["4"] == "Air transport (airport)"
    rb, rep = un.build_atlas(rel, raw, terms, pub)
    frame = rb.frame()
    validate_atlas_frame(frame, source="unlocode", dataset="locations", raw_sha256s=[raw.sha256], geo_scheme="iso2")
    z = zipfile.ZipFile(io.BytesIO(raw.body))
    codes: dict[str, set[str]] = {}
    ports: Counter[str] = Counter()
    seen: set[str] = set()
    for n in z.namelist():
        if "CodeListPart" in n:
            for r in csv.reader(io.StringIO(z.read(n).decode("utf-8"))):
                if r[2].strip():
                    codes.setdefault(r[1], set()).add(r[2])
                    if "1" in r[6] and r[1] + r[2] not in seen:
                        ports[r[1]] += 1
                    seen.add(r[1] + r[2])
    got = {(x["provider_geo"], x["indicator"]): int(x["value"]) for x in frame.to_dicts()}
    for c, s in codes.items():
        assert got[(c, "LOCATIONS")] == len(s)
        if ports[c]:
            assert got[(c, "LOCATIONS|function=1")] == ports[c]
    assert "XZ" in rep["unmapped"]
    row = frame.filter((frame["provider_geo"] == "DZ") & (frame["indicator"] == "LOCATIONS")).to_dicts()[0]
    fl_ = json.loads(row["flags"])
    assert row["iso3"] == "DZA" and fl_["edition"] == "2025-1" and fl_["licence_status"] == "conflicting_statements"


def test_unlocode_kg_published_code_and_point_in_polygon(
    unlo: tuple[un.Release, ic.Fetched, ic.Fetched, ic.Fetched], ne: vc.Adm1Index
) -> None:
    rel, raw, _, _ = unlo
    kb = un.build_kg(rel, raw, ne, QIDS)
    alg = _sts(kb, "unlocode:DZALG")
    assert alg["P17"].object_qid == "Q262"
    assert alg["unlocode:in_adm1"].object_qid == "iso3166_2:DZ-16"
    assert alg["unlocode:in_adm1"].qualifiers["basis"] == "published_code"
    assert alg["P625"].value == {"type": "globe_coordinate", "lat": "36.75", "lon": "3.05", "globe": "earth"}
    assert "1" in alg["unlocode:function"].value["value"]
    rtm = _sts(kb, "unlocode:NLRTM")
    assert rtm["unlocode:in_adm1"].object_qid == "iso3166_2:NL-ZH"
    # every KG location has function 1 or 4; every ADM1 edge says how it was obtained
    ents = {e.qid for e in kb.entities}
    assert all(any(f in rel.locations[q.split(":")[1]].primary()[6] for f in "14") for q in ents)
    bases = Counter(s.qualifiers["basis"] for s in kb.statements if s.property_pid == "unlocode:in_adm1")
    assert set(bases) <= {"published_code", "point_in_polygon", "nearest_polygon"} and bases["published_code"] > 0
    for s in kb.statements:
        if s.property_pid == "unlocode:in_adm1" and s.qualifiers["basis"] != "published_code":
            assert s.qualifiers["derived"] == s.qualifiers["basis"] and s.qualifiers["ne_raw_sha256"]


# --------------------------------------------------------------------------- WPI KG
def test_wpi_kg_ports(ne: vc.Adm1Index, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _fx(po.NAME, po.URL, "world-port-index.csv")
    kb = po.build_kg(raw, ne, QIDS)
    rows = list(csv.DictReader(io.StringIO(raw.body.decode("utf-8-sig"))))
    assert len(kb.entities) == len(rows)
    dz = [r for r in rows if r["countryCode"] == "DZ" and r["unloCode"].strip()]
    assert dz
    s = _sts(kb, f"wpi:{dz[0]['portNumber']}")
    assert s["P17"].object_qid == "Q262"
    assert s["wpi:at_unlocode"].object_qid == "unlocode:" + dz[0]["unloCode"].replace(" ", "")
    adm = [x for x in kb.statements if x.property_pid == "wpi:in_adm1"]
    assert adm and all(x.object_qid.startswith("iso3166_2:DZ-") or x.object_qid.startswith("iso3166_2:MA-") or
                       x.object_qid.startswith("iso3166_2:SG-") for x in adm)  # fmt: skip
    assert {x.qualifiers["basis"] for x in adm} <= {"point_in_polygon", "nearest_polygon"}
    near = [x for x in adm if x.qualifiers["basis"] == "nearest_polygon"]
    assert all(x.qualifiers["distance_deg"] <= po.COASTAL_DEG for x in near)


# --------------------------------------------------------------------------- OurAirports
class _Raw:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.retrieved_at = datetime(2026, 10, 17, tzinfo=UTC)
        self.response_metadata: dict[str, Any] = {}


class _Run:
    origin = "test_fixture"

    def __init__(self, pages: dict[str, bytes | Exception]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    async def get(self, url: str, **_: Any) -> _Raw:
        self.asked.append(url)
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        return _Raw(v)

    def archive(self, raw: _Raw) -> str:
        return hashlib.sha256(raw.body).hexdigest()


async def test_ourairports_licence_fallback_to_repository_license() -> None:
    lic = b"This is free and unencumbered software released into the public domain.\n"
    maint = b"<h1>Temporarily unavailable</h1> OurAirports is undergoing some routine maintenance"
    run = _Run({fl.OA_PAGE: maint, fl.OA_REPO_LICENSE: lic})
    files, basis = await fl.oa_licence(run)
    assert run.asked == [fl.OA_PAGE, fl.OA_REPO_LICENSE] and "Unlicense" in basis and len(files) == 2
    run = _Run({fl.OA_PAGE: SourceResponseError("HTTP 503", retryable=True), fl.OA_REPO_LICENSE: lic})
    assert "LICENSE" in (await fl.oa_licence(run))[1]
    run = _Run({fl.OA_PAGE: maint, fl.OA_REPO_LICENSE: b"All rights reserved"})
    with pytest.raises(SourceResponseError, match="public domain"):
        await fl.oa_licence(run)
    ok = _Run({fl.OA_PAGE: b"All data is released to the Public Domain"})
    assert (await fl.oa_licence(ok))[1].startswith("terms page") and ok.asked == [fl.OA_PAGE]


def test_ourairports_kg_airports(ne: vc.Adm1Index) -> None:
    raw = _fx(fl.OA_NAME, fl.OA_BASE + "airports.csv", "airports.csv")
    kb = fl.build_ourairports_kg(raw, ne, QIDS, "test")
    rows = list(csv.DictReader(io.StringIO(raw.body.decode("utf-8-sig"))))
    want = {f"ourairports:{r['id']}" for r in rows if r["type"] != "closed" and
            (r["type"] in fl.OA_KG_TYPES or r["scheduled_service"] == "yes")}  # fmt: skip
    assert {e.qid for e in kb.entities} == want and want
    for s in kb.statements:
        if s.property_pid == "ourairports:in_adm1":
            assert s.qualifiers["basis"] in ("published_code", "point_in_polygon")


# --------------------------------------------------------------------------- PortWatch
def test_portwatch_chokepoints_daily() -> None:
    raw = _fx(pw.NAME, pw.choke_csv_url(), "Daily_Chokepoints_Data.csv")
    item = _fx(pw.NAME, pw.item_url(pw.ITEM_CHOKE_DAILY))
    rb, rep = pw.build_chokepoints(raw, item, min_rows=10)
    frame = rb.frame()
    validate_atlas_frame(frame, source="portwatch", dataset="chokepoints_daily", raw_sha256s=[raw.sha256],
                         geo_scheme="iso3")  # fmt: skip
    rows = list(csv.DictReader(io.StringIO(raw.body.decode("utf-8-sig"))))
    r0 = rows[0]
    d = date(int(r0["year"]), int(r0["month"]), int(r0["day"]))
    one = frame.filter((frame["provider_geo"] == r0["portid"]) & (frame["indicator"] == "TRANSITS|vessel_type=total")
                       & (frame["period_start"] == d)).to_dicts()  # fmt: skip
    assert len(one) == 1 and one[0]["value"] == r0["n_total"] and one[0]["iso3"] is None
    est = frame.filter(frame["indicator"] == "TRADE_VOLUME_EST|vessel_type=total").to_dicts()
    assert est and all(json.loads(x["flags"])["estimate_kind"] == "model_estimate" for x in est)
    assert rep["chokepoints"] == len({r["portid"] for r in rows})
    with pytest.raises(SourceResponseError, match="description"):
        bad = ic.Fetched("i", item.url, item.body.replace(b"transits through", b"passes"), "x", item.retrieved_at)
        pw.build_chokepoints(raw, bad, min_rows=10)


def test_portwatch_country_monthly_partial_month() -> None:
    item = _fx(pw.NAME, pw.item_url(pw.ITEM_PORTS_DAILY))
    f = _fx(pw.NAME, pw.monthly_url(2026, 0))
    feats, more = pw._features(f)
    assert feats and not more
    rb, _ = pw.build_country_monthly([(f, feats)], date(2026, 10, 9), item)
    frame = rb.frame()
    validate_atlas_frame(
        frame, source="portwatch", dataset="country_monthly", raw_sha256s=[f.sha256], geo_scheme="iso3"
    )
    oct_ = frame.filter((frame["period_start"] == date(2026, 10, 1)) & (frame["indicator"] == "PORT_CALLS")).to_dicts()
    assert oct_ and all("partial_period" in json.loads(x["flags"]) for x in oct_)
    sep = frame.filter((frame["period_start"] == date(2026, 9, 1)) & (frame["provider_geo"] == "DZA")).to_dicts()
    a = next(x for x in feats if x["ISO3"] == "DZA" and x["month"] == 9)
    pc = next(x for x in sep if x["indicator"] == "PORT_CALLS")
    assert int(pc["value"]) == int(a["portcalls"]) and pc["iso3"] == "DZA"
    assert json.loads(pc["flags"])["derived"] == "sum" and "partial_period" not in json.loads(pc["flags"])


def test_portwatch_kg(ne: vc.Adm1Index) -> None:
    pf = _fx(pw.NAME, pw.ports_url("PortWatch_ports_database", 0))
    cf = _fx(pw.NAME, pw.ports_url("PortWatch_chokepoints_database", 0))
    ports = [{**a, "_raw_sha256": pf.sha256} for a in pw._features(pf)[0]]
    chokes = pw._features(cf)[0]
    kb = pw.build_kg(ports, chokes, [pf], cf, ne, QIDS, True)
    assert {e.qid for e in kb.entities if e.kind == "maritime_chokepoint"} == {
        f"portwatch:{c['portid']}" for c in chokes
    }
    mb = [s for s in kb.statements if s.property_pid == vc.MEASURED_BY]
    assert len(mb) == len(chokes) and all(s.object_qid == "atlas:portwatch.chokepoints_daily" for s in mb)
    assert any(e.qid == "atlas:portwatch.chokepoints_daily" and e.kind == "data_series" for e in kb.entities)
    dz = next(p for p in ports if p["ISO3"] == "DZA" and p.get("LOCODE"))
    s = _sts(kb, f"portwatch:{dz['portid']}")
    assert s["P17"].object_qid == "Q262"
    assert s["portwatch:at_unlocode"].object_qid == "unlocode:" + dz["LOCODE"].replace(" ", "")
    assert all(s.object_qid.startswith("iso3166_2:") for s in kb.statements if s.property_pid == "portwatch:in_adm1")
    assert any(e.qid.startswith("portwatch:fso") for e in kb.entities)
