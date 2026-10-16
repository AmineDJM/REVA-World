"""EVMAP: country links from the causality datasets — coded sanctions episodes (TIES / EUSANCT dyad-years, one link
per source, never summed), active OFAC programs, COLDAT and COW-entities colonial rule, and the same NCEI tsunami
observed in several countries. None is causal; each carries its licence / terms in ``basis``.

The sanctions tables are built offline with A-SANCTIONS' own builders from its recorded fixtures
(``tests/fixtures/source/atlas.{ties,eusanct,ofac_programs}``) and loaded into a DuckDB file exactly like
:func:`reva.atlas.query._build_db`; the colonial / hazard derivations run on statements shaped like A-COLONIAL's and
A-HAZARDS' KG loads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_sanctions as S
from reva.adapters.connectors.atlas_geopol_common import atlas_frame, crosswalk_from_countrycode_tarball, crosswalk_keys
from reva.atlas.query import CELLS_SELECT, DDL
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FIXTURE_ROOT
from reva.links.atlas_links import AtlasDerivation, AtlasLinkReader, spec_sources, stored_in_full
from reva.links.dataset_kg import DATASET_PROPERTIES, derive_dataset_links
from reva.links.derive import assert_all_evidenced
from reva.links.model import PartialDate, Stmt
from reva.links.store import link_from_row, link_row
from reva.links.taxonomy import LINK_TYPES_FILE, load_taxonomy, validate_taxonomy

SHA = "0" * 64
EVMAP = ("sanction_episode", "us_sanctions_program", "colonised_by_coldat", "colonial_rule_cow", "hazard_shared_event")


# ---------------------------------------------------------------------------------------------- fixtures
def _fixture(name: str, url: str) -> bytes:
    man = json.loads((FIXTURE_ROOT / name / "manifest.json").read_text())
    e = next(e for e in man if e["url"] == url)
    return (FIXTURE_ROOT / name / e["file"]).read_bytes()


_FRAMES: list[pl.DataFrame] = []


def frames() -> list[pl.DataFrame]:
    """ties.dyad_year, eusanct.dyad_year and ofac.programs built by A-SANCTIONS' builders (cached)."""
    if _FRAMES:
        return _FRAMES
    cw = crosswalk_from_countrycode_tarball(
        _fixture("atlas.ties", "https://cran.r-project.org/src/contrib/countrycode_1.9.0.tar.gz"), crosswalk_keys()
    )
    for ds, url, last, strict in (
        ("ties", S.TIES_URL, 2005, True),
        ("eusanct", S.EUSANCT_URL, S.EUSANCT_LAST_YEAR, False),
    ):
        cases, file = S.build_cases(ds, _fixture(f"atlas.{ds}", url), strict=strict)
        dyad, _ = S.case_atlas_rows(cases, raw_sha256=SHA, crosswalk=cw, last_year=last, file=file)
        _FRAMES.append(validate_atlas_frame(atlas_frame(dyad), source=ds, dataset="dyad_year", raw_sha256s=[SHA])[0])
    rows = S.ofac_rows(S.parse_ofac(_fixture("atlas.ofac_programs", S.OFAC_URL)), raw_sha256=SHA, day=date(2026, 10, 16))
    _FRAMES.append(validate_atlas_frame(atlas_frame(rows), source="ofac", dataset="programs", raw_sha256s=[SHA])[0])
    return _FRAMES


def snapshot_reader(tmp: Path, tables: Iterable[pl.DataFrame]) -> AtlasLinkReader:
    """A snapshot DuckDB built like ``reva.atlas.query._build_db`` from atlas frames (as LINKS3's tests)."""
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

    return AtlasLinkReader(rows, snapshot_sha256="c" * 64, origin="test_fixture")


@pytest.fixture(scope="module")
def reader(tmp_path_factory: pytest.TempPathFactory) -> AtlasLinkReader:
    return snapshot_reader(tmp_path_factory.mktemp("evmap_atlas"), frames())


def derive(r: AtlasLinkReader, kind: str, **kw: Any) -> AtlasDerivation:
    d = r.dyadic(load_taxonomy().link(kind), **kw)
    assert_all_evidenced(d.links)
    return d


# ---------------------------------------------------------------------------------------------- taxonomy
def test_taxonomy_declares_the_evmap_kinds_honestly() -> None:
    tax = load_taxonomy()
    for k in EVMAP:
        lt = tax.link(k)
        assert lt.status == "derivable" and lt.scope == "country_country" and lt.directed
        assert lt.honesty["fr"].startswith("relié via") and lt.honesty["en"].startswith("connected via")
        for w in ("cause", "because", "parce que", "effet de", "led to"):
            assert w not in (lt.labels["en"] + lt.honesty["en"] + lt.labels["fr"] + lt.honesty["fr"]).lower()
    se = tax.link("sanction_episode")
    assert se.atlas and spec_sources(se.atlas) == ["ties", "eusanct"] and se.atlas["facet_source"]
    assert set(se.atlas["licences"]) == {"ties", "eusanct"} and "cite" in se.atlas["licences"]["ties"]
    assert stored_in_full(se) and stored_in_full(tax.link("us_sanctions_program"))
    hz = tax.link("hazard_shared_event")
    assert "même événement naturel observé" in hz.labels["fr"] and hz.method == "kg_dataset_hazard"
    assert {"coldat:colonized_by", "cowent:colony_of", "ncei:runup_country"} <= set(DATASET_PROPERTIES)
    assert validate_taxonomy(yaml.safe_load(LINK_TYPES_FILE.read_text())) == []


# ---------------------------------------------------------------------------------------------- sanctions
def test_sanction_episode_one_link_per_source_with_its_terms(reader: AtlasLinkReader) -> None:
    d = derive(reader, "sanction_episode")
    assert d.links and d.considered > len(d.links)
    by_src: dict[str, list[Any]] = {}
    for lk in d.links:
        by_src.setdefault(lk.basis["source"], []).append(lk)
        assert lk.basis["facet"] == f"source={lk.basis['source']}"
        assert lk.evidence[0].source == lk.basis["source"] and lk.evidence[0].pid.startswith("CASES_ACTIVE|partner=")
        assert lk.temporal.kind == "snapshot" and lk.unit and int(lk.weight or "0") > 0
    assert set(by_src) == {"ties", "eusanct"}
    assert all("cite" in lk.basis["licence"] for lk in by_src["ties"])
    assert all(lk.basis["licence"].startswith("CC BY 4.0") for lk in by_src["eusanct"])
    # USA -> Cuba (TIES 1959-2013 case): sender -> target, a count of cases, the impositions companion cited
    us_cub = [lk for lk in by_src["ties"] if lk.basis["iso3"] == ["USA", "CUB"] and lk.temporal.year == 1960]
    assert len(us_cub) == 1
    lk = us_cub[0]
    assert lk.basis["reported_by"] == "USA" and lk.basis["caveat"]["fr"]
    if "impositions_active" in lk.basis:
        assert any(e.pid.startswith("IMPOSITIONS_ACTIVE|") for e in lk.evidence)
    # organisation senders (EU / UN / EEC 1000) are never mapped to a country: counted, not linked
    assert d.skipped.get("iso3_unmapped", 0) > 0
    # one store row per (source, pair, year): the facet keeps TIES and EUSANCT apart, and a row round-trips
    keys = {lk.key for lk in d.links}
    assert len(keys) == len(d.links)
    row = link_row(lk, 1, {"Q30": "USA", "Q241": "CUB"})
    assert row["facet"] == "source=ties" and link_from_row(row).basis["licence"] == lk.basis["licence"]


def test_us_sanctions_program_counts_programs_naming_the_country(reader: AtlasLinkReader) -> None:
    d = derive(reader, "us_sanctions_program")
    assert len(d.links) == 20  # 20 of 36 program titles name exactly one country
    assert all(lk.basis["iso3"][0] == "USA" and lk.basis["licence"].startswith("public domain") for lk in d.links)
    cub = next(lk for lk in d.links if lk.basis["iso3"][1] == "CUB")
    assert cub.weight == "1" and cub.temporal.year == 2026 and "cuba-sanctions" in json.dumps(cub.basis)


# ---------------------------------------------------------------------------------------------- colonial + hazard
def y(year: int) -> PartialDate:
    return PartialDate(year, 1, 1, 9)


def node_of(iso3: str) -> str:
    return {"BRA": "Q155", "PRT": "Q45", "IDN": "Q252", "LKA": "Q854", "IND": "Q668"}.get(iso3) or f"iso3:{iso3}"


QID_ISO3 = {"Q155": "BRA", "Q45": "PRT", "Q252": "IDN", "Q854": "LKA", "Q668": "IND"}


def test_coldat_spell_merges_equal_aggregations_and_cites_both() -> None:
    q = {"colonizer_as_coded": "portugal", "country_as_coded": "Brazil"}
    sts = [
        Stmt("c1", "Q155", "coldat:colonized_by", "Q45", start=y(1533), end=y(1822),
             qualifiers={**q, "aggregation": ("last date aggregation",)}, source_key="harvard.dataverse.coldat"),
        Stmt("c2", "Q155", "coldat:colonized_by", "Q45", start=y(1533), end=y(1822),
             qualifiers={**q, "aggregation": ("mean aggregation",)}, source_key="harvard.dataverse.coldat"),
    ]  # fmt: skip
    (lk,) = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)["colonised_by_coldat"].links
    assert (lk.subject, lk.object, lk.directed) == ("Q155", "Q45", True)
    assert {e.statement_id for e in lk.evidence} == {"c1", "c2"}
    assert lk.basis["aggregations"] == ["last date aggregation", "mean aggregation"] and "CC0" in lk.basis["licence"]
    assert (lk.temporal.first_year, lk.temporal.last_year) == (1533, 1822)
    # different years per aggregation: two links, each naming its aggregation
    sts[1] = Stmt("c2", "Q155", "coldat:colonized_by", "Q45", start=y(1530), end=y(1822),
                  qualifiers={**q, "aggregation": ("mean aggregation",)}, source_key="harvard.dataverse.coldat")  # fmt: skip
    two = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)["colonised_by_coldat"].links
    assert sorted(lk.basis["aggregation"] for lk in two) == ["last date aggregation", "mean aggregation"]


def test_cow_entity_colonial_rule_is_left_censored_at_1816_and_ignores_occupation() -> None:
    corr = Stmt("k1", "cowent:140", "geopol:corresponds_to_iso3", value="BRA", start=y(1822), end=y(2019),
                source_key="cow.terrchange")  # fmt: skip
    sts = [
        Stmt("s1", "cowent:140", "cowent:colony_of", "Q45", start=y(1816), end=y(1822), source_key="cow.terrchange"),
        Stmt("s2", "cowent:140", "cowent:occupied_by", "Q45", start=y(1816), end=y(1817), source_key="cow.terrchange"),
        corr,
    ]
    (lk,) = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)["colonial_rule_cow"].links
    assert (lk.subject, lk.object) == ("Q155", "Q45") and lk.basis["status"] == "colony"
    assert lk.basis["territory_mapping"] == "later_state" and lk.basis["left_censored"] is True
    assert {e.statement_id for e in lk.evidence} == {"s1", "k1"} and "cite" in lk.basis["licence"]


def test_hazard_shared_event_links_the_source_country_to_runup_countries_only() -> None:
    ev = "ncei_hazards:tsunami:5413"
    sts = [
        Stmt("t0", ev, "P585", value="2004-12-26", value_date=PartialDate(2004, 12, 26, 11), source_key="noaa.ncei_hazards"),
        Stmt("t1", ev, "ncei:country", "Q252", source_key="noaa.ncei_hazards"),
        Stmt("t2", ev, "ncei:tsunami_validity", value="4", source_key="noaa.ncei_hazards"),
        Stmt("r1", ev, "ncei:runup_country", "Q252", qualifiers={"runup_records": ("900",)}, source_key="noaa.ncei_hazards"),
        Stmt("r2", ev, "ncei:runup_country", "Q854", qualifiers={"runup_records": ("200",)}, source_key="noaa.ncei_hazards"),
        Stmt("r3", ev, "ncei:runup_country", "Q668", qualifiers={"runup_records": ("300",)}, source_key="noaa.ncei_hazards"),
        # an erroneous record (validity -1) and an event without a source country give no link
        Stmt("x1", "ncei_hazards:tsunami:9", "ncei:country", "Q252", source_key="noaa.ncei_hazards"),
        Stmt("x2", "ncei_hazards:tsunami:9", "ncei:tsunami_validity", value="-1", source_key="noaa.ncei_hazards"),
        Stmt("x3", "ncei_hazards:tsunami:9", "ncei:runup_country", "Q668", source_key="noaa.ncei_hazards"),
        Stmt("x4", "ncei_hazards:tsunami:10", "ncei:runup_country", "Q668", source_key="noaa.ncei_hazards"),
    ]  # fmt: skip
    d = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)["hazard_shared_event"]
    assert sorted((lk.subject, lk.object) for lk in d.links) == [("Q252", "Q668"), ("Q252", "Q854")]
    lk = d.links[0]
    assert lk.via == (ev,) and lk.temporal.kind == "point" and lk.temporal.point.iso() == "2004-12-26"  # type: ignore[union-attr]
    assert {"t0", "t1"} <= {e.statement_id for e in lk.evidence} and lk.weight is None
    assert d.skipped["erroneous_record"] == 1 and d.skipped["source_without_country"] == 1
    assert d.skipped["runup_in_source_country"] == 1
    # bounded: an event listing more runup countries than the bound is not expanded
    small = derive_dataset_links(sts, node_of=node_of, qid_iso3=QID_ISO3)
    assert small["hazard_shared_event"].links
    from reva.links.dataset_kg import derive_hazard_shared_event
    from reva.links.derive import KgIndex

    capped = derive_hazard_shared_event(KgIndex(sts), node_of, QID_ISO3, max_countries=2)
    assert not capped.links and capped.skipped["too_many_runup_countries"] == 1
