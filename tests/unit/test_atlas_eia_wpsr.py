"""V7 BEAT-ENERGY: EIA Weekly Petroleum Status Report connector (fixtures: excerpts of the real 2026-10-17 pages)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from reva.adapters.connectors import atlas_eia_wpsr as w
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp


def _files() -> dict[str, hc.Fetched]:
    http = FixtureHttp(w.NAME)
    out = {}
    for i, f in enumerate(w.FILES, start=1):
        r = asyncio.run(http.get(f.url))
        out[f.key] = hc.Fetched(f.key, f.url, r.body, f"{i:064x}", r.retrieved_at)
    return out


def test_spec_and_fixture_build() -> None:
    spec = w.atlas_source()
    assert spec.name == "atlas.eia_wpsr" and spec.planned_requests == len(w.FILES)
    b, rep = w.build(_files())
    rows = b["wpsr_w"].rows.rows()
    frame = b["wpsr_w"].rows.frame()
    validate_atlas_frame(frame, source="eia", dataset="wpsr_w", raw_sha256s=sorted(set(frame["raw_sha256"].to_list())))
    assert set(rep["series"]) == {s.sid for s in w.SERIES}
    assert rep["policy_quotes_found"] == {"public_domain": True}
    prod = {r["period_end"] - timedelta(days=1): r for r in rows if r["indicator"] == "EIA_WPSR|series=WCRFPUS2"}
    oct9 = prod[date(2026, 10, 9)]
    assert oct9["value"] == "13679" and oct9["unit"] == "Thousand Barrels per Day"
    assert oct9["period_start"] == date(2026, 10, 3)  # the 7 days ending Friday 2026-10-09
    fl = json.loads(oct9["flags"])
    assert fl["first_available"] == "2026-10-16" and fl["precision"] == "week" and fl["value_normalised"] is True
    assert fl["release_date"] == "2026-10-15" and "public domain" in fl["licence"]
    assert all(r["iso3"] == "USA" for r in rows)


def test_week_end_must_be_friday_and_in_row_month() -> None:
    f = _files()["wpsr_WCRFPUS2"]
    bad = hc.Fetched(f.key, f.url, f.body.replace(b"10/09&nbsp;", b"10/10&nbsp;"), f.sha256, f.retrieved_at)
    with pytest.raises(hc.HfError, match="not a Friday"):
        w.parse_page(w.SERIES[0], bad, hc.DailyRows("eia", "wpsr_w"), {})
    bad2 = hc.Fetched(f.key, f.url, f.body.replace(b"10/09&nbsp;", b"11/06&nbsp;"), f.sha256, f.retrieved_at)
    with pytest.raises(hc.HfError, match="not in row month"):
        w.parse_page(w.SERIES[0], bad2, hc.DailyRows("eia", "wpsr_w"), {})
