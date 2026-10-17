"""V6INFRA connectors end to end on a throwaway database (fixture bytes, no network): atlas tables registered,
KG entities/statements loaded with their ADM1 / UN/LOCODE / measured_by edges, a second run is a no-op."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_ghcnm as gh
from reva.adapters.connectors import atlas_portwatch as pw
from reva.adapters.connectors import atlas_unlocode as un
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import atlas_sources, ensure_registered

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(un, "MIN_LOCATIONS", 10)
    monkeypatch.setattr(pw, "MIN_CHOKE_ROWS", 10)
    monkeypatch.setattr(pw, "MIN_PORTS", 10)


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, gh.ne_spec())  # Natural Earth bytes are archived under its own spec
    ensure_registered(db.owner, db.store, atlas_sources()[name].spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    assert out["summary"]["ok"], out["summary"]
    return out["summary"]  # type: ignore[no-any-return]


def _edges(db: Any, pid: str) -> list[tuple[str, str]]:
    with db.owner.connect() as c:
        rows = c.execute(text("SELECT subject_qid, object_qid FROM kg.statement WHERE property_pid = :p"), {"p": pid})
        return [(r[0], r[1]) for r in rows]


async def test_unlocode_and_portwatch_fixture_ingest(db: Any) -> None:
    u = await _run(db, un.NAME)
    assert u["tables"][0]["table"] == "unlocode.locations" and u["edition"] == "2025-1"
    assert u["kg"]["loaded"] and u["kg"]["load"].get("rejected_sample") == []
    adm = dict(_edges(db, "unlocode:in_adm1"))
    assert adm["unlocode:DZALG"] == "iso3166_2:DZ-16"
    again = await _run(db, un.NAME)
    assert again["tables"][0]["created"] is False

    p = await _run(db, pw.NAME)
    assert {t["table"] for t in p["tables"]} == {"portwatch.chokepoints_daily", "portwatch.country_monthly"}
    mb = _edges(db, "infra:measured_by")
    assert mb and all(o == "atlas:portwatch.chokepoints_daily" for _, o in mb)
    at = _edges(db, "portwatch:at_unlocode")
    assert at and all(o.startswith("unlocode:") for _, o in at)
