"""V6MIGR human-mobility connectors (``atlas_migration``) over their recorded fixtures (real provider bytes; slices noted
in each manifest): UNHCR asylum applications / decisions, Eurostat monthly asylum applicants, IOM Missing Migrants,
IDMC national figures; and the mobility link kinds (``refugee_flow``, ``asylum_applications``)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from reva.adapters.connectors import atlas_migration as m
from reva.atlas.query import CELLS_SELECT, DDL
from reva.evidence.runner import FIXTURE_ROOT
from reva.kernel.errors import SourceResponseError
from reva.links.atlas_links import AtlasLinkReader
from reva.links.derive import assert_all_evidenced

ES_KEY = "M.PER.SY+UA.T.TOTAL+FRST.TOTAL.DE+FR+EU27_2020"
SHA = "a" * 64


def fx(conn: str, url: str) -> bytes:
    for e in json.loads((FIXTURE_ROOT / conn / "manifest.json").read_text()):
        if e["url"] == url:
            body: bytes = (FIXTURE_ROOT / conn / e["file"]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == e["sha256"]
            assert e.get("licence")
            return body
    raise KeyError(url)


def _unhcr(i: int) -> tuple[pl.DataFrame, dict[str, Any]]:
    ep = m.UNHCR_ENDPOINTS[i]
    chk = m.unhcr_check_totals(fx("atlas.unhcr_asylum", ep.check_url("SYR")), ep)
    return m.build_unhcr_asylum(ep, fx("atlas.unhcr_asylum", ep.download_url("SYR")), SHA, chk)


def _cells(df: pl.DataFrame, **eq: Any) -> list[dict[str, Any]]:
    f = df
    for k, v in eq.items():
        f = f.filter(pl.col(k) == v)
    return f.to_dicts()


# ------------------------------------------------------------------------------------------------ UNHCR
def test_unhcr_applications_bilateral_cells_checked_against_the_api() -> None:
    df, rep = _unhcr(0)
    assert rep["checked_against_api"] and rep["records"] == 2322 and rep["column_totals"] == {"applied": 2336949}
    c = _cells(df, provider_geo="DEU", indicator="applied|origin=SYR|authority=G|app_type=N|stage=NA|count=P")
    c = [x for x in c if x["period_start"].year == 2015]
    assert [(x["value"], x["unit"], x["iso3"]) for x in c] == [("158657", "persons", "DEU")]
    fl = json.loads(c[0]["flags"])
    assert fl["counterpart_iso3"] == "SYR" and fl["counterpart_role"] == "origin"
    assert json.loads(c[0]["selector"])["file"] == "asylum-applications.csv"
    assert set(df["unit"]) <= {"persons", "cases"}


def test_unhcr_decisions_zero_cells_are_no_figure_never_zero() -> None:
    df, rep = _unhcr(1)
    assert "0" not in set(df["value"])
    assert rep["dec_other_no_figure_omitted"] == 1411 and rep["dec_total_no_figure_omitted"] == 2
    assert rep["column_totals"]["dec_total"] == 2320360


def test_unhcr_download_disagreeing_with_the_api_is_refused() -> None:
    ep = m.UNHCR_ENDPOINTS[0]
    with pytest.raises(SourceResponseError, match="disagrees"):
        m.build_unhcr_asylum(ep, fx("atlas.unhcr_asylum", ep.download_url("SYR")), SHA, (2322, {"applied": 1}))
    # a changed CSV header is schema drift
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("asylum-applications.csv", "Year,Origin\n2020,SYR\n")
    with pytest.raises(SourceResponseError, match="header"):
        m.build_unhcr_asylum(ep, buf.getvalue(), SHA, None)


# ------------------------------------------------------------------------------------------------ Eurostat
def test_eurostat_monthly_applicants_keep_zeros_omit_not_available() -> None:
    flags = m.parse_obs_flags(fx("atlas.eurostat_asylum", m.ES_FLAGS_URL))
    assert flags["p"] == "provisional"
    df, rep = m.build_eurostat_asylum(fx("atlas.eurostat_asylum", m.es_tsv_url(ES_KEY)), SHA, flags)
    assert rep["series"] == 12 and rep["no_value"] == {"not_available": 308} and rep["reported_zeros"] == 50
    c = _cells(df, provider_geo="DE", indicator="asylum_applicants|applicant=FRST|citizen=SY")
    oct15 = [x for x in c if str(x["period_start"]) == "2015-10-01"]
    assert [(x["value"], x["iso3"], str(x["period_end"])) for x in oct15] == [("30215", "DEU", "2015-11-01")]
    assert json.loads(oct15[0]["flags"])["counterpart_iso3"] == "SYR"
    assert df.filter(pl.col("provider_geo") == "EU27_2020")["iso3"].null_count() > 0
    assert set(df.filter(pl.col("indicator").str.contains("citizen=TOTAL"))["concept_id"]) <= {None}
    with pytest.raises(ValueError):
        m.es_tsv_url("M.PER.SY")


# ------------------------------------------------------------------------------------------------ IOM MMP
def _mmp() -> tuple[pl.DataFrame, dict[str, Any]]:
    url, _meta = m.hdx_resource_url(fx("atlas.iom_mmp", m.MMP_PACKAGE_URL), m.MMP_PACKAGE, m.MMP_ORG, m.MMP_RESOURCE)
    return m.build_mmp(fx("atlas.iom_mmp", url), SHA)


def test_mmp_cells_are_labelled_reva_sums_of_provider_records() -> None:
    df, rep = _mmp()
    assert rep["records"] == 244 and rep["fully_identical_records"] == [[2, 3]] and not rep["unmapped_country_names"]
    lby = [x for x in _cells(df, provider_geo="Libya", indicator="dead_and_missing|cause=ALL")
           if str(x["period_start"]) == "2015-04-01"]  # fmt: skip
    assert [x["value"] for x in lby] == ["1022"]
    fl = json.loads(lby[0]["flags"])
    assert fl["provider_text_respelled"] == ["1,022"] and "computed by REVA" in fl["aggregation"]
    assert lby[0]["iso3"] == "LBY" and lby[0]["concept_id"] == "migration.iom.mmp_dead_and_missing_monthly"
    # the all-causes cell is the sum over the cause cells of the same country and month
    sums = (
        df.filter(
            pl.col("indicator").str.starts_with("dead_and_missing|")
            & ~pl.col("provider_geo").str.starts_with("mmp_region:")
        )
        .with_columns(
            pl.col("value").cast(pl.Int64), (pl.col("indicator") == "dead_and_missing|cause=ALL").alias("all")
        )
        .group_by("provider_geo", "period_start", "all")
        .agg(pl.col("value").sum())
        .pivot(on="all", index=["provider_geo", "period_start"], values="value")
    )
    assert sums.filter(pl.col("true") != pl.col("false")).height == 0
    # HTML-escaped provider names map only through the exact-name aliases
    esc = df.filter(pl.col("provider_geo").str.contains("&#039;")).select("provider_geo", "iso3").unique()
    assert set(esc["iso3"]) == {"CIV", "PRK", "LAO"}
    assert df.filter(pl.col("provider_geo").str.starts_with("mmp_region:"))["iso3"].null_count() > 0


# ------------------------------------------------------------------------------------------------ IDMC
def _idmc_body() -> bytes:
    url, _ = m.hdx_resource_url(
        fx("atlas.idmc_national", m.IDMC_PACKAGE_URL), m.IDMC_PACKAGE, m.IDMC_ORG, m.IDMC_RESOURCE
    )
    return fx("atlas.idmc_national", url)


def test_idmc_national_raw_and_rounded_kept_apart() -> None:
    df, rep = m.build_idmc_national(_idmc_body(), SHA)
    assert rep["years"] == [2008, 2024]
    syr = {x["indicator"]: x["value"] for x in _cells(df, iso3="SYR") if x["period_start"].year == 2023}
    assert syr["conflict_idps|rounded=no"] == "7248188" and syr["conflict_idps|rounded=yes"] == "7248000"
    assert (
        df.filter(pl.col("provider_geo") == "AB9")["iso3"].null_count()
        == df.filter(pl.col("provider_geo") == "AB9").height
    )


def test_idmc_changed_readme_definition_is_refused() -> None:
    body = _idmc_body()
    src = zipfile.ZipFile(io.BytesIO(body))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for i in src.infolist():
            b = src.read(i.filename)
            if i.filename == "xl/worksheets/sheet4.xml":
                b = b.replace(b"as a result of conflict and violence as of", b"as of")
            z.writestr(i.filename, b)
    with pytest.raises(SourceResponseError, match="README"):
        m.build_idmc_national(out.getvalue(), SHA)


# ------------------------------------------------------------------------------------------------ ingest (offline)
class _Run:
    def __init__(self, name: str, options: dict[str, Any]) -> None:
        from reva.atlas.ingest import AtlasRun
        from reva.atlas.sources import get_source
        from reva.evidence.runner import FixtureHttp

        self.spec = get_source(name).spec
        self._run = AtlasRun.offline(self.spec, FixtureHttp(name))
        self.origin, self.options = "test_fixture", options
        self.registered: list[dict[str, Any]] = []

    def wants(self, d: str) -> bool:
        return True

    async def get(self, url: str, **kw: Any) -> Any:
        return await self._run.get(url, **kw)

    def archive(self, raw: Any) -> str:
        return hashlib.sha256(raw.body).hexdigest()

    def register(self, **kw: Any) -> dict[str, Any]:
        self.registered.append(kw)
        return {"dataset_key": kw["dataset"], "row_count": kw["frame"].height, "created": True}


@pytest.mark.parametrize(
    ("name", "options", "datasets"),
    [
        ("atlas.unhcr_asylum", {"unhcr_origin": "SYR"}, ["asylum_applications", "asylum_decisions"]),
        ("atlas.eurostat_asylum", {"eurostat_key": ES_KEY}, ["asylum_applicants_m"]),
        ("atlas.iom_mmp", {}, ["mmp_monthly"]),
        ("atlas.idmc_national", {}, ["gidd_national"]),
    ],
)
def test_offline_ingest_registers_every_table_with_its_raw_artifacts(
    name: str, options: dict[str, Any], datasets: list[str]
) -> None:
    run = _Run(name, options)
    out = asyncio.run(m.atlas_ingest(run))
    assert out["ok"], out
    assert [r["dataset"] for r in run.registered] == datasets
    for r in run.registered:
        assert len(r["raw_sha256s"]) == 2 and set(r["frame"]["raw_sha256"]) <= set(r["raw_sha256s"])
        assert r["rights"]["licence"] and r["coverage_note"]
    if name == "atlas.idmc_national":
        assert run.registered[0]["rights"]["non_commercial"] is True


# ------------------------------------------------------------------------------------------------ links
def _reader(tmp: Path, frames: list[pl.DataFrame]) -> AtlasLinkReader:
    files = []
    for i, f in enumerate(frames):
        p = tmp / f"{hashlib.sha256(f'migr{i}'.encode()).hexdigest()}.parquet"
        f.write_parquet(p)
        files.append(p)
    con = duckdb.connect(str(tmp / "atlas.duckdb"))
    con.execute(DDL)
    listing = "[" + ",".join(f"'{fp}'" for fp in files) + "]"
    con.execute(
        f"""INSERT INTO series SELECT CAST(row_number() OVER (ORDER BY source, dataset, indicator) AS INTEGER),
                   source, dataset, indicator FROM (SELECT DISTINCT source, dataset, indicator FROM read_parquet({listing}))"""
    )
    for fp in files:
        con.execute(
            f"""CREATE OR REPLACE TEMP VIEW raw AS SELECT *, regexp_extract(filename, '([0-9a-f]{{64}})\\.parquet$', 1)
                AS table_sha FROM read_parquet('{fp}', filename = true)"""
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


def _pop_bilateral() -> pl.DataFrame:
    """A synthetic row shaped like A-UN's unhcr.pop_bilateral (indicator refugees|origin=<ISO3>, row = asylum)."""
    r = m.Rows("unhcr", "pop_bilateral")
    for origin, iso, v in (("SYR", "SYR", "100"), ("XXA", None, "7"), ("AFG", "AFG", "0")):
        r.add(indicator=f"refugees|origin={origin}", provider_geo="DEU", iso3="DEU", period=m.year_period(2020),
              value=v, unit="persons", flags={"counterpart_iso3": iso}, raw_sha256=SHA,
              selector={"item": "unhcr:bilateral:2020:p1", "record": 1})  # fmt: skip
    return r.frame()


def test_refugee_flow_and_asylum_application_links(tmp_path: Path) -> None:
    apps, _ = _unhcr(0)
    r = _reader(tmp_path, [apps, _pop_bilateral()])
    rf = r.dyadic(r.taxonomy.link("refugee_flow"), top_k=5)
    assert [(lk.subject, lk.object, lk.weight, lk.unit) for lk in rf.links] == [
        ("iso3:SYR", "iso3:DEU", "100", "persons")
    ]
    assert rf.skipped["partner_unmapped"] == 1 and rf.skipped["reported_zero"] == 1
    aa = r.dyadic(r.taxonomy.link("asylum_applications"), top_k=50)
    assert aa.links and all(lk.subject == "iso3:SYR" for lk in aa.links)
    deu15 = [lk for lk in aa.links if lk.object == "iso3:DEU" and lk.temporal.year == 2015]
    facets = {lk.basis["facet"]: lk.weight for lk in deu15}
    assert facets["authority=G,app_type=N,stage=NA"] == "158657"
    assert all("count=C" not in lk.basis["indicator"] for lk in aa.links)  # case-level series never weigh a link
    assert_all_evidenced(rf.links + aa.links)
    assert not r.taxonomy.link("remittance").atlas  # no open bilateral source: nothing derived
