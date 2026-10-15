"""Offline tests of the FAOSTAT atlas connector (agent A-FAO).

Fixtures: tests/fixtures/source/atlas.fao/ — excerpts of the live FAOSTAT bulk zips of 2026-10-15 (code-list
members verbatim; data rows of a few areas/years plus out-of-scope, missing-flag and text-value rows; see
each manifest entry's ``excerpt`` and ``live_sha256``).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_fao
from reva.adapters.connectors.atlas_fao import (
    DOMAINS,
    DOMAINS_BY_DATASET,
    FaoParseError,
    fao_area_iso3,
    parse_domain,
    parse_listing,
)
from reva.atlas.sources import MAX_ATLAS_RESPONSE_BYTES, discover
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp, _fixture_fingerprint

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source" / "atlas.fao"
CATALOG = Path(__file__).resolve().parents[2] / "registry" / "atlas" / "catalog"


def _manifest() -> dict[str, dict[str, Any]]:
    return {e["url"]: e for e in json.loads((FIX / "manifest.json").read_text())}


def _body(url: str) -> bytes:
    e = _manifest()[url]
    b = (FIX / e["file"]).read_bytes()
    assert hashlib.sha256(b).hexdigest() == e["sha256"]
    return b


_CACHE: dict[str, tuple[pl.DataFrame, Any, list[dict[str, Any]]]] = {}


def _parsed(ds: str) -> tuple[pl.DataFrame, Any, list[dict[str, Any]]]:
    if ds not in _CACHE:
        d = DOMAINS_BY_DATASET[ds]
        body = _body(d.url)
        sha = hashlib.sha256(body).hexdigest()
        f, rep = parse_domain(d, body, sha)
        v, unmapped = validate_atlas_frame(f, source="fao", dataset=ds, raw_sha256s=[sha])
        _CACHE[ds] = (v, rep, unmapped)
    return _CACHE[ds]


def _cell(f: pl.DataFrame, **kw: Any) -> dict[str, Any]:
    q = f
    for k, v in kw.items():
        q = q.filter(pl.col(k) == v)
    assert q.height == 1, (kw, q.height)
    return q.row(0, named=True)


# ----------------------------------------------------------------------------- spec / discovery
def test_spec_is_discovered_and_bounded() -> None:
    spec = atlas_fao.atlas_source()
    assert spec.name == "atlas.fao" and spec.source_key == "fao.faostat" and spec.atlas_source == "fao"
    assert spec.allowed_hosts == ("bulks-faostat.fao.org",)
    assert set(spec.datasets) == {d.dataset for d in DOMAINS} and len(spec.datasets) == 19
    assert spec.max_response_bytes <= MAX_ATLAS_RESPONSE_BYTES
    assert spec.planned_requests >= len(DOMAINS) + 1  # listing + one zip per domain
    sources, errors = discover(["reva.adapters.connectors.atlas_fao"])
    assert "atlas.fao" in sources and errors == []


def test_every_domain_has_a_fixture_and_the_listing_names_it() -> None:
    man = _manifest()
    listing = parse_listing(_body(atlas_fao.LISTING_URL))
    for d in DOMAINS:
        assert d.url in man, d.code
        assert man[d.url]["request_fingerprint"] == _fixture_fingerprint(d.url)
        assert listing[d.code]["file_location"] == d.url, d.code  # FAO's own listing points at our URL
    assert listing["QCL"]["date_update"].startswith("20")


def test_registry_files_exist() -> None:
    root = Path(__file__).resolve().parents[2] / "registry"
    src = yaml.safe_load((root / "sources" / "atlas_fao.yaml").read_text())
    assert src["source_key"] == "fao.faostat"
    req = yaml.safe_load((root / "atlas" / "budget_requests" / "a_fao.yaml").read_text())
    assert req["requests"][0]["source_key"] == "fao.faostat"
    assert req["requests"][0]["daily_requests"] >= atlas_fao.atlas_source().planned_requests


# ----------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("ds", [d.dataset for d in DOMAINS])
def test_every_fixture_domain_parses_into_a_valid_atlas_table(ds: str) -> None:
    f, rep, _unmapped = _parsed(ds)
    assert f.height > 0 and rep.cells == f.height
    assert f["iso3"].drop_nulls().n_unique() >= 3
    d = DOMAINS_BY_DATASET[ds]
    items = f["indicator"].str.split("|").list.first()
    if d.items is not None:
        assert set(items.unique().to_list()) <= set(d.items)  # out-of-scope rows were dropped
    for flags in f["flags"].unique().to_list():
        obj = json.loads(flags)
        assert "flag" in obj and "flag_meaning" in obj
        assert obj["flag"] not in atlas_fao.MISSING_FLAGS
    sel = json.loads(f["selector"][0])
    assert sel["zip"] == d.file and sel["file"].endswith("_All_Data_(Normalized).csv") and sel["row"] >= 2


def test_area_mapping_uses_m49_and_keeps_aggregates_and_historical_states_unmapped() -> None:
    assert fao_area_iso3("'156") == "CHN"  # FAO 41 "China, mainland"
    assert fao_area_iso3("159") is None  # FAO 351 "China" (incl. Taiwan, Hong Kong, Macao): aggregate
    assert fao_area_iso3("158") is None  # Taiwan: not a crosswalk entity
    assert fao_area_iso3("'830") == "CHI"  # alias a_fao.yaml
    assert fao_area_iso3("810") is None and fao_area_iso3("736") is None  # USSR, Sudan (former)
    assert fao_area_iso3("729") == "SDN" and fao_area_iso3("231") == "ETH"
    assert fao_area_iso3("097.1203") is None and fao_area_iso3("") is None
    f, rep, unmapped = _parsed("rl")
    um = {u["provider_geo"] for u in unmapped}
    assert {"351", "5000", "228"} <= um
    assert rep.areas["228"]["name"] == "USSR" and rep.areas["228"]["years"][1] <= "1991"
    assert _cell(f, provider_geo="41", indicator="6600|5110", period_start=date(2021, 1, 1))["iso3"] == "CHN"
    assert f.filter(pl.col("provider_geo") == "68")["iso3"].unique().to_list() == ["FRA"]


def test_missing_flags_and_empty_values_produce_no_cell() -> None:
    f, rep, _ = _parsed("qcl")
    assert rep.dropped_missing_flag.get("M", 0) > 0  # "Missing value; data cannot exist"
    assert not f["flags"].str.contains('"flag":"M"').any()
    s, rep_s, _ = _parsed("sdgb")
    assert sum(rep_s.dropped_missing_flag.values()) > 0
    assert s["value"].is_not_null().sum() + s["value_text"].is_not_null().sum() == s.height


def test_estimated_and_imputed_rows_are_kept_and_flagged() -> None:
    f, rep, _ = _parsed("fbs")
    assert rep.flags.get("E", 0) > 0 and rep.flags.get("I", 0) > 0
    kinds = {json.loads(x).get("estimate_kind") for x in f["flags"].unique().to_list()}
    assert {"estimated", "imputed"} <= kinds
    e = json.loads(f.filter(pl.col("flags").str.contains('"flag":"E"'))["flags"][0])
    assert e["flag_meaning"] == "Estimated value"


def test_text_values_are_never_numbers() -> None:
    f, rep, _ = _parsed("fs")
    assert rep.text_cells > 0
    t = f.filter(pl.col("value_text").is_not_null())
    assert t["value"].is_null().all()
    assert set(t["value_text"].to_list()) <= {"<0.1", "<0.5", "<2.5", "<100"}


def test_values_are_verbatim_and_periods_follow_faostat_years() -> None:
    f, _, _ = _parsed("fbs")
    c = _cell(f, iso3="FRA", indicator="2901|664", period_start=date(2020, 1, 1))
    assert c["unit"] == "kcal/cap/d" and c["period_end"] == date(2021, 1, 1)
    assert "." not in c["value"] or c["value"].endswith("0") or c["value"][-1].isdigit()
    fs, _, _ = _parsed("fs")
    multi = fs.filter((pl.col("period_end") - pl.col("period_start")).dt.total_days() > 366)
    assert multi.height > 0  # "2000-2002" 3-year averages -> [2000, 2003)
    r = multi.row(0, named=True)
    assert r["period_end"].year - r["period_start"].year == 3
    cp, _, _ = _parsed("cp")
    months = cp.filter(pl.col("period_start").dt.month() == 12)
    assert months.height > 0 and (months["period_end"].dt.month() == 1).all()


def test_extra_dimensions_are_folded_into_the_indicator() -> None:
    f, _, _ = _parsed("oea")
    assert f["indicator"].str.contains(r"\|sex=\d+\|src=\d+$").all()
    g, _, _ = _parsed("gt")
    assert g["indicator"].str.contains(r"\|src=305[01]$").all()
    t, _, _ = _parsed("tcli")
    assert t["indicator"].str.contains(r"^\d+\|[0-9.]+$").all()  # <item>|<indicator>


def test_cp1252_member_is_read() -> None:
    _f, rep, _ = _parsed("fbsh")
    assert rep.encoding in ("cp1252", "utf-8")


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in members.items():
            z.writestr(n, b)
    return buf.getvalue()


HEAD = b"Area Code,Area Code (M49),Area,Item Code,Item,Element Code,Element,Year Code,Year,Unit,Value,Flag,Note\n"


def test_layout_surprises_are_refused() -> None:
    d = DOMAINS_BY_DATASET["rl"]
    with pytest.raises(FaoParseError):
        parse_domain(d, b"not a zip", "0" * 64)
    with pytest.raises(FaoParseError):  # no data member
        parse_domain(d, _zip({"x.csv": b"a\n"}), "0" * 64)
    row = b'"68","\'250","France","6600","Country area","5110","Area","2020","2020","1000 ha","1.5","A",""\n'
    bad_year = row.replace(b'"2020","2020"', b'"2020","20x0"')
    with pytest.raises(FaoParseError):
        parse_domain(d, _zip({"Inputs_LandUse_E_All_Data_(Normalized).csv": HEAD + bad_year}), "0" * 64)
    defs = b"Area Code,M49 Code,Area\n68,'251,France\n"
    with pytest.raises(FaoParseError):  # AreaCodes.csv disagrees with the rows
        parse_domain(
            d,
            _zip({"Inputs_LandUse_E_All_Data_(Normalized).csv": HEAD + row, "Inputs_LandUse_E_AreaCodes.csv": defs}),
            "0" * 64,
        )
    dup = HEAD + row + row
    with pytest.raises(FaoParseError):
        parse_domain(d, _zip({"Inputs_LandUse_E_All_Data_(Normalized).csv": dup}), "0" * 64)
    ok, rep = parse_domain(d, _zip({"Inputs_LandUse_E_All_Data_(Normalized).csv": HEAD + row}), "0" * 64)
    assert ok["iso3"].to_list() == ["FRA"] and ok["value"].to_list() == ["1.5"] and rep.cells == 1


def test_zero_reported_by_fao_is_kept_but_missing_never_becomes_zero() -> None:
    d = DOMAINS_BY_DATASET["rl"]
    rows = (
        b'"68","\'250","France","6600","Country area","5110","Area","2019","2019","1000 ha","0.000000","A",""\n'
        b'"68","\'250","France","6600","Country area","5110","Area","2020","2020","1000 ha","","M",""\n'
        b'"68","\'250","France","6600","Country area","5110","Area","2021","2021","1000 ha","0","O",""\n'
        b'"68","\'250","France","6600","Country area","5110","Area","2022","2022","1000 ha","","A",""\n'
    )
    f, rep = parse_domain(d, _zip({"Inputs_LandUse_E_All_Data_(Normalized).csv": HEAD + rows}), "0" * 64)
    assert f["value"].to_list() == ["0.000000"]  # FAO reported a zero (flag A): kept verbatim
    assert rep.dropped_missing_flag == {"M": 1, "O": 1} and rep.dropped_empty_value == 1


# ----------------------------------------------------------------------------- catalog
def test_catalog_series_point_at_declared_fao_datasets_and_fixture_indicators() -> None:
    declared = set(atlas_fao.atlas_source().datasets)
    for name in ("food_agriculture", "water"):
        doc = yaml.safe_load((CATALOG / f"{name}.yaml").read_text())
        assert doc["domain"] == name
        for c in doc["concepts"]:
            assert c["id"].startswith(f"{name}.") and c["provider_declared"] is True
            fao = [s for s in c["source_order"] if s.startswith("fao.")]
            assert fao, c["id"]
            for s in fao:
                _src, ds, ind = s.split(".", 2)
                assert ds in declared, s
                assert "|" in ind, s


# ----------------------------------------------------------------------------- ingest (no database)
class _FakeRun:
    origin = "test_fixture"

    def __init__(self, dataset: str | None = None) -> None:
        self.spec = atlas_fao.atlas_source()
        self.http = FixtureHttp(atlas_fao.CONNECTOR_NAME)
        self.dataset = dataset
        self.archived: dict[str, bytes] = {}
        self.registered: dict[str, Any] = {}

    def wants(self, ds: str) -> bool:
        assert ds in self.spec.datasets
        return self.dataset is None or self.dataset == ds

    async def get(self, url: str, **kw: Any) -> Any:
        r = await self.http.get(url)

        class Raw:
            body = r.body
            response_metadata = r.response_metadata()

        return Raw()

    def archive(self, raw: Any) -> str:
        sha = hashlib.sha256(raw.body).hexdigest()
        self.archived[sha] = raw.body
        return sha

    def register(self, *, dataset: str, frame: pl.DataFrame, raw_sha256s: list[str], **kw: Any) -> dict[str, Any]:
        assert set(raw_sha256s) <= set(self.archived)
        f, unmapped = validate_atlas_frame(
            frame, source="fao", dataset=dataset, raw_sha256s=raw_sha256s, geo_scheme=kw.get("geo_scheme")
        )
        assert kw["rights"]["licence"] == "CC-BY-4.0" and kw["coverage_note"].startswith("FAOSTAT")
        self.registered[dataset] = (f, kw)
        return {"dataset_key": f"atlas.fao.{dataset}", "row_count": f.height, "coverage": {"countries": 0}, "created": True}


def test_atlas_ingest_registers_every_domain_from_fixtures() -> None:
    run = _FakeRun()
    out = asyncio.run(atlas_fao.atlas_ingest(run))
    assert set(run.registered) == {d.dataset for d in DOMAINS}
    assert len(run.archived) == len(DOMAINS) + 1  # listing + one zip per domain
    assert out["listing_sha256"] in run.archived
    note = run.registered["oa"][1]["coverage_note"]
    assert "World Population Prospects" in note and "Historical areas" in note
    one = _FakeRun(dataset="rl")
    asyncio.run(atlas_fao.atlas_ingest(one))
    assert set(one.registered) == {"rl"} and len(one.archived) == 2
