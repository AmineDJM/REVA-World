"""A-GEO: CCKP 1991-2020 climatologies and INFORM natural-hazard scores (captured real responses, offline)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import polars as pl
import pytest

from reva.adapters.connectors import atlas_climate as cl
from reva.adapters.connectors import atlas_hazards as hz
from reva.atlas.tables import ATLAS_SCHEMA, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import RawAcquisition

T = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)


def _raw(item, body: bytes) -> RawAcquisition:  # type: ignore[no-untyped-def]
    return RawAcquisition(item=item, body=body, media_type="application/json", retrieved_at=T, response_metadata={},
                          request_fingerprint="fp", origin="test_fixture")


@pytest.fixture(scope="module")
def cckp() -> dict[str, RawAcquisition]:
    http = FixtureHttp(cl.CONNECTOR_NAME)
    return {it.key: _raw(it, asyncio.run(http.get(it.url)).body) for it in cl.fetch_items()}


@pytest.fixture(scope="module")
def cckp_rows(cckp: dict[str, RawAcquisition]) -> dict[str, list[dict]]:
    return cl.build(cckp, {k: f"{i:064x}" for i, k in enumerate(cckp, start=1)})


def test_cckp_rows_validate_and_map_kosovo(cckp_rows: dict[str, list[dict]]) -> None:
    for ds, rows in cckp_rows.items():
        f, unmapped = validate_atlas_frame(pl.DataFrame(rows, schema=ATLAS_SCHEMA), source="cckp", dataset=ds,
                                           raw_sha256s=sorted({r["raw_sha256"] for r in rows}), geo_scheme="cckp")
        assert [u["provider_geo"] for u in unmapped] == ["TWN"]
        ksv = f.filter(pl.col("provider_geo") == "KSV")
        assert ksv.height > 0 and set(ksv["iso3"].to_list()) == {"XKX"}
        assert set(f["period_start"].to_list()) == {date(1991, 1, 1)} and set(f["period_end"].to_list()) == {date(2021, 1, 1)}


def test_cckp_values_units_and_null_series(cckp_rows: dict[str, list[dict]]) -> None:
    cru = cckp_rows[cl.DS_CRU]
    fra = {r["indicator"]: r for r in cru if r["provider_geo"] == "FRA"}
    assert len(fra) == 2 + 4 * 12  # annual tas/pr + 12 months x tas/pr/tasmax/tasmin
    assert fra["tas|period=annual"]["unit"] == "°C" and fra["pr|period=annual"]["unit"] == "mm/year"
    assert fra["pr|month=01"]["unit"] == "mm/month"
    assert 10 < float(fra["tas|period=annual"]["value"]) < 14
    assert json.loads(fra["tas|month=07"]["selector"])["path"] == ["data", "tas", "FRA", "1991-07"]
    # provider-null series (BLM/BVT on the 0.5 degree grid) are omitted, never filled
    assert not [r for r in cru if r["provider_geo"] in ("BLM", "BVT")]
    era = {r["indicator"] for r in cckp_rows[cl.DS_ERA5] if r["provider_geo"] == "FRA"}
    assert era == {"tas|period=annual", "pr|period=annual"}


def test_cckp_monthly_annual_consistency_check(cckp: dict[str, RawAcquisition]) -> None:
    annual = cl.parse_response(cckp["cru_annual"].body, ("tas", "pr"))
    monthly = cl.parse_response(cckp["cru_monthly"].body, ("tas", "pr", "tasmax", "tasmin"))
    assert cl.check_consistency(annual, monthly) == []
    doc = json.loads(cckp["cru_annual"].body)
    doc["data"]["pr"]["FRA"] = {"1991-07": 2000}
    broken = cl.parse_response(json.dumps(doc).encode(), ("tas", "pr"))
    assert any(p.startswith("FRA") for p in cl.check_consistency(broken, monthly))


def test_cckp_refuses_empty_or_drifted_responses() -> None:
    with pytest.raises(SourceResponseError, match="incomplete|not an object"):
        cl.parse_response(b'{"metadata":{"status":"success"},"data":[]}', ("tas", "pr"))
    with pytest.raises(SourceResponseError, match="status"):
        cl.parse_response(b'{"metadata":{"status":"error"},"data":{}}', ("tas",))
    with pytest.raises(SourceResponseError, match="non-numeric"):
        cl.parse_response(b'{"metadata":{"status":"success"},"data":{"tas":{"FRA":{"1991-07":"x"}}}}', ("tas",))


@pytest.fixture(scope="module")
def inform() -> tuple[dict, bytes]:
    http = FixtureHttp(hz.CONNECTOR_NAME)
    wf = hz.parse_workflows(asyncio.run(http.get(hz.workflows_item().url)).body)
    return wf, asyncio.run(http.get(hz.scores_item(wf["id"]).url)).body


def test_inform_main_workflow_and_scores(inform: tuple[dict, bytes]) -> None:
    wf, body = inform
    assert wf["name"] == "INFORM Risk 2026"
    scores = hz.parse_scores(body)
    rows = hz.atlas_rows(scores, wf, "c" * 64)
    f, unmapped = validate_atlas_frame(pl.DataFrame(rows, schema=ATLAS_SCHEMA), source="inform",
                                       dataset=hz.ATLAS_DATASET, raw_sha256s=["c" * 64], geo_scheme="iso3")
    assert unmapped == []
    assert set(f["indicator"].unique().to_list()) == set(hz.INDICATORS)
    assert f.filter(pl.col("indicator") == "HA.NAT")["iso3"].n_unique() >= hz.MIN_COUNTRIES
    flags = json.loads(rows[0]["flags"])
    assert flags["nature"].startswith("modelled exposure index") and flags["period_basis"].startswith("edition year")


def test_inform_refuses_out_of_range_and_unknown() -> None:
    with pytest.raises(SourceResponseError, match="outside"):
        hz.parse_scores(b'[{"Iso3":"FRA","IndicatorId":"HA.NAT","IndicatorScore":11.2}]')
    with pytest.raises(SourceResponseError, match="unexpected indicator"):
        hz.parse_scores(b'[{"Iso3":"FRA","IndicatorId":"VU","IndicatorScore":1.0}]')
    with pytest.raises(SourceResponseError, match="exactly one workflow"):
        hz.parse_workflows(b'[{"WorkflowId":1,"Name":"INFORM Risk 2026 - 2017"}]')
    assert hz.parse_scores(b'[{"Iso3":"FRA","IndicatorId":"HA.NAT","IndicatorScore":null}]') == []


def test_specs() -> None:
    assert cl.CckpClimatologyConnector().describe().allowed_hosts == ("cckpapi.worldbank.org",)
    spec = hz.InformHazardsConnector().describe()
    assert spec.allowed_hosts == ("drmkc.jrc.ec.europa.eu",) and "confirm" in spec.licence
