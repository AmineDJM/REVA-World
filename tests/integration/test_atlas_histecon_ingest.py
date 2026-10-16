"""A-HISTECON atlas sources end to end on a throwaway database through the atlas platform
(``reva.atlas.ingest.run_atlas_ingest`` in fixture mode: no network).

Raw responses are archived as evidence acquisitions, atlas tables are registered from them, and the COW
state-system membership / war participation statements are loaded into the KG; re-running with the same
bytes is a no-op. Truncation guards (minimum row/dataset counts) are relaxed in fixture mode only
(the fixtures are trimmed).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_clio, atlas_cow, atlas_gapminder, atlas_hyde, atlas_maddison
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _run(db: Any, module: Any) -> dict[str, Any]:
    spec = module.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
    assert out["status"] == "completed", out
    return out


def _summary(out: dict[str, Any]) -> dict[str, Any]:
    return out["summary"]


async def test_maddison_clio_gapminder_hyde_ingest_idempotent(db: Any) -> None:
    out = await _run(db, atlas_maddison)
    (t,) = _summary(out)["tables"]
    assert t["dataset_key"] == "atlas.maddison.mpd2023" and t["created"] is True
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    fra = frame.filter(
        (pl.col("iso3") == "FRA") & (pl.col("indicator") == "pop") & (pl.col("period_start") == date(1820, 1, 1))
    )
    assert fra["value"].to_list() == ["31250"]
    again = await _run(db, atlas_maddison)
    assert _summary(again)["tables"][0]["created"] is False

    (t,) = _summary(await _run(db, atlas_clio))["tables"]
    assert t["dataset_key"] == "atlas.clio.infra" and t["row_count"] > 10_000
    (t,) = _summary(await _run(db, atlas_gapminder))["tables"]
    assert t["coverage"]["period_max"] == "2025-01-01"
    (t,) = _summary(await _run(db, atlas_hyde))["tables"]
    assert t["dataset_key"] == "atlas.hyde.hyde321" and t["coverage"]["period_min"] == "-10000-01-01"  # BCE steps, astronomical
    with db.owner.connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM evidence.dataset WHERE dataset_key IN ('atlas.maddison.mpd2023',"
                "'atlas.clio.infra','atlas.gapminder.fasttrack','atlas.hyde.hyde321')"
            )
        ).scalar_one()
    assert n == 4


async def test_cow_atlas_and_kg_load(db: Any) -> None:
    out = await _run(db, atlas_cow)
    s = _summary(out)
    assert {t["dataset_key"] for t in s["tables"]} == {"atlas.cow.nmc7", "atlas.cow.system2024", "atlas.cow.wars4"}
    kg = s["kg"]
    assert not kg["rejected_counts"], kg["rejected_sample"]
    assert kg["statements_inserted"] > 1000
    with db.owner.connect() as c:
        rows = c.execute(
            text("""SELECT start_time, end_time FROM kg.statement_current
                    WHERE subject_qid = 'cow:220' AND property_pid = 'P463'
                      AND object_qid = 'cow:system:interstate' ORDER BY start_time""")
        ).all()
        ussr = (
            c.execute(
                text("""SELECT object_qid FROM kg.statement_current
                    WHERE subject_qid = 'cow:365' AND property_pid = 'cow:code_denotes'""")
            )
            .scalars()
            .all()
        )
    assert [tuple(r) for r in rows] == [("1816-01-01", "1942-11-11"), ("1944-08-25", None)]
    assert "Q15180" in ussr
    again = _summary(await _run(db, atlas_cow))
    assert again["kg"]["statements_inserted"] == 0
    assert all(t["created"] is False for t in again["tables"])
