"""Connector robustness against ambiguous, truncated, drifting or hostile provider payloads.

Parse/validate tests use captured fixtures (mutated in-test); runner tests use a
private database so quarantined/blocked items can be checked end to end.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors.comtrade import MAX_RECORDS, REPORTERS, ComtradeConnector
from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.http.safe_client import request_fingerprint
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchBudget, FetchItem, ObservationBatch, QualityReport, RawAcquisition

FIX = Path(__file__).resolve().parents[1] / "fixtures"
SRC = FIX / "source"
ADV = FIX / "adversarial"
CT = "un.comtrade.public_exact"
WB = "worldbank.wdi.indicators"


def fixture_body(connector: str, url_contains: list[str]) -> Any:
    entries = json.loads((SRC / connector / "manifest.json").read_text())
    hits = [e for e in entries if all(s in e["url"] for s in url_contains)]
    assert len(hits) == 1, [e["url"] for e in hits]
    return json.loads((SRC / connector / hits[0]["file"]).read_text()), hits[0]["url"]


def raw_for(item: FetchItem, payload: Any) -> RawAcquisition:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return RawAcquisition(
        item=item,
        body=body,
        media_type="application/json",
        retrieved_at=datetime.now(UTC),
        response_metadata={},
        request_fingerprint=request_fingerprint("GET", item.url),
        origin="test_fixture",
    )


def parse_validate(conn: Any, item: FetchItem, payload: Any) -> tuple[list[ObservationBatch], list[QualityReport]]:
    batches = list(conn.parse(raw_for(item, payload)))
    return batches, [conn.validate(b) for b in batches]


def codes(reports: list[QualityReport]) -> set[str]:
    return {f.code for r in reports for f in r.findings if f.severity == "blocker"}


# =========================================================================== Comtrade
@pytest.fixture()
def ct() -> ComtradeConnector:
    return ComtradeConnector()


def ct_item(ct: ComtradeConnector, key: str) -> FetchItem:
    return next(i for i in ct.plan(None, FetchBudget(max_requests=1000)).items if i.key == key)


@pytest.fixture()
def fra_m_2023() -> dict[str, Any]:
    body, _ = fixture_body(CT, ["period=2023", "reporterCode=251", "flowCode=M", "partnerCode=0"])
    assert body["count"] == 1 and len(body["data"]) == 1
    return body


def test_comtrade_captured_total_passes(ct: ComtradeConnector, fra_m_2023: dict) -> None:
    batches, reports = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), fra_m_2023)
    assert [r.outcome for r in reports] == ["pass"]
    value = next(o for o in batches[0].observations if o.logical_key.endswith("primaryValue"))
    assert value.value["kind"] == "decimal"
    assert Decimal(value.value["value"]) == Decimal(str(fra_m_2023["data"][0]["primaryValue"]))
    # CIF/FOB/primaryValue preserved separately; the declared basis is derived, not assumed.
    assert value.value["basis"]["valuation"] == "CIF"
    assert value.source_flags["cifvalue"] is not None and value.source_flags["fobvalue"] is None


def test_comtrade_count_mismatch_blocks(ct: ComtradeConnector, fra_m_2023: dict) -> None:
    p = copy.deepcopy(fra_m_2023)
    p["count"] = 2
    _, reports = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    assert reports[0].blocking and "count_mismatch" in codes(reports)
    p["count"] = None
    _, reports = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    assert "count_mismatch" in codes(reports)


def test_comtrade_max_records_truncation_blocks(ct: ComtradeConnector, fra_m_2023: dict) -> None:
    row = fra_m_2023["data"][0]
    rows = [{**row, "partnerCode": i + 1} for i in range(MAX_RECORDS)]
    p = {**fra_m_2023, "count": len(rows), "data": rows}
    _, reports = parse_validate(ct, ct_item(ct, "bilateral:FRA:M:2023"), p)
    assert "possible_truncation" in codes(reports)


@pytest.mark.parametrize(
    "field,value",
    [
        ("partnerCode", 4),
        ("cmdCode", "2710"),
        ("flowCode", "X"),
        ("reporterCode", REPORTERS["USA"]),
        ("customsCode", "C01"),
        ("motCode", 1000),
        ("partner2Code", 12),
        ("freqCode", "M"),
        ("typeCode", "S"),
        ("period", "2022"),
    ],
)
def test_comtrade_dimension_mismatch_blocks(ct: ComtradeConnector, fra_m_2023: dict, field: str, value: Any) -> None:
    p = copy.deepcopy(fra_m_2023)
    p["data"][0][field] = value
    _, reports = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    assert "dimension_mismatch" in codes(reports), (field, reports)


def test_comtrade_duplicate_rows_block(ct: ComtradeConnector, fra_m_2023: dict) -> None:
    p = copy.deepcopy(fra_m_2023)
    p["data"].append({**p["data"][0], "primaryValue": 1.0, "cifvalue": 1.0})
    p["count"] = 2
    _, reports = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    assert "ambiguous_duplicate" in codes(reports)


def test_comtrade_missing_flow_stays_missing_never_zero(ct: ComtradeConnector) -> None:
    body, _ = fixture_body(CT, ["period=2023", "reporterCode=12", "flowCode=M", "partnerCode=0"])
    assert body["count"] == 0 and body["data"] == []  # the real DZA 2023 answer
    batches, reports = parse_validate(ct, ct_item(ct, "total:DZA:M:2023"), body)
    obs = batches[0].observations
    assert len(obs) == 2 and all(o.value == {"kind": "missing", "reason": "unavailable"} for o in obs)
    assert all(o.source_flags.get("provider_returned_no_rows") for o in obs)
    assert not reports[0].blocking
    assert any(f.code == "missing_flow" for f in reports[0].findings)
    for o in obs:
        assert "value" not in o.value  # nothing that could be read as 0


@pytest.mark.parametrize("weight", [None, 0, 0.0, "0", -5])
def test_comtrade_unusable_weight_stays_missing(ct: ComtradeConnector, fra_m_2023: dict, weight: Any) -> None:
    p = copy.deepcopy(fra_m_2023)
    p["data"][0]["netWgt"] = weight
    batches, _ = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    w = next(o for o in batches[0].observations if o.logical_key.endswith("netWgt"))
    assert w.value == {"kind": "missing", "reason": "unavailable"}
    assert w.source_flags["weight_unusable"] is True
    # The value series is unaffected and no quantity is derived from value.
    v = next(o for o in batches[0].observations if o.logical_key.endswith("primaryValue"))
    assert v.value["kind"] == "decimal"


def test_comtrade_positive_weight_is_kept_exactly(ct: ComtradeConnector, fra_m_2023: dict) -> None:
    p = copy.deepcopy(fra_m_2023)
    p["data"][0]["netWgt"] = 123456789012.5
    batches, _ = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    w = next(o for o in batches[0].observations if o.logical_key.endswith("netWgt"))
    assert w.value["value"] == "123456789012.5" and w.value["unit_ref"] == "unit.kg"


def test_comtrade_null_primary_value_stays_missing(ct: ComtradeConnector, fra_m_2023: dict) -> None:
    p = copy.deepcopy(fra_m_2023)
    p["data"][0]["primaryValue"] = None
    batches, _ = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    v = next(o for o in batches[0].observations if o.logical_key.endswith("primaryValue"))
    assert v.value == {"kind": "missing", "reason": "unavailable"}


def test_comtrade_bilateral_excludes_world_total(ct: ComtradeConnector) -> None:
    body, _ = fixture_body(CT, ["period=2023", "reporterCode=251", "flowCode=M", "flowCode=M&partner2Code"])
    p = copy.deepcopy(body)
    world = {**p["data"][0], "partnerCode": 0}
    p["data"].append(world)
    p["count"] = len(p["data"])
    batches, _ = parse_validate(ct, ct_item(ct, "bilateral:FRA:M:2023"), p)
    assert all(o.source_flags["partnerCode"] != "0" for o in batches[0].observations)
    assert all(o.support.resolution["partner"] != "0" for o in batches[0].observations)


@pytest.mark.parametrize(
    "payload,kind",
    [
        ((ADV / "comtrade_api_error.json").read_bytes(), None),
        ({"elapsedTime": "x", "rows": []}, "schema_drift"),
        ([], "schema_drift"),
        ((ADV / "html_error_200.html").read_bytes(), "html_body"),
        (b'{"count": 1, "data": [{"primaryValue": NaN}]}', "malformed_json"),
    ],
)
def test_comtrade_error_and_drift_payloads_fail(ct: ComtradeConnector, payload: Any, kind: str | None) -> None:
    with pytest.raises(SourceResponseError) as ei:
        list(ct.parse(raw_for(ct_item(ct, "total:FRA:M:2023"), payload)))
    if kind:
        assert ei.value.details.get("kind") == kind


def test_comtrade_hostile_strings_are_inert_data(ct: ComtradeConnector, fra_m_2023: dict) -> None:
    p = copy.deepcopy(fra_m_2023)
    hostile = "x'); DROP TABLE evidence.observation;-- <script>alert(1)</script> {{7*7}}"
    p["data"][0]["classificationCode"] = hostile
    batches, reports = parse_validate(ct, ct_item(ct, "total:FRA:M:2023"), p)
    v = next(o for o in batches[0].observations if o.logical_key.endswith("primaryValue"))
    assert v.source_flags["classificationCode"] == hostile  # kept verbatim as a string flag
    assert not reports[0].blocking


# =========================================================================== World Bank
@pytest.fixture()
def wb() -> WorldBankConnector:
    return WorldBankConnector()


def wb_item(wb: WorldBankConnector, key: str) -> FetchItem:
    return next(i for i in wb.plan(None, FetchBudget()).items if i.key == key)


@pytest.fixture()
def pop_series() -> list[Any]:
    body, _ = fixture_body(WB, ["indicator/SP.POP.TOTL?", "page=1"])
    return body


def test_wb_captured_series_passes(wb: WorldBankConnector, pop_series: list) -> None:
    batches, reports = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), pop_series)
    assert [r.outcome for r in reports] == ["pass"] and len(batches[0].observations) == 100


def test_wb_pagination_truncation_blocks(wb: WorldBankConnector, pop_series: list) -> None:
    header, rows = copy.deepcopy(pop_series)
    _, reports = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), [header, rows[:60]])
    assert {"pagination_truncated", "incomplete"} <= codes(reports)
    # A header claiming more rows than delivered on a single page also blocks.
    _, reports = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), [{**header, "total": 150}, rows])
    assert "pagination_truncated" in codes(reports)


def test_wb_multi_page_header_schedules_all_pages(wb: WorldBankConnector, pop_series: list) -> None:
    header, rows = copy.deepcopy(pop_series)
    header = {**header, "pages": 3, "per_page": 40, "total": 100}
    batches, reports = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), [header, rows[:40]])
    assert [i.params["page"] for i in batches[0].follow_up] == [2, 3]
    assert not reports[0].blocking
    _, short = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), [header, rows[:39]])
    assert "pagination_truncated" in codes(short)


def test_wb_duplicate_and_unexpected_rows_block(wb: WorldBankConnector, pop_series: list) -> None:
    header, rows = copy.deepcopy(pop_series)
    dup = [*rows[:99], rows[0]]
    _, reports = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), [header, dup])
    assert "duplicate_keys" in codes(reports)
    alien = [*rows[:99], {**rows[99], "countryiso3code": "GBR"}]
    _, reports = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), [header, alien])
    assert "unexpected_subject" in codes(reports)


def test_wb_null_values_stay_missing(wb: WorldBankConnector, pop_series: list) -> None:
    header, rows = copy.deepcopy(pop_series)
    rows[0]["value"] = None
    batches, reports = parse_validate(wb, wb_item(wb, "series:SP.POP.TOTL:p1"), [header, rows])
    o = next(
        o for o in batches[0].observations if o.logical_key.endswith(f"{rows[0]['countryiso3code']}:{rows[0]['date']}")
    )
    assert o.value == {"kind": "missing", "reason": "unavailable"}
    assert not reports[0].blocking


def test_wb_api_error_payload_is_a_failure(wb: WorldBankConnector) -> None:
    with pytest.raises(SourceResponseError, match="World Bank API error"):
        list(wb.parse(raw_for(wb_item(wb, "series:SP.POP.TOTL:p1"), (ADV / "wb_api_error.json").read_bytes())))


@pytest.mark.parametrize("payload", [{"page": 1}, [{"page": 1}], [1, 2, 3], "text", [{"page": 1}, [], []]])
def test_wb_schema_drift_is_a_failure(wb: WorldBankConnector, payload: Any) -> None:
    with pytest.raises(SourceResponseError) as ei:
        list(wb.parse(raw_for(wb_item(wb, "series:SP.POP.TOTL:p1"), payload)))
    assert ei.value.details.get("kind") == "schema_drift"


def test_wb_semantic_drift_in_indicator_metadata_blocks(wb: WorldBankConnector) -> None:
    body, _ = fixture_body(WB, ["v2/indicator/SP.POP.TOTL?format=json"])
    _, reports = parse_validate(wb, wb_item(wb, "indicator:SP.POP.TOTL"), body)
    assert not reports[0].blocking
    drifted = copy.deepcopy(body)
    drifted[1][0]["sourceNote"] = drifted[1][0]["sourceNote"].replace("midyear", "1 January")
    _, reports = parse_validate(wb, wb_item(wb, "indicator:SP.POP.TOTL"), drifted)
    assert "semantic_drift" in codes(reports)


# =========================================================================== through the runner
def _helper() -> Any:
    name = "reva_h2_isolated_db"
    if name not in sys.modules:
        path = FIX / "adversarial" / "isolated_db.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def rdb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    with _helper().isolated_db(tmp_path_factory) as d:
        yield d


def _obs_count(d: Any, concept: str) -> int:
    with d.owner.connect() as c:
        return int(
            c.execute(
                text(
                    "SELECT count(*) FROM evidence.observation o JOIN ontology.concept_version cv "
                    "ON cv.id = o.concept_version_id WHERE cv.concept_id = :c"
                ),
                {"c": concept},
            ).scalar_one()
        )


@pytest.mark.db
def test_runner_semantic_drift_blocks_dependent_series(rdb: Any) -> None:
    wb = WorldBankConnector()
    body, url = fixture_body(WB, ["v2/indicator/SP.POP.TOTL?format=json"])
    body[1][0]["sourceNote"] = "Total population counts residents on 1 January of each year."
    http = FixtureHttp(WB, overrides={request_fingerprint("GET", url): json.dumps(body).encode()})
    rep = asyncio.run(run_connector(wb, http, rdb.store, rdb.worker, persist_cursor=True))
    status = {o.item_key: o.status for o in rep.outcomes}
    assert status["indicator:SP.POP.TOTL"] == "quarantined"
    assert status["series:SP.POP.TOTL:p1"] == "failed"
    assert any("blocked" in e["error"] for e in rep.errors if e["item"] == "series:SP.POP.TOTL:p1")
    # The independent indicator still publishes; the drifted one publishes nothing.
    assert status["series:NY.GDP.MKTP.CD:p1"] == "published"
    assert _obs_count(rdb, "demography.population.wdi_midyear_total") == 0
    assert _obs_count(rdb, "economics.gdp.nominal_current_usd") == 100
    # The cursor does not advance past an incomplete run, and health records the quarantine.
    assert rep.cursor_advanced is False
    with rdb.owner.connect() as c:
        st = c.execute(text("SELECT status FROM ops.connector_health ORDER BY recorded_at DESC LIMIT 1")).scalar_one()
        q = c.execute(text("SELECT count(*) FROM meta.artifact_location WHERE namespace = 'quarantine'")).scalar_one()
    assert st == "quarantined" and q >= 1


@pytest.mark.db
def test_runner_quarantines_truncated_page_and_html(rdb: Any) -> None:
    wb = WorldBankConnector()
    pop, pop_url = fixture_body(WB, ["indicator/SP.POP.TOTL?", "page=1"])
    _, gdp_url = fixture_body(WB, ["indicator/NY.GDP.MKTP.CD?", "page=1"])
    truncated = [pop[0], pop[1][:70]]
    http = FixtureHttp(
        WB,
        overrides={
            request_fingerprint("GET", pop_url): json.dumps(truncated).encode(),
            request_fingerprint("GET", gdp_url): (ADV / "html_error_200.html").read_bytes(),
        },
    )
    before = _obs_count(rdb, "demography.population.wdi_midyear_total")
    rep = asyncio.run(run_connector(wb, http, rdb.store, rdb.worker, persist_cursor=False))
    status = {o.item_key: o.status for o in rep.outcomes}
    assert status["series:SP.POP.TOTL:p1"] == "quarantined"
    assert status["series:NY.GDP.MKTP.CD:p1"] == "quarantined"
    assert _obs_count(rdb, "demography.population.wdi_midyear_total") == before
    kinds = {f["code"] for q in rep.quality["series:NY.GDP.MKTP.CD:p1"] for f in q["findings"]}
    assert "html_body" in kinds


@pytest.mark.db
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: row-level schema drift (e.g. WB rows without 'date') raises KeyError/ValueError from "
        "connector.parse; run_connector only catches SourceResponseError/RevaError, so the whole run "
        "crashes instead of quarantining the item and continuing"
    ),
)
def test_runner_quarantines_row_level_schema_drift(rdb: Any) -> None:
    wb = WorldBankConnector()
    pop, pop_url = fixture_body(WB, ["indicator/SP.POP.TOTL?", "page=1"])
    drift = [pop[0], [{("year" if k == "date" else k): v for k, v in r.items()} for r in pop[1]]]
    http = FixtureHttp(WB, overrides={request_fingerprint("GET", pop_url): json.dumps(drift).encode()})
    rep = asyncio.run(run_connector(wb, http, rdb.store, rdb.worker, persist_cursor=False))
    status = {o.item_key: o.status for o in rep.outcomes}
    assert status["series:SP.POP.TOTL:p1"] in ("quarantined", "failed")
