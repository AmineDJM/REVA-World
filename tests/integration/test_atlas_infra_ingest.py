"""A-INFRA atlas connectors end to end on a throwaway database (fixture bytes, no network).

Each source runs through the single atlas ingest entry point in fixture mode: raw bytes archived as
evidence acquisitions of the atlas connector versions, tables registered, a second run is a no-op.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_cables as cb
from reva.adapters.connectors import atlas_flights as fl
from reva.adapters.connectors import atlas_ports as po
from reva.adapters.connectors import atlas_space as sp
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixtures are trimmed excerpts: lower the truncation guards."""
    for mod, names in ((fl, ("MIN_OF_AIRPORTS", "MIN_OF_ROUTES", "MIN_OA_AIRPORTS")), (sp, ("MIN_LAUNCHES", "MIN_OBJECTS")),
                       (po, ("MIN_PORTS",))):  # fmt: skip
        for n in names:
            monkeypatch.setattr(mod, n, 10)


async def _run(db: Any, spec: Any, name: str, **kw: Any) -> dict[str, Any]:
    from reva.adapters.connectors import atlas_ghcnm as gh

    ensure_registered(db.owner, db.store, gh.ne_spec())  # V6INFRA: WPI/OurAirports archive Natural Earth under it
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", **kw)
    assert out["status"] == "completed" and out["budget"] is None, out
    assert out["summary"]["ok"]
    return out["summary"]  # type: ignore[no-any-return]


def _spec(name: str) -> Any:
    from reva.atlas.sources import atlas_sources

    return atlas_sources()[name].spec


async def test_infra_sources_fixture_ingest(db: Any) -> None:
    rep = await _run(db, _spec(fl.OF_NAME), fl.OF_NAME)
    (t,) = rep["tables"]
    assert t["table"] == "openflights.routes_2014" and t["unmapped_provider_geo"] == ["Netherlands Antilles"]
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    pair = frame.filter(pl.col("indicator") == "ROUTES_OPERATED|partner=DZA").filter(pl.col("provider_geo") == "France")
    assert pair.height == 1 and json.loads(pair["flags"][0])["partner_iso3"] == "DZA"
    with db.owner.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM evidence.acquisition WHERE origin = 'test_fixture' AND raw_sha256 = :s"),
            {"s": pair["raw_sha256"][0]},
        ).scalar_one()
    assert n == 1
    again = await _run(db, _spec(fl.OF_NAME), fl.OF_NAME)
    assert again["tables"][0]["created"] is False

    oa = await _run(db, _spec(fl.OA_NAME), fl.OA_NAME)
    assert oa["tables"][0]["table"] == "ourairports.airports"
    assert sorted(oa["tables"][0]["unmapped_provider_geo"]) == ["XP", "ZZ"]

    g = await _run(db, _spec(sp.NAME), sp.NAME)
    assert {x["table"] for x in g["tables"]} == {"gcat.launches", "gcat.payloads"}
    only = await _run(db, _spec(sp.NAME), sp.NAME, dataset="launches")
    assert [x["table"] for x in only["tables"]] == ["gcat.launches"] and only["tables"][0]["created"] is False

    w = await _run(db, _spec(po.NAME), po.NAME)
    assert w["tables"][0]["table"] == "nga.wpi" and sorted(w["tables"][0]["unmapped_provider_geo"]) == ["QM", "XU"]

    c = await _run(db, _spec(cb.NAME), cb.NAME)
    assert c["tables"][0]["table"] == "wikidata.submarine_cables" and c["report"]["cables"] == 20
    cf = read_atlas_table(db.store, c["tables"][0]["artifact_sha256"])
    assert cf.filter(pl.col("indicator") == "CABLES_SHARED|partner=CAN").height > 0


async def test_live_run_refused_without_budget(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live run fetches nothing when its budget key is not pinned (fail closed, invariant 30)."""
    import dataclasses

    from reva.operations import budgets as B

    ensure_registered(db.owner, db.store, _spec(po.NAME))
    real = B.load_budget()
    without = {k: v for k, v in real.per_source_requests.items() if k != po.SOURCE_KEY}
    monkeypatch.setattr(B, "load_budget", lambda *a, **k: dataclasses.replace(real, per_source_requests=without))
    calls: list[str] = []

    class Boom:
        async def get(self, url: str, **kw: Any) -> Any:
            calls.append(url)
            raise AssertionError("no request may be sent")

        async def aclose(self) -> None:
            return None

    with pytest.raises(B.BudgetExhausted):
        await run_atlas_ingest(
            db.settings, db.worker, db.store, po.NAME, mode="live", http_factory=lambda s, st: Boom()
        )
    assert calls == []
