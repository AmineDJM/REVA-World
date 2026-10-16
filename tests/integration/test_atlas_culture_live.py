"""Live A-CULTURE ingest (UNESCO ICH, World Heritage KG links, UIS culture, Olympic medal tables) into the
per-session throwaway database of the ``db`` fixture (created and dropped by the fixture; never the dev DB).

Deselected by default: ``pytest -m "live and db" tests/integration/test_atlas_culture_live.py
--basetemp=<scratch>``. Every run goes through ``reva.atlas.ingest.run_atlas_ingest(mode="live")``
(reserve -> metered SafeHttpClient -> settle). Until ATLASCORE merges
``registry/atlas/budget_requests/a_culture.yaml``, this test overlays exactly the requested per-source limits on
the pinned policy for the duration of the test (the pinned file is not modified). ~14 requests, ~4 MB.
Wikimedia rate-limits anonymous API traffic per IP: an HTTP 429 fails the Olympics run (nothing registered).

``REVA_CULTURE_LIVE_OUT=<path.json>`` writes the run summaries used by docs/dev/v3_a_culture.md.
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

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = [pytest.mark.live, pytest.mark.db]

NAMES = ("atlas.unesco_ich", "atlas.unesco_whc_links", "atlas.uis_culture", "atlas.olympics_medals")
REQUESTS = Path(__file__).resolve().parents[2] / "registry/atlas/budget_requests/a_culture.yaml"
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
            per[k] = per.get(k, 0) + v
        return replace(b, per_source_requests=per, policy_version=b.policy_version + "+a_culture_request")

    monkeypatch.setattr(budgets, "load_budget", overlay)


def _coverage(frame: pl.DataFrame) -> dict[str, Any]:
    f = frame.with_columns(pl.col("period_start").dt.year().alias("y"))
    mapped = f.filter(pl.col("iso3").is_not_null())
    return {
        "rows": frame.height,
        "indicators": frame["indicator"].n_unique(),
        "countries": mapped["iso3"].n_unique(),
        "years": [int(f["y"].min()), int(f["y"].max())],
        "unmapped_provider_geo": sorted(f.filter(pl.col("iso3").is_null())["provider_geo"].unique().to_list()),
    }


@pytest.mark.parametrize("name", NAMES)
async def test_live_ingest(db, requested_budget: None, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="live")
    assert rep["status"] == "completed", rep.get("error")
    summary = rep["summary"]
    res: dict[str, Any] = {"budget": rep.get("budget"), "archived_raw": rep.get("archived_raw")}
    if "atlas" in summary:
        atlas = summary["atlas"]
        assert atlas["row_count"] > 0
        res["atlas"] = atlas
        res["coverage"] = _coverage(read_atlas_table(db.store, atlas["artifact_sha256"]))
    if "kg" in summary:
        kg = summary["kg"]
        assert kg["status"] == "loaded", kg
        res["kg"] = {k: v for k, v in kg.items() if k != "rejected_sample"}
    res.update({k: v for k, v in summary.items() if k not in ("atlas", "kg")})
    _RESULTS[name] = res
    out = os.environ.get("REVA_CULTURE_LIVE_OUT")
    if out:
        Path(out).write_text(json.dumps(_RESULTS, indent=1, default=str))
