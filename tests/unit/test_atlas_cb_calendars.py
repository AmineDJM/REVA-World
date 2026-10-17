"""Central-bank meeting calendars (V6 PRED-FIN): FOMC, ECB Governing Council, Bank of England MPC decision days.

Fixtures: tests/fixtures/source/atlas.cb_calendars — verbatim slices of the real pages downloaded 2026-10-17 (the
calendar entries and the terms paragraphs; licence per file in the manifest).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest

from reva.adapters.connectors import atlas_cb_calendars as C
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp


@pytest.fixture(scope="module")
def built() -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    http = FixtureHttp(C.NAME)
    files = {}
    for i, f in enumerate(C.FILES, start=1):
        r = asyncio.run(http.get(f.url))
        files[f.key] = hc.Fetched(f.key, f.url, r.body, f"{i:064x}", r.retrieved_at)
    return C.build(files)


def _days(rows: list[dict[str, Any]], body: str) -> list[date]:
    return sorted(r["period_start"] for r in rows if r["indicator"] == f"POLICY_DECISION|body={body}")


def test_spec_hosts() -> None:
    spec = C.atlas_source()
    assert isinstance(spec, AtlasSourceSpec) and spec.datasets == ("meetings",)
    assert {f.url.split("/")[2] for f in C.FILES} <= set(spec.allowed_hosts)
    assert spec.planned_requests == len(C.FILES)


def test_decision_days_and_rules(built: tuple[dict[str, hc.TableBuild], dict[str, Any]]) -> None:
    builds, report = built
    rows = builds["meetings"].rows.rows()
    fomc, ecb, boe = _days(rows, "FOMC"), _days(rows, "ECB_GC"), _days(rows, "BOE_MPC")
    # FOMC: last day of each scheduled meeting; the notation vote is not a meeting
    assert date(2026, 10, 28) in fomc and date(2026, 12, 9) in fomc and date(2027, 1, 27) in fomc
    assert date(2025, 8, 22) not in fomc and report["FOMC"]["skipped"]
    assert len([d for d in fomc if d.year == 2026]) == 8
    # ECB: Day 2 only (Day 1 and non-monetary meetings excluded)
    assert ecb[:2] == [date(2026, 10, 29), date(2026, 12, 17)]
    assert date(2026, 10, 28) not in ecb and date(2028, 10, 11) not in ecb and date(2028, 10, 12) in ecb
    # Bank of England: announcement days, weekday checked against the page
    assert boe[:2] == [date(2026, 2, 5), date(2026, 3, 19)] and date(2026, 11, 5) in boe and len(boe) == 16
    assert all(report["policy_quotes_found"].values()) and all(report["page_quotes_found"].values())


def test_rows_are_announced_schedules_with_licence_per_row(built: tuple[dict[str, hc.TableBuild], dict[str, Any]]
                                                           ) -> None:  # fmt: skip
    builds, _ = built
    frame = builds["meetings"].rows.frame()
    f, _ = validate_atlas_frame(frame, source="cbcal", dataset="meetings", raw_sha256s=sorted(set(frame["raw_sha256"])),
                                geo_scheme="iso2")  # fmt: skip
    assert f.height == len(builds["meetings"].rows)
    for r in f.to_dicts():
        fl = json.loads(r["flags"])
        assert r["value"] is None and r["value_text"]
        assert fl["estimate_kind"] == "announced_schedule" and fl["as_of"] and fl["licence"] and fl["decision_rule"]
        body = r["indicator"].split("=")[1]
        assert fl["licence"] == C.BODIES[body]["licence"]
        assert (fl.get("licence_restriction") == "non_commercial_internal") == (body == "BOE_MPC")
    oct28 = f.filter(f["period_start"] == date(2026, 10, 28)).to_dicts()[0]
    assert oct28["value_text"] == "2026 FOMC Meetings: October 27-28"
    assert set(f["provider_geo"].unique()) == {"US", "U2", "GB"}


def test_parsers_refuse_a_changed_page() -> None:
    with pytest.raises(hc.HfError):
        C.parse_fomc("<html>no panels</html>")
    with pytest.raises(hc.HfError):
        C.parse_ecb("<html></html>")
    with pytest.raises(hc.HfError):
        C.parse_boe("<html><h2>dates</h2></html>")
