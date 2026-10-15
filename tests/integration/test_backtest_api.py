"""Worker job ``backtest`` -> model.evaluation + evaluation artifact -> GET /v1/models/{key}/backtests.

Offline: recorded Eurostat fixtures (demography LI/LU since 2008 + EUROPOP2023 for the cohort
model; labour/pensions for the France 2010 retirement back-test) and the World Bank fixture (a
release needs selected estimates). Runs in its own throwaway database so its datasets and
evaluations never enter other test modules.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors import eurostat_demography as ed
from reva.adapters.connectors import eurostat_labour as lab
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evaluation import backtests as B
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.time import utc_now
from reva.operations.context import JobContext
from reva.settings import Settings

pytestmark = pytest.mark.db


@dataclass
class Env:
    settings: Settings
    owner: Engine
    worker: Engine
    store: FilesystemArtifactStore
    release_id: UUID
    client: TestClient
    token: str


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
    root = tmp_path_factory.mktemp("backtest_objects")
    with throwaway_database(base, prefix="reva_test_backtest") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker = make_engine("migration", settings), make_engine("worker", settings)
        seed(owner, store)
        demo = ed.EurostatDemographyConnector(countries=("LI", "LU"), since_year=2008, window_years=ed.WINDOW_YEARS)
        asyncio.run(run_connector(demo, FixtureHttp(ed.NAME), store, worker, persist_cursor=False,
                                  post_publish=ed.post_publish_datasets))
        asyncio.run(run_connector(lab.EurostatLabourPensionsConnector(), FixtureHttp(lab.CONNECTOR_NAME), store,
                                  worker, persist_cursor=False, post_publish=lab.post_publish_datasets))
        wb = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, worker, persist_cursor=False))
        rel = publish_release(worker, store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
        tok, sha = new_api_token()
        with owner.begin() as c:
            c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'bt-read')"),
                      {"h": sha, "s": ["read"]})
        api_settings = Settings.model_validate({
            **settings.model_dump(), "reva_session_secret": SecretStr("s" * 48),
            "reva_allowed_hosts": "testserver,localhost", "reva_cors_origins": ""})
        app = create_app(api_settings)
        try:
            with TestClient(app) as client:
                yield Env(settings, owner, worker, store, rel.release_id, client, tok)
        finally:
            app.state.engine.dispose()
            for e in (owner, worker):
                e.dispose()


def q(engine: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings()]


def run_job(env: Env, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the handler as the worker would: a leased ops.job row and a real JobContext."""
    with env.owner.begin() as conn:
        jid = conn.execute(text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                                   VALUES ('backtest', CAST(:p AS jsonb), :k, 'worker:test') RETURNING id"""),
                           {"p": json.dumps(payload), "k": f"bt-test-{utc_now().timestamp()}"}).scalar_one()
        token = conn.execute(text("""UPDATE ops.job SET status='running', worker_id='t', attempts=attempts+1,
                                     lease_until=now()+interval '10 minutes', fencing_token=nextval('ops.fencing_seq')
                                     WHERE id=:j RETURNING fencing_token"""), {"j": jid}).scalar_one()

    def hb(_p: Any = None) -> str:
        with env.owner.connect() as c:
            return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

    ctx = JobContext(job_id=jid, job_type="backtest", worker_id="t", fencing_token=int(token), attempt=1,
                     settings=env.settings, worker_engine=env.worker, sim_engine=None, store=env.store, heartbeat=hb)
    return B.handle_backtest(payload, ctx)


def get(env: Env, url: str, **kw: Any) -> Any:
    return env.client.get(url, headers={"Authorization": f"Bearer {env.token}"}, **kw)


@pytest.fixture(scope="module")
def population(env: Env) -> dict[str, Any]:
    return run_job(env, {"model_key": "demography.cohort_component"})


@pytest.fixture(scope="module")
def retirement(env: Env) -> dict[str, Any]:
    return run_job(env, {"model_key": "social.retirement_age", "release_id": str(env.release_id)})


def test_handler_is_registered() -> None:
    from reva.operations.jobs import HANDLERS, SIM_ENGINE_JOB_TYPES, resolve_handler

    assert HANDLERS["backtest"] == "reva.evaluation.backtests:handle_backtest"
    assert resolve_handler("backtest") is B.handle_backtest
    assert "backtest" not in SIM_ENGINE_JOB_TYPES  # writes artifacts/evaluations with worker credentials


def test_payload_is_validated() -> None:
    with pytest.raises(CapabilityGap):
        B.parse_payload({"model_key": "forecast.last_value"})
    with pytest.raises(ValidationProblem):
        B.parse_payload({"model_key": "social.retirement_age", "countries": ["DEU"]})
    with pytest.raises(ValidationProblem):
        B.parse_payload({"model_key": "demography.cohort_component", "countries": ["lux"]})
    with pytest.raises(ValidationProblem):
        B.parse_payload({"model_key": "demography.cohort_component", "extra": 1})


def test_population_backtest_job_stores_rows_from_pinned_datasets(env: Env, population: dict[str, Any]) -> None:
    assert population["status"] == "succeeded" and population["reused"] is False
    assert population["units"] == ["LIE", "LUX"]  # default: every country whose population table is pinned
    rows = q(env.owner, """SELECT e.id, e.replay_mode, e.task_scope, e.evaluation_manifest_sha256, c.mode, c.origin,
                                  a.parameters
                           FROM model.evaluation e JOIN meta.context c ON c.id = e.context_id
                           JOIN meta.activity a ON a.id = e.provenance_activity_id
                           JOIN model.version v ON v.id = e.model_version_id
                           WHERE v.model_key = 'demography.cohort_component'""")
    assert len(rows) == 2 and {r["mode"] for r in rows} == {"evaluation"}
    assert {r["replay_mode"] for r in rows} == {"revised_data_retrospective"}
    assert {r["origin"] for r in rows} == {"test_fixture"}
    assert {r["parameters"]["release_id"] for r in rows} == {str(env.release_id)}
    [sha] = population["evaluation_manifest_sha256"]
    manifest = json.loads(env.store.get("evaluation", sha))
    pinned = {r["artifact_sha256"] for r in q(env.owner, """SELECT d.artifact_sha256 FROM belief.release_dataset p
                                                           JOIN evidence.dataset d ON d.id = p.dataset_id
                                                           WHERE p.release_id = :r""", r=env.release_id)}
    used = {p["artifact_sha256"] for c in manifest["countries"].values() for p in c["inputs"].values()}
    assert used and used <= pinned  # only release-pinned datasets, by digest
    assert manifest["baseline_release"]["id"] == str(env.release_id)


def test_rerun_reuses_the_stored_backtest(env: Env, population: dict[str, Any]) -> None:
    again = run_job(env, {"model_key": "demography.cohort_component"})
    assert again["reused"] is True and sorted(again["evaluation_ids"]) == sorted(population["evaluation_ids"])
    n = q(env.owner, """SELECT count(*) AS n FROM model.evaluation e JOIN model.version v ON v.id = e.model_version_id
                        WHERE v.model_key = 'demography.cohort_component'""")[0]["n"]
    assert n == 2


def test_retirement_backtest_job_matches_the_pure_function(env: Env, retirement: dict[str, Any]) -> None:
    from reva.models.social import backtest as bt

    assert retirement["status"] == "succeeded" and retirement["units"] == ["FRA"]
    [row] = q(env.owner, """SELECT e.task_scope, e.metrics, e.evaluation_manifest_sha256, e.replay_mode
                            FROM model.evaluation e JOIN model.version v ON v.id = e.model_version_id
                            WHERE v.model_key = 'social.retirement_age'""")
    assert row["replay_mode"] == "revised_data_retrospective"
    assert row["task_scope"]["task"] == "retirement_backtest" and row["task_scope"]["base_year"] == bt.BASE_YEAR
    # independent recomputation from the captured provider bytes (no database)
    from reva.ports.connectors import FetchBudget

    async def raws() -> list[Any]:
        c, http = lab.EurostatLabourPensionsConnector(), FixtureHttp(lab.CONNECTOR_NAME)
        return [await c.fetch(i, http) for i in c.plan(None, FetchBudget()).items]

    derived = {r.item.params["table"]: lab.derive_dataset_tables(r.body, r.item.params["table"])
               for r in asyncio.run(raws())}
    tables = {k: derived[k]["FRA"].to_pylist() for k in ("lfsa_ergan", "lfsa_argan")}
    pure = bt.run(tables["lfsa_ergan"], tables["lfsa_argan"])
    assert row["metrics"]["report_digest"] == pure["digest"]
    central = pure["results"][bt.CENTRAL]
    for s in ("F", "M"):
        assert row["metrics"]["central"][s]["mean_absolute_error_pp"] == central[s]["mean_absolute_error_pp"]
    manifest = json.loads(env.store.get("evaluation", row["evaluation_manifest_sha256"]))
    assert manifest["replay_mode"] == "revised_data_retrospective" and manifest["report"]["digest"] == pure["digest"]
    assert set(manifest["inputs"]) == {"employment_rate", "activity_rate"}
    assert all(len(p["artifact_sha256"]) == 64 for p in manifest["inputs"].values())


def test_model_without_backtest_function_is_a_typed_gap(env: Env) -> None:
    with pytest.raises(CapabilityGap):
        run_job(env, {"model_key": "forecast.last_value"})


def test_api_lists_backtests_with_metrics_design_sha_and_labels(env: Env, population: dict[str, Any],
                                                                 retirement: dict[str, Any]) -> None:
    r = get(env, "/v1/models/social.retirement_age/backtests")
    assert r.status_code == 200, r.text
    body = r.json()
    [bt_] = body["data"]
    assert bt_["task"] == "retirement_backtest" and bt_["country"] == "FRA"
    assert bt_["backtest_function"] == "reva.models.social.backtest:run"
    assert bt_["evaluation_manifest_sha256"] == retirement["evaluation_manifest_sha256"][0]
    assert bt_["release_id"] == str(env.release_id) and bt_["origin"] == "test_fixture"
    assert bt_["design"]["protocol"].startswith("reva-pen-backtest-fra-2010")
    assert bt_["metrics"]["central"]["F"]["mean_absolute_error_pp"]
    assert bt_["labels"][0] == "revised-data retrospective — not a real-time backtest"
    assert "experimental model, not yet reviewed" in bt_["labels"]
    codes = {w["code"] for w in body["warnings"]}
    assert "revised_data_retrospective" in codes
    assert body["context_mode"] == "evaluation" and body["historical_validation"]["status"] == "reported"

    first = get(env, "/v1/models/demography.cohort_component/backtests", params={"limit": 1}).json()
    assert len(first["data"]) == 1 and first["next_cursor"]
    second = get(env, "/v1/models/demography.cohort_component/backtests",
                 params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert {first["data"][0]["country"], second["data"][0]["country"]} == {"LIE", "LUX"}
    assert first["data"][0]["design"]["jump_off"] == 2013

    # the generic evaluation listing now reports V2 models too
    ev = get(env, "/v1/models/social.retirement_age/evaluations").json()
    assert len(ev["data"]) == 1


def test_api_unknown_model_unsupported_model_and_auth(env: Env) -> None:
    assert get(env, "/v1/models/no.such_model/backtests").status_code == 404
    r = get(env, "/v1/models/forecast.last_value/backtests")
    assert r.status_code == 200 and r.json()["data"] == []
    assert "backtest_unavailable" in {w["code"] for w in r.json()["warnings"]}
    assert env.client.get("/v1/models/social.retirement_age/backtests").status_code == 401


def test_enqueue_backtest_names_the_release(env: Env) -> None:
    jid = B.enqueue_backtest(env.worker, "social.retirement_age")
    [job] = q(env.owner, "SELECT job_type, payload, idempotency_key, status FROM ops.job WHERE id=:j", j=jid)
    assert job["job_type"] == "backtest" and job["status"] == "queued"
    assert job["payload"] == {"model_key": "social.retirement_age", "release_id": str(env.release_id)}
    assert B.enqueue_backtest(env.worker, "social.retirement_age") == jid  # idempotent
    with env.owner.begin() as c:
        c.execute(text("DELETE FROM ops.job WHERE id=:j"), {"j": jid})


def test_api_post_enqueues_a_backtest_job(env: Env) -> None:
    r = env.client.post("/v1/models/demography.cohort_component/backtests",
                        headers={"Authorization": f"Bearer {env.token}", "Idempotency-Key": "bt-api-post-0001"},
                        json={"countries": ["li", "LU"]})
    assert r.status_code == 202, r.text
    job_id = r.json()["data"]["job_id"] if "data" in r.json() else r.json()["job_id"]
    rows = q(env.owner, "SELECT job_type, payload FROM ops.job WHERE id = :j", j=job_id)
    assert rows and rows[0]["job_type"] == "backtest"
    assert rows[0]["payload"]["model_key"] == "demography.cohort_component"
    assert rows[0]["payload"]["countries"] == ["LI", "LU"]
    bad = env.client.post("/v1/models/demography.cohort_component/backtests",
                          headers={"Authorization": f"Bearer {env.token}", "Idempotency-Key": "bt-api-post-0002"},
                          json={"unexpected": 1})
    assert bad.status_code == 422
