"""Live A-HAZARDS ingest (NOAA NCEI hazard-service, ~240 GETs, ~25 MB; then one WDQS query) into the per-session
throwaway database of the ``db`` fixture (created and dropped by the fixture; never the dev database).

Deselected by default: ``pytest -m "live and db" tests/integration/test_atlas_hazards_events_live.py
--basetemp=<scratch>``. Every run goes through ``reva.atlas.ingest.run_atlas_ingest(mode="live")`` (reserve -> metered
SafeHttpClient -> settle). Until ATLASCORE merges ``registry/atlas/budget_requests/a_hazards.yaml``, the test overlays
exactly the requested per-source limit on the pinned policy for the duration of the test (the pinned file is not
modified). ``REVA_HAZARDS_LIVE_OUT=<path.json>`` writes the run summaries used by docs/dev/v3_a_hazards.md.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_noaa_hazards as hz
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = [pytest.mark.live, pytest.mark.db]

REQUESTS = Path(__file__).resolve().parents[2] / "registry/atlas/budget_requests/a_hazards.yaml"
_RESULTS: dict[str, Any] = {}


@pytest.fixture
def requested_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import reva.operations.budgets as budgets

    pinned = budgets.load_budget
    req = {r["source_key"]: int(r["daily_requests"]) for r in yaml.safe_load(REQUESTS.read_text())["requests"]}

    def overlay(policy: Any = None) -> Any:
        b = pinned(policy)
        per = dict(b.per_source_requests)
        for k, v in req.items():
            per.setdefault(k, v)
        return replace(b, per_source_requests=per, policy_version=b.policy_version + "+a_hazards_request")

    monkeypatch.setattr(budgets, "load_budget", overlay)


def _save() -> None:
    out = os.environ.get("REVA_HAZARDS_LIVE_OUT")
    if out:
        Path(out).write_text(json.dumps(_RESULTS, indent=1, default=str))


async def test_live_ncei_hazards(db, requested_budget: None) -> None:  # type: ignore[no-untyped-def]
    for spec in hz.atlas_source():
        ensure_registered(db.owner, db.store, spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, hz.NAME, mode="live")
    assert rep["status"] == "completed", rep.get("error")
    s = rep["summary"]
    assert s["records"]["earthquakes"] >= 6000 and s["records"]["runups"] >= 30000
    frame = read_atlas_table(db.store, s["atlas"]["artifact_sha256"])
    f = frame.with_columns(pl.col("period_start").dt.year().alias("y"))
    res = {
        "budget": rep.get("budget"),
        "archived_raw": rep.get("archived_raw"),
        **{k: v for k, v in s.items() if k != "atlas"},
        "atlas": {k: v for k, v in s["atlas"].items() if k != "unmapped_provider_geo"},
        "atlas_unmapped": s["atlas"]["unmapped_provider_geo"],
        "atlas_coverage": {
            "rows": frame.height,
            "countries": f.filter(pl.col("iso3").is_not_null())["iso3"].n_unique(),
            "years": [int(f["y"].min()), int(f["y"].max())],  # type: ignore[arg-type]
            "rows_bce": f.filter(pl.col("y") <= 0).height,
            "by_indicator": dict(f.group_by("indicator").len().sort("indicator").iter_rows()),
        },
    }
    _RESULTS[hz.NAME] = res
    _save()
    assert s["kg"]["status"] == "loaded", s["kg"]
    if s["causal"].get("status") != "unavailable":
        assert not s["causal"].get("rejected_counts"), s["causal"]
    wd = await run_atlas_ingest(db.settings, db.worker, db.store, hz.WD_NAME, mode="live")
    _RESULTS[hz.WD_NAME] = {
        "status": wd["status"],
        "error": wd.get("error"),
        "summary": wd.get("summary"),
        "budget": wd.get("budget"),
    }
    _save()
    assert wd["status"] == "completed", wd.get("error")
