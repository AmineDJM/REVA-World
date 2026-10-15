"""Politics atlas connectors (agent A-POL): V-Dem, LIED, BMR, IPU Parline -- offline, on fixtures."""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.connectors import atlas_ipu as ipu
from reva.adapters.connectors import atlas_rdata as rd
from reva.adapters.connectors import atlas_regimes as reg
from reva.adapters.connectors import atlas_vdem as vdem
from reva.atlas.catalog import load_catalog
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

sys.path.insert(0, str(Path(__file__).parent))
import atlas_pol_rdata_writer as w

SHA = "0" * 64


def _raws(module, connector: str, *args) -> dict:
    return asyncio.run(module.fetch_all(FixtureHttp(connector), *args))


# ----------------------------------------------------------------------------- RData reader
def _sample() -> bytes:
    cols = {
        "name": ("str", ["François", None, "plain"]),
        "x": ("real", [0.123, None, float("nan")]),
        "n": ("int", [1, None, -7]),
        "seq": w.Alt(5, 3),
    }
    return w.rdata({"other": w.data_frame({"a": ("real", [1.0])}, 1), "df": w.data_frame(cols, 3)})


def test_rdata_round_trip_keeps_na_nan_and_exact_text():
    raw = _sample()
    assert rd.rdata_object_names(raw) == ["other", "df"]
    f = rd.read_rdata_frame(raw, "df")
    assert f.all_columns == ("name", "x", "n", "seq") and f.nrows == 3 and f.classes == ("data.frame",)
    assert [rd.column_text(f.columns["name"], i) for i in range(3)] == ["François", None, "plain"]
    x = f.columns["x"]
    assert rd.column_text(x, 0) == "0.123"
    assert x.na.tolist() == [False, True, False] and x.nan.tolist() == [False, False, True]
    assert rd.column_text(x, 1) is None and rd.column_text(x, 2) is None  # NA and NaN never become numbers
    assert [rd.column_text(f.columns["n"], i) for i in range(3)] == ["1", None, "-7"]
    assert [rd.column_text(f.columns["seq"], i) for i in range(3)] == ["5", "6", "7"]  # ALTREP compact_intseq


def test_rdata_column_selection_and_errors():
    raw = _sample()
    f = rd.read_rdata_frame(raw, "df", ["x"])
    assert set(f.columns) == {"x"} and "name" in f.all_columns
    with pytest.raises(rd.RDataError, match="not in df"):
        rd.read_rdata_frame(raw, "df", ["nope"])
    with pytest.raises(rd.RDataError, match="not found"):
        rd.read_rdata_frame(raw, "missing", None)
    with pytest.raises(rd.RDataError, match="not an RData"):
        rd.read_rdata_frame(gzip.compress(b"PK\x03\x04junk"), "df")
    body = gzip.decompress(raw)
    with pytest.raises(rd.RDataError, match="truncated"):
        rd.read_rdata_frame(gzip.compress(body[: len(body) // 2]), "df")
    with pytest.raises(rd.RDataError, match="exceeds"):
        rd.read_rdata_frame(raw, "df", max_bytes=100)


def test_rdata_refuses_code_objects():
    # a closure (CLOSXP = 3) is never read
    head = b"RDX3\nX\n" + (3).to_bytes(4, "big") * 3 + (5).to_bytes(4, "big") + b"UTF-8"
    pl_ = (2 | 1 << 10).to_bytes(4, "big") + (1).to_bytes(4, "big") + (9 | 64 << 12).to_bytes(4, "big")
    pl_ += (1).to_bytes(4, "big") + b"f" + (3).to_bytes(4, "big")
    with pytest.raises(rd.RDataError, match="never read"):
        rd.read_rdata_frame(gzip.compress(head + pl_), "df")  # walking past the closure "f" refuses it


# ----------------------------------------------------------------------------- V-Dem
@pytest.fixture(scope="module")
def vdem_raws():
    return _raws(vdem, vdem.CONNECTOR_NAME)


@pytest.fixture(scope="module")
def vdem_frames(vdem_raws):
    data = vdem.read_vdem(vdem_raws["vdemdata.vdem"].body)
    return data, {
        ds: validate_atlas_frame(
            vdem.build_frame(data, ds, raw_sha256=SHA, release="16.0", qids=vdem.vdem_qids()),
            source="vdem",
            dataset=ds,
            raw_sha256s=[SHA],
            geo_scheme="vdem",
        )
        for ds in vdem.DATASETS
    }


def test_vdem_description_and_codebook(vdem_raws):
    desc = vdem.parse_description(vdem_raws["vdemdata.description"].body)
    assert vdem.dataset_version(desc) == "16.0"
    cb = vdem.parse_codebook(vdem_raws["vdemdata.codebook"].body)
    assert cb["v2x_polyarchy"]["name"] == "Electoral democracy index"
    assert cb["v2x_regime"]["responses"].startswith("0: Closed autocracy")
    with pytest.raises(SourceResponseError):
        vdem.parse_description(b"Package: other\nVersion: 1.0\n")


def test_vdem_reads_only_v_dem_variables(vdem_frames):
    data, _ = vdem_frames
    assert vdem.missing_variables(data.all_columns) == []
    assert not [c for c in data.columns if c.startswith("e_")]  # third-party "Others" never loaded


def test_vdem_values_flags_and_geo(vdem_frames):
    _, frames = vdem_frames
    ind, _ = frames["indices"]

    def cell(geo, var, year):
        f = ind.filter(
            (pl.col("provider_geo") == geo) & (pl.col("indicator") == var) & (pl.col("period_start").dt.year() == year)
        )
        return f.to_dicts()

    fr = cell("FRA", "v2x_polyarchy", 2024)
    assert fr[0]["value"] == "0.864" and fr[0]["iso3"] == "FRA" and fr[0]["unit"] == "index 0-1"
    assert json.loads(fr[0]["selector"])["column"] == "v2x_polyarchy"
    row = cell("FRA", "v2x_regime", 2024)[0]
    assert (row["value"], row["value_text"]) == ("3", "Liberal democracy")
    assert cell("FRA", "v2x_polyarchy_codelow", 2024)[0]["value"] < "0.864"
    de = cell("DEU", "v2x_polyarchy", 1800)[0]
    assert de["iso3"] == "DEU" and "Prussia" in json.loads(de["flags"])["vdem_histname"]
    assert cell("PSE", "v2x_polyarchy", 2024)[0]["iso3"] is None  # West Bank only: not the State of Palestine
    assert de["entity_qid"] == "Q27306"  # V-Dem histname "Kingdom of Prussia" -> Wikidata Kingdom of Prussia
    assert cell("DEU", "v2x_polyarchy", 2024)[0]["entity_qid"] is None  # crosswalk country: linked via iso3
    bvr = cell("BVR", "v2x_polyarchy", 1800)[0]
    assert bvr["iso3"] is None and bvr["entity_qid"] is None  # 1800 = Electorate of Bavaria: not mapped
    assert cell("PSE", "v2x_polyarchy", 2024)[0]["entity_qid"] == "Q36678"  # West Bank
    assert cell("TWN", "v2x_polyarchy", 2024)[0]["iso3"] == "TWN"  # crosswalk 1.1.0 (ISO 3166-1)
    assert cell("USA", "v2x_regime", 1800) == []  # RoW starts 1900: no invented cell
    acc = cell("FRA", "v2x_accountability", 2024)[0]
    assert acc["unit"].startswith("V-Dem latent") and cell("FRA", "v2x_accountability_osp", 2024)[0]["unit"].startswith(
        "index 0-1"
    )


def test_vdem_institutions_text_and_categories(vdem_frames):
    _, frames = vdem_frames
    inst, unmapped = frames["institutions"]
    hos = inst.filter(
        (pl.col("iso3") == "USA") & (pl.col("indicator") == "v2exnamhos") & (pl.col("period_start").dt.year() == 1789)
    )
    assert hos["value_text"].to_list() == ["George Washington"] and hos["value"].to_list() == [None]
    bicam = inst.filter(
        (pl.col("iso3") == "FRA") & (pl.col("indicator") == "v2lgbicam") & (pl.col("period_start").dt.year() == 2024)
    )
    assert bicam.select("value", "value_text").row(0) == ("2", "2 or more chambers")
    assert {u["provider_geo"] for u in unmapped} == {"PSE", "BVR"}
    lib, _ = frames["liberties"]
    assert lib.filter(pl.col("indicator") == "v2cltort_osp").height > 0


def test_vdem_unknown_category_code_is_refused(vdem_raws):
    data = vdem.read_vdem(vdem_raws["vdemdata.vdem"].body)
    saved = vdem.CATEGORY_LABELS["v2x_regime"]
    vdem.CATEGORY_LABELS["v2x_regime"] = {"0": "Closed autocracy"}
    try:
        with pytest.raises(SourceResponseError, match="no codebook label"):
            vdem.build_frame(data, "indices", raw_sha256=SHA, release="16.0")
    finally:
        vdem.CATEGORY_LABELS["v2x_regime"] = saved


# ----------------------------------------------------------------------------- LIED / BMR
@pytest.fixture(scope="module")
def regime_raws():
    return {**_raws(reg, reg.LIED_CONNECTOR, "lied"), **_raws(reg, reg.BMR_CONNECTOR, "bmr")}


def test_dataverse_record_checks(regime_raws):
    meta = reg.check_record(
        regime_raws["lied.record"].body, file_id=reg.LIED_FILE_ID, filename="x", payload=regime_raws["lied.file"].body
    )
    assert meta["licence"] == "CC0 1.0"
    with pytest.raises(SourceResponseError, match="does not match"):
        reg.check_record(regime_raws["lied.record"].body, file_id=reg.LIED_FILE_ID, filename="x", payload=b"other")
    doc = json.loads(regime_raws["bmr.record"].body)
    doc["data"]["latestVersion"]["license"]["name"] = "CC BY-NC 4.0"
    with pytest.raises(SourceResponseError, match="licence"):
        reg.check_record(
            json.dumps(doc).encode(), file_id=reg.BMR_FILE_ID, filename="x", payload=regime_raws["bmr.file"].body
        )


def test_lied_frame_geo_and_labels(regime_raws):
    from reva.adapters.connectors.atlas_xlsx import Workbook

    wb = Workbook(regime_raws["lied.file"].body)
    table = wb.table(next(iter(wb.sheets)))
    header = table[0]
    rows = [dict(zip(header, r, strict=False), _row=i) for i, r in enumerate(table[1:], start=2)]
    f, unmapped = validate_atlas_frame(
        reg.lied_frame(rows, raw_sha256=SHA, qids=vdem.qid_index()),
        source="lied",
        dataset="lied",
        raw_sha256s=[SHA],
        geo_scheme="lied",
    )
    geo = dict(zip(f["provider_geo"], f["iso3"], strict=True))
    assert geo["Prussia|vdem=77"] == "DEU" and geo["Germany, West|vdem=77"] == "DEU"
    assert geo["Vietnam|vdem=34"] == "VNM" and geo["Vietnam, North|vdem=34"] == "VNM"
    assert geo["Brunei|vdem=151"] == "BRN"
    q = dict(zip(zip(f["provider_geo"], f["period_start"].dt.year()), f["entity_qid"], strict=True))
    assert q[("Prussia|vdem=77", 1800)] == "Q27306" and q[("Bavaria|vdem=350", 1850)] == "Q154195"
    assert q[("Vietnam, North|vdem=34", 1960)] == "Q172640"
    assert {"Vietnam|vdem=35", "Bavaria|vdem=350", "Taiwan|vdem=48", "Palestine/West Bank|vdem=128"} <= {
        u["provider_geo"] for u in unmapped
    }
    fr = f.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "lexical_index")
        & (pl.col("period_start").dt.year() == 2024)
    )
    assert fr.select("value", "value_text").row(0) == ("6", "Electoral democracy")
    assert json.loads(fr["flags"][0])["reference_date"] == "2024-12-31"
    # no DEU year twice (Prussia / Germany / West Germany are consecutive lineages)
    de = f.filter((pl.col("iso3") == "DEU") & (pl.col("indicator") == "lexical_index"))
    assert de["period_start"].is_duplicated().sum() == 0


def test_partial_files_are_refused(regime_raws):
    with pytest.raises(SourceResponseError, match="partial"):
        reg.parse_lied(regime_raws["lied.file"].body)  # the fixture is a subset (< 20,000 rows)
    with pytest.raises(SourceResponseError, match="partial"):
        reg.parse_bmr(regime_raws["bmr.file"].body)


def test_atlas_sources_are_declared():
    from reva.atlas.sources import discover

    found, errors = discover(
        [
            "reva.adapters.connectors.atlas_vdem",
            "reva.adapters.connectors.atlas_regimes",
            "reva.adapters.connectors.atlas_ipu",
        ]
    )
    assert errors == []
    assert set(found) == {"atlas.vdem", "atlas.lied", "atlas.bmr", "atlas.ipu"}
    assert found["atlas.vdem"].spec.datasets == vdem.DATASETS
    assert found["atlas.ipu"].spec.licence.startswith("CC-BY-NC-SA")
    for e in found.values():
        assert (Path(__file__).parents[2] / "tests" / "fixtures" / "source" / e.spec.name / "manifest.json").exists()


def test_bmr_frame(regime_raws):
    text = regime_raws["bmr.file"].body.decode()
    import csv
    import io

    rows = [dict(r, _row=i) for i, r in enumerate(csv.DictReader(io.StringIO(text)), start=2)]
    f, unmapped = validate_atlas_frame(
        reg.bmr_frame(rows, raw_sha256=SHA, qids=vdem.qid_index()),
        source="bmr",
        dataset="bmr",
        raw_sha256s=[SHA],
        geo_scheme="bmr",
    )
    geo = dict(zip(f["provider_geo"], f["iso3"], strict=True))
    assert geo["2"] == "USA" and geo["255"] == "DEU" and geo["490"] == "COD" and geo["365"] == "RUS"
    assert {u["provider_geo"] for u in unmapped} == {"260", "364", "713"}  # West Germany, USSR, Taiwan
    q = dict(zip(f["provider_geo"], f["entity_qid"], strict=True))
    assert (q["260"], q["364"], q["713"], q["2"]) == ("Q713750", "Q15180", "Q865", None)
    fr = f.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "democracy")
        & (pl.col("period_start").dt.year().is_in([1870, 1871]))
    )
    assert fr.sort("period_start")["value"].to_list() == ["0", "1"]  # v4.0: France democratises in 1871
    omitted = f.filter((pl.col("indicator") == "democracy_omitteddata") & pl.col("value").is_null())
    assert omitted.height > 0
    assert {json.loads(x)["missing"] for x in omitted["flags"]} == {"not_applicable"}


# ----------------------------------------------------------------------------- IPU
@pytest.fixture(scope="module")
def ipu_data():
    raws = _raws(ipu, ipu.CONNECTOR_NAME)
    return raws, {k.split(".", 1)[1]: ipu.parse_response(r.body, k) for k, r in raws.items()}


def test_ipu_response_checks(ipu_data):
    raws, _ = ipu_data
    doc = json.loads(raws["ipu.countries"].body)
    doc["meta"]["licence"]["en"] = "CC BY 4.0"
    with pytest.raises(SourceResponseError, match="licence"):
        ipu.parse_response(json.dumps(doc).encode(), "countries")
    doc = json.loads(raws["ipu.countries"].body)
    doc["meta"]["total"] = 193
    with pytest.raises(SourceResponseError, match="truncated"):
        ipu.parse_response(json.dumps(doc).encode(), "countries")


def test_ipu_parline_frame(ipu_data):
    raws, d = ipu_data
    retrieved = ipu.date_accessed(raws["ipu.chambers"].body)
    shas = {k: SHA for k in ("chambers", "parliaments", "countries")}
    frame, _skipped = ipu.parline_frame(d["chambers"], d["parliaments"], d["countries"], raw=shas, retrieved=retrieved)
    f, unmapped = validate_atlas_frame(frame, source="ipu", dataset="parline", raw_sha256s=[SHA], geo_scheme="iso2")
    assert unmapped == [] and set(f["iso3"].unique()) == {"FRA", "NAM"}  # "NA" is Namibia, never null
    ye = f.filter(
        (pl.col("iso3") == "FRA") & (pl.col("indicator") == "current_women_percent|chamber=lower|at=year_end")
    )
    y2024 = ye.filter(pl.col("period_start").dt.year() == 2024).to_dicts()[0]
    assert y2024["value"] == "36.173913043478265"
    flags = json.loads(y2024["flags"])
    assert flags["selection"] == "value valid on 2024-12-31" and flags["provider_interval"].startswith("2024-12-30/")
    assert ye["period_start"].dt.year().min() == 1945
    assert ye.filter(pl.col("period_start").dt.year() >= retrieved.year).height == 0  # 31 Dec not reached yet
    exact = f.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "current_women_percent|chamber=lower"))
    cur = exact.filter(pl.col("flags").str.contains("open_ended"))
    assert cur.height == 1 and cur["period_end"][0] > retrieved
    for row in exact.iter_rows(named=True):  # exact provider intervals: no year-end flag
        assert "selection" not in json.loads(row["flags"])


def test_ipu_elections_frame(ipu_data):
    raws, d = ipu_data
    retrieved = ipu.date_accessed(raws["ipu.elections"].body)
    frame, _ = ipu.elections_frame(d["elections"], raw_sha256=SHA, retrieved=retrieved)
    f, unmapped = validate_atlas_frame(frame, source="ipu", dataset="elections", raw_sha256s=[SHA], geo_scheme="iso2")
    assert unmapped == []
    t = f.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "turnout|chamber=lower")
        & (pl.col("period_start").dt.year() == 2024)
    ).to_dicts()[0]
    assert (
        t["value"] == "66.7120334091984"
        and str(t["period_start"]) == "2024-06-30"
        and str(t["period_end"]) == "2024-07-08"
    )
    dates = f.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "election_date|chamber=lower"))
    assert "2024-06-30/2024-07-07" in dates["value_text"].to_list()


# ----------------------------------------------------------------------------- catalog / geo schemes
def test_politics_catalog_series_exist(vdem_frames, ipu_data):
    cat = load_catalog()
    concepts = [c for c in cat.concepts.values() if c.domain == "politics"]
    assert len(concepts) > 100
    data, _ = vdem_frames
    planned = {(ds, s.column) for ds in vdem.DATASETS for s in vdem.plan_series(data.all_columns, ds)}
    raws, d = ipu_data
    retrieved = ipu.date_accessed(raws["ipu.chambers"].body)
    pf, _ = ipu.parline_frame(
        d["chambers"],
        d["parliaments"],
        d["countries"],
        raw={k: SHA for k in ("chambers", "parliaments", "countries")},
        retrieved=retrieved,
    )
    ef, _ = ipu.elections_frame(d["elections"], raw_sha256=SHA, retrieved=retrieved)
    ipu_ind = {("parline", i) for i in pf["indicator"].unique()} | {("elections", i) for i in ef["indicator"].unique()}
    for c in concepts:
        assert c.labels["fr"] and c.labels["en"]
        for s in c.source_order:
            if s.source == "vdem":
                assert (s.dataset, s.indicator) in planned, (c.id, s)
            elif s.source == "lied":
                assert s.dataset == "lied" and s.indicator in reg.LIED_INDICATORS, (c.id, s)
            elif s.source == "bmr":
                assert s.dataset == "bmr" and s.indicator in reg.BMR_INDICATORS, (c.id, s)
            else:  # year-end / year variants are derived mechanically from their base field
                base = s.indicator.replace("|at=year_end", "").replace("|at=year", "")
                assert s.source == "ipu" and (s.dataset, base) in ipu_ind, (c.id, s)
        if any(s.source == "ipu" for s in c.source_order):
            assert "non_commercial" in c.tags and "NON-COMMERCIAL" in (c.notes or "")


def test_geo_schemes():
    assert country_iso3("PSE", "vdem") is None and country_iso3("FRA", "vdem") == "FRA"
    assert country_iso3("XKX", "vdem") == "XKX"
    assert country_iso3("Prussia|vdem=77", "lied") == "DEU"
    assert country_iso3("260", "bmr") is None and country_iso3("360", "bmr") == "ROU"


def test_qid_crosswalk_shape():
    path = vdem.QIDS_PATH
    if not path.exists():
        pytest.skip("registry/entities/vdem_qids.json not built")
    doc = json.loads(path.read_text())
    assert doc["review_status"].startswith("agent_mapped")
    used = set()
    for u in doc["units"]:
        assert u["qid"] is None or (u["qid"].startswith("Q") and u["qid"][1:].isdigit())
        assert u["method"]
        for p in u["periods"]:
            assert p["from"] <= p["to"]
            if p["qid"]:
                assert p["method"]
                used.add(p["qid"])
    used |= {o["qid"] for o in doc["other_units"] if o["qid"]} | {u["qid"] for u in doc["units"] if u["qid"]}
    assert used <= set(doc["evidence"])  # every QID carries its verification evidence
    assert len(doc["units"]) >= 202
