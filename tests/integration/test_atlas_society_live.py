"""Live A-SOCIETY ingest (RCS-Dem, WRP, HIEF, SSCP, CDPD, WHO homicide, TI CPI, OECD SIGI, ParlGov) into the
per-session throwaway database of the ``db`` fixture (created and dropped by the fixture; never the dev DB).

Deselected by default: ``pytest -m "live and db" tests/integration/test_atlas_society_live.py
--basetemp=<scratch>``. Every run goes through ``reva.atlas.ingest.run_atlas_ingest(mode="live")``
(reserve -> metered SafeHttpClient -> settle). Until ATLASCORE merges ``registry/atlas/budget_requests/
a_society.yaml``, the pinned budget has no entry for the six new source keys and a live run would be refused
(fail closed); this throwaway-database test therefore overlays exactly the requested per-source limits on the
pinned policy for the duration of the test (the pinned file is not modified). ~22 requests, ~70 MB.

``REVA_SOCIETY_LIVE_OUT=<path.json>`` writes the run and coverage summary used by docs/dev/v3_a_society.md.
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

NAMES = (
    "atlas.hief", "atlas.sscp_frac", "atlas.cdpd", "atlas.parlgov", "atlas.ti_cpi", "atlas.oecd_sigi",
    "atlas.who_violence", "atlas.wrp", "atlas.rcs_dem",
)  # fmt: skip
REQUESTS = Path(__file__).resolve().parents[2] / "registry/atlas/budget_requests/a_society.yaml"
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
            per[k] = max(per.get(k, 0), v)
        return replace(b, per_source_requests=per, policy_version=b.policy_version + "+a_society_request")

    monkeypatch.setattr(budgets, "load_budget", overlay)


def _coverage(frame: pl.DataFrame) -> dict[str, Any]:
    keys = known_country_keys()
    f = frame.with_columns(pl.col("period_start").dt.year().alias("y"))
    mapped = f.filter(pl.col("iso3").is_not_null())
    per_ind = (
        mapped.group_by("indicator")
        .agg(pl.col("iso3").n_unique().alias("countries"), pl.col("y").min().alias("y0"), pl.col("y").max().alias("y1"))
        .sort("countries", descending=True)
    )
    return {
        "rows": frame.height,
        "indicators": frame["indicator"].n_unique(),
        "countries": mapped["iso3"].n_unique(),
        "years": [int(f["y"].min()), int(f["y"].max())],
        "crosswalk_entities_missing": len(keys - set(mapped["iso3"].unique().to_list())),
        "unmapped_provider_geo": sorted(f.filter(pl.col("iso3").is_null())["provider_geo"].unique().to_list())[:200],
        "top_indicators": per_ind.head(12).to_dicts(),
    }


@pytest.mark.parametrize("name", NAMES)
async def test_live_ingest(db, requested_budget: None, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="live")
    assert rep["status"] == "completed", rep["error"]
    atlas = rep["summary"]["atlas"]
    assert atlas["row_count"] > 0
    frame = read_atlas_table(db.store, atlas["artifact_sha256"])
    _RESULTS[name] = {
        "dataset_key": atlas["dataset_key"],
        "artifact_sha256": atlas["artifact_sha256"],
        "row_count": atlas["row_count"],
        "budget": rep["budget"],
        "archived_raw": rep["archived_raw"],
        "parse": rep["summary"].get("parse"),
        "record": rep["summary"].get("record"),
        "coverage": _coverage(frame),
        "unmapped_provider_geo": atlas.get("unmapped_provider_geo"),
    }
    out = os.environ.get("REVA_SOCIETY_LIVE_OUT")
    if out:
        Path(out).write_text(json.dumps(_RESULTS, indent=1, default=str))
