"""REVA ensemble end to end (V7 BEAT-ENSEMBLE): issue (seal) -> ensemble supplement (equal weights, labelled) ->
new table version -> resolve (ensemble scored like any forecaster) -> weights from the resolved scores only ->
GET /v1/forecasts/ensemble.

Offline, in its own throwaway database (same synthetic FAOSTAT-CP-shaped table as test_forecast_registry_db).
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
from reva.evaluation.ledger import load_frozen_manifest
from reva.evaluation.registry import ensemble as E
from reva.evaluation.registry import periods as P
from reva.evaluation.registry import service as svc
from reva.evidence.runner import FixtureHttp, run_connector
from reva.models.forecasting import online_combination as OC
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
    root = tmp_path_factory.mktemp("ensemble_objects")
    with throwaway_database(base, prefix="reva_test_fcens") as s0:
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


def test_01_ensemble_is_sealed_with_labelled_equal_weights(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    register(env, history(last_month(env)))
    out = svc.issue_cycle(env.worker, env.store, origin="test_fixture", families=[FAM])
    assert out["status"] == "issued" and out["n_questions"] == 12
    with monkeypatch.context() as m:  # same-date rule: an original sealed outside the window gets no ensemble
        m.setattr(E, "SAME_WINDOW_DAYS", -1)
        late = E.issue_ensemble(env.worker, env.store, origin="test_fixture")
    assert late["status"] == "nothing_to_issue" and late["originals_outside_same_window"] == 1
    ens = E.issue_ensemble(env.worker, env.store, origin="test_fixture")
    assert ens["status"] == "issued" and ens["ensembles"][0]["questions"] == 12 and ens["ensembles"][0]["issued"] == 12
    assert set(ens["ensembles"][0]["weight_tasks"]) == {OC.EQUAL}
    assert E.issue_ensemble(env.worker, env.store, origin="test_fixture")["status"] == "nothing_to_issue"
    sup = load_frozen_manifest(env.store, ens["ensembles"][0]["batch_sha256"])
    orig = {**load_frozen_manifest(env.store, out["batch_sha256"]), "_sha256": out["batch_sha256"]}
    assert sup["supplement_of"] == out["batch_sha256"] and sup["data"] == orig["data"]
    comps = E.components_of(out["batch_sha256"], [orig])
    assert E.rebuild_forecasts(sup, comps) == sup["forecasts"] and E.recheck_weights(sup)
    with env.worker.connect() as c:
        row = c.execute(
            text("SELECT v.model_key, e.task_scope FROM model.evaluation e JOIN model.version v ON v.id = "
                 "e.model_version_id WHERE e.evaluation_manifest_sha256 = :s"), {"s": ens["ensembles"][0]["batch_sha256"]}
        ).one()  # fmt: skip
    assert row[0] == E.ENSEMBLE_KEY and row[1]["ensemble"] is True and len(row[1]["question_ids"]) == 12


def test_02_resolution_scores_the_ensemble_and_weights_use_only_resolved_scores(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "period_ended", lambda q, today: True)
    real_extract = svc.extract_series  # time has passed: the later versions' cells are no longer future-dated (FIX5)
    monkeypatch.setattr(svc, "extract_series", lambda frame, fam, recorded=None: real_extract(frame, fam, None))
    lm = last_month(env)
    data = history(lm)
    for geo in ("FRA", "DEU"):
        for h in range(1, 20):
            data[(geo, lm + h)] = format(3.5 + 0.01 * h, ".6f")
    v2 = register(env, data)
    assert v2["created"], v2
    out = svc.resolve_due(env.worker, env.store, origin="test_fixture")
    assert sorted(b["status"] for b in out["batches"]) == ["scored", "scored"], out
    with env.worker.connect() as c:
        now = c.execute(text("SELECT clock_timestamp()")).scalar_one()
        scores = E._score_records(c, "test_fixture", now)
        bs = c.execute(text("SELECT DISTINCT evaluation_manifest_sha256 FROM model.evaluation "
                            "WHERE metrics->>'kind' = 'registry_issue' AND task_scope->>'ensemble' IS NULL")).scalar_one()  # fmt: skip
    assert sum(1 for s in scores if s["model"] == E.ENSEMBLE_KEY) == 12
    orig = {**load_frozen_manifest(env.store, bs), "_sha256": bs}
    b = E.build_ensemble_batch(orig, E.components_of(bs, [orig]), scores, issued_at=now, already=set(),
                               rule=OC.WeightRule(min_resolved=2))  # fmt: skip
    assert b is not None
    t = b["ensemble"]["tasks"][f"{FAM}|interval|location"]
    assert t["status"] == OC.ONLINE and t["n_resolved_questions"] == 8
    assert E.ENSEMBLE_KEY not in {r[1] for r in t["history"]}  # its own scores never set its weights
    assert len({float(w) for w in t["weights"].values()}) > 1


def test_03_api_scoreboard_fills_only_from_resolved_ensemble_scores(env: Env) -> None:
    board = get(env, "/v1/forecasts/ensemble", origin="test_fixture")["data"]
    assert board["model"] == E.ENSEMBLE_KEY and board["n_ensemble_forecasts"] == 12 and board["n_ensemble_scored"] == 12
    assert board["weights_by_family"] == {FAM: {OC.EQUAL: 12}}
    assert {c["status"] for c in board["cells"]} == {"awaiting_resolutions"} and board["holm_family_size"] == 0
    assert {(c["type"], c["metric"]) for c in board["cells"]} == {
        ("interval", "interval_score"), ("interval", "abs_error"), ("binary", "brier")}  # fmt: skip
    lb = get(env, "/v1/forecasts/leaderboard", origin="test_fixture")["data"]
    row = next(m for m in lb["models"] if m["model"] == E.ENSEMBLE_KEY)
    assert row["kind"] == "reva_ensemble" and row["n_scored"] == 12 and row["n_issued"] == 12
    assert lb["questions"]["resolved"] == 12 and lb["questions"].get("pending", 0) == 0  # supplements not re-counted
    qs = get(env, "/v1/forecasts/questions", origin="test_fixture", status="resolved")["data"]["questions"]
    d = get(env, f"/v1/forecasts/questions/{qs[0]['question_id']}")["data"]
    e = next(f for f in d["forecasts"] if f["model"] == E.ENSEMBLE_KEY)
    assert e["score"] is not None and e["ensemble"]["weight_status"]
