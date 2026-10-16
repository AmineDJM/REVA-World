"""V3 SIMWORLD: reviewed built-in connectors ``un.wpp`` (WPP 2024 demography) and ``ilostat.labour_age5``.

Offline: excerpts of the real bytes retrieved 2026-10-15 (tests/fixtures/source/{un.wpp,ilostat.labour_age5}).
"""

from __future__ import annotations

import asyncio
import gzip
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "simworld"))

import simworld_fixtures as SF  # noqa: E402

from reva.adapters.connectors import ilostat_labour as IL  # noqa: E402
from reva.adapters.connectors import un_wpp_demography as W  # noqa: E402
from reva.adapters.connectors.registry import builtin_connectors  # noqa: E402
from reva.evidence.runner import FixtureHttp  # noqa: E402
from reva.kernel.errors import SourceResponseError  # noqa: E402
from reva.ports.connectors import FetchBudget  # noqa: E402


# --------------------------------------------------------------------------- WPP
def test_builtin_registration_and_pinned_files() -> None:
    conns = builtin_connectors()
    spec = conns[W.NAME].describe()
    assert spec.source_key == "un.wpp" and spec.allowed_hosts == ("population.un.org",)
    assert spec.max_response_bytes <= 52_428_800 and spec.template == "eurostat_sdmx_csv"
    assert conns[IL.NAME].describe().source_key == "ilostat.rplumber"
    c = W.UnWppDemographyConnector()
    items = c.all_items()
    # 62.1 MB -> 2 parts, 45.2 MB -> 1, 82.9 MB -> 2, 16.6 MB -> 1: six requests within un.wpp's 10/day
    assert [i.key for i in items] == ["pop:0", "pop:1", "deaths:0", "fert5:0", "fert5:1", "indicators:0"]
    for f in W.FILES:
        parts = [i for i in items if i.params["file_key"] == f.key]
        assert parts[0].params["range"][0] == 0 and parts[-1].params["range"][1] == f.size - 1
        assert all(i.params["range"][1] - i.params["range"][0] + 1 <= W.CHUNK_BYTES for i in parts)
        assert all("?bytes=" in i.url for i in parts) and len({i.url for i in parts}) == len(parts)
        assert f.etag and f.etag.startswith('"0x')
    plan = c.plan(None, FetchBudget(max_requests=4))
    assert len(plan.items) == 4 and plan.budget_exhausted


def test_chunk_ranges_cover_the_file_exactly() -> None:
    assert W.chunk_ranges(10, 4) == [(0, 3), (4, 7), (8, 9)]
    assert W.chunk_ranges(8, 4) == [(0, 3), (4, 7)]
    with pytest.raises(ValueError):
        W.chunk_ranges(0, 4)


def test_parts_validate_content_range_and_etag() -> None:
    c = SF.wpp_connector()
    http = FixtureHttp(W.NAME)
    item = c.all_items()[0]
    raw = asyncio.run(c.fetch(item, http))
    batch = next(iter(c.parse(raw)))
    assert batch.observations == () and c.validate(batch).outcome == "pass"
    md = dict(batch.metadata)
    for change, code in (
        ({"content_range": "bytes 0-9/10"}, "content_range_mismatch"),
        ({"status": 200}, "not_partial_content"),
        ({"byte_size": 3}, "size_mismatch"),
        ({"pinned_etag": '"a"', "etag": '"b"'}, "file_revised"),
        ({"gzip_magic": False}, "not_gzip"),
    ):
        bad = batch.model_copy(update={"metadata": {**md, **change}})
        rep = c.validate(bad)
        assert rep.outcome == "blocked" and code in {f.code for f in rep.findings}


def test_wpp_tables_are_exact_complete_and_filtered() -> None:
    tables, report = SF.wpp_tables()
    assert set(tables) == {"wpp2024_pop_jan1", "wpp2024_deaths", "wpp2024_births", "wpp2024_indicators"}
    for ds in tables:
        assert set(tables[ds]) == set(SF.COUNTRIES) and report[ds]["incomplete"] == {}
    pop = report["wpp2024_pop_jan1"]["parse"]
    assert pop["aggregate_rows"] > 0 and pop["out_of_range_year_rows"] > 0  # World row, SEN 2004 row dropped
    assert report["wpp2024_births"]["parse"]["other_variant_rows"] > 0  # Low variant dropped
    sen = tables["wpp2024_pop_jan1"]["SEN"].to_pylist()
    assert len(sen) == 19 * 2 * 101  # 2005-2023 x F/M x ages 0..99, 100+
    cell = next(r for r in sen if r["year"] == 2023 and r["sex"] == "F" and r["age"] == "Y0")
    assert Decimal(cell["value"]) == Decimal(cell["value_thousands"]) * 1000  # exact, never rounded
    top = next(r for r in sen if r["year"] == 2023 and r["sex"] == "M" and r["age"] == "Y_GE100")
    assert top["age_kind"] == "open_upper" and top["age_start"] == 100 and top["age_end"] is None
    births = tables["wpp2024_births"]["SEN"].to_pylist()
    groups = sorted({r["age"] for r in births if r["sex"] == "T" and r["age_kind"] == "group"})
    assert groups == [f"Y{a}-{a + 4}" for a in range(10, 55, 5)]
    by_sex = {r["sex"]: r for r in births if r["year"] == 2020 and r["age"] == "TOTAL"}
    assert Decimal(by_sex["M"]["value"]) + Decimal(by_sex["F"]["value"]) == Decimal(by_sex["T"]["value"])
    assert by_sex["M"]["derivation"].startswith("derived") and by_sex["T"]["derivation"].startswith("verbatim")
    ind = {r["age"]: r for r in tables["wpp2024_indicators"]["SEN"].to_pylist() if r["year"] == 2020}
    srb = Decimal(ind["SRB"]["value"])
    share = Decimal(by_sex["M"]["value"]) / Decimal(by_sex["T"]["value"])
    assert abs(share - srb / (100 + srb)) < Decimal("1e-9")


def test_incomplete_country_is_not_registered_and_drift_is_refused() -> None:
    rows = SF.wpp_table("wpp2024_pop_jan1", "SEN").to_pylist()
    holed = [r for r in rows if not (r["year"] == 2010 and r["sex"] == "F" and r["age"] == "Y42")]
    assert W.check_complete("wpp2024_pop_jan1", holed, 2005, 2023) == ["2010 F: 1 age cells missing"]
    parts = SF.wpp_parts()["indicators"]
    text = gzip.decompress(b"".join(parts)).decode("utf-8-sig").replace("NetMigrations", "NetMigration")
    with pytest.raises(SourceResponseError, match="header drift"):
        W.parse_indicators([gzip.compress(text.encode())], first_year=2005)
    with pytest.raises(SourceResponseError):
        W.parse_indicators([b"<html>no</html>"])
    with pytest.raises(SourceResponseError):  # a missing part: truncated gzip stream (CRC/length trailer)
        W.parse_single_age("pop", SF.wpp_parts()["pop"][:-1], first_year=2005)


# --------------------------------------------------------------------------- ILOSTAT
def test_ilostat_bands_status_and_area_mapping() -> None:
    body = (SF.fixture_root() / IL.NAME / "EMP_DWAP_SEX_AGE_RT_A.csv.gz").read_bytes()
    tables, rep = IL.build_tables("EMP_DWAP_SEX_AGE_RT_A", body)
    assert {"SEN", "JPN", "BRA", "XKX"} <= set(tables)  # ILOSTAT KOS -> crosswalk XKX (reviewed alias)
    assert "ANT" in rep["parse"]["unmapped_areas"] and "ANT" not in tables  # dissolved: listed, never mapped
    rows = tables["XKX"].to_pylist()
    assert {r["ref_area"] for r in rows} == {"KOS"}
    ages = {r["age"] for r in tables["JPN"].to_pylist()}
    assert {"Y55-59", "Y60-64", "Y_GE65"} <= ages and "Y65-69" not in ages  # no 65-69 band in ILOSTAT
    assert all(r["status"] in IL.OBS_STATUS for r in rows)
    with pytest.raises(SourceResponseError):
        IL.age_band("AGE_5YRBANDS_Y60-69")
    with pytest.raises(SourceResponseError):
        IL.build_tables("EMP_DWAP_SEX_AGE_RT_A", b"")


def test_ilostat_connector_parse_and_validate() -> None:
    c = IL.IlostatLabourConnector()
    http = FixtureHttp(IL.NAME)
    raw = asyncio.run(c.fetch(c.all_items()[0], http))
    batch = next(iter(c.parse(raw)))
    assert batch.observations == () and batch.metadata["countries"] >= 4
    assert c.validate(batch).outcome == "pass"
    empty = batch.model_copy(update={"metadata": {**batch.metadata, "countries": 0}})
    assert c.validate(empty).outcome == "blocked"
