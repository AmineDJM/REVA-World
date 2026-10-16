"""Live A-ECRISES ingest (Laeven-Valencia, Reinhart-Rogoff, JST, ESRB) into the per-session throwaway database.

Deselected by default: ``pytest -m "live and db" tests/integration/test_atlas_ecrises_live.py``. Goes through
``reva.atlas.ingest.run_atlas_ingest(mode="live")`` (reserve -> metered SafeHttpClient -> settle): 5 requests,
~3.3 MB. Until ATLASCORE merges ``registry/atlas/budget_requests/a_ecrises.yaml`` the pinned budget has no entry for
these sources and a live run is refused (fail closed); this throwaway-database test overlays exactly the requested
limits on the pinned policy for the duration of the test (the pinned file is not modified).

``REVA_ECRISES_LIVE_OUT=<path.json>`` writes the run and coverage summary used by docs/dev/v3_a_ecrises.md.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml
from sqlalchemy import text

from reva.adapters.connectors import atlas_ecrises as m
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import known_country_keys, read_atlas_table

pytestmark = [pytest.mark.live, pytest.mark.db]

REQUESTS = Path(__file__).resolve().parents[2] / "registry/atlas/budget_requests/a_ecrises.yaml"


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
        return replace(b, per_source_requests=per, policy_version=b.policy_version + "+a_ecrises_request")

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
    }


async def test_live_ingest(db, requested_budget: None) -> None:  # type: ignore[no-untyped-def]
    out: dict[str, Any] = {}
    for key, src in m.SOURCES.items():
        ensure_registered(db.owner, db.store, get_source(src.name).spec)
        rep = await run_atlas_ingest(db.settings, db.worker, db.store, src.name, mode="live")
        assert rep["status"] == "completed", rep["error"]
        s = rep["summary"]
        assert not s["kg"]["rejected_counts"], s["kg"]
        frame = read_atlas_table(db.store, s["atlas"]["artifact_sha256"])
        out[key] = {
            "run": {k: rep[k] for k in ("budget", "archived_raw", "registered")},
            "summary": {k: v for k, v in s.items() if k != "report"},
            "report": s["report"],
            "coverage": _coverage(frame),
        }
    with db.owner.connect() as c:
        ev = c.execute(
            text("SELECT count(*) FROM kg.entity WHERE kind = 'event' AND qid LIKE 'ecrises:%'")
        ).scalar_one()
        claims = c.execute(
            text(
                "SELECT relation, source_tier, count(*) FROM kg.causal_claim_current WHERE source_key = 'esrb.fcdb' GROUP BY 1, 2"
            )
        ).all()
        grades = c.execute(
            text(
                """SELECT g.grade, g.temporal_check, count(*) FROM kg.causal_grade_current g
                   JOIN kg.causal_claim_current k ON k.id = g.claim_row_id
                   WHERE k.source_key = 'esrb.fcdb' GROUP BY 1, 2"""
            )
        ).all()
        by_year = c.execute(
            text(
                """SELECT substr(value->>'time', 1, 3) AS decade3, count(*) FROM kg.statement_current
                   WHERE property_pid = 'P580' AND subject_qid LIKE 'ecrises:%' GROUP BY 1 ORDER BY 1"""
            )
        ).all()
    assert ev > 2000 and claims
    out["kg_events"] = ev
    out["claims"] = [list(r) for r in claims]
    out["grades"] = [list(r) for r in grades]
    out["events_by_decade"] = dict(Counter({f"{r[0]}0s": r[1] for r in by_year}))
    path = os.environ.get("REVA_ECRISES_LIVE_OUT")
    if path:
        Path(path).write_text(json.dumps(out, indent=1, default=str))
