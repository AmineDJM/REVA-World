"""V8 series (V8MONDE) against the dev database `reva` and its live atlas snapshot (read-only).

Skipped when the database is unreachable or the latest snapshot's query file is not materialised (never built
here: opening it only verifies it). Checks real slices against direct reads of the same DuckDB file and the warm
latency of a ~200-country map slice (p95 < 300 ms).
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

pytestmark = pytest.mark.db

GDP_PC = "wb.wdi_econ.NY.GDP.PCAP.CD"


@pytest.fixture(scope="module")
def ctx() -> dict[str, Any]:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.catalog import cached_catalog
    from reva.atlas.query import SnapshotNotMaterialised, query_for, request_snapshot
    from reva.settings import get_settings
    from reva.v8.series.index import index_for

    s = get_settings()
    store = build_store(s)
    try:
        eng = make_engine("api", s)
        with eng.connect() as conn:
            snap, _latest = request_snapshot(conn, store, origin="live")
    except Exception as e:
        pytest.skip(f"dev database or live atlas snapshot not available: {type(e).__name__}")
    try:
        q = query_for(snap, store, build=False)
    except SnapshotNotMaterialised:
        pytest.skip("the live atlas query file is not materialised on this machine")
    idx = index_for(q, cached_catalog())
    if ("wb", "wdi_econ", "NY.GDP.PCAP.CD") not in q.sid_of:
        pytest.skip("World Bank GDP per capita not in this snapshot")
    return {"q": q, "idx": idx}


def test_map_slice_matches_the_query_file(ctx: dict[str, Any]) -> None:
    from reva.v8.series import service as svc

    q, idx = ctx["q"], ctx["idx"]
    sid, *_ = svc.resolve(idx, series=GDP_PC)
    d = svc.map_slice(q, idx, sid, precision="year", period="2020")
    direct = q._rows(
        "SELECT x.iso3, x.value FROM cells_s x WHERE x.sid = ? AND x.year = 2020 AND x.iso3 IS NOT NULL AND x.has_value",
        [sid],
    )
    assert {v["iso3"]: v["v"] for v in d["values"]} == {r["iso3"]: r["value"] for r in direct}
    assert len(d["values"]) > 150  # ~200 countries
    reasons = {m["reason"] for m in d["missing"]}
    assert reasons <= {"not_in_series", "not_this_period", "projection_hidden", "other_granularity"} | {
        r for r in reasons if r.startswith("provider:")
    }
    have = {v["iso3"] for v in d["values"]}
    assert not have & {m["geo"] for m in d["missing"]}  # a place is either valued or missing, never both
    assert all(v["v"] is None or not v["v"].startswith("0") or v["v"].startswith("0.") for v in d["values"])
    assert d["licences"] and d["meta"]["concept"] is not None


def test_place_series_and_catalog(ctx: dict[str, Any]) -> None:
    from reva.v8.series import service as svc

    q, idx = ctx["q"], ctx["idx"]
    sid, *_ = svc.resolve(idx, series=GDP_PC)
    p = svc.place_series(q, idx, sid, "FRA")
    years = [int(x["p"]) for x in p["points"]]
    assert years == sorted(years) and len(years) > 40
    hits = idx.search("pib par habitant")
    assert (
        any(svc.split_series_key(GDP_PC) == idx.primary_series(h.concept) for h in hits["concepts"]) or hits["concepts"]
    )


def test_warm_map_slice_p95_under_300_ms(ctx: dict[str, Any]) -> None:
    from reva.v8.series import service as svc

    q, idx = ctx["q"], ctx["idx"]
    sid, *_ = svc.resolve(idx, series=GDP_PC)
    svc.map_slice(q, idx, sid, precision="year", period="2000")  # warm the series meta and periods
    times = []
    for y in range(1995, 2024):
        t0 = time.perf_counter()
        svc.map_slice(q, idx, sid, precision="year", period=str(y))
        times.append((time.perf_counter() - t0) * 1000)
    p95 = sorted(times)[int(0.95 * len(times)) - 1]
    print(f"map slice warm: p50 {statistics.median(times):.1f} ms, p95 {p95:.1f} ms over {len(times)} periods")
    assert p95 < 300
