"""Live A-COUPS ingest (Powell & Thyne coups, Archigos 4.1 + WDQS same_as, REIGN-format leader list) into the
per-session throwaway database of the ``db`` fixture (created and dropped by the fixture; never the dev DB).

Deselected by default: ``pytest -m "live and db" tests/integration/test_atlas_coups_live.py --basetemp=<scratch>``.
Every run goes through ``reva.atlas.ingest.run_atlas_ingest(mode="live")`` (reserve -> metered SafeHttpClient ->
settle). Until ATLASCORE merges ``registry/atlas/budget_requests/a_coups.yaml``, this test overlays exactly the
requested per-source limits on the pinned policy for the duration of the test (the pinned file is not modified).
~17 requests, ~4 MB. ``REVA_COUPS_LIVE_OUT=<path.json>`` writes the run summaries used by docs/dev/v3_a_coups.md.
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
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = [pytest.mark.live, pytest.mark.db]

REQUESTS = Path(__file__).resolve().parents[2] / "registry/atlas/budget_requests/a_coups.yaml"
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
        return replace(b, per_source_requests=per, policy_version=b.policy_version + "+a_coups_request")

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


def _save() -> None:
    out = os.environ.get("REVA_COUPS_LIVE_OUT")
    if out:
        Path(out).write_text(json.dumps(_RESULTS, indent=1, default=str))


async def _run(db: Any, name: str, **kw: Any) -> dict[str, Any] | None:
    """One live run; a provider refusal (e.g. the HTTP 403 bot challenge jonathanmpowell.com answers the platform
    client) is recorded, not hidden: the summary file says which source could not be reached and why."""
    from reva.kernel.errors import SourceResponseError

    ensure_registered(db.owner, db.store, get_source(name).spec)
    try:
        rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="live", **kw)
    except SourceResponseError as e:
        _RESULTS[name] = {"status": "provider_refused", "error": str(e)[:300], "details": dict(e.details)}
        _save()
        return None
    assert rep["status"] == "completed", rep.get("error")
    res: dict[str, Any] = {
        "budget": rep.get("budget"),
        "archived_raw": rep.get("archived_raw"),
        "summary": rep["summary"],
    }
    res["coverage"] = {}
    for ds, a in (rep["summary"].get("atlas") or {}).items():
        with db.owner.connect() as c:
            art = c.execute(
                text(
                    "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = :k ORDER BY recorded_at DESC LIMIT 1"
                ),
                {"k": a["dataset_key"]},
            ).scalar_one()
        res["coverage"][ds] = _coverage(read_atlas_table(db.store, art))
    _RESULTS[name] = res
    _save()
    return rep["summary"]


async def test_live_coups_then_leaders(db: Any, requested_budget: None) -> None:
    s = await _run(db, "atlas.coups")
    assert s is None or (s["events"] >= 490 and not s["kg"]["rejected"]), s
    a = await _run(db, "atlas.archigos", options={"wikidata": "1"})
    assert a is not None and a["archigos"]["rows"] == 3409 and not a["kg"]["rejected"], a
    assert a["join"]["coup_removals"] > 300
    with db.owner.connect() as c:
        _RESULTS["kg_counts"] = dict(
            c.execute(
                text(
                    "SELECT property_pid, count(*) FROM kg.statement_current WHERE source_key IN "
                    "('powell_thyne.coups', 'archigos.leaders', 'reign.leaderlist') GROUP BY 1 ORDER BY 1"
                )
            ).all()
        )
    r = await _run(db, "atlas.reign_leaders")
    assert r is None or (r["leaderlist"]["rows"] >= 2700 and not r["kg"]["rejected"]), r
    with db.owner.connect() as c:
        _RESULTS["kg_counts"] = dict(
            c.execute(
                text(
                    "SELECT property_pid, count(*) FROM kg.statement_current WHERE source_key IN "
                    "('powell_thyne.coups', 'archigos.leaders', 'reign.leaderlist') GROUP BY 1 ORDER BY 1"
                )
            ).all()
        )
    if a.get("causal", {}).get("status") == "loaded":
        from reva.causal import grade_claims

        _RESULTS["grading"] = grade_claims(db.worker, origin="live").as_dict()
    with db.owner.connect() as c:
        if c.execute(text("SELECT to_regclass('kg.causal_claim_current') IS NOT NULL")).scalar_one():
            _RESULTS["causal_claims"] = dict(
                c.execute(
                    text(
                        "SELECT relation, count(*) FROM kg.causal_claim_current WHERE source_key = 'archigos.leaders' "
                        "GROUP BY 1"
                    )
                ).all()
            )
            _RESULTS["grades"] = dict(
                c.execute(
                    text(
                        "SELECT g.grade, count(*) FROM kg.causal_grade_current g JOIN kg.causal_claim_current c "
                        "ON c.id = g.claim_row_id WHERE c.source_key = 'archigos.leaders' GROUP BY 1"
                    )
                ).all()
            )
    _save()
