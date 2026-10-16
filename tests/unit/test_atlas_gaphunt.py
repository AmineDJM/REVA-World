"""GAPHUNT gap-fill atlas sources (offline): SPC PDH.Stat, Eurostat NUTS-2, INSEE BDM, CBS Caribbean NL.

Every test replays real provider bytes captured in ``tests/fixtures/source/atlas.gap_*`` (no network).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_gap_cbs_bes, atlas_gap_eurostat_nuts, atlas_gap_insee, atlas_gap_spc
from reva.adapters.connectors.atlas_gap_common import Fetched, strict_decimal
from reva.atlas.ingest import AtlasRun
from reva.atlas.sources import atlas_sources
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError, ValidationProblem

REPO = Path(__file__).resolve().parents[2]
MODULES = (atlas_gap_spc, atlas_gap_eurostat_nuts, atlas_gap_insee, atlas_gap_cbs_bes)


def _fetch(mod: Any) -> dict[str, Fetched]:
    spec = mod.atlas_source()
    run = AtlasRun.offline(spec, FixtureHttp(spec.name))

    async def go() -> dict[str, Fetched]:
        out = {}
        for it in mod.items(fixture=True):
            raw = await run.get(it.url, key=it.key, media_types=it.media_types, headers=dict(it.headers) or None)
            out[it.key] = Fetched(raw.body, hashlib.sha256(raw.body).hexdigest(), it.url, str(raw.retrieved_at))
        return out

    return asyncio.run(go())


def _tables(mod: Any) -> dict[str, Any]:
    got = _fetch(mod)
    spec = mod.atlas_source()
    out = {}
    for t in mod.build(got):
        frame, _unmapped = validate_atlas_frame(
            t.frame, source=spec.atlas_source, dataset=t.dataset, raw_sha256s=list(t.raw_sha256s), geo_scheme=t.geo_scheme
        )
        out[t.dataset] = frame
    return out


def test_sources_are_discovered_with_valid_specs() -> None:
    found = atlas_sources()
    for m in MODULES:
        spec = m.atlas_source()
        assert spec.name in found and found[spec.name].module == m.__name__
        assert spec.planned_requests == len(m.items())
        assert spec.code_digest.startswith("sha256:")
        assert all(it.url.startswith("https://" + spec.allowed_hosts[0] + "/") for it in m.items())
        assert set(spec.datasets) == {d for it in m.items() for d in it.datasets}
    # the Eurostat regional source shares A-ILO's Eurostat budget key (same provider service)
    assert atlas_gap_eurostat_nuts.atlas_source().budget_key == "eurostat.dissemination"


def test_sources_and_budget_requests_are_declared() -> None:
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    for m in MODULES:
        assert m.atlas_source().source_key in keys
    req = yaml.safe_load((REPO / "registry/atlas/budget_requests/gaphunt.yaml").read_text())
    asked = {r["source_key"] for r in req["requests"]}
    assert asked == {"spc.pdh_stat", "insee.bdm", "cbs.opendata"}
    for r in req["requests"]:
        mod = next(m for m in MODULES if m.atlas_source().source_key == r["source_key"])
        assert r["daily_requests"] >= 2 * mod.atlas_source().planned_requests


def test_geo_aliases_map_only_the_gap_entities() -> None:
    assert [country_iso3(c, "nuts_gaphunt") for c in ("FRY1", "FRY2", "FRY3", "FRY4", "FRY5", "FI20", "NO0B")] == [
        "GLP",
        "MTQ",
        "GUF",
        "REU",
        "MYT",
        "ALA",
        "SJM",
    ]
    assert country_iso3("FR", "nuts_gaphunt") is None and country_iso3("NO0B1", "nuts_gaphunt") is None
    assert country_iso3("D974", "insee_gaphunt") == "REU" and country_iso3("FE", "insee_gaphunt") is None
    assert country_iso3("CN01", "cbs_gaphunt") == "BES" and country_iso3("GM9001", "cbs_gaphunt") is None


# ----------------------------------------------------------------------------------------------- SPC
def test_spc_tables_cover_pacific_territories_and_drop_carried_forward_repeats() -> None:
    t = _tables(atlas_gap_spc)
    assert set(t) == {"pocket", "keyfacts", "emprates", "digital"}
    pocket = t["pocket"]
    assert set(pocket["iso3"].unique()) == {"NIU", "TKL", "WLF"}
    for r in pocket.iter_rows(named=True):
        flags = json.loads(r["flags"])
        assert flags["data_year"] == str(r["period_start"].year)  # never a repeat of an older value
    pop = pocket.filter(pocket["indicator"] == "MIDYEARPOPEST")
    assert pop.height > 0 and set(pop["unit"]) == {"units"}
    gdp = pocket.filter(pocket["indicator"] == "GDPCUSD")
    assert set(gdp["unit"]) == {"thousand USD"}  # multiplier spelled out, value not rescaled
    caps = t["keyfacts"].filter(t["keyfacts"]["indicator"] == "CAP")
    assert caps.height == 5 and caps["value"].null_count() == 5 and caps["value_text"].null_count() == 0
    assert set(t["emprates"]["indicator"]) == {"UNEMPRATE|sex=_T|age=_T|urbanization=_T|disability=_T"}
    assert set(t["digital"]["indicator"]) == {"DD024"}


def test_spc_counts_omitted_rows_in_the_note() -> None:
    got = _fetch(atlas_gap_spc)
    rows, stats = atlas_gap_spc.parse("pocket", got["data:pocket"])
    assert stats["carried_forward_omitted"] > 0
    assert stats["rows_read"] == len(rows) + stats["carried_forward_omitted"] + stats["missing_status_omitted"] + stats["empty_omitted"]


def test_spc_refuses_a_coded_cell_without_code() -> None:
    body = (
        "DATAFLOW,FREQ: Frequency,GEO_PICT: Geo,INDICATOR: Indicator,TIME_PERIOD: Time,OBS_VALUE\n"
        "SPC:DF_X(1.0),A: Annual,Niue,POP: Population,2020,1600\n"
    ).encode()
    with pytest.raises(SourceResponseError):
        atlas_gap_spc.parse("pocket", Fetched(body, "0" * 64, "u", "t"))


# ------------------------------------------------------------------------------------------ Eurostat
def test_eurostat_nuts_regions_fill_the_seven_entities() -> None:
    f = _tables(atlas_gap_eurostat_nuts)["nuts2_gapfill"]
    assert set(f["iso3"].unique()) == {"GLP", "MTQ", "GUF", "REU", "MYT", "ALA", "SJM"}
    pop = f.filter(f["indicator"] == "demo_r_d2jan|unit=NR|sex=T|age=TOTAL")
    assert set(pop["iso3"].unique()) == {"GLP", "MTQ", "GUF", "REU", "MYT", "ALA", "SJM"}
    assert all(json.loads(x)["reference"] == "stock on 1 January of the year" for x in pop["flags"])
    gdp = f.filter(f["indicator"] == "nama_10r_2gdp|unit=MIO_EUR")
    assert set(gdp["unit"]) == {"million EUR"}


def test_eurostat_label_change_fails_closed() -> None:
    got = _fetch(atlas_gap_eurostat_nuts)
    f = got["data:demo_r_d2jan"]
    body = f.body.replace("Svalbard og Jan Mayen".encode(), b"Somewhere else")
    with pytest.raises(SourceResponseError):
        atlas_gap_eurostat_nuts.parse_jsonstat("demo_r_d2jan", Fetched(body, f.sha256, f.url, f.retrieved_at))


# --------------------------------------------------------------------------------------------- INSEE
def test_insee_cpi_and_smic_for_overseas_departments() -> None:
    t = _tables(atlas_gap_insee)
    ipc = t["ipc_dom"]
    assert set(ipc["iso3"].unique()) == {"GLP", "MTQ", "GUF", "REU", "MYT"}
    infl = ipc.filter(ipc["indicator"].str.starts_with("CPI_ANNUAL_CHANGE"))
    assert set(infl["unit"]) == {"% change"} and infl.height >= 20
    smic = t["smic_dom"]
    assert set(smic["iso3"].unique()) == {"GLP", "MTQ", "GUF", "REU", "MYT"}
    dom = smic.filter(smic["provider_geo"] == "DOM")
    assert set(dom["iso3"].unique()) == {"GLP", "MTQ", "GUF", "REU"}
    assert all("applies_to_rule" in json.loads(x) for x in dom["flags"])
    assert set(smic["unit"]) == {"EUR per hour"}  # never turned into a monthly amount


def test_insee_refuses_dtd_and_scope_change() -> None:
    with pytest.raises(SourceResponseError):
        atlas_gap_insee.read_series(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>', "t")
    got = _fetch(atlas_gap_insee)
    f = got["data:SMIC"]
    body = f.body.replace("hors Mayotte".encode(), b"y compris Mayotte")
    with pytest.raises(SourceResponseError):
        atlas_gap_insee.parse_smic(Fetched(body, f.sha256, f.url, f.retrieved_at))


# ----------------------------------------------------------------------------------------------- CBS
def test_cbs_caribbean_netherlands_maps_total_and_keeps_islands_unmapped() -> None:
    t = _tables(atlas_gap_cbs_bes)
    assert set(t) == {"bes_population", "bes_gdp", "bes_labour"}
    for frame in t.values():
        for r in frame.iter_rows(named=True):
            if r["provider_geo"] == "CN01":
                assert r["iso3"] == "BES"
            else:
                assert r["iso3"] is None and json.loads(r["flags"])["partial_of_iso3"] == "BES"
    gdp = t["bes_gdp"].filter(t["bes_gdp"]["indicator"] == "gdp_current_prices")
    assert set(gdp["unit"]) == {"million USD"}
    une = t["bes_labour"].filter(t["bes_labour"]["indicator"].str.starts_with("unemployment_rate"))
    assert une.height > 0 and "0" not in set(une["value"])  # empty survey cells are never zeros


# ------------------------------------------------------------------------------------------- helpers
def test_strict_decimal_is_exact_and_never_guesses() -> None:
    assert strict_decimal("12.50") == "12.50"
    assert strict_decimal("1E+3") == "1E+3"  # already strict: kept verbatim
    assert strict_decimal(".5") == "0.5" and strict_decimal("+3") == "3"
    assert strict_decimal(" ") is None and strict_decimal(None) is None
    for bad in ("NaN", "1,000", "..", "inf"):
        with pytest.raises(ValidationProblem):
            strict_decimal(bad)


def test_proposals_follow_the_source_order_schema() -> None:
    import jsonschema

    schema = json.loads((REPO / "registry/atlas/source_order/_schema.json").read_text())
    doc = yaml.safe_load((REPO / "registry/atlas/source_order/_proposals_gaphunt.yaml").read_text())
    series_schema = {"$defs": schema["$defs"], **schema["$defs"]["series"]}
    concept_schema = {"$defs": schema["$defs"], **schema["$defs"]["concept"]}
    for p in doc["add_series"]:
        assert p["concept"].startswith("reva.")
        jsonschema.validate(p["entry"], series_schema)
    for c in doc.get("new_concepts", []):
        jsonschema.validate(c["concept"], concept_schema)
    for ne in doc.get("not_equivalent", []):
        jsonschema.validate(ne["entry"], {"$defs": schema["$defs"], **schema["$defs"]["not_equivalent"]})
