"""atlas.fed_nowcasts (V7, BEAT-NOWCAST): parsers on captured excerpts of the real provider files.

Every official nowcast cell is a projection keyed by its publication day and usable only from the next day;
reconstructed / pre-relaunch NY Fed cells carry no availability; RTDSM monthly vintages are usable from the first
day of the next month; truths come from the Philadelphia Fed first/second/third-release file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_fed_nowcasts as F
from reva.adapters.connectors.vintage_common import Rows
from reva.atlas.sources import MAX_ATLAS_RESPONSE_BYTES, discover
from reva.atlas.tables import validate_atlas_frame
from reva.evaluation.vintages import VintageAccessor, cells_from_atlas_frame

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "source" / F.NAME
SHA = "b" * 64


def _fixture(url: str) -> bytes:
    man = json.loads((FIX / "manifest.json").read_text())
    hits = [e for e in man if e["url"] == url]
    assert len(hits) == 1, url
    return bytes((FIX / hits[0]["file"]).read_bytes())


def _flags(df: pl.DataFrame) -> list[dict[str, Any]]:
    return [json.loads(f) for f in df["flags"]]


def _valid(df: pl.DataFrame) -> None:
    validate_atlas_frame(df, source=F.ATLAS_SOURCE, dataset=str(df["dataset"][0]), raw_sha256s=[SHA])


def test_spec_discovered_bounded_and_registered() -> None:
    found, errors = discover()
    assert not [e for e in errors if "fed_nowcasts" in json.dumps(e)]
    spec = found[F.NAME].spec
    assert spec.max_response_bytes <= MAX_ATLAS_RESPONSE_BYTES and spec.planned_requests <= 40
    assert set(spec.allowed_hosts) == {F.ATL_HOST, F.NY_HOST, F.CLE_HOST, F.PHL_HOST}
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (ROOT / "registry" / "sources").glob("*.yaml")}
    assert F.SOURCE_KEY in keys
    req = yaml.safe_load((ROOT / "registry" / "atlas" / "budget_requests" / "beat_nowcast.yaml").read_text())
    assert {r["source_key"] for r in req["requests"]} == {F.SOURCE_KEY}
    budgets = yaml.safe_load((ROOT / "registry" / "policies" / "budgets.yaml").read_text())
    assert F.SOURCE_KEY in json.dumps(budgets)


def test_fixture_manifest_matches_bytes_and_has_licences() -> None:
    man = json.loads((FIX / "manifest.json").read_text())
    assert len(man) == 11
    for e in man:
        assert hashlib.sha256((FIX / e["file"]).read_bytes()).hexdigest() == e["sha256"]
        assert e["licence"] and e["live_sha256"] and e["excerpt"]


def test_gdpnow_updates_are_projections_usable_the_next_day() -> None:
    rows = Rows()
    rep = F.parse_gdpnow(_fixture(F.GDPNOW_URL), raw_sha256=SHA, rows=rows)
    df = rows.frame()
    _valid(df)
    fl = _flags(df)
    proj = [(r, f) for r, f in zip(df.iter_rows(named=True), fl, strict=True) if f["estimate_kind"] == "projection"]
    assert proj and all(f["availability_basis"] == "fed_nowcast_publication_day" for _, f in proj)
    for r, f in proj:
        d = date.fromisoformat(f["vintage"])
        assert f["available_at"] == f"{date.fromordinal(d.toordinal() + 1).isoformat()}T00:00:00Z"
        assert r["indicator"] == f"GDPNOW|v={d.isoformat()}"
    deep = [f for _, f in proj if f["sheet"] == "TrackingDeepArchives"]
    assert deep and all(f["made_before_public_launch"] for f in deep)
    adv = df.filter(pl.col("indicator") == "GDPNOW_BEA_ADVANCE|v=advance")
    assert adv.height >= 2 and rep.notes["current_quarter"] == "2026Q3"
    # 2014Q1 advance estimate as recorded by the Atlanta Fed (published 2014-04-30)
    q1 = adv.filter(pl.col("period_start") == date(2014, 1, 1)).row(0, named=True)
    assert q1["value"] == "0.107933" and json.loads(q1["flags"])["bea_publication_date"] == "2014-04-30"


def test_nyfed_reconstructions_and_pre_relaunch_rows_have_no_availability() -> None:
    rows = Rows()
    F.parse_nyfed(_fixture(F.NYFED_URLS["2016"]), edition="2016", raw_sha256=SHA, rows=rows)
    F.parse_nyfed(_fixture(F.NYFED_URLS["2023"]), edition="2023", raw_sha256=SHA, rows=rows)
    df = rows.frame()
    _valid(df)
    for r, f in zip(df.iter_rows(named=True), _flags(df), strict=True):
        d = date.fromisoformat(f["vintage"])
        if f["model_edition"] == "2016":
            assert f["reconstruction"] is (r["period_start"] < date(2016, 1, 1))
        else:
            assert f["reconstruction"] is (d < date(2023, 9, 8))
        assert (f["available_at"] is None) is f["reconstruction"]
    with pytest.raises(F.FedNowcastError):
        F.parse_nyfed(_fixture(F.NYFED_URLS["2023"]), edition="2016", raw_sha256=SHA, rows=Rows())


def test_cleveland_dates_years_and_actuals() -> None:
    assert F.clev_date("01/14", (2020, 12)) == date(2021, 1, 14)
    assert F.clev_date("12/02", (2020, 12)) == date(2020, 12, 2)
    assert F.clev_date("08/20", (2013, 7)) == date(2013, 8, 20)
    assert F.clev_target("quarter", "2013:Q4") == (2013, 10)
    rows = Rows()
    F.parse_cleveland(_fixture(F.CLEV_URL.format(freq="month")), freq="month", raw_sha256=SHA, rows=rows)
    df = rows.frame()
    _valid(df)
    acts = df.filter(pl.col("indicator").str.contains("_ACTUAL"))
    assert acts.height >= 1
    for f in _flags(acts):
        assert f["estimate_kind"] == "published_history" and f["release_day"]
    proj = df.filter(~pl.col("indicator").str.contains("_ACTUAL"))
    assert all(f["estimate_kind"] == "projection" for f in _flags(proj))


def test_rtdsm_monthly_vintages_and_month_end_rule() -> None:
    assert F.rt_vintage("EMPLOY64M12", "EMPLOY") == (1964, 12)
    assert F.rt_vintage("EMPLOY26M9", "EMPLOY") == (2026, 9)
    assert F.rt_vintage("IPT10M1", "EMPLOY") is None
    rows = Rows()
    F.parse_rtdsm_monthly(_fixture(F.PHL_XLSX.format(file="employMvMd.xlsx")), file="employMvMd.xlsx", raw_sha256=SHA,
                          rows=rows)  # fmt: skip
    df = rows.frame()
    fl = _flags(df)
    assert {f["vintage"] for f in fl} == {"2010M01", "2010M02"}  # 2009M12 is before FIRST_VINTAGE
    assert all(f["available_at"] in ("2010-02-01T00:00:00Z", "2010-03-01T00:00:00Z") for f in fl)
    assert df["period_start"].min() >= date(1990, 1, 1)


def test_release_values_bounds_and_cells_select_as_of() -> None:
    rows = Rows()
    rep = F.parse_release_values(_fixture(F.PHL_XLSX.format(file=F.RELEASE_FILE)), raw_sha256=SHA, rows=rows)
    df = rows.frame()
    assert rep.notes["last_updated"] == "2026-09-30"
    first = df.filter((pl.col("indicator") == "ROUTPUT|v=first") & (pl.col("period_start") == date(2014, 1, 1)))
    f = json.loads(first["flags"][0])
    assert f["available_at"] == "2014-07-01T00:00:00Z" and f["availability_basis"] == "bea_release_fourth_month_bound"
    cells = cells_from_atlas_frame(df, dataset_key="atlas.fed_nowcasts.release_values", table_recorded_at=None)
    acc = VintageAccessor(cells, datetime(2014, 6, 30, tzinfo=UTC))
    got = acc.latest(family="philfed_release_values").rows
    assert got.filter(pl.col("period_start") == date(2014, 1, 1)).height == 0  # not yet provably published
    acc2 = VintageAccessor(cells, datetime(2014, 7, 1, tzinfo=UTC))
    assert acc2.latest(family="philfed_release_values").rows.filter(pl.col("period_start") == date(2014, 1, 1)).height


class _FixtureHttp:
    origin = "test_fixture"

    async def get(self, url: str, **_: Any) -> Any:
        from reva.adapters.http.safe_client import HttpResult

        return HttpResult(url, url, 200, {"content-type": "application/octet-stream"}, _fixture(url),
                          datetime(2026, 10, 17, tzinfo=UTC), 0.0, (), 1)  # fmt: skip


def test_ingest_registers_five_tables_offline() -> None:
    from reva.atlas.ingest import AtlasRun

    run = AtlasRun.offline(F.atlas_source(), _FixtureHttp())
    registered: list[str] = []
    run.archive = lambda raw: SHA  # type: ignore[method-assign]

    def reg(**kw: Any) -> dict[str, Any]:
        _valid(kw["frame"])
        assert kw["rights"]["licence"] and kw["raw_sha256s"]
        registered.append(kw["dataset"])
        return {"dataset_key": f"atlas.fed_nowcasts.{kw['dataset']}", "row_count": kw["frame"].height, "created": True}

    run.register = reg  # type: ignore[method-assign]
    out = asyncio.run(F.atlas_ingest(run))
    assert sorted(registered) == sorted(F.DATASETS)
    assert all(t["rows"] > 0 for t in out["tables"].values())
