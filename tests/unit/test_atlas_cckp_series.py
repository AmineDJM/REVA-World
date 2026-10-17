"""V4CLIMATE: CCKP country climate series (CRU/ERA5 observed, CMIP6 projections) on captured real responses.

Fixtures: tests/fixtures/source/atlas.cckp_series (CC BY 4.0, World Bank CCKP), captured 2026-10-17 for the
restricted geocode list FRA,KSV,BVT,MLI (the run option ``geos``) instead of ``all_countries``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from reva.adapters.connectors import atlas_cckp_series as cs
from reva.atlas.tables import ATLAS_SCHEMA, validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

GEOS = ["FRA", "KSV", "BVT", "MLI"]


@pytest.fixture(scope="module")
def bodies() -> dict[str, bytes]:
    http = FixtureHttp(cs.NAME)
    return {it.key: asyncio.run(http.get(cs.item_url(it, GEOS))).body for it in cs.ITEMS}


@pytest.fixture(scope="module")
def built(bodies: dict[str, bytes]) -> tuple[dict[str, list[dict]], dict]:
    return cs.build(bodies, {k: f"{i:064x}" for i, k in enumerate(bodies, start=1)}, whole_world=False)


def test_items_and_urls() -> None:
    assert len(cs.ITEMS) == 4 + 3 * 5 + 1 + 16
    assert cs.item_url(cs.BY_KEY["cru_monthly"]).endswith("/all_countries?_format=json")
    assert cs.item_url(cs.BY_KEY["cru_monthly"], ["MLI", "FRA"]).endswith("/FRA,MLI?_format=json")
    with pytest.raises(SourceResponseError):
        cs.geo_part(["fr"])
    assert cs.atlas_source().planned_requests == len(cs.ITEMS)


def test_rows_validate_per_dataset(built: tuple[dict[str, list[dict]], dict]) -> None:
    rows, report = built
    assert set(rows) == set(cs.DATASETS)
    for ds, rs in rows.items():
        f, unmapped = validate_atlas_frame(
            pl.DataFrame(rs, schema=ATLAS_SCHEMA),
            source=cs.ATLAS_SOURCE,
            dataset=ds,
            raw_sha256s=sorted({r["raw_sha256"] for r in rs}),
            geo_scheme=cs.GEO_SCHEME,
        )
        assert not unmapped
        assert set(f.filter(pl.col("provider_geo") == "KSV")["iso3"].to_list()) == {"XKX"}
    # BVT is a null series on the 0.5 degree CRU grid: omitted and reported, never filled
    assert "BVT" in report["null_series"]["cru_monthly"]
    assert not [r for r in rows[cs.DS_CRU_M] if r["provider_geo"] == "BVT"]


def test_observed_monthly_values_and_periods(built: tuple[dict[str, list[dict]], dict]) -> None:
    rows, _ = built
    fra = [r for r in rows[cs.DS_CRU_M] if r["provider_geo"] == "FRA"]
    assert len(fra) == 2 * 124 * 12
    jan1901 = next(r for r in fra if r["indicator"] == "tas" and r["period_start"] == date(1901, 1, 1))
    assert jan1901["period_end"] == date(1901, 2, 1) and jan1901["unit"] == "°C"
    assert json.loads(jan1901["selector"])["path"] == ["data", "tas", "FRA", "1901-01"]
    flags = json.loads(jan1901["flags"])
    assert flags["estimate_kind"] == "observation_gridded" and "projection" not in flags
    pr = next(r for r in rows[cs.DS_ERA_A] if r["indicator"] == "pr" and r["provider_geo"] == "MLI")
    assert pr["unit"] == "mm/year" and json.loads(pr["flags"])["estimate_kind"] == "reanalysis"
    last = max(r["period_start"] for r in rows[cs.DS_ERA_M])
    assert last == date(2024, 12, 1)


def test_projections_are_flagged_and_never_mixed(built: tuple[dict[str, list[dict]], dict]) -> None:
    rows, _ = built
    proj = rows[cs.DS_PROJ_A]
    inds = {r["indicator"] for r in proj}
    assert "tas|scenario=ssp245|stat=median" in inds and "pr|scenario=ssp585|stat=p90" in inds
    assert "tas|scenario=historical|stat=p10" in inds
    for r in proj + rows[cs.DS_PROJ_C]:
        f = json.loads(r["flags"])
        assert f["projection"] and f["projection_upstream"]["model"] == cs.MODEL
        assert f["estimate_kind"] in ("projection", "model_simulation")
        assert (f["estimate_kind"] == "model_simulation") == ("scenario=historical" in r["indicator"])
    clim = [r for r in rows[cs.DS_PROJ_C] if r["provider_geo"] == "FRA" and r["indicator"].startswith("tas|")]
    assert len(clim) == 12 * (1 + 4 * 4)
    c = next(r for r in clim if r["indicator"] == "tas|scenario=ssp585|stat=median|clim=2080-2099|month=07")
    assert (c["period_start"], c["period_end"]) == (date(2080, 1, 1), date(2100, 1, 1))
    # observed tables carry no projection flag at all
    for ds in (cs.DS_CRU_M, cs.DS_CRU_A, cs.DS_ERA_M, cs.DS_ERA_A):
        assert all("projection" not in json.loads(r["flags"]) for r in rows[ds][:50])


def test_annual_monthly_consistency_blocks_tampered_data(bodies: dict[str, bytes]) -> None:
    from reva.adapters.connectors import atlas_climate as cl

    a = cl.parse_response(bodies["cru_annual"], cs.VARS)
    m = cl.parse_response(bodies["cru_monthly"], cs.VARS)
    assert cs.check_annual_monthly(a, m) == []
    m["pr"]["FRA"]["2000-05"] += Decimal(50)
    m["tas"]["MLI"]["1950-03"] += Decimal(2)
    probs = cs.check_annual_monthly(a, m)
    assert any(p.startswith("pr FRA 2000") for p in probs) and any(p.startswith("tas MLI 1950") for p in probs)


def test_whole_world_guard_and_date_checks(bodies: dict[str, bytes]) -> None:
    from reva.adapters.connectors import atlas_climate as cl

    with pytest.raises(SourceResponseError, match="geographies"):
        cs.check_item(cs.BY_KEY["era5_annual"], cl.parse_response(bodies["era5_annual"], cs.VARS), whole_world=True)
    p = cl.parse_response(bodies["era5_annual"], cs.VARS)
    p["tas"]["FRA"]["2030-07"] = Decimal(1)
    with pytest.raises(SourceResponseError, match="outside"):
        cs.check_item(cs.BY_KEY["era5_annual"], p, whole_world=False)
