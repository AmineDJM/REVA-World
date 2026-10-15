"""POST /v1/scenario-drafts against a real database through the reva_api role (offline: recorded LLM responses).

The module runs in its own freshly bootstrapped database. Until migration 0008_v2 ships the two
LLM-ledger SECURITY DEFINER functions, the fixture installs them from
``reva.nl.budget.LEDGER_FUNCTIONS_SQL`` (the exact SQL requested for 0008) as the migration owner;
``test_ledger_functions_come_from_the_migration_when_present`` records which path was used.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.api.app import create_app
from reva.api.auth import hash_password, new_api_token
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, post_publish_for, run_connector
from reva.kernel.context import Origin
from reva.nl.budget import LEDGER_FUNCTIONS_SQL, LlmBudget
from reva.nl.recorded import RecordedDrafter, load_fixtures
from reva.settings import Settings

pytestmark = pytest.mark.db

ADMIN_PASSWORD = "correct horse battery staple"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nl" / "recorded"
TEXTS = {f["label"]: f["text"] for f in load_fixtures(FIXTURES)}
URL = "/v1/scenario-drafts"


@dataclass
class Env:
    settings: Settings
    owner: Engine
    client: TestClient
    app: Any
    release_id: str
    drafter: RecordedDrafter
    functions_from_migration: bool


def _settings(base: Settings, **over: Any) -> Settings:
    data = base.model_dump()
    data.update(reva_admin_password_hash=SecretStr(hash_password(ADMIN_PASSWORD)),
                reva_session_secret=SecretStr("s" * 48), reva_allowed_hosts="testserver,localhost",
                reva_cors_origins="", reva_env="test", reva_llm_enabled=True)
    data.update(over)
    return Settings.model_validate(data)


@pytest.fixture(scope="module")
def env(db: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from reva.cli.bootstrap import bootstrap

    name = f"reva_test_nl_{uuid.uuid4().hex[:8]}"
    admin = create_engine(db.settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp("nl_objects")
    url = db.settings.migration_url().set(database=name).render_as_string(hide_password=False)
    base = Settings.model_validate({**db.settings.model_dump(), "reva_migration_database_url": SecretStr(url),
                                    "reva_db_name": name, "reva_object_root": root})
    bootstrap(base)
    store = FilesystemArtifactStore(root, base.reva_s3_prefix)
    owner, worker = make_engine("migration", base), make_engine("worker", base)
    conn_ = builtin_connectors()["worldbank.wdi.indicators"]
    spec = conn_.describe()
    asyncio.run(run_connector(conn_, FixtureHttp(spec.name), store, worker, persist_cursor=False,
                              post_publish=post_publish_for(spec.template)))
    rel = publish_release(worker, store, cutoff=datetime.now(UTC), origin=Origin.test_fixture, environment="test")
    with owner.begin() as c:
        present = c.execute(text("SELECT to_regprocedure('ops.reserve_llm_budget(text,integer,bigint,integer,"
                                 "bigint)') IS NOT NULL")).scalar_one()
        if not present:
            c.execute(text(LEDGER_FUNCTIONS_SQL))
    app = create_app(_settings(base))
    drafter = RecordedDrafter(FIXTURES)
    app.state.nl_drafter_factory = lambda settings: [drafter]
    with TestClient(app) as client:
        r = client.post("/v1/session", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        client.headers["X-CSRF-Token"] = client.cookies.get("reva_csrf")
        yield Env(base, owner, client, app, str(rel.release_id), drafter, bool(present))
    app.state.engine.dispose()
    for e in (owner, worker):
        e.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def post(env: Env, text_: str, key: str | None = None, **kw: Any) -> Any:
    headers = {"Idempotency-Key": key or f"draft-{uuid.uuid4().hex}", **kw.pop("headers", {})}
    return env.client.post(URL, json={"text": text_, **kw}, headers=headers)


def counts(env: Env) -> dict[str, int]:
    with env.owner.connect() as c:
        return {t: int(c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())
                for t in ("sim.scenario", "ops.job", "sim.run")
                if c.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar_one() is not None}


def token(env: Env, scopes: list[str]) -> dict[str, str]:
    tok, sha = new_api_token()
    with env.owner.begin() as c:
        c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, :l)"),
                  {"h": sha, "s": scopes, "l": f"nl-{uuid.uuid4().hex[:6]}"})
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
def test_ledger_functions_come_from_the_migration_when_present(env: Env) -> None:
    with env.owner.connect() as c:
        fn = c.execute(text("SELECT p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                            "WHERE n.nspname = 'ops' AND p.proname = 'reserve_llm_budget'")).scalar_one()
        can = c.execute(text("SELECT has_function_privilege('reva_api', 'ops.reserve_llm_budget(text,integer,"
                             "bigint,integer,bigint)', 'EXECUTE')")).scalar_one()
        public = c.execute(text("SELECT has_function_privilege('public', 'ops.settle_llm_budget(text,integer,"
                                "bigint)', 'EXECUTE')")).scalar_one()
    assert fn is True and can is True and public is False
    print(f"ledger functions from migration: {env.functions_from_migration}")


def test_draft_returns_a_valid_spec_and_runs_nothing(env: Env) -> None:
    before = counts(env)
    r = post(env, TEXTS["openai_fr_sensitivity"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "draft_ready" and body["executed"] is False and body["confirmation_required"] is True
    assert body["draft_spec"]["baseline_release_id"] == env.release_id
    assert body["provider"] == "openai" and body["model"].startswith("gpt-4.1") and body["language"] == "fr"
    assert body["usage"]["total_tokens"] > 0 and body["prompt_version"].startswith("nl-draft-")
    assert counts(env) == before  # no scenario, no job, no run


def test_confirmed_draft_runs_through_the_normal_scenario_endpoint(env: Env) -> None:
    body = post(env, TEXTS["openai_en_basic"]).json()
    assert body["status"] == "draft_ready"
    before = counts(env)["sim.scenario"]
    created = env.client.post("/v1/scenarios", json=body["draft_spec"])  # the user's confirmation
    assert created.status_code in (200, 201), created.text
    assert created.json()["spec_sha256"] == body["draft_spec_sha256"]
    assert counts(env)["sim.scenario"] == before + 1


def test_audit_row_has_hash_and_length_but_never_the_text(env: Env) -> None:
    secret = "Jean Dupont 12 rue de la Paix: " + TEXTS["openai_en_basic"]
    # an unrecorded text: the replay has no fixture and refuses (typed), which is audited too
    r = post(env, secret)
    assert r.status_code == 502 and r.json()["code"] == "llm_provider_error"
    post(env, TEXTS["openai_en_retirement"])
    with env.owner.connect() as c:
        rows = c.execute(text("SELECT action, target, details FROM ops.audit WHERE action LIKE 'scenario_draft.%'"
                              )).mappings().all()
    dump = json.dumps([dict(x) for x in rows], default=str)
    assert "Dupont" not in dump and "retirement age" not in dump
    actions = {x["action"] for x in rows}
    assert {"scenario_draft.created", "scenario_draft.refused"} <= actions
    created = next(x for x in rows if x["action"] == "scenario_draft.created")
    assert len(created["target"]["text_sha256"]) == 64 and created["details"]["text_length"] > 0
    assert "restatement" not in dump


def test_scope_idempotency_and_size_limits(env: Env) -> None:
    r = env.client.post(URL, json={"text": "France +20% 2023"})
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"
    r = post(env, "France +20% 2023", headers=token(env, ["read"]))
    assert r.status_code == 403
    calls = len(env.drafter.calls)
    r = post(env, "é" * 2049)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
    r = env.client.post(URL, content=b'{"text": "' + b"a" * 9000 + b'"}',
                        headers={"Idempotency-Key": "big-body-1", "Content-Type": "application/json"})
    assert r.status_code == 413
    r = env.client.post(URL, json={"text": "PRIVATE-TEXT-123", "spec": {}},
                        headers={"Idempotency-Key": "extra-field-1"})
    assert r.status_code == 422 and "PRIVATE-TEXT-123" not in r.text
    assert len(env.drafter.calls) == calls  # none of these reached the provider

    key = f"same-key-{uuid.uuid4().hex}"
    a = post(env, TEXTS["openai_en_multiyear"], key=key)
    b = post(env, TEXTS["openai_en_multiyear"], key=key)
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert b.headers.get("Idempotent-Replayed") == "true"
    assert len(env.drafter.calls) == calls + 1  # replayed from the cache, not re-billed
    c = post(env, TEXTS["openai_en_basic"], key=key)
    assert c.status_code == 409 and c.json()["code"] == "idempotency_conflict"
    env.app.state.nl_draft_cache = None  # e.g. a restart: the ledger still knows the key was charged
    d = post(env, TEXTS["openai_en_multiyear"], key=key)
    assert d.status_code == 409 and d.json()["code"] == "idempotency_conflict"
    assert len(env.drafter.calls) == calls + 1


def test_budget_is_charged_and_settled_in_the_ledger(env: Env) -> None:
    r = post(env, TEXTS["openai_en_multiyear"])
    usage = r.json()["usage"]["total_tokens"]
    with env.owner.connect() as c:
        rows = c.execute(text("SELECT account_key, unit, reserved, consumed FROM ops.budget_ledger "
                              "WHERE account_key LIKE 'llm:%' ORDER BY recorded_at DESC LIMIT 2")).mappings().all()
    by_unit = {x["unit"]: x for x in rows}
    assert by_unit["requests"]["account_key"] == "llm:scenario_drafts:requests" and by_unit["requests"]["consumed"] == 1
    assert by_unit["tokens"]["consumed"] == usage and by_unit["tokens"]["reserved"] >= usage


def test_over_budget_is_429_and_nothing_is_sent(env: Env) -> None:
    env.app.state.nl_budget = LlmBudget("test", "", 1, 10_000_000, 1500)  # one request a day, already used
    try:
        calls = len(env.drafter.calls)
        r = post(env, TEXTS["openai_en_basic"])
        assert r.status_code == 429 and r.json()["code"] == "budget_exhausted", r.text
        assert len(env.drafter.calls) == calls
    finally:
        env.app.state.nl_budget = None


def test_disabled_llm_and_missing_ledger_functions_fail_closed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.api.routes import nl

    factory = env.app.state.nl_drafter_factory
    env.app.state.nl_drafter_factory = None
    monkeypatch.setattr(env.app.state, "settings", _settings(env.settings, reva_llm_enabled=False))
    r = post(env, TEXTS["openai_en_basic"])
    assert r.status_code == 503 and r.json()["code"] == "llm_unavailable"
    env.app.state.nl_drafter_factory = factory

    import reva.nl.budget as budget

    monkeypatch.setattr(budget.DbLlmLedger, "_functions", lambda self: False)  # as before migration 0008
    calls = len(env.drafter.calls)
    r = post(env, TEXTS["openai_en_basic"])
    assert r.status_code == 503 and r.json()["details"]["reason"] == "budget_ledger_unavailable", r.text
    assert len(env.drafter.calls) == calls
    assert nl.default_drafters is not None


def test_injection_text_is_drafted_as_data(env: Env) -> None:
    r = post(env, TEXTS["openai_hostile_multiplier"])
    body = r.json()
    assert r.status_code == 200 and body["draft_spec"] is None
    assert body["candidate_spec"]["intervention"]["import_multiplier"] == 1000
    assert "nl.instruction_in_text" in {u["capability_key"] for u in body["unsupported"]}
    r = post(env, TEXTS["openai_hostile_role"])
    assert r.json()["status"] == "nothing_draftable"
