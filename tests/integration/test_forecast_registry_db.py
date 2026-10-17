"""Forecast registry end to end (PRED-REGISTRY): atlas table -> issue (seal) -> new table version -> resolve
(score) -> back-test -> GET /v1/forecasts/*.

Offline, in its own throwaway database: the raw artifacts are World Bank fixture acquisitions (test_fixture
origin) cited by a small synthetic FAOSTAT-CP-shaped table built around the database clock.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.atlas.tables import ATLAS_SCHEMA, register_atlas_table
from reva.evaluation.registry import periods as P
from reva.evaluation.registry import service as svc
from reva.evidence.runner import FixtureHttp, run_connector
from reva.settings import Settings

pytestmark = pytest.mark.db
FAM = "fao.food_inflation"


@dataclass
class Env:
    owner: Engine
    worker: Engine
    store: FilesystemArtifactStore
    raw: str
    client: TestClient
    token: str
    today: date


def frame(raw: str, data: dict[tuple[str, int], str]) -> pl.DataFrame:
    rows = [
        {
            "source": "fao",
            "dataset": "cp",
            "indicator": "23014|6121",
            "concept_id": None,
            "iso3": None,
            "provider_geo": geo,
            "entity_qid": None,
            "period_start": P.start_of("M", i),
            "period_end": P.end_of("M", i),
            "value": v,
            "value_text": None,
            "unit": "%",
            "flags": "{}",
            "raw_sha256": raw,
            "selector": json.dumps({"geo": geo, "m": i}),
        }
        for (geo, i), v in sorted(data.items())
    ]
    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


def history(last: int) -> dict[tuple[str, int], str]:
    out = {}
    for geo, k in (("FRA", 1.0), ("DEU", 2.0)):
        for j in range(120):
            out[(geo, last - 119 + j)] = format(3.0 + k * math.sin(j / 4.0), ".6f")
    return out


def register(env: Env, data: dict[tuple[str, int], str]) -> dict[str, Any]:
    return register_atlas_table(
        env.worker, env.store, source="fao", dataset="cp", frame=frame(env.raw, data), raw_sha256s=[env.raw],
        parser_version="test-registry/1", origin="test_fixture", geo_scheme="iso3",
    )  # fmt: skip


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.api.app import create_app
    from reva.api.auth import new_api_token
    from reva.cli.seed import seed

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    root = tmp_path_factory.mktemp("registry_objects")
    with throwaway_database(base, prefix="reva_test_fcreg") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker = make_engine("migration", settings), make_engine("worker", settings)
        seed(owner, store)
        wb = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, worker, persist_cursor=False))
        with worker.connect() as c:
            raw = c.execute(
                text("SELECT raw_sha256 FROM evidence.acquisition WHERE origin='test_fixture' ORDER BY 1 LIMIT 1")
            ).scalar_one()
            today = c.execute(text("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date")).scalar_one()
        tok, sha = new_api_token()
        with owner.begin() as c:
            c.execute(
                text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'fc-read')"),
                {"h": sha, "s": ["read"]},
            )
        api_settings = Settings.model_validate(
            {**settings.model_dump(), "reva_session_secret": SecretStr("s" * 48),
             "reva_allowed_hosts": "testserver,localhost", "reva_cors_origins": ""}
        )  # fmt: skip
        app = create_app(api_settings)
        try:
            with TestClient(app) as client:
                yield Env(owner, worker, store, raw, client, tok, today)
        finally:
            app.state.engine.dispose()
            owner.dispose()
            worker.dispose()


def get(env: Env, path: str, **params: Any) -> Any:
    r = env.client.get(path, params=params, headers={"Authorization": f"Bearer {env.token}"})
    assert r.status_code == 200, r.text
    return r.json()


def last_month(env: Env) -> int:
    return P.index_of("M", P.cycle_anchor(P.cycle_of(env.today))) - 6


def test_01_issue_seals_a_batch_once_per_cycle(env: Env) -> None:
    register(env, history(last_month(env)))
    out = svc.issue_cycle(env.worker, env.store, origin="test_fixture", families=[FAM])
    assert out["status"] == "issued", out
    assert out["n_questions"] == 12  # FRA, DEU x (4 interval + 2 binary)
    again = svc.issue_cycle(env.worker, env.store, origin="test_fixture", families=[FAM])
    assert again["status"] == "nothing_to_issue" and again["already_issued_in_cycle"] == 12
    with env.worker.connect() as c:
        rows = c.execute(
            text("SELECT evaluation_manifest_sha256, replay_mode, metrics FROM model.evaluation "
                 "WHERE metrics->>'kind' = 'registry_issue'")
        ).all()  # fmt: skip
    assert len(rows) == 5 and {r[0] for r in rows} == {out["batch_sha256"]}
    assert {r[1] for r in rows} == {"operational_as_known"}
    with pytest.raises(Exception, match=r"immutable|not allowed|reject"):  # sealed rows never change
        with env.owner.begin() as c:
            c.execute(text("UPDATE model.evaluation SET metrics = '{}'::jsonb WHERE evaluation_manifest_sha256 = :s"),
                      {"s": out["batch_sha256"]})  # fmt: skip


def test_02_resolution_waits_then_scores_from_a_later_version(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    first = svc.resolve_due(env.worker, env.store, origin="test_fixture")
    assert [b["status"] for b in first["batches"]] == ["nothing_due"]  # reference periods have not ended
    monkeypatch.setattr(svc, "period_ended", lambda q, today: True)
    pending = svc.resolve_due(env.worker, env.store, origin="test_fixture")
    assert pending["batches"][0]["status"] == "awaiting_outcomes"  # no version after the seal yet
    lm = last_month(env)
    data = history(lm)
    for geo in ("FRA", "DEU"):
        for h in range(1, 20):
            data[(geo, lm + h)] = format(3.5 + 0.01 * h, ".6f")
    v2 = register(env, data)
    assert v2["created"]
    out = svc.resolve_due(env.worker, env.store, origin="test_fixture")
    b = out["batches"][0]
    assert b["status"] == "scored" and b["resolved"] == 12, out
    assert svc.resolve_due(env.worker, env.store, origin="test_fixture")["batches"][0]["status"] == "nothing_due"


def test_03_backtest_is_labelled_final_vintage(env: Env) -> None:
    out = svc.run_backtest(env.worker, env.store, origin="test_fixture", families=[FAM], years=3)
    assert out["status"] == "recorded" and "final-vintage pseudo-real-time" in out["label"]
    with env.worker.connect() as c:
        modes = c.execute(
            text("SELECT DISTINCT replay_mode FROM model.evaluation WHERE metrics->>'kind' = 'registry_backtest'")
        ).scalars().all()  # fmt: skip
    assert modes == ["revised_data_retrospective"]


def test_04_api(env: Env) -> None:
    fams = get(env, "/v1/forecasts/families", origin="test_fixture")["data"]
    st = {f["key"]: f["status"] for f in fams}
    assert st[FAM] == "available" and st["fao.food_price_index"] == "awaiting_table"
    assert st["imf.cpi_inflation_monthly"] == "awaiting_table"
    batches = get(env, "/v1/forecasts/batches", origin="test_fixture")
    assert batches["origin"] == "test_fixture" and len(batches["data"]) == 1
    assert {w["code"] for w in batches["warnings"]} >= {"baseline_forecasts_only", "not_advice", "fixture_data"}
    qs = get(env, "/v1/forecasts/questions", origin="test_fixture", status="resolved")["data"]
    assert qs["total"] == 12 and all(q["outcome"] is not None for q in qs["questions"])
    qid = next(q["question_id"] for q in qs["questions"] if q["type"] == "binary")
    d = get(env, f"/v1/forecasts/questions/{qid}")["data"]
    assert d["seal"]["verified"] and d["seal"]["batch_sha256"] == batches["data"][0]["batch_sha256"]
    issued = [f for f in d["forecasts"] if f["status"] == "issued"]
    assert issued and all(f["score"] is not None and f["score"]["brier"] is not None for f in issued)
    assert d["outcome"]["source"]["dataset_key"] == "atlas.fao.cp"
    lb = get(env, "/v1/forecasts/leaderboard", origin="test_fixture")["data"]
    assert lb["questions"]["resolved"] == 12
    pers = next(m for m in lb["models"] if m["model"] == "forecast.registry_persistence")
    assert pers["n_scored"] > 0 and pers["mae_vs_persistence"]["ratio"] == pytest.approx(1.0)
    weo = next(m for m in lb["models"] if m["model"] == "forecast.reference_imf_weo")
    assert weo["n_scored"] == 0 and weo["mae"] is None  # not applicable, never 0
    cal = get(env, "/v1/forecasts/calibration", origin="test_fixture", model="forecast.registry_ar")["data"]
    assert cal["n_scored"] > 0 and sum(cal["pit_histogram"]["counts"]) == cal["pit_histogram"]["n"]
    bt = get(env, "/v1/forecasts/backtests", origin="test_fixture")
    assert bt["data"][0]["vintage_mode"] == "final_vintage_pseudo_real_time"
    assert "final_vintage_pseudo_real_time" in {w["code"] for w in bt["warnings"]}
    r = env.client.get("/v1/forecasts/questions/fq_" + "0" * 24, headers={"Authorization": f"Bearer {env.token}"})
    assert r.status_code == 404


def test_05_void_by_registry_rule_is_appended_never_edited(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """FIX4B (audit AUDIT3 M7): a sealed question that cannot be resolved as worded is closed as void with a
    reason by an appended resolution; the sealed batch is untouched; unknown ids are refused before any write."""
    qs = get(env, "/v1/forecasts/questions", origin="test_fixture", status="resolved")["data"]["questions"]
    qid = qs[0]["question_id"]
    void = {qid: {"reason": "fiscal_year_basis", "detail": "synthetic test: worded as a calendar year"}}
    with pytest.raises(ValueError, match="not in any sealed batch"):
        svc.void_questions(env.worker, env.store, {"fq_" + "0" * 24: void[qid]}, origin="test_fixture")
    with pytest.raises(ValueError, match="reason and a detail"):
        svc.void_questions(env.worker, env.store, {qid: {"reason": "x"}}, origin="test_fixture")
    out = svc.void_questions(env.worker, env.store, void, origin="test_fixture")
    assert out["batches"][0]["status"] == "already_handled"  # resolved questions are never re-closed
    with env.worker.connect() as c:
        before = c.execute(
            text("SELECT count(*) FROM model.evaluation WHERE metrics->>'kind' = 'registry_score'")
        ).scalar()
    monkeypatch.setattr(svc, "handled_questions", lambda conn, sha: set())  # pretend it is still open
    # FIX7 (QA-PRED X11): its period has ended -> a void needs logged counterfactual scores; refused before any write
    with pytest.raises(ValueError, match="requires counterfactual=True"):
        svc.void_questions(env.worker, env.store, void, origin="test_fixture")
    with env.worker.connect() as c:
        assert c.execute(
            text("SELECT count(*) FROM model.evaluation WHERE metrics->>'kind' = 'registry_score'")
        ).scalar() == before  # fmt: skip
    out = svc.void_questions(env.worker, env.store, void, origin="test_fixture", counterfactual=True)
    b = out["batches"][0]
    assert b["status"] == "voided" and b["void"] == 1, out
    doc = json.loads(env.store.get("evaluation", b["resolution_sha256"]))
    assert doc["outcomes"] == [{"question_id": qid, "status": "void", **void[qid]}] and doc["scores"] == []
    assert "never edited or re-sealed" in doc["rule"] and "counterfactual" in doc["rule"]
    assert doc["counterfactual_scores"] and {s["question_id"] for s in doc["counterfactual_scores"]} == {qid}
    with env.worker.connect() as c:
        rows = c.execute(
            text("SELECT metrics FROM model.evaluation WHERE metrics->>'kind' = 'registry_score' "
                 "AND metrics->>'resolution_sha256' = :s"), {"s": b["resolution_sha256"]}
        ).scalars().all()  # fmt: skip
        after = c.execute(
            text("SELECT count(*) FROM model.evaluation WHERE metrics->>'kind' = 'registry_score'")
        ).scalar()
    assert after == before + len(rows) and rows and all(r["n_void"] == 1 and r["scores"] == [] for r in rows)
    assert any(r.get("counterfactual_scores") for r in rows)  # kept per model for the leaderboard audit
