"""Live source checks (deselected by default; run with ``uv run pytest -m live tests/live``).

These talk to the real providers through SafeHttpClient and assert *structural*
expectations (codes, shapes, pagination, typed Missing, pins), never exact values,
which providers revise. Nothing is recorded into tests/fixtures. The end-to-end test
uses the session ``db`` fixture (a fresh database, never the dev database), runs every
built-in connector live, publishes a live release and checks the coverage matrix.

The full report generator is ``scripts/live_source_checks.py``; its helpers are
reused here so the test and the report cannot drift apart.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors.comtrade import MAX_RECORDS, REPORTERS, ComtradeConnector
from reva.adapters.connectors.crossref import CURATED_DOIS, CrossrefConnector
from reva.adapters.connectors.naturalearth import NaturalEarthConnector
from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.evidence.runner import make_live_client
from reva.ports.connectors import FetchBudget

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[2]


def _load_script() -> Any:
    path = REPO / "scripts" / "live_source_checks.py"
    spec = importlib.util.spec_from_file_location("reva_live_source_checks", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


lsc = _load_script()


@pytest.fixture(scope="module")
def settings() -> Any:
    return lsc._base_settings()


async def _fetch_parse(connector: Any, settings: Any, keys: set[str] | None = None,
                       include_follow_up: bool = False) -> list[tuple[Any, Any, Any]]:
    """Fetch selected plan items live (origin=live, no recording), parse and validate."""
    spec = connector.describe()
    http = lsc.TracingHttp(make_live_client(spec, settings), spec.name)
    out = []
    try:
        queue = [i for i in connector.plan(None, FetchBudget()).items if keys is None or i.key in keys]
        while queue:
            item = queue.pop(0)
            raw = await connector.fetch(item, http)
            assert raw.origin == "live"
            batches = list(connector.parse(raw))
            for b in batches:
                if include_follow_up:
                    queue.extend(b.follow_up)
                out.append((raw, b, connector.validate(b)))
    finally:
        await http.aclose()
    return out


# ----------------------------------------------------------------------------- Comtrade
async def test_comtrade_reporter_codes_match_provider_catalogue() -> None:
    ref = await lsc.check_comtrade_reference()
    assert "error" not in ref, ref.get("error")
    rep = ref["reporters"]
    assert rep["count"] > 200
    for iso, code in lsc.EXPECTED_REPORTERS.items():
        entry = rep["codes"][iso]
        assert REPORTERS[iso] == code
        assert entry["provider_entry"] is not None, f"{iso}: code {code} absent from Reporters.json"
        assert entry["provider_entry"]["reporterCodeIsoAlpha3"] == iso
        assert entry["verified"], entry
    # France is Comtrade 251, not its M49 code 250
    assert 250 not in rep["codes"]["FRA"]["provider_codes_for_iso3"]
    for t in ref["requests"]:
        assert t["status"] == 200 and len(t["sha256"]) == 64 and t["bytes"] > 0
    assert ref["partner_areas"]["world_code_0"] is not None


async def test_comtrade_exact_world_total_shape(settings: Any) -> None:
    c = ComtradeConnector(reporters=("FRA",), total_years=(2023,), bilateral_years=())
    [(_raw, batch, q)] = await _fetch_parse(c, settings, {"total:FRA:M:2023"})
    md = batch.metadata
    assert md["count"] is not None and int(md["count"]) == md["returned_rows"] < MAX_RECORDS
    assert q.outcome == "pass", [(f.code, f.message) for f in q.findings]
    value = next(o for o in batch.observations if o.logical_key.endswith(":primaryValue"))
    assert value.concept_id == "trade.hs2709.import_value.world_total"
    assert value.value["kind"] == "decimal" and value.value["unit_ref"] == "unit.usd"
    assert value.source_flags.get("classificationCode")  # classification revision retained
    for d in md["rows"]:
        assert str(d["cmdCode"]) == "2709" and str(d["partnerCode"]) == "0" and str(d["reporterCode"]) == "251"


async def test_comtrade_algeria_is_typed_missing_or_reported_never_zero_filled(settings: Any) -> None:
    c = ComtradeConnector(reporters=("DZA",), total_years=(2023, 2024), bilateral_years=())
    res = await _fetch_parse(c, settings, {"total:DZA:M:2023", "total:DZA:X:2023", "total:DZA:M:2024"})
    assert len(res) == 3
    for raw, batch, q in res:
        assert q.outcome == "pass", [(f.code, f.message) for f in q.findings]
        for o in batch.observations:
            if batch.metadata["returned_rows"] == 0:
                assert o.value == {"kind": "missing", "reason": "unavailable"}
                assert o.source_flags.get("provider_returned_no_rows") is True
            elif o.value["kind"] == "decimal":
                assert o.value["value"] not in ("", None)


# ----------------------------------------------------------------------------- World Bank
async def test_worldbank_pagination_metadata_and_missing(settings: Any) -> None:
    c = WorldBankConnector()
    res = await _fetch_parse(c, settings, {"indicator:SP.POP.TOTL", "series:SP.POP.TOTL:p1"}, include_follow_up=True)
    meta = [b for _, b, _ in res if b.metadata.get("kind") == "indicator_metadata"]
    series = [b for _, b, _ in res if b.metadata.get("kind") == "series"]
    assert meta and "midyear" in (meta[0].metadata["indicator"]["sourceNote"] or "").lower()
    header = series[0].metadata["page_header"]
    assert int(header["pages"]) == len(series)
    assert sum(len(b.observations) for b in series) == int(header["total"]) == 4 * 25
    assert header.get("lastupdated")  # dataset-level metadata, kept, never a publication time
    assert all(q.outcome == "pass" for *_, q in res), [q.findings for *_, q in res]
    keys = {o.logical_key for b in series for o in b.observations}
    assert {f"wb:SP.POP.TOTL:{iso}:2023" for iso in ("FRA", "DZA", "CHN", "USA")} <= keys
    for b in series:
        for o in b.observations:
            assert o.value["kind"] in ("decimal", "missing")


# ----------------------------------------------------------------------------- Natural Earth
async def test_naturalearth_asset_structure(settings: Any) -> None:
    c = NaturalEarthConnector()
    [(raw, batch, q)] = await _fetch_parse(c, settings)
    assert raw.response_metadata["status"] == 200
    assert q.outcome == "pass"
    md = batch.metadata
    assert md["feature_count"] >= 150 and "ADM0_A3" in md["fields"]
    gj = NaturalEarthConnector.to_geojson(raw.body)
    a3 = {f["properties"].get("ADM0_A3") for f in gj["features"]}
    assert {"FRA", "DZA", "CHN", "USA"} <= a3


# ----------------------------------------------------------------------------- Crossref
async def test_crossref_doi_metadata(settings: Any) -> None:
    c = CrossrefConnector()
    [(_raw, batch, q)] = await _fetch_parse(c, settings)
    study = batch.metadata["study"]
    assert study["doi"] == CURATED_DOIS[0]
    assert "oil" in (study["title"] or "").lower()
    assert q.outcome == "pass" or all(f.severity != "blocker" for f in q.findings)


# ----------------------------------------------------------------------------- Eurostat
async def test_eurostat_demo_pjan_fr_2023(settings: Any, tmp_path: Path) -> None:
    res = await lsc.eurostat_check(settings, tmp_path)
    assert res.get("status") != "blocked", res
    assert res["dimension_order"] == ["freq", "unit", "age", "sex", "geo", "time"]
    assert res["status_codes_seen"] and set("".join(res["status_codes_seen"])) <= set("bcdefnprsuz")
    if res["mode"] == "template":
        assert res["quality"]["outcome"] == "pass", res["quality"]["findings"]
        assert res["value_count"] <= res["cube_positions"] == res["observations"]
        assert res["published"] is False


# ----------------------------------------------------------------------------- end to end (fresh DB)
@pytest.mark.db
def test_full_live_run_release_and_coverage(db: Any, tmp_path: Path) -> None:
    live = lsc.LiveDB(db.settings, db.name, db.owner, db.worker, db.store, db.owner)
    result = asyncio.run(lsc.run_all(live, None, eurostat=False, work=tmp_path))
    (tmp_path / "live_checks.json").write_text(json.dumps(result, default=lsc._default))
    assert result["database"]["name"] != "reva"
    assert result["gates"]["no_fixture_origin_in_db"]
    assert result["gates"]["comtrade_reporter_codes_verified"]
    assert result["release"]["published"], result["release"]
    for name, run in result["connectors"].items():
        assert "crash" not in run["report"], (name, run["report"].get("crash"))
        assert run["request_count"] > 0
    cov = result["coverage"]
    cells = [c for r in cov["requirements"] for c in r["cells"]]
    assert all(c["status"] in ("value", "missing", "absent") for c in cells)
    by = {(r["id"], c["geo"], c["year"]): c for r in cov["requirements"] for c in r["cells"]}
    for iso in ("FRA", "CHN", "USA"):  # documented live facts; structural, not values
        assert by[("R-CRUDE-M", iso, 2023)]["status"] == "value"
        assert by[("R-POP-WDI", iso, 2023)]["status"] == "value"
    for (rid, iso, y), c in by.items():
        if c["status"] == "missing":
            assert c["reason"], c  # typed Missing always carries a reason
    wb = result["source_details"]["worldbank.wdi.indicators"]
    assert wb["pagination_fully_consumed"], wb["pagination"]
