"""V2 step 1: all-country Comtrade (multi-reporter, rotating) and World Bank connectors, offline."""

from __future__ import annotations

import json
from typing import Any

from reva.adapters.connectors import comtrade_batch as cb
from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.comtrade_batch import ComtradeBatchConnector
from reva.adapters.connectors.worldbank_all import WorldBankAllConnector
from reva.evidence.revisions import semantic_hash
from reva.evidence.runner import FixtureHttp, _fixture_fingerprint
from reva.operations.budgets import load_budget
from reva.ports.connectors import Cursor, FetchBudget, FetchOutcome

CT_SUBSET = {"FRA": 251, "DZA": 12, "CHN": 156, "USA": 842, "CHE": 757, "IRQ": 368, "NLD": 528, "ATG": 28}


def subset() -> ComtradeBatchConnector:
    return ComtradeBatchConnector(CT_SUBSET, total_years=(2023,), bilateral_years=(2023,), bilateral_batch=4)


async def _parse(conn: Any, item: Any, overrides: dict[str, bytes] | None = None) -> tuple[Any, Any]:
    http = FixtureHttp(conn.describe().name, overrides=overrides)
    raw = await conn.fetch(item, http)
    batches = list(conn.parse(raw))
    return batches, [conn.validate(b) for b in batches]


def _codes(report: Any, severity: str) -> set[str]:
    return {f.code for f in report.findings if f.severity == severity}


def test_default_plan_is_one_period_per_request_and_covers_every_mapped_reporter() -> None:
    c = ComtradeBatchConnector()
    units = c.units()
    assert all("period=" in u.url and "," not in u.url.split("period=")[1].split("&")[0] for u in units)
    totals = [u for u in units if u.params["scope"] == "total"]
    for y in cb.TOTAL_YEARS:
        covered = sorted(code for u in totals if u.params["year"] == y for code in u.params["reporters"])
        assert covered == sorted(c.by_code)  # every crosswalk reporter, once per year
    assert all(len(u.params["reporters"]) * 2 < cb.MAX_RECORDS for u in totals)
    assert 490 not in c.by_code and 97 not in c.by_code  # composite codes are never requested
    assert len(c.by_code) == 219


def test_rotation_covers_the_sweep_within_the_daily_budget() -> None:
    c = ComtradeBatchConnector()
    units = c.units()
    limit = load_budget().per_source_requests["un.comtrade.public_preview"]
    legacy = len(ComtradeConnector().plan(None, FetchBudget()).items)  # the 4-country monthly run
    assert cb.MAX_REQUESTS_PER_RUN + legacy <= limit
    cursor: Cursor | None = None
    seen: list[str] = []
    for _ in range(10):
        plan = c.plan(cursor, FetchBudget())
        assert 0 < len(plan.items) <= cb.MAX_REQUESTS_PER_RUN
        seen += [i.key for i in plan.items]
        outcomes = [FetchOutcome(item_key=i.key, status="published") for i in plan.items]
        nxt = c.next_cursor(plan, outcomes)
        assert nxt.state["complete"] is True
        cursor = nxt
        if nxt.state["sweep"] == 1:
            break
    assert seen == [u.key for u in units]  # one full sweep, each unit exactly once, in order
    assert cursor is not None and cursor.state["offset"] == 0
    # A failed required item leaves the cursor un-advanced (the runner saves only complete cursors).
    plan = c.plan(cursor, FetchBudget())
    bad = c.next_cursor(plan, [FetchOutcome(item_key=plan.items[0].key, status="failed")])
    assert bad.state["complete"] is False
    # A smaller request budget shrinks the slice; a changed reporter list restarts the sweep.
    assert len(c.plan(Cursor(state={"offset": 5, "plan_digest": cursor.state["plan_digest"]}),
                      FetchBudget(max_requests=3)).items) == 3
    other = ComtradeBatchConnector(CT_SUBSET)
    assert other.plan(Cursor(state={"offset": 5, "plan_digest": cursor.state["plan_digest"]}),
                      FetchBudget()).items[0].key == other.units()[0].key


async def test_multi_reporter_totals_keep_missing_typed_and_old_editions_ineligible() -> None:
    c = subset()
    item = c.units()[0]
    (batch,), (report,) = await _parse(c, item)
    obs = {o.logical_key: o for o in batch.observations}
    assert len(obs) == len(CT_SUBSET) * 2 * 2  # reporter x flow x {value, net weight}
    dza = obs["comtrade:HS2709:M:12:0:2023:primaryValue"]
    assert dza.value == {"kind": "missing", "reason": "unavailable"}
    assert dza.source_flags["provider_returned_no_rows"] is True
    irq = obs["comtrade:HS2709:X:368:0:2023:primaryValue"]
    assert irq.source_flags["classificationCode"] == "H2" and irq.value["kind"] == "decimal"
    assert report.outcome == "pass"
    assert "classification_unreviewed_ineligible" in _codes(report, "warning")
    assert "classification_unreviewed" not in _codes(report, "blocker")


async def test_batch_rows_share_the_lineage_of_the_reviewed_single_reporter_connector() -> None:
    c = subset()
    (batch,), _ = await _parse(c, c.units()[0])
    new = {o.logical_key: o for o in batch.observations}
    legacy = ComtradeConnector(reporters=("FRA",), total_years=(2023,), bilateral_years=())
    for item in legacy.plan(None, FetchBudget()).items:
        (lb,), _ = await _parse(legacy, item)
        for o in lb.observations:
            assert semantic_hash(new[o.logical_key]) == semantic_hash(o), o.logical_key


async def test_unknown_hs_edition_still_blocks_the_batch() -> None:
    c = subset()
    item = c.units()[0]
    http = FixtureHttp(c.describe().name)
    body = json.loads((await http.get(item.url)).body)
    body["data"][0]["classificationCode"] = "H7"
    (_,), (report,) = await _parse(c, item, {_fixture_fingerprint(item.url): json.dumps(body).encode()})
    assert report.outcome == "blocked" and "classification_unreviewed" in _codes(report, "blocker")


async def test_bilateral_truncation_blocks_and_splits_into_halves() -> None:
    c = subset()
    item = next(u for u in c.units() if u.params["scope"] == "bilateral")
    codes = item.params["reporters"]
    rows = [{"typeCode": "C", "freqCode": "A", "period": "2023", "reporterCode": codes[i % len(codes)],
             "flowCode": "M", "partnerCode": 1000 + i, "partner2Code": 0, "cmdCode": "2709", "customsCode": "C00",
             "motCode": 0, "classificationCode": "H6", "primaryValue": 1.0, "cifvalue": None, "fobvalue": None,
             "netWgt": None, "legacyEstimationFlag": 0}
            for i in range(cb.MAX_RECORDS)]  # synthetic truncated response (test-only bytes)
    payload = json.dumps({"elapsedTime": "0", "count": cb.MAX_RECORDS, "data": rows, "error": ""}).encode()
    (batch,), (report,) = await _parse(c, item, {_fixture_fingerprint(item.url): payload})
    assert "possible_truncation" in _codes(report, "blocker")
    halves = [f.params["reporters"] for f in batch.follow_up]
    assert len(halves) == 2 and sorted(halves[0] + halves[1]) == sorted(codes)
    assert all(f.params["scope"] == "bilateral" and f.params["year"] == 2023 for f in batch.follow_up)


async def test_bilateral_rows_keep_provider_partner_codes() -> None:
    c = subset()
    partners: set[str] = set()
    for item in [u for u in c.units() if u.params["scope"] == "bilateral"]:
        (batch,), (report,) = await _parse(c, item)
        assert report.outcome == "pass", report
        for o in batch.observations:
            p = o.relation.participants[1]
            assert p["provider"] == "comtrade_partner" and p["key"] == o.source_flags["partnerCode"] != "0"
            partners.add(p["key"])
    assert partners  # e.g. composite partners such as 899 stay provider codes when reported


async def test_worldbank_all_economies_single_page_with_expected_keys() -> None:
    wb = WorldBankAllConnector(["FRA", "DZA", "CHN", "USA", "CHE", "IRQ", "NLD", "ATG", "XKX", "PRI"])
    assert wb.describe().name == "worldbank.wdi.all_economies"
    for item in wb.plan(None, FetchBudget()).items:
        batches, reports = await _parse(wb, item)
        for b, r in zip(batches, reports, strict=True):
            assert b.connector == "worldbank.wdi.all_economies" and r.outcome == "pass", r
            if item.params["kind"] == "series":
                assert len(b.expected_keys) == 10 * 6 and not b.follow_up
    default = WorldBankAllConnector()
    assert len(default.countries) == 217 and "WLD" not in default.countries
