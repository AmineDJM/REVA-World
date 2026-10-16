"""LINKS3: country links from A-INFRA's physical-network atlas tables — Wikidata submarine cables, OpenFlights
June-2014 routes, GCAT payloads by launch-site state — and the explicit, cited Q55 → NLD mapping hop.

The tables are built offline from A-INFRA's recorded fixtures (``tests/fixtures/source/atlas.{wikidata_cables,
openflights,gcat}``: trimmed real provider bytes) with the connectors' own builders, then loaded into a DuckDB
file exactly like :func:`reva.atlas.query._build_db` (same DDL, same ``CELLS_SELECT`` year attachment).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_cables as cb
from reva.adapters.connectors import atlas_flights as fl
from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors import atlas_space as sp
from reva.atlas.query import CELLS_SELECT, DDL
from reva.evidence.runner import FIXTURE_ROOT
from reva.links.atlas_links import AtlasDerivation, AtlasLinkReader, stored_in_full
from reva.links.derive import assert_all_evidenced
from reva.links.fixture import HOP_FIXTURE_FILE, load_fixture
from reva.links.geo_hops import hop_evidence_from_statements, load_hops
from reva.links.model import Link
from reva.links.store import link_from_row, link_row
from reva.links.taxonomy import REPO_ROOT, kg_allowlist, load_taxonomy

INFRA = ("submarine_cable", "air_routes_2014", "launch_service")
HOP_STATEMENT = "q55$3F58B793-57C8-4C66-87ED-403C7E298EBF"


# ---------------------------------------------------------------------------------------------- fixtures
def _fetched(conn: str, url: str, name: str) -> ic.Fetched:
    d = FIXTURE_ROOT / conn
    for e in json.loads((d / "manifest.json").read_text()):
        if e["url"] == url:
            body = (d / e["file"]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == e["sha256"]
            return ic.Fetched(name, url, body, e["sha256"], datetime.fromisoformat(e["retrieved_at"]))
    raise KeyError(url)


class _OfflineRun:
    """The cable connector's own fetch path over its recorded responses (no network, no database)."""

    def __init__(self) -> None:
        from reva.atlas.ingest import AtlasRun
        from reva.evidence.runner import FixtureHttp

        spec = cb.atlas_source()
        self._run = AtlasRun.offline(spec, FixtureHttp(spec.name))
        self.spec, self.origin = spec, "test_fixture"
        self.frames: list[pl.DataFrame] = []

    def wants(self, d: str) -> bool:
        return True

    async def get(self, url: str, **kw: Any) -> Any:
        return await self._run.get(url, **kw)

    def archive(self, raw: Any) -> str:
        return hashlib.sha256(raw.body).hexdigest()

    def register(self, **kw: Any) -> dict[str, Any]:
        self.frames.append(kw["frame"])
        return {"dataset_key": "x", "row_count": kw["frame"].height, "created": True, "artifact_sha256": "0" * 64}


_FRAMES: dict[str, pl.DataFrame] = {}


def frames() -> dict[str, pl.DataFrame]:
    """dataset -> atlas frame built by A-INFRA's builders from the recorded fixtures (cached)."""
    if _FRAMES:
        return _FRAMES
    run = _OfflineRun()
    asyncio.run(cb.atlas_ingest(run))
    _FRAMES["submarine_cables"] = run.frames[0]
    with pytest.MonkeyPatch.context() as m:
        m.setattr(fl, "MIN_OF_AIRPORTS", 10)
        m.setattr(fl, "MIN_OF_ROUTES", 10)
        m.setattr(sp, "MIN_LAUNCHES", 10)
        m.setattr(sp, "MIN_OBJECTS", 10)
        files = {f: _fetched(fl.OF_NAME, fl.OF_RAW_BASE + f, f) for f in fl.OF_FILES}
        _FRAMES["routes_2014"] = fl.build_openflights(files)[0].frame()
        idx = _fetched(sp.NAME, sp.INDEX_URL, "index.html")
        gfiles = {f: _fetched(sp.NAME, u, f) for f, u in sp.FILES.items()}
        _FRAMES["payloads"] = sp.build(gfiles, sp.parse_index(idx.body))[0]["payloads"].frame()
    return _FRAMES


def snapshot_reader(tmp: Path, tables: Iterable[pl.DataFrame], **kw: Any) -> AtlasLinkReader:
    """A snapshot DuckDB built like ``reva.atlas.query._build_db`` from atlas frames."""
    files = []
    for i, f in enumerate(tables):
        p = tmp / f"{hashlib.sha256(f'table{i}'.encode()).hexdigest()}.parquet"
        f.write_parquet(p)
        files.append(p)
    con = duckdb.connect(str(tmp / "atlas.duckdb"))
    con.execute(DDL)
    listing = "[" + ",".join(f"'{fp}'" for fp in files) + "]"
    con.execute(
        f"""INSERT INTO series SELECT CAST(row_number() OVER (ORDER BY source, dataset, indicator) AS INTEGER),
                   source, dataset, indicator
              FROM (SELECT DISTINCT source, dataset, indicator FROM read_parquet({listing}))"""
    )
    for fp in files:
        con.execute(
            f"""CREATE OR REPLACE TEMP VIEW raw AS SELECT *,
                regexp_extract(filename, '([0-9a-f]{{64}})\\.parquet$', 1) AS table_sha
                FROM read_parquet('{fp}', filename = true)"""
        )
        con.execute(f"CREATE OR REPLACE TEMP TABLE t AS {CELLS_SELECT}")
        con.execute("INSERT INTO cells SELECT * FROM t")
        con.execute("INSERT INTO cells_s SELECT * FROM t ORDER BY sid, year, iso3")
        con.execute("DROP TABLE t")
        con.execute("DROP VIEW raw")

    def rows(sql: str, params: Any) -> list[dict[str, Any]]:
        res = con.execute(sql, list(params))
        names = [d[0] for d in res.description]
        return [dict(zip(names, r, strict=True)) for r in res.fetchall()]

    return AtlasLinkReader(rows, snapshot_sha256="c" * 64, origin="test_fixture", **kw)


def hop_evidence() -> dict[str, Any]:
    return hop_evidence_from_statements(load_fixture(HOP_FIXTURE_FILE).statements)


@pytest.fixture()
def reader(tmp_path: Path) -> AtlasLinkReader:
    return snapshot_reader(tmp_path, frames().values())


@pytest.fixture()
def hop_reader(tmp_path: Path) -> AtlasLinkReader:
    return snapshot_reader(tmp_path, frames().values(), hop_evidence=hop_evidence())


def derive(r: AtlasLinkReader, kind: str, **kw: Any) -> AtlasDerivation:
    d = r.dyadic(load_taxonomy().link(kind), **kw)
    assert_all_evidenced(d.links)
    return d


def pair(links: Iterable[Link], a: str, b: str) -> list[Link]:
    return [lk for lk in links if {lk.basis["iso3"][0], lk.basis["iso3"][1]} == {a, b}]


# ---------------------------------------------------------------------------------------------- taxonomy
def test_taxonomy_declares_the_three_physical_network_kinds_honestly() -> None:
    tax = load_taxonomy()
    for k in INFRA:
        lt = tax.link(k)
        assert lt.status == "derivable" and lt.method == "atlas_dyadic" and lt.scope == "country_country"
        assert stored_in_full(lt) and lt.atlas and lt.atlas["caveat"]["fr"] and lt.atlas["licence"]
        assert lt.honesty["fr"].startswith("relié via") and lt.honesty["en"].startswith("connected via")
    assert not tax.link("submarine_cable").directed
    assert tax.link("air_routes_2014").directed and tax.link("launch_service").directed
    air = tax.link("air_routes_2014")
    assert "juin 2014" in air.labels["fr"] and "June 2014" in air.labels["en"] and "figées" in air.honesty["fr"]
    assert "ODbL" in air.atlas["caveat"]["en"] and "June 2014" in air.atlas["caveat"]["en"]  # type: ignore[index]
    assert "communautaire" in tax.link("submarine_cable").honesty["fr"]
    # a launch-site state is not the launcher's supplier: the wording never says "launched by"
    ls = tax.link("launch_service")
    assert "launched by" not in (ls.labels["en"] + ls.honesty["en"]) and "site" in ls.labels["en"]


def test_taxonomy_refuses_misused_physical_network_options() -> None:
    from reva.links.taxonomy import LINK_TYPES_FILE, validate_taxonomy

    doc = yaml.safe_load(LINK_TYPES_FILE.read_text())
    assert validate_taxonomy(doc) == []
    air = next(d for d in doc["derived_links"] if d["id"] == "air_routes_2014")
    air["derivation"]["atlas"]["union_selector_key"] = "where"
    air["derivation"]["atlas"]["mapping_hops"] = True
    air["derivation"]["atlas"]["companions"]["iso3"] = "ROUTE_AIRLINES"
    probs = " | ".join(validate_taxonomy(doc))
    assert "undirected pair only" in probs and "no hop of geo_mapping_hops.yaml" in probs
    assert "companion key iso3" in probs


def test_mapping_hop_registry_is_explicit_and_consistent() -> None:
    (h,) = [x for x in load_hops() if x.provider_code == "Q55"]
    qids = json.loads((REPO_ROOT / "registry/entities/country_qids.json").read_text())
    nld = qids["NLD"] if isinstance(qids.get("NLD"), str) else None
    if nld is None:  # {"countries": [{"iso3":..., "qid":...}]} shape
        from reva.adapters.connectors.atlas_geo_common import load_country_qids

        nld = load_country_qids()["NLD"]
    assert (h.iso3, h.target_qid, nld) == ("NLD", "Q29999", "Q29999")
    assert (h.subject, h.property, h.object) == ("Q55", "P17", "Q29999") and "P17" in kg_allowlist()
    assert h.applies_to == {("wikidata", "submarine_cables")}
    doc = yaml.safe_load((REPO_ROOT / "registry/links/geo_mapping_hops.yaml").read_text())
    assert doc["hops"][0]["citation"]["hop_statement"] == HOP_STATEMENT
    fx = load_fixture(HOP_FIXTURE_FILE)
    assert [s.statement_id for s in fx.statements] == [HOP_STATEMENT]
    assert not any(s.pid in ("P297", "P298") for s in fx.statements)  # deprecated ISO codes never loaded


# ---------------------------------------------------------------------------------------------- cables
def test_cables_are_one_undirected_link_per_pair_with_names_and_first_service_year(reader: AtlasLinkReader) -> None:
    d = derive(reader, "submarine_cable")
    f = frames()["submarine_cables"]
    mapped = f.filter(
        pl.col("indicator").str.starts_with("CABLES_SHARED|partner=") & pl.col("iso3").is_not_null()
    ).height
    assert len(d.links) * 2 == mapped  # both directions emitted by A-INFRA -> one link
    keys = [lk.key for lk in d.links]
    assert len(keys) == len(set(keys))
    (gu,) = pair(d.links, "GBR", "USA")
    assert not gu.directed and gu.weight == "1" and gu.unit == "cables"
    assert gu.temporal.kind == "snapshot" and gu.temporal.year == 2026  # retrieval day of the items
    assert gu.basis["first_service_year"] == 2003
    pids = sorted(e.pid or "" for e in gu.evidence)
    assert pids == [
        "CABLES_SHARED|partner=GBR",
        "CABLES_SHARED|partner=USA",
        "CABLE_FIRST_SERVICE_YEAR|partner=GBR",
        "CABLE_FIRST_SERVICE_YEAR|partner=USA",
    ]
    main = next(e for e in gu.evidence if e.pid == "CABLES_SHARED|partner=USA")
    assert main.note and "Apollo (Q618973" in main.note and json.loads(main.selector or "")["wikidata_entities"]
    assert [c["label"] for c in gu.basis["source_flags"]["cables"]] == ["Apollo"]
    assert "community" in gu.basis["caveat"]["en"].lower() and gu.basis["licence"].startswith("CC0")
    (cg,) = pair(d.links, "CAN", "GBR")
    assert cg.basis["first_service_year"] == 1956  # TAT-1, P729 1956-09-25
    assert not pair(d.links, "GBR", "NLD")  # Q55 rows without the KG hop statement: no link


def test_q55_is_not_attached_to_nld_without_the_kg_statement(reader: AtlasLinkReader) -> None:
    d = derive(reader, "submarine_cable")
    assert not any("NLD" in lk.basis["iso3"] for lk in d.links)
    # 5 rows of Q55 (row side) + 5 rows of partners folded as partner_code=Q55, all counted, none linked
    assert d.skipped["mapping_hop_without_kg_evidence"] == 10 and not d.mapped_via_hop


def test_q55_cables_reach_nld_only_through_the_cited_hop(hop_reader: AtlasLinkReader) -> None:
    d = derive(hop_reader, "submarine_cable")
    assert d.mapped_via_hop == {"Q55": 10} and not d.skipped.get("mapping_hop_without_kg_evidence")
    (gn,) = pair(d.links, "GBR", "NLD")
    assert gn.weight == "2" and gn.basis["first_service_year"] == 1999
    hop = [e for e in gn.evidence if e.kind == "kg_statement"]
    assert len(hop) == 1 and hop[0].statement_id == HOP_STATEMENT and hop[0].pid == "P17"
    assert hop[0].subject == "Q55" and hop[0].object == "Q29999" and hop[0].origin == "test_fixture"
    assert hop[0].note and "geo_mapping_hops.yaml" in hop[0].note
    mh = gn.basis["mapping_hop"]
    assert (mh["provider_code"], mh["iso3"], mh["statement_id"]) == ("Q55", "NLD", HOP_STATEMENT)
    assert {e.pid for e in gn.evidence if e.kind == "atlas_row"} >= {
        "CABLES_SHARED|partner=GBR",
        "CABLES_SHARED|partner_code=Q55",
    }
    assert len(pair(d.links, "NLD", "BEL")) == len(pair(d.links, "NLD", "NOR")) == 1
    # every other link is unchanged by the hop
    plain = derive(snapshot_reader_cached(), "submarine_cable")
    assert len(d.links) == len(plain.links) + 5
    # the pair view's filter reads the mapped codes
    one = hop_reader.dyadic(
        load_taxonomy().link("submarine_cable"),
        where="((row_iso3 = 'GBR' AND partner = 'NLD') OR (row_iso3 = 'NLD' AND partner = 'GBR'))",
    )
    assert [lk.weight for lk in one.links] == ["2"]


_PLAIN: dict[str, AtlasLinkReader] = {}


def snapshot_reader_cached() -> AtlasLinkReader:
    if not _PLAIN:
        import tempfile

        _PLAIN["r"] = snapshot_reader(Path(tempfile.mkdtemp()), frames().values())
    return _PLAIN["r"]


def _cable(qid: str, countries: list[str], year: int | None, label: str) -> dict[str, Any]:
    return {"qid": qid, "label": label, "revision": 1, "countries": countries, "year": year,
            "year_property": "P729" if year else None}  # fmt: skip


def test_direct_and_hop_rows_of_one_pair_are_united_never_summed(tmp_path: Path) -> None:
    """Q55 and Q29999 both listed: a cable listing both is counted once (union of the selectors' items)."""
    cables = [
        _cable("Q9001", ["Q55", "Q145"], 1990, "A"),  # Netherlands (Q55) - UK
        _cable("Q9002", ["Q29999", "Q145"], 2005, "B"),  # Kingdom of the Netherlands - UK
        _cable("Q9003", ["Q55", "Q29999", "Q145"], 1985, "C"),  # both items listed
    ]
    rb, _rep = cb.build(cables, {c["qid"]: "d" * 64 for c in cables}, date(2026, 10, 16))
    r = snapshot_reader(tmp_path, [rb.frame()], hop_evidence=hop_evidence())
    d = derive(r, "submarine_cable")
    (gn,) = pair(d.links, "GBR", "NLD")
    assert gn.weight == "3" and gn.basis["items"] == ["Q9001", "Q9002", "Q9003"]  # 2 + 2 listed, 3 distinct
    assert gn.basis["first_service_year"] == 1985 and "union" in gn.basis["combination"]
    assert sorted(map(sorted, gn.basis["combined_from"])) == [["Q145", "Q29999"], ["Q145", "Q55"]]
    assert gn.basis["mapping_hop"]["statement_id"] == HOP_STATEMENT
    assert not pair(d.links, "NLD", "NLD")  # Q55-Q29999 maps onto one country: never a self link
    # without the hop statement only the direct rows count
    (tmp_path / "plain").mkdir()
    d0 = derive(snapshot_reader(tmp_path / "plain", [rb.frame()]), "submarine_cable")
    (gn0,) = pair(d0.links, "GBR", "NLD")
    assert gn0.weight == "2" and "mapping_hop" not in gn0.basis


# ---------------------------------------------------------------------------------------------- air routes
def test_air_routes_are_directed_june_2014_with_stale_and_odbl_caveat(reader: AtlasLinkReader) -> None:
    d = derive(reader, "air_routes_2014")
    assert d.links and all(lk.directed and lk.temporal.year == 2014 for lk in d.links)
    for lk in d.links:
        assert "June 2014" in lk.basis["caveat"]["en"] and "ODbL" in lk.basis["caveat"]["en"]
        assert lk.basis["licence"].startswith("ODbL") and lk.basis["source_flags"]["data_as_of"] == "2014-06"
    (gm,) = [lk for lk in d.links if lk.basis["iso3"] == ["GBR", "MAR"]]
    (mg,) = [lk for lk in d.links if lk.basis["iso3"] == ["MAR", "GBR"]]
    assert gm.weight == "25" and gm.unit == "routes" and gm.basis["airport_pairs"] == 14 and gm.basis["airlines"] == 7
    assert mg.weight == "25" and mg.subject != gm.subject  # two directions, two links
    assert sorted(e.pid or "" for e in gm.evidence) == [
        "ROUTES_OPERATED|partner=MAR",
        "ROUTE_AIRLINES|partner=MAR",
        "ROUTE_AIRPORT_PAIRS|partner=MAR",
    ]
    f = frames()["routes_2014"]
    ops = f.filter(pl.col("indicator").str.starts_with("ROUTES_OPERATED|partner="))
    assert len(d.links) == ops.filter(pl.col("iso3").is_not_null()).height
    # an unmapped airport country (Netherlands Antilles) gives no link and is counted
    assert d.skipped["iso3_unmapped"] >= 2 and d.skipped["partner_unmapped"] >= 1


def test_air_route_text_states_the_2014_snapshot() -> None:
    from reva.links.service import describe

    lk = next(x for x in derive(snapshot_reader_cached(), "air_routes_2014").links if x.basis["iso3"] == ["ESP", "FRA"])
    t = describe(lk, {})
    assert "juin 2014" in t["fr"] and "= 19 routes" in t["fr"] and "June 2014" in t["en"]


# ---------------------------------------------------------------------------------------------- launches
def test_launch_service_is_owner_to_site_state_per_launch_year(reader: AtlasLinkReader) -> None:
    d = derive(reader, "launch_service")
    pf = frames()["payloads"].filter(pl.col("indicator").str.starts_with("PAYLOADS_LAUNCHED_FROM|partner="))
    mapped = pf.filter(pl.col("iso3").is_not_null())
    assert len(d.links) == mapped.height
    for r in mapped.to_dicts():
        lk = next(
            x
            for x in d.links
            if x.basis["iso3"] == [r["iso3"], json.loads(r["flags"])["partner_iso3"]]
            and x.temporal.year == r["period_start"].year
        )
        assert lk.weight == r["value"] and lk.directed and lk.unit == "payloads"
        assert lk.evidence[0].raw_sha256 == r["raw_sha256"] and lk.evidence[0].selector == r["selector"]
        assert lk.basis["source_flags"]["direction"] == "owner->site" and "GCAT" in lk.basis["caveat"]["en"]
    # ESA / Intersputnik / NATO payloads (intergovernmental owners) are never a country link
    assert d.skipped["iso3_unmapped"] == pf.filter(pl.col("iso3").is_null()).height


def test_stored_in_full_ignores_top_k(reader: AtlasLinkReader) -> None:
    for k in INFRA:
        assert len(derive(reader, k, top_k=1).links) == len(derive(reader, k).links)


def test_store_round_trip_keeps_notes_hops_and_caveats(hop_reader: AtlasLinkReader) -> None:
    links = [lk for k in INFRA for lk in derive(hop_reader, k).links]
    for lk in links:
        row = link_row(lk, 1, {})
        back = link_from_row(json.loads(json.dumps(row, default=str)))
        assert back.evidence == lk.evidence and back.weight == lk.weight and back.basis == dict(lk.basis)
        assert row["evidence"] and row["weight_num"] and row["unit"]
    gn = next(lk for lk in links if lk.link_type == "submarine_cable" and "NLD" in lk.basis["iso3"])
    assert link_row(gn, 1, {})["object_iso3" if gn.object == "iso3:NLD" else "subject_iso3"] == "NLD"


def test_hop_evidence_ignores_deprecated_or_other_statements() -> None:
    from reva.links.model import Stmt

    assert hop_evidence_from_statements([Stmt("x1", "Q55", "P17", "Q31")]) == {}
    assert hop_evidence_from_statements([Stmt("x2", "Q55", "P361", "Q29999")]) == {}
    ev = hop_evidence_from_statements([Stmt("x3", "Q55", "P17", "Q29999", rank="preferred")])
    assert ev["Q55"].statement_id == "x3"
