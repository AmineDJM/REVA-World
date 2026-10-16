"""A-CONTENTION sources end to end on a throwaway database through the atlas platform (fixture mode: no network).

Raw slices are archived as evidence acquisitions, the computed atlas tables are registered, the KG events load
without rejection, the T1 causal claims are delivered to CAUSCORE's loader (or staged when it is absent), and
re-running is a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_massmob, atlas_navco
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, spec: Any, **kw: Any) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture", **kw)
    assert out["status"] == "completed", out
    return out["summary"]


def _check_delivery(s: dict[str, Any]) -> None:
    """Claims went to CAUSCORE's loader without rejection, or were staged as complete deliveries."""
    total = 0
    for ds, d in s["claims_delivery"].items():
        assert d["status"] in ("loaded", "staged"), (ds, d)
        total += d["claims"]
        if d["status"] == "loaded":
            assert not d["report"].get("rejected_counts"), d["report"]
        else:
            doc = json.loads(Path(d["file"]).read_text())
            assert doc["interface"] == "reva.causal.load_dataset_claims" and len(doc["claims"]) == d["claims"]
    assert total == sum(s["claims"].values()) > 0


def _spec(name: str) -> Any:
    return next(s for s in atlas_navco.atlas_source() if s.name == name)


async def test_navco_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, _spec("atlas.navco"), options={"claims_staging": str(tmp_path)})
    assert s["atlas"]["n13_cy"]["created"] and s["atlas"]["n21_cy"]["created"]
    assert not s["kg"]["rejected"], s["kg"]
    assert s["kg"]["statements_inserted"] == s["kg_counts"]["statements"]
    _check_delivery(s)
    with db.owner.connect() as c:
        kinds = dict(
            c.execute(
                text("SELECT e.kind, count(*) FROM kg.entity_current e WHERE e.qid LIKE 'navco%' GROUP BY e.kind")
            ).all()
        )
        assert kinds.get("event", 0) > 50 and kinds.get("campaign_outcome", 0) > 10
        locs = dict(
            c.execute(
                text(
                    "SELECT subject_qid, object_qid FROM kg.statement_current WHERE property_pid = 'contention:location' "
                    "AND subject_qid IN ('navco13:238-1946', 'navco13:1035-1920')"
                )
            ).all()
        )
        # NAVCO location 'USSR': the Soviet Union's QID in 1946; the 1920 Tambov rebellion predates the USSR (1922)
        assert locs == {"navco13:238-1946": "Q15180", "navco13:1035-1920": "navco:place:ussr"}
        n = c.execute(
            text(
                "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'contention:phase_of' "
                "AND object_qid = 'navco21:282'"
            )
        ).scalar_one()
        assert n == 2
    again = await _run(db, _spec("atlas.navco"))
    assert not again["atlas"]["n13_cy"]["created"] and again["kg"]["statements_inserted"] == 0


async def test_mec_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, _spec("atlas.mec"), options={"claims_staging": str(tmp_path)})
    _check_delivery(s)
    assert s["atlas"]["episodes_cy"]["created"] and not s["kg"]["rejected"], s
    assert s["claims"]["coded_outcome"] > 0


async def test_massmob_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_massmob.atlas_source(), options={"claims_staging": str(tmp_path)})
    _check_delivery(s)
    assert s["atlas"]["protests_cy"]["created"] and not s["kg"]["rejected"], s
    assert s["kg"]["statements_inserted"] == s["kg_counts"]["statements"]
    with db.owner.connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'contention:directed_at' "
                "AND subject_qid LIKE :pat"
            ),
            {"pat": "mmp:%:state_response"},
        ).scalar_one()
    assert n == s["claims"]["reaction_to"]
