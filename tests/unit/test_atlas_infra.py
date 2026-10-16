"""A-INFRA atlas connectors, offline (trimmed excerpts of real 2026-10-16 provider bytes).

Fixtures: tests/fixtures/source/atlas.openflights (routes among 8 countries + one synthetic unresolvable
route; data page and countries.dat verbatim), atlas.ourairports, atlas.gcat (orbital launches of 1957, 1965,
1977, 1999 and a 2024 sample with their payloads), atlas.nga_wpi. Each manifest says how it was trimmed.
Counts are re-derived here independently from the raw bytes and compared exactly.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_cables as cb
from reva.adapters.connectors import atlas_flights as fl
from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors import atlas_ports as po
from reva.adapters.connectors import atlas_space as sp
from reva.atlas.catalog import load_catalog
from reva.atlas.sources import atlas_sources
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FIXTURE_ROOT
from reva.kernel.errors import SourceResponseError

REPO = Path(__file__).resolve().parents[2]
MODULES = ("atlas_flights", "atlas_space", "atlas_ports", "atlas_cables", "atlas_infra_common")


def _fixture(conn: str, url: str, name: str) -> ic.Fetched:
    d = FIXTURE_ROOT / conn
    for e in json.loads((d / "manifest.json").read_text()):
        if e["url"] == url:
            body = (d / e["file"]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == e["sha256"]
            return ic.Fetched(name, url, body, e["sha256"], datetime.fromisoformat(e["retrieved_at"]))
    raise KeyError(url)


@pytest.fixture(autouse=True)
def _trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures are trimmed excerpts: lower the truncation guards (full files are far above them)."""
    for mod, names in ((fl, ("MIN_OF_AIRPORTS", "MIN_OF_ROUTES", "MIN_OA_AIRPORTS")), (sp, ("MIN_LAUNCHES", "MIN_OBJECTS")),
                       (po, ("MIN_PORTS",))):  # fmt: skip
        for n in names:
            monkeypatch.setattr(mod, n, 10)


def _flags(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(row["flags"])
    return out


def _one(frame: pl.DataFrame, geo: str, indicator: str) -> dict[str, Any] | None:
    rows = frame.filter((pl.col("provider_geo") == geo) & (pl.col("indicator") == indicator)).to_dicts()
    assert len(rows) <= 1
    return rows[0] if rows else None


# --------------------------------------------------------------------------- OpenFlights
@pytest.fixture(scope="module")
def of_files() -> dict[str, ic.Fetched]:
    return {f: _fixture(fl.OF_NAME, fl.OF_RAW_BASE + f, f) for f in fl.OF_FILES}


def test_openflights_counts_are_exact(of_files: dict[str, ic.Fetched]) -> None:
    import pytest as _pt

    with _pt.MonkeyPatch.context() as m:
        m.setattr(fl, "MIN_OF_AIRPORTS", 10)
        m.setattr(fl, "MIN_OF_ROUTES", 10)
        rb, rep = fl.build_openflights(of_files)
    frame, unmapped = validate_atlas_frame(
        rb.frame(), source="openflights", dataset="routes_2014", raw_sha256s=[f.sha256 for f in of_files.values()]
    )
    # independent recount: airports by id -> country; routes France -> Algeria
    ap = {r[0]: r[3] for r in csv.reader(io.StringIO(of_files["airports.dat"].body.decode()))}
    code: dict[str, str] = {}
    for r in csv.reader(io.StringIO(of_files["airports.dat"].body.decode())):
        code.setdefault(r[4], r[0])
        code.setdefault(r[5], r[0])
    op = 0
    pairs, airlines = set(), set()
    for r in csv.reader(io.StringIO(of_files["routes.dat"].body.decode())):
        a = ap.get(r[3]) if r[3] != "\\N" else ap.get(code.get(r[2], ""))
        b = ap.get(r[5]) if r[5] != "\\N" else ap.get(code.get(r[4], ""))
        if (a, b) == ("France", "Algeria"):
            op += r[6] != "Y"
            pairs.add((r[3], r[5]))
            airlines.add(r[1])
    row = _one(frame, "France", "ROUTES_OPERATED|partner=DZA")
    assert row is not None and row["iso3"] == "FRA" and int(row["value"]) == op
    f = _flags(row)
    assert f["partner_iso3"] == f["counterpart_iso3"] == "DZA" and f["derived"] == "count" and "formula" in f
    assert f["stale"].startswith("provider warning") and f["data_as_of"] == "2014-06" and f["licence"] == "ODbL-1.0"
    assert row["period_start"] == date(2014, 6, 1) and row["period_end"] == date(2014, 7, 1)
    assert int(_one(frame, "France", "ROUTE_AIRPORT_PAIRS|partner=DZA")["value"]) == len(pairs)  # type: ignore[index]
    assert int(_one(frame, "France", "ROUTE_AIRLINES|partner=DZA")["value"]) == len(airlines)  # type: ignore[index]
    # provider-spelled names: Virgin Islands -> VIR (alias with evidence); Netherlands Antilles (AN) stays unmapped
    assert set(frame.filter(pl.col("provider_geo") == "Virgin Islands")["iso3"].to_list()) <= {"VIR"}
    assert rep["unmapped_countries"] == ["Netherlands Antilles"]
    assert {u["provider_geo"] for u in unmapped} == {"Netherlands Antilles"}
    assert any("partner_code=Netherlands Antilles" in i for i in frame["indicator"].to_list())
    assert rep["routes_unresolved_airport"] == 1  # the synthetic route, never assigned
    assert frame.filter(pl.col("value") == "0").height == 0  # no zero for missing
    assert not frame.filter(
        pl.col("indicator").str.contains("partner=FRA") & (pl.col("provider_geo") == "France")
    ).height


def test_openflights_licence_change_is_refused(of_files: dict[str, ic.Fetched]) -> None:
    class Run:
        spec = next(s for s in fl.atlas_source() if s.name == fl.OF_NAME)

        def wants(self, d: str) -> bool:
            return True

        async def get(self, url: str, **kw: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(body=b"<html>All rights reserved</html>", retrieved_at=datetime.now(UTC),
                                   response_metadata={})  # fmt: skip

        def archive(self, raw: Any) -> str:
            return "0" * 64

    import asyncio

    with pytest.raises(SourceResponseError, match="ODbL"):
        asyncio.run(fl.ingest_openflights(Run()))


# --------------------------------------------------------------------------- OurAirports
def test_ourairports_counts_and_codes() -> None:
    a = _fixture(fl.OA_NAME, fl.OA_BASE + "airports.csv", "airports.csv")
    c = _fixture(fl.OA_NAME, fl.OA_BASE + "countries.csv", "countries.csv")
    rb, rep = fl.build_ourairports(a, c, date(2026, 10, 16))
    frame, unmapped = validate_atlas_frame(
        rb.frame(), source="ourairports", dataset="airports", raw_sha256s=[a.sha256, c.sha256], geo_scheme="ourairports"
    )
    rows = list(csv.DictReader(io.StringIO(a.body.decode())))
    fr_open = sum(1 for r in rows if r["iso_country"] == "FR" and r["type"] != "closed")
    fr_large = sum(1 for r in rows if r["iso_country"] == "FR" and r["type"] == "large_airport")
    assert int(_one(frame, "FR", "AIRPORTS_OPEN")["value"]) == fr_open  # type: ignore[index]
    got_large = _one(frame, "FR", "AIRPORTS|type=large_airport")
    assert (int(got_large["value"]) if got_large else 0) == fr_large
    assert set(frame.filter(pl.col("provider_geo") == "XK")["iso3"].to_list()) == {"XKX"}
    assert {u["provider_geo"] for u in unmapped} == {"XP", "ZZ"} == set(rep["unmapped"])
    assert frame["period_start"].unique().to_list() == [date(2026, 10, 16)]
    assert frame.filter(pl.col("value") == "0").height == 0


# --------------------------------------------------------------------------- GCAT
@pytest.fixture(scope="module")
def gcat() -> tuple[dict[str, pl.DataFrame], dict[str, Any], dict[str, ic.Fetched]]:
    idx = _fixture(sp.NAME, sp.INDEX_URL, "index.html")
    meta = sp.parse_index(idx.body)
    files = {f: _fixture(sp.NAME, u, f) for f, u in sp.FILES.items()}
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sp, "MIN_LAUNCHES", 10)
        m.setattr(sp, "MIN_OBJECTS", 10)
        builders, rep = sp.build(files, meta)
    shas = [idx.sha256, *(f.sha256 for f in files.values())]
    frames = {
        ds: validate_atlas_frame(rb.frame(), source="gcat", dataset=ds, raw_sha256s=shas)[0]
        for ds, rb in builders.items()
    }
    return frames, rep, files


def test_gcat_index_licence_and_release() -> None:
    idx = _fixture(sp.NAME, sp.INDEX_URL, "index.html")
    meta = sp.parse_index(idx.body)
    assert meta["release"] == "1.8.8" and meta["data_update"] == "2026-10-15"
    with pytest.raises(SourceResponseError, match=r"CC-BY-4\.0"):
        sp.parse_index(idx.body.replace(b"CC-BY-4.0", b"All rights reserved"))


def test_gcat_launch_counts(gcat: tuple[dict[str, pl.DataFrame], dict[str, Any], dict[str, ic.Fetched]]) -> None:
    frames, rep, files = gcat
    lf = frames["launches"]
    y57 = lf.filter(pl.col("period_start") == date(1957, 1, 1))
    su = _one(y57, "SU", "ORBITAL_LAUNCH_ATTEMPTS|by=site_state")
    assert su is not None and su["value"] == "2" and su["iso3"] is None and su["entity_qid"] == "Q15180"
    assert _one(y57, "SU", "ORBITAL_LAUNCH_SUCCESSES|by=site_state")["value"] == "2"  # type: ignore[index]
    us = _one(y57, "US", "ORBITAL_LAUNCH_ATTEMPTS|by=site_state")
    assert us is not None and us["value"] == "1" and us["iso3"] == "USA"  # Vanguard TV3 (failed)
    assert _one(y57, "US", "ORBITAL_LAUNCH_SUCCESSES|by=site_state") is None  # no success: absent, not zero
    # independent recount of all attempts per year
    launches = ic.read_tsv(files["launch.tsv"].body)
    n = sum(1 for r in launches if r["LaunchCode"][:1] in "OD" and r["LaunchCode"][1:2] in "SFU")
    by_site = lf.filter(pl.col("indicator") == "ORBITAL_LAUNCH_ATTEMPTS|by=site_state")
    by_agency = lf.filter(pl.col("indicator") == "ORBITAL_LAUNCH_ATTEMPTS|by=agency_state")
    assert sum(int(v) for v in by_site["value"]) == n == sum(int(v) for v in by_agency["value"])
    assert rep["launches"]["orbital_attempts"] == n
    f = _flags(su)
    assert f["derived"] == "count" and f["gcat_release"] == "1.8.8" and f["licence"] == "CC BY 4.0"


def test_gcat_payload_pairs(gcat: tuple[dict[str, pl.DataFrame], dict[str, Any], dict[str, ic.Fetched]]) -> None:
    frames, _rep, files = gcat
    pf = frames["payloads"]
    sat = [r for r in ic.read_tsv(files["satcat.tsv"].body) if r["Type"].startswith("P")]
    per = pf.filter(pl.col("indicator") == "PAYLOADS_CATALOGUED|by=owner_state")
    assert sum(int(v) for v in per["value"]) == len(sat)
    pairs = pf.filter(pl.col("indicator").str.starts_with("PAYLOADS_LAUNCHED_FROM|"))
    assert pairs.height > 0
    for r in pairs.to_dicts():
        f = _flags(r)
        assert f["direction"] == "owner->site" and f["partner_code"] != r["provider_geo"]
        if f["partner_iso3"]:
            assert r["indicator"].endswith(f"partner={f['partner_iso3']}") and r["iso3"] != f["partner_iso3"]
        else:
            assert "partner_code=" in r["indicator"]
    # every owner-year pair total <= that owner's payloads that year
    tot: dict[tuple[str, date], int] = defaultdict(int)
    for r in pairs.to_dicts():
        tot[(r["provider_geo"], r["period_start"])] += int(r["value"])
    for (g, y), v in tot.items():
        assert v <= int(_one(per.filter(pl.col("period_start") == y), g, "PAYLOADS_CATALOGUED|by=owner_state")["value"])  # type: ignore[index]
    snap = pf.filter(pl.col("indicator") == "PAYLOADS_STATUS_IN_ORBIT|by=owner_state")
    assert snap["period_start"].unique().to_list() == [date(2026, 10, 15)]
    igo = pf.filter(pl.col("provider_geo").str.starts_with("I-"))
    assert igo.height == 0 or igo["iso3"].null_count() == igo.height  # IGOs never mapped to a country


def test_gcat_aliases_have_evidence_and_skip_historical_states() -> None:
    doc = yaml.safe_load((REPO / "registry/atlas/geo_aliases/a_infra.yaml").read_text())
    al = doc["schemes"]["gcat"]["aliases"]
    assert all(a["source"].startswith("GCAT orgs.tsv row Code=" + a["code"] + " ") for a in al)
    for code in ("SU", "CSSR", "CSFR", "HKUK", "TTPI", "EARTH", "I-ESA", "DD", "ESIB"):
        assert country_iso3(code, "gcat") is None
    assert country_iso3("F", "gcat") == "FRA" and country_iso3("GUF", "gcat") == "GUF"
    assert country_iso3("UK", "gcat") == "GBR" and country_iso3("TW", "gcat") == "TWN"
    assert country_iso3("ESCN", "gcat") is None  # Canary Islands: part of Spain (E), not merged


# --------------------------------------------------------------------------- WPI
def test_wpi_counts() -> None:
    raw = _fixture(po.NAME, po.URL, "world-port-index.csv")
    rb, rep = po.build(raw)
    frame, unmapped = validate_atlas_frame(
        rb.frame(), source="nga", dataset="wpi", raw_sha256s=[raw.sha256], geo_scheme="nga_wpi"
    )
    rows = list(csv.DictReader(io.StringIO(raw.body.decode("utf-8-sig"))))
    assert int(_one(frame, "FR", "PORTS_LISTED")["value"]) == sum(r["countryCode"] == "FR" for r in rows)  # type: ignore[index]
    # parts of ISO UM (UMI) stay unmapped: three provider geographies are never merged into one country
    assert {u["provider_geo"] for u in unmapped} == {"QM", "XU"} == set(rep["unmapped"])
    assert _flags(_one(frame, "FR", "PORTS_LISTED"))["coverage"].startswith("WPI lists ports")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Wikidata cables
class _OfflineRun:
    """The connector's own fetch path over recorded responses; archive = sha256, register = capture."""

    def __init__(self, spec: Any) -> None:
        from reva.atlas.ingest import AtlasRun
        from reva.evidence.runner import FixtureHttp

        self._run = AtlasRun.offline(spec, FixtureHttp(spec.name))
        self.spec, self.origin = spec, "test_fixture"
        self.registered: list[dict[str, Any]] = []

    def wants(self, d: str) -> bool:
        return True

    async def get(self, url: str, **kw: Any) -> Any:
        return await self._run.get(url, **kw)

    def archive(self, raw: Any) -> str:
        return hashlib.sha256(raw.body).hexdigest()

    def register(self, **kw: Any) -> dict[str, Any]:
        self.registered.append(kw)
        return {"dataset_key": f"atlas.{cb.ATLAS_SOURCE}.{kw['dataset']}", "row_count": kw["frame"].height,
                "created": True, "artifact_sha256": "0" * 64}  # fmt: skip


_CABLES: dict[str, Any] = {}


def cables_run() -> tuple[pl.DataFrame, dict[str, Any], dict[str, Any]]:
    if not _CABLES:
        import asyncio

        run = _OfflineRun(cb.atlas_source())
        out = asyncio.run(cb.atlas_ingest(run))
        (kw,) = run.registered
        frame, _unmapped = validate_atlas_frame(kw["frame"], source="wikidata", dataset=cb.DATASET,
                                               raw_sha256s=kw["raw_sha256s"])  # fmt: skip
        _CABLES.update(frame=frame, out=out, kw=kw)
    return _CABLES["frame"], _CABLES["out"], _CABLES["kw"]


def test_wikidata_cables_pairs_and_subclasses() -> None:
    frame, out, kw = cables_run()
    rep = out["report"]
    assert rep["classes"] == ["Q2395922", "Q361047", "Q506572"]  # subclasses found via P279 and expanded
    # TAT-1 (instance of the transatlantic subclass) is a cable; TAT-8 too but lists no P17
    assert rep["cables"] == 20 and rep["cables_without_country"] == 2
    gbr_usa = _one(frame, "Q145", "CABLES_SHARED|partner=USA")
    assert gbr_usa is not None and gbr_usa["iso3"] == "GBR"
    f = _flags(gbr_usa)
    assert [c["label"] for c in f["cables"]] == ["Apollo"] and gbr_usa["value"] == "1"
    assert (
        f["partner_iso3"] == "USA" and f["undirected"] is True and f["verification"] == "community_edited_not_verified"
    )
    assert _one(frame, "Q30", "CABLES_SHARED|partner=GBR")["value"] == "1"  # type: ignore[index]  # both directions
    assert _one(frame, "Q145", "CABLE_FIRST_SERVICE_YEAR|partner=USA")["value"] == "2003"  # type: ignore[index]
    # TAT-1 (instance of the transatlantic subclass): P17 = United Kingdom, Canada; P729 1956-09-25
    first = _one(frame, "Q145", "CABLE_FIRST_SERVICE_YEAR|partner=CAN")
    assert first is not None and first["value"] == "1956" and first["unit"] == "year"
    assert _flags(first)["cables"][0]["year_property"] == "P729"
    assert _flags(first)["derived"] == "min"
    # every cable raw (EntityData snapshot) cited is registered with the table
    assert {c["raw_sha256"] for r in frame.to_dicts() for c in _flags(r).get("cables", [])} <= set(kw["raw_sha256s"])
    assert frame.filter(pl.col("value") == "0").height == 0
    # FARICE-1: inception 2004-01 (month precision) -> service year 2004 via P571
    fo = frame.filter((pl.col("provider_geo") == "Q189") & (pl.col("indicator") == "CABLES_ENTERING_SERVICE"))
    assert date(2004, 1, 1) in fo["period_start"].to_list()


def test_wikidata_cables_skip_telegeography_and_parse_guards() -> None:
    assert "non-commercial" in cb.SKIPPED["telegeography"]
    with pytest.raises(SourceResponseError):
        cb.parse_links(b"<html>nothing</html>", "Q1")
    e = {"id": "Q1", "claims": {"P31": [{"rank": "deprecated", "mainsnak": {"datavalue": {"value": {"id": "Q506572"}}}}],
         "P571": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"time": "+1900-00-00T00:00:00Z", "precision": 7}}}}]}}  # fmt: skip
    d = cb.describe(e)
    assert d["p31"] == [] and d["year"] is None  # deprecated rank ignored; century precision is no year


# --------------------------------------------------------------------------- platform contract
def test_sources_discovered_and_no_http_client_in_modules() -> None:
    names = set(atlas_sources())
    assert {fl.OF_NAME, fl.OA_NAME, sp.NAME, po.NAME, cb.NAME} <= names
    banned = ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen(", "requests.get(")
    for m in MODULES:
        p = REPO / "src/reva/adapters/connectors" / f"{m}.py"
        if p.exists():
            src = p.read_text()
            assert not any(b in src for b in banned), m


def test_catalog_series_exist_in_fixture_tables(
    gcat: tuple[dict[str, pl.DataFrame], dict[str, Any], dict[str, ic.Fetched]], of_files: dict[str, ic.Fetched]
) -> None:
    cat = load_catalog()
    concepts = [c for c in cat.by_domain("infrastructure")]
    assert concepts and all(c.provider_declared for c in concepts)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(fl, "MIN_OF_AIRPORTS", 10)
        m.setattr(fl, "MIN_OF_ROUTES", 10)
        m.setattr(fl, "MIN_OA_AIRPORTS", 10)
        m.setattr(po, "MIN_PORTS", 10)
        of = fl.build_openflights(of_files)[0].frame()
        oa = fl.build_ourairports(
            _fixture(fl.OA_NAME, fl.OA_BASE + "airports.csv", "a"), _fixture(fl.OA_NAME, fl.OA_BASE + "countries.csv", "c"),
            date(2026, 10, 16),
        )[0].frame()  # fmt: skip
        wpi = po.build(_fixture(po.NAME, po.URL, "w"))[0].frame()
    indicators = {
        ("openflights", "routes_2014"): set(of["indicator"]),
        ("ourairports", "airports"): set(oa["indicator"]),
        ("nga", "wpi"): set(wpi["indicator"]),
        ("gcat", "launches"): set(gcat[0]["launches"]["indicator"]),
        ("gcat", "payloads"): set(gcat[0]["payloads"]["indicator"]),
    }
    indicators[(cb.ATLAS_SOURCE, cb.DATASET)] = set(cables_run()[0]["indicator"])
    for c in concepts:
        for s in c.source_order:
            assert s.indicator in indicators[(s.source, s.dataset)], (c.id, s.key)
