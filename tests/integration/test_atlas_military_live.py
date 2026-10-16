"""Live A-MILITARY ingest (UN Peace and Security Data Hub) into the per-session throwaway database.

Deselected by default: ``pytest -m "live and db" tests/integration/test_atlas_military_live.py``. Goes through
``reva.atlas.ingest.run_atlas_ingest(mode="live")`` (reserve -> metered SafeHttpClient -> settle): 6 HDX licence
records + 6 Hub CSVs, ~46.5 MB; loads ~20k KG statements. Until ATLASCORE merges
``registry/atlas/budget_requests/a_military.yaml`` the pinned budget has no ``un.psdata`` entry and a live run is
refused (fail closed); this throwaway-database test overlays exactly the requested limit on the pinned policy for
the duration of the test (the pinned file is not modified).

``REVA_MILITARY_LIVE_OUT=<path.json>`` writes the run and coverage summary used by docs/dev/v3_a_military.md.
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
from reva.atlas.tables import known_country_keys, read_atlas_table

pytestmark = [pytest.mark.live, pytest.mark.db]

REQUESTS = Path(__file__).resolve().parents[2] / "registry/atlas/budget_requests/a_military.yaml"


@pytest.fixture
def requested_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import reva.operations.budgets as budgets

    pinned = budgets.load_budget
    req = {r["source_key"]: int(r["daily_requests"]) for r in yaml.safe_load(REQUESTS.read_text())["requests"]}

    def overlay(policy: Any = None) -> Any:
        b = pinned(policy)
        per = dict(b.per_source_requests)
        for k, v in req.items():
            per[k] = max(per.get(k, 0), v)
        return replace(b, per_source_requests=per, policy_version=b.policy_version + "+a_military_request")

    monkeypatch.setattr(budgets, "load_budget", overlay)


def _coverage(frame: pl.DataFrame) -> dict[str, Any]:
    f = frame.with_columns(pl.col("period_start").dt.year().alias("y"))
    mapped = f.filter(pl.col("iso3").is_not_null())
    return {
        "rows": frame.height,
        "indicators": frame["indicator"].n_unique(),
        "countries": mapped["iso3"].n_unique(),
        "years": [int(f["y"].min()), int(f["y"].max())],
        "crosswalk_entities_missing": len(known_country_keys() - set(mapped["iso3"].unique().to_list())),
        "historical_qids": sorted(f["entity_qid"].drop_nulls().unique().to_list()),
    }


async def test_live_ingest(db, requested_budget: None) -> None:  # type: ignore[no-untyped-def]
    name = "atlas.peacekeeping"
    ensure_registered(db.owner, db.store, get_source(name).spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="live")
    assert rep["status"] == "completed", rep["error"]
    s = rep["summary"]
    assert rep["registered"] and all(r["row_count"] > 0 for r in rep["registered"])
    assert not s["kg"]["rejected"], s["kg"]
    cov = {v["dataset_key"]: _coverage(read_atlas_table(db.store, v["artifact_sha256"])) for v in s["atlas"].values()}
    out = os.environ.get("REVA_MILITARY_LIVE_OUT")
    if out:
        Path(out).write_text(
            json.dumps(
                {"run": {k: rep[k] for k in ("budget", "archived_raw", "registered")}, "summary": s, "coverage": cov},
                indent=1,
                default=str,
            )
        )
