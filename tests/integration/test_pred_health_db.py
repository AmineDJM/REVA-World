"""PRED-HEALTH end to end on a throwaway database (no network): WHO FluNet, the CDC FluSight / COVID-19 hubs and ECDC
ERVISS in fixture mode through the atlas platform -> stored back-tests (as-of for the hubs, labelled final-vintage
for the others) -> a sealed issue round (baselines + attributed hub ensemble, candidates only as winners) -> a later
table version -> resolution and WIS scores.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table, register_atlas_table
from reva.evaluation.health_registry import service as svc
from reva.evaluation.ledger import load_frozen_manifest

pytestmark = pytest.mark.db
SOURCES = ("atlas.who_flunet", "atlas.cdc_flusight", "atlas.cdc_covidhub", "atlas.ecdc_erviss")
DAY = date(2026, 10, 17)  # the fixtures' capture date: rounds are computed for it, whatever the test date


async def _ingest(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out


def _rows(db: Any, sql: str, **kw: Any) -> list[Any]:
    with db.worker.connect() as c:
        return list(c.execute(text(sql), kw).all())


def test_health_registry_end_to_end(db: Any) -> None:
    out = {n: asyncio.run(_ingest(db, n)) for n in SOURCES}
    hub = out["atlas.cdc_flusight"]["summary"]
    assert set(hub) == {"flusight_target", "flusight_vintages", "flusight_ensemble"}
    assert hub["flusight_target"]["atlas"]["unmapped_provider_geo"]  # states stay unmapped; US -> USA
    target = read_atlas_table(db.store, hub["flusight_target"]["atlas"]["artifact_sha256"])
    assert target.filter(pl.col("provider_geo") == "US")["iso3"].unique().to_list() == ["USA"]

    # back-tests: as-of for the hubs, final vintage elsewhere; GHE table absent -> awaiting
    bt = {f["family"]: f for f in svc.run_backtests(db.worker, db.store, origin="test_fixture")["families"]}
    assert bt["ghe.deaths"]["status"] == "awaiting_table"
    for k in ("flunet.flu_positivity", "erviss.ili_rate", "nhsn.flu_hosp", "nhsn.covid_hosp"):
        assert bt[k]["status"] == "recorded", bt[k]
    assert (
        bt["nhsn.flu_hosp"]["kind"] == "as_of_vintages"
        and "forecast.reference_flusight_ensemble" in bt["nhsn.flu_hosp"]["mean_wis"]
    )
    modes = dict(
        _rows(
            db,
            "SELECT task_scope->>'family', min(replay_mode) FROM model.evaluation WHERE metrics->>'kind' = 'health_backtest' GROUP BY 1",
        )
    )
    assert modes["nhsn.flu_hosp"] == "available_then_reconstruction"
    assert modes["flunet.flu_positivity"] == "revised_data_retrospective"
    doc = load_frozen_manifest(db.store, bt["nhsn.flu_hosp"]["backtest_sha256"])
    assert doc["result"]["label"].startswith("as-of back-test") and doc["result"]["vintages"]

    # issue: sealed once per round, baselines + reference (+ winners only)
    iss = svc.issue_round(db.worker, db.store, origin="test_fixture", today=DAY)
    assert iss["status"] == "issued", iss
    assert iss["by_family"]["nhsn.flu_hosp"] == 12  # 3 locations x horizons 0..3
    again = svc.issue_round(db.worker, db.store, origin="test_fixture", today=DAY)
    assert again["status"] == "nothing_to_issue"
    man = load_frozen_manifest(db.store, iss["batch_sha256"])
    models = {f["model"] for f in man["forecasts"]}
    assert {"health.persistence", "health.climatology", "forecast.reference_flusight_ensemble"} <= models
    for fam, d in man["families"].items():
        assert set(d["issued_models"]) - set(d["winners"]) <= {"health.persistence", "health.climatology"}, fam
    ref = next(
        f for f in man["forecasts"] if f["model"] == "forecast.reference_flusight_ensemble" and f["status"] == "issued"
    )
    assert "not a REVA forecast" in ref["attribution"] and len(ref["quantiles"]) == 23
    with pytest.raises(Exception, match=r"calendar override"):
        svc.issue_round(db.worker, db.store, origin="live", today=DAY)

    # resolution waits for a version registered after the seal, then scores from it
    later_day = DAY + timedelta(days=40)
    first = svc.resolve_due(db.worker, db.store, origin="test_fixture", today=later_day)
    assert first["batches"][0]["status"] == "awaiting_outcomes"
    extra = []
    for q in man["questions"]:
        if q["family"] != "nhsn.flu_hosp":
            continue
        row = target.filter(pl.col("provider_geo") == q["geo"]).row(-1, named=True)
        extra.append(
            {
                **row,
                "period_start": date.fromisoformat(q["period_start"]),
                "period_end": date.fromisoformat(q["period_end"]),
                "value": "100",
                "selector": json.dumps({"test": q["question_id"]}),
            }
        )
    v2 = register_atlas_table(
        db.worker, db.store, source="cdc", dataset="flusight_target", frame=pl.concat([target, pl.DataFrame(extra, schema=target.schema)]),
        raw_sha256s=sorted(set(target["raw_sha256"].to_list())), parser_version="test-pred-health/1", origin="test_fixture",
        geo_scheme="cdc_hub",
    )  # fmt: skip
    assert v2["created"]
    res = svc.resolve_due(db.worker, db.store, origin="test_fixture", today=later_day)
    b = res["batches"][0]
    assert b["status"] == "scored" and b["outcomes"]["resolved"] == 12, res
    scores = _rows(
        db,
        "SELECT metrics->>'model', (metrics->>'n_scored')::int FROM model.evaluation WHERE metrics->>'kind' = 'health_score'",
    )
    assert dict(scores)["forecast.reference_flusight_ensemble@1.0.0"] == 12
    assert (
        svc.resolve_due(db.worker, db.store, origin="test_fixture", today=later_day)["batches"][0]["status"]
        == "awaiting_outcomes"
    )
