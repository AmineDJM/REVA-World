"""POST /v1/ask on a throwaway database through the ``reva_api`` role (offline: replayed planner fixtures).

The module creates its own freshly bootstrapped database (never the dev database), loads the NLQA test
fixture graph (``tests/fixtures/nlqa/nlqa_fakes.py``, origin ``test_fixture``) through KGCORE's loader and
answers through the *real* KG adapter (``reva.kg.query``) and the real LINKS adapters (``reva.links``); the
atlas is the in-memory fake (no atlas snapshot exists in a fresh database — that path is tested too).
"""

from __future__ import annotations

import json
import sys
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

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.api.app import create_app
from reva.api.auth import hash_password, new_api_token
from reva.api.routes.ask import _RateLimiter
from reva.nl.budget import LlmBudget
from reva.settings import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "nlqa"))
import nlqa_fakes as F

from reva.nlqa.recorded import PlanReplay, load

pytestmark = pytest.mark.db

ADMIN_PASSWORD = "correct horse battery staple"
URL = "/v1/ask"
FIXTURES = {f["label"]: f for f in load(F.RECORDED_DIR)}


@dataclass
class Env:
    owner: Engine
    api: Engine
    client: TestClient
    app: Any


def _load_graph(worker: Engine, store: FilesystemArtifactStore) -> None:
    from reva.kg.model import KgEntity, KgProperty, KgStatement, KgTime, load_batch, register_properties

    register_properties(
        worker,
        [
            KgProperty(pid, "item", name, F.PROPS[pid]["en"], F.PROPS[pid]["fr"])
            for pid, name in (
                ("P37", "official_language"),
                ("P793", "significant_event"),
                ("P828", "has_cause"),
                ("P1891", "signatory"),
                ("P1365", "replaces"),
                ("P17", "country"),
                ("P463", "member_of"),
                ("P35", "head_of_state"),
            )
        ]
        + [
            KgProperty("P571", "time", "inception", "inception", "date de fondation ou de création"),
            KgProperty("P298", "external_id", "iso_3166_1_alpha3", "ISO 3166-1 alpha-3 code"),
        ],
    )
    raw = store.put_bytes(b"nlqa integration fixture graph v1", "raw", "application/json").sha256
    ents = [
        KgEntity(
            e.qid,
            e.kind or "other",
            dict(e.labels),
            descriptions=dict(e.descriptions),
            aliases={"fr": F.ALIASES.get(e.qid, [])} if e.qid in F.ALIASES else {},
            sitelinks=e.sitelinks,
        )
        for e in F.E.values()
    ]

    def t(v: Any) -> KgTime | None:
        return None if v is None else KgTime(v.iso, v.precision)

    sts = []
    for s in F.STATEMENTS:
        if s.object_qid:
            sts.append(
                KgStatement(
                    s.subject_qid,
                    s.property,
                    s.object_qid,
                    start=t(s.start),
                    end=t(s.end),
                    point_in_time=t(s.point_in_time),
                    statement_id=s.statement_id,
                )
            )
        else:
            sts.append(
                KgStatement(
                    s.subject_qid,
                    s.property,
                    value={"type": "time", "time": f"+{s.literal}T00:00:00Z", "precision": 11},
                    statement_id=s.statement_id,
                )
            )
    for e in F.E.values():
        if e.iso3:
            sts.append(
                KgStatement(
                    e.qid, "P298", value={"type": "external_id", "value": e.iso3}, statement_id=f"{e.qid}$FIXTURE-ISO3"
                )
            )
    rep = load_batch(
        worker,
        store,
        entities=ents,
        statements=sts,
        source_key="wikidata",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected


def _settings(base: Settings) -> Settings:
    data = base.model_dump()
    data.update(
        reva_admin_password_hash=SecretStr(hash_password(ADMIN_PASSWORD)),
        reva_session_secret=SecretStr("s" * 48),
        reva_allowed_hosts="testserver,localhost",
        reva_cors_origins="",
        reva_env="test",
        reva_llm_enabled=False,
    )
    return Settings.model_validate(data)


class MemLedger:
    def __init__(self) -> None:
        self.ops: list[tuple[str, str, int]] = []

    def reserve(self, operation_id: str, *, requests: int, tokens: int) -> None:
        self.ops.append(("reserve", operation_id, tokens))

    def settle(self, operation_id: str, *, requests: int, tokens: int) -> None:
        self.ops.append(("settle", operation_id, tokens))


@pytest.fixture(scope="module")
def env(db: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from reva.cli.bootstrap import bootstrap
    from reva.nlqa.adapters import build_backends

    name = f"reva_test_nlqa_{uuid.uuid4().hex[:8]}"
    assert name != "reva"
    admin = create_engine(db.settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp("nlqa_objects")
    url = db.settings.migration_url().set(database=name).render_as_string(hide_password=False)
    base = Settings.model_validate(
        {
            **db.settings.model_dump(),
            "reva_migration_database_url": SecretStr(url),
            "reva_db_name": name,
            "reva_object_root": root,
        }
    )
    bootstrap(base)
    store = FilesystemArtifactStore(root, base.reva_s3_prefix)
    owner, worker = make_engine("migration", base), make_engine("worker", base)
    _load_graph(worker, store)
    app = create_app(_settings(base))
    real = build_backends(app.state.engine(), app.state.settings, atlas_store=store)
    real.atlas = F.FakeAtlas()
    app.state.nlqa_backends = real
    app.state.nlqa_catalog = F.catalog()
    app.state.nlqa_real_notes = list(real.notes)
    with TestClient(app) as client:
        r = client.post("/v1/session", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        client.headers["X-CSRF-Token"] = client.cookies.get("reva_csrf")
        yield Env(owner, app.state.engine(), client, app)
    app.state.engine.dispose()
    for e in (owner, worker):
        e.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def ask(env: Env, question: str, **kw: Any) -> Any:
    return env.client.post(URL, json={"question": question, **kw.pop("body", {})}, **kw)


def token(env: Env, scopes: list[str]) -> dict[str, str]:
    tok, sha = new_api_token()
    with env.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, :l)"),
            {"h": sha, "s": scopes, "l": f"nlqa-{uuid.uuid4().hex[:6]}"},
        )
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture()
def llm(env: Env) -> Iterator[MemLedger]:
    led = MemLedger()
    env.app.state.nlqa_planner_factory = lambda settings: [PlanReplay(F.RECORDED_DIR)]
    env.app.state.nlqa_ledger = led
    env.app.state.nlqa_budget = LlmBudget("test", "", 50, 500_000, 1500)
    yield led
    for k in ("nlqa_planner_factory", "nlqa_ledger", "nlqa_budget"):
        delattr(env.app.state, k)


def cited(body: dict[str, Any]) -> None:
    ids = {e["id"] for e in body["evidence"]}
    for s in body["sentences"]:
        assert s["citations"] and set(s["citations"]) <= ids, s
        assert len(s["citation_refs"]) == len(s["citations"])


# ---------------------------------------------------------------------------
def test_columbus_path_through_the_real_kg_and_links_engines(env: Env, llm: MemLedger) -> None:
    r = ask(env, FIXTURES["fr_columbus_portuguese"]["text"])
    assert r.status_code == 200, r.text
    b = r.json()
    cited(b)
    assert b["planner"] == "llm" and b["model"] == "synthetic-fixture"
    assert "Christophe Colomb est relié(e) à portugais via" in b["answer_text"], b["answer_text"]
    assert "pas une cause" in b["answer_text"]
    assert "Brésil — « langue officielle » : portugais" in b["answer_text"]
    hop_ev = [e for e in b["evidence"] if e["kind"] == "kg_statement"]
    assert hop_ev and all(e["citation"]["origin"] == "test_fixture" for e in hop_ev)
    assert all(len(e["citation"]["raw_sha256"]) == 64 for e in hop_ev)
    assert any(e["citation"]["statement_id"] == "Q45$FIXTURE-0005" for e in hop_ev)
    # one reservation then a settlement, in the shared NL ledger's operation-id format
    assert [o[0] for o in llm.ops] == ["reserve", "settle"] and llm.ops[0][1].startswith("nl_draft:")


def test_entity_resolution_uses_kg_labels_and_aliases(env: Env, llm: MemLedger) -> None:
    b = ask(env, FIXTURES["fr_us_decolonisation"]["text"]).json()
    cited(b)
    ident = [e for e in b["evidence"] if e["kind"] == "entity_identification"]
    assert {e["qid"] for e in ident} == {"Q30", "Q90000002"}
    assert "1953-01-20 : États-Unis — « chef d'État » : Dwight D. Eisenhower" in b["answer_text"], b["answer_text"]
    assert "Non couvert par les données de REVA" in b["answer_text"]


def test_atlas_answers_and_missing_value(env: Env, llm: MemLedger) -> None:
    b = ask(env, FIXTURES["fr_minimum_wage_senegal"]["text"]).json()
    cited(b)
    assert "Sénégal, 2019 : 58 900 XOF per month" in b["answer_text"]  # country label from the KG (P298)
    ev = next(e for e in b["evidence"] if e["kind"] == "atlas_cell")
    assert ev["citation"]["raw_sha256"] == F.RAW_ILO and json.loads(ev["citation"]["selector"])["row"] == "SEN"


def test_keyword_planner_when_llm_is_not_enabled(env: Env) -> None:
    r = ask(env, "Quels pays ont la plus forte densité de population ?")
    assert r.status_code == 200, r.text
    b = r.json()
    cited(b)
    assert b["planner"] == "keyword_fallback" and b["model"] is None and b["usage"]["requests"] == 0
    assert b["warnings"][0].startswith("llm_not_enabled_for_questions")
    assert "1. Monaco" in b["answer_text"]


def test_real_wiring_reports_missing_atlas_snapshot(env: Env) -> None:
    notes = env.app.state.nlqa_real_notes
    assert any(n.startswith("capability_unavailable: atlas") for n in notes), notes
    assert env.app.state.nlqa_backends.paths.name == "reva.links.paths"


def test_injection_is_data(env: Env, llm: MemLedger) -> None:
    b = ask(env, FIXTURES["en_injection_concept_id"]["text"]).json()
    assert b["plan"]["operations"] == [] and "999" not in b["answer_text"]
    assert "treated as data and not followed" in b["answer_text"]


def test_scope_size_validation_and_rate_limit(env: Env) -> None:
    assert ask(env, "densité", headers=token(env, ["read"])).status_code == 200
    r = ask(env, "densité", headers=token(env, ["simulate"]))
    assert r.status_code == 403
    assert ask(env, "x", body={"extra": 1}).status_code == 422
    assert ask(env, "é" * 1100).status_code == 413
    big = env.client.post(
        URL, content=b'{"question": "' + b"a" * 9000 + b'"}', headers={"Content-Type": "application/json"}
    )
    assert big.status_code == 413
    env.app.state.nlqa_rate_limiter = _RateLimiter(2)
    try:
        codes = [ask(env, "densité").status_code for _ in range(3)]
        assert codes == [200, 200, 429]
    finally:
        del env.app.state.nlqa_rate_limiter


def test_audit_has_hash_and_length_never_the_question(env: Env) -> None:
    secret = "Jean Dupont 12 rue de la Paix demande : quel est le salaire minimum au Senegal ?"
    assert ask(env, secret).status_code == 200
    with env.owner.connect() as c:
        rows = (
            c.execute(text("SELECT action, target, details FROM ops.audit WHERE action LIKE 'ask.%'")).mappings().all()
        )
    dump = json.dumps([dict(x) for x in rows], default=str)
    assert "Dupont" not in dump and "salaire" not in dump
    answered = [x for x in rows if x["action"] == "ask.answered"]
    assert answered and len(answered[-1]["target"]["question_sha256"]) == 64
    assert answered[-1]["details"]["question_length"] > 0


def test_sample_answers_for_the_docs(env: Env, llm: MemLedger) -> None:
    """Every fixture question end to end (``-s`` prints the answers quoted in docs/dev/v3_nlqa.md)."""
    for label, fx in sorted(FIXTURES.items()):
        if label.startswith("rephrase_"):
            continue
        b = ask(env, fx["text"], body={"rephrase": label == "fr_minimum_wage_senegal"}).json()
        cited(b)
        print(
            f"\n### {label}\nQ: {fx['text']}\nRESTATEMENT: {b['restatement']}\nANSWER:\n{b['answer_text']}\n"
            f"REPHRASE: {b['rephrase'].get('status')}\nWARNINGS: {b['warnings']}\n"
            f"EVIDENCE: {len(b['evidence'])} items; e.g. {b['sentences'][0]['citation_refs'][:2]}"
        )
