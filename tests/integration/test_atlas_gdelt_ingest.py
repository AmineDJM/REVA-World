"""V6SIGNALS: GDELT media-coverage signals end to end on the per-session throwaway database (fixture mode).

The sampled update files are archived as evidence acquisitions, ``gdelt.events_cd`` is registered with its rights
(signal kind, attribution), a missing update is a typed-missing row, and a second run serves the update files from
the archive (immutable GDELT files are not downloaded again) and registers nothing new.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_gdelt as g
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

OPTIONS = {"end": "2026-10-15", "days": "3"}


async def _run(db: Any, name: str, options: dict[str, Any]) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", options=options)
    assert out["status"] == "completed", out
    summary: dict[str, Any] = out["summary"]
    return summary


async def test_gdelt_events_end_to_end_and_archive_reuse(db: Any) -> None:
    s = await _run(db, g.NAME_EVENTS, OPTIONS)
    (t,) = s["tables"]
    assert t["table"] == "gdelt.events_cd" and t["created"] and t["rows"] > 0
    rep = s["report"]["events_cd"]
    assert (rep["fetched"], rep["reused"], list(rep["missing"])) == (2, 0, ["20261014120000"])
    f = read_atlas_table(db.store, t["artifact_sha256"])
    assert {json.loads(x)["signal_kind"] for x in f["flags"].unique().to_list()} == {g.SIGNAL_KIND}
    assert set(f.filter(pl.col("provider_geo") == "UP")["iso3"].unique().to_list()) == {"UKR"}
    miss = f.filter(pl.col("period_start").cast(pl.String) == "2026-10-14")
    assert miss.height == 1 and miss["value"].to_list() == [None]

    again = await _run(db, g.NAME_EVENTS, OPTIONS)
    rep2 = again["report"]["events_cd"]
    assert (rep2["fetched"], rep2["reused"]) == (0, 2)
    assert not again["tables"][0]["created"]
