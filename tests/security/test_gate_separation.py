"""GATE: the promotion gate is a separate process with its own login (reva_gate).

* the worker (and the API and simulation logins) cannot record gate results;
* the gate login can, and its rows carry ``recorded_by = 'reva_gate'``;
* gate results are insertable only by reva_gate (even the owner is refused);
* the approval functions refuse gate results not recorded by reva_gate, and
  ``restore_eligibility`` refuses a version without an independent approval;
* the gate login reads what it needs and nothing else; it registers only its own
  decision evidence;
* the reva_gate role is a NOINHERIT login without memberships (bootstrap verifies it).

Negative checks run in rolled-back transactions; positive controls show what each login may still do.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from test_db_hardening import activity, build_world, tx

from reva.kernel.errors import ConfigurationError

pytestmark = pytest.mark.db

DENIED = "permission denied"
RECORD = "SELECT ops.record_gate_result(:p, 'promotion', :v, :d, :s, 'gate-evaluator:promotion')"
APPROVE_ELIG = "SELECT evidence.approve_eligibility(:p, :g, :cv, :c, CAST(:ap AS jsonb), :pv, :a)"


@pytest.fixture(scope="module")
def w(db: Any) -> dict[str, Any]:
    if db.gate is None:
        pytest.skip("REVA_GATE_DB_PASSWORD not configured")
    return build_world(db)


def refused(c: Connection, sql: str, params: dict[str, Any] | None = None, match: str = DENIED) -> None:
    with pytest.raises(DBAPIError, match=match), c.begin_nested():
        c.execute(text(sql), params or {})


def proposal(w: dict[str, Any], scope: dict[str, Any] | None = None, ptype: str = "connector_promotion") -> UUID:
    with w["db"].worker.begin() as c:
        return UUID(str(c.execute(
            text("INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, author, summary) "
                 "VALUES (:t, :s, 1, 'agent:discovery', CAST(:sum AS jsonb)) RETURNING id"),
            {"t": ptype, "s": w["verified"], "sum": json.dumps({"scope": scope or {}})}).scalar_one()))


def scope(c: Connection, w: dict[str, Any]) -> dict[str, Any]:
    ref = c.execute(text("SELECT concept_id || '@' || version FROM ontology.concept_version WHERE id = :c"),
                    {"c": w["obs"]["concept_version_id"]}).scalar_one()
    return {"concept": ref, "subject_ids": [str(w["obs"]["subject_id"])], "years": {"from": 2020, "to": 2023},
            "mapping_key": "gate.map"}


# --------------------------------------------------------------------------- who records gate results
def test_worker_api_and_sim_cannot_record_gate_results(w: dict[str, Any]) -> None:
    db = w["db"]
    pid = proposal(w)
    params = {"p": pid, "v": "promotion-1", "d": "pass", "s": w["verified"]}
    direct = ("INSERT INTO ops.gate_result(proposal_id, gate_name, policy_version, decision, evidence_sha256, "
              "evaluator) VALUES (:p, 'promotion', :v, :d, :s, 'gate-evaluator:promotion')")
    for engine in (db.worker, db.api, db.sim):
        with tx(engine) as c:
            refused(c, RECORD, params)
            refused(c, direct, params)
    with db.worker.connect() as c:  # the worker still sees decisions (it promotes on them)
        assert c.execute(text("SELECT has_function_privilege('ops.record_gate_result(uuid,text,text,text,text,"
                              "text)', 'EXECUTE')")).scalar_one() is False
        c.execute(text("SELECT count(*) FROM ops.gate_result")).scalar_one()


def test_gate_login_records_attributed_results(w: dict[str, Any]) -> None:
    db = w["db"]
    pid = proposal(w)
    with tx(db.gate) as c:
        gid = c.execute(text(RECORD), {"p": pid, "v": "promotion-1", "d": "pass", "s": w["verified"]}).scalar_one()
        row = c.execute(text("SELECT recorded_by, evaluator FROM ops.gate_result WHERE id = :g"),
                        {"g": gid}).one()
        assert tuple(row) == ("reva_gate", "gate-evaluator:promotion")
        # the gate's own checks still apply (evaluator name, verified evidence)
        refused(c, "SELECT ops.record_gate_result(:p, 'promotion', 'v', 'pass', :s, 'gate-evaluator:other')",
                {"p": pid, "s": w["verified"]}, match="evaluator must be")
        refused(c, RECORD, {"p": pid, "v": "promotion-1", "d": "pass", "s": "0" * 64}, match="not a verified")
        # a direct INSERT is still refused: only the checked function writes gate results
        refused(c, "INSERT INTO ops.gate_result(proposal_id, gate_name, policy_version, decision, evidence_sha256, "
                   "evaluator) VALUES (:p, 'promotion', 'v', 'pass', :s, 'gate-evaluator:promotion')",
                {"p": pid, "s": w["verified"]})


def test_owner_cannot_insert_gate_results(w: dict[str, Any]) -> None:
    pid = proposal(w)
    with tx(w["db"].owner) as c:
        refused(c, RECORD, {"p": pid, "v": "promotion-1", "d": "pass", "s": w["verified"]}, match="reva_gate")


# --------------------------------------------------------------------------- approvals need reva_gate
def _forged_gate_row(c: Connection, pid: UUID, sha: str, recorded_by: str) -> UUID:
    """A gate_result row as an attacker with trigger bypass (or a pre-GATE database) would leave it."""
    c.execute(text("SET LOCAL session_replication_role = replica"))
    gid = c.execute(text(
        "INSERT INTO ops.gate_result(proposal_id, gate_name, policy_version, decision, evidence_sha256, evaluator, "
        "recorded_by) VALUES (:p, 'promotion', 'promotion-1', 'pass', :s, 'gate-evaluator:promotion', :r) "
        "RETURNING id"), {"p": pid, "s": sha, "r": recorded_by}).scalar_one()
    c.execute(text("SET LOCAL session_replication_role = origin"))
    return UUID(str(gid))


def _superuser(db: Any) -> bool:
    with db.owner.connect() as c:
        return bool(c.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one())


def test_approval_functions_refuse_gate_results_not_recorded_by_the_gate(w: dict[str, Any]) -> None:
    db, o = w["db"], w["obs"]
    if not _superuser(db):
        pytest.skip("forging a gate row needs a superuser (trigger bypass)")
    subj = [str(o["subject_id"])]
    with tx(db.owner) as c:
        pid = proposal(w, scope(c, w))
    for forger in ("reva_worker", "reva_owner"):
        with tx(db.owner) as c:
            gid = _forged_gate_row(c, pid, w["verified"], forger)
            act = activity(c, "review", "gate-evaluator:promotion")
            app = {"subject_ids": subj, "years": {"from": 2020, "to": 2023}, "gate_result_id": str(gid)}
            params = {"p": pid, "g": gid, "cv": o["connector_version_id"], "c": o["concept_version_id"],
                      "ap": json.dumps(app), "pv": "promotion-1", "a": act}
            refused(c, APPROVE_ELIG, params, match="not recorded by the independent gate")
            refused(c, "SELECT ontology.approve_mapping(:p, :g, 'gate.map', '1', '{}'::jsonb, :c, 'exact', "
                       "'{}'::jsonb, '{}'::jsonb, 'promotion-1', :a)",
                    {"p": pid, "g": gid, "c": o["concept_version_id"], "a": act},
                    match="not recorded by the independent gate")
    # positive control: the same call with a reva_gate pass approves
    with db.gate.begin() as c:
        g = c.execute(text(RECORD), {"p": pid, "v": "promotion-1", "d": "pass", "s": w["verified"]}).scalar_one()
    with tx(db.worker) as c:
        act = activity(c, "review", "gate-evaluator:promotion")
        app = {"subject_ids": subj, "years": {"from": 2020, "to": 2023}, "gate_result_id": str(g)}
        eid = c.execute(text(APPROVE_ELIG), {"p": pid, "g": g, "cv": o["connector_version_id"],
                                             "c": o["concept_version_id"], "ap": json.dumps(app),
                                             "pv": "promotion-1", "a": act}).scalar_one()
        assert eid is not None


def test_model_approval_refuses_a_forged_gate_pass(w: dict[str, Any]) -> None:
    db = w["db"]
    if not _superuser(db):
        pytest.skip("forging a gate row needs a superuser (trigger bypass)")
    pid = proposal(w, {}, ptype="model_registration")
    with tx(db.owner) as c:
        gid = _forged_gate_row(c, pid, w["verified"], "reva_worker")
        act = activity(c, "review", "worker:model-registry")
        refused(c, "SELECT model.approve_version(:p, :g, 'gate.m', '1', 'forecast', :s, 'x', '[]'::jsonb, "
                   "'[]'::jsonb, '{}'::jsonb, '[]'::jsonb, :a)",
                {"p": pid, "g": gid, "s": w["verified"], "a": act}, match="not recorded by the independent gate")


def _restore_world(c: Connection, w: dict[str, Any], prev_recorded_by: str | None) -> tuple[UUID, UUID, UUID]:
    """Version X eligible (written by ``prev_recorded_by``), then retired as superseded by RB; RB retired."""
    o = w["obs"]
    cvs = c.execute(text("SELECT id FROM evidence.connector_version WHERE id <> :c ORDER BY name LIMIT 2"),
                    {"c": o["connector_version_id"]}).scalars().all()
    x, rb = cvs[0], cvs[1]
    concept = o["concept_version_id"]
    act = activity(c, "review", "test:gate")
    app = {"subject_ids": [str(o["subject_id"])], "years": {"from": 2020, "to": 2023}}
    ins = ("INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision, "
           "policy_version, review_activity_id{rb}) VALUES (:cv, :c, CAST(:ap AS jsonb), :d, 'p', :a{rbv})")
    if prev_recorded_by is not None:  # a non-owner 'eligible' row with no approval behind it
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text(ins.format(rb=", recorded_by", rbv=", :r")),
                  {"cv": x, "c": concept, "ap": json.dumps(app), "d": "eligible", "a": act, "r": prev_recorded_by})
        c.execute(text("SET LOCAL session_replication_role = origin"))
    else:
        c.execute(text(ins.format(rb="", rbv="")), {"cv": x, "c": concept, "ap": json.dumps(app), "d": "eligible",
                                                    "a": act})
    sup = {**app, "superseded_by": {"connector_version_id": str(rb), "version": "9"}}
    c.execute(text(ins.format(rb="", rbv="")), {"cv": x, "c": concept, "ap": json.dumps(sup), "d": "retired",
                                                "a": act})
    c.execute(text(ins.format(rb="", rbv="")), {"cv": rb, "c": concept, "ap": json.dumps(app), "d": "retired",
                                                "a": act})
    return x, rb, act


def test_restore_requires_an_independent_approval(w: dict[str, Any]) -> None:
    db = w["db"]
    if not _superuser(db):
        pytest.skip("simulating an unapproved eligible row needs a superuser (trigger bypass)")
    restore = "SELECT evidence.restore_eligibility(:cv, :c, :rb, :a)"
    with tx(db.owner) as c:  # an 'eligible' row the worker wrote without a reva_gate pass or review behind it
        x, rb, act = _restore_world(c, w, "reva_worker")
        refused(c, restore, {"cv": x, "c": w["obs"]["concept_version_id"], "rb": rb, "a": act},
                match="no independent approval")
    with tx(db.owner) as c:  # positive control: the reviewed (owner-written) approval may be restored
        x, rb, act = _restore_world(c, w, None)
        assert c.execute(text(restore), {"cv": x, "c": w["obs"]["concept_version_id"], "rb": rb,
                                         "a": act}).scalar_one() is not None


# --------------------------------------------------------------------------- least privilege of reva_gate
def test_gate_login_reads_only_what_it_needs(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.gate) as c:
        for t in ("ops.proposal", "ops.gate_result", "ops.gate_request", "evidence.connector_version",
                  "evidence.source", "evidence.eligibility", "meta.artifact", "meta.artifact_location"):
            c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
        for t in ("evidence.observation", "belief.estimate", "ops.job", "ops.api_token", "ops.proposal_review",
                  "ops.audit", "meta.activity", "model.version", "sim.scenario", "ontology.mapping"):
            refused(c, f"SELECT count(*) FROM {t}")
        for fn, args in (("evidence.approve_eligibility", "NULL, NULL, NULL, NULL, '{}', 'p', NULL"),
                         ("ops.submit_job", "'x', '{}'::jsonb, 'k', 0, 'x'"),
                         ("model.record_promotion", "NULL, '{}'::jsonb, 'promote', NULL, 'p', 'x', NULL")):
            refused(c, f"SELECT {fn}({args})")
        refused(c, "INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, author) "
                   "VALUES ('x', :s, 1, 'x')", {"s": w["verified"]})
        refused(c, "INSERT INTO ops.gate_request(proposal_id) SELECT id FROM ops.proposal LIMIT 1")


def test_gate_login_registers_only_its_own_evidence(w: dict[str, Any]) -> None:
    db = w["db"]
    sha = uuid.uuid4().hex * 2
    ins = ("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights) "
           "VALUES (:s, 'application/json', 3, :v, 'public_aggregate', '{}')")
    with tx(db.gate) as c:
        refused(c, ins, {"s": sha, "v": "connector-spec/1"}, match="gate-decision/1")
        c.execute(text(ins), {"s": sha, "v": "gate-decision/1"})
        refused(c, "INSERT INTO meta.artifact_location(artifact_sha256, storage_uri, namespace, verified_byte_size, "
                   "checksum_verified_at) VALUES (:s, 'file:///x', 'raw', 3, now())", {"s": sha},
                match="row-level security")
        c.execute(text("INSERT INTO meta.artifact_location(artifact_sha256, storage_uri, namespace, "
                       "verified_byte_size, checksum_verified_at) VALUES (:s, 'file:///x', 'evaluation', 3, now())"),
                  {"s": sha})


def test_gate_requests_are_worker_written_and_append_only(w: dict[str, Any]) -> None:
    db = w["db"]
    pid = proposal(w)
    with db.worker.begin() as c:
        rid = c.execute(text("INSERT INTO ops.gate_request(proposal_id, reports, recorded_by) "
                             "VALUES (:p, '[]', 'reva_gate') RETURNING id"), {"p": pid}).scalar_one()
        assert c.execute(text("SELECT recorded_by FROM ops.gate_request WHERE id = :i"),
                         {"i": rid}).scalar_one() == "reva_worker"
    with tx(db.worker) as c:
        refused(c, "UPDATE ops.gate_request SET reports = '[]' WHERE id = :i", {"i": rid},
                match="permission denied|Immutable")
        refused(c, "INSERT INTO ops.gate_request(proposal_id, reports) VALUES (:p, '{}')", {"p": pid},
                match="check constraint")
    with tx(db.owner) as c:
        refused(c, "DELETE FROM ops.gate_request WHERE id = :i", {"i": rid}, match="Immutable")
    with db.gate.connect() as c:
        assert c.execute(text("SELECT proposal_id FROM ops.gate_request WHERE id = :i"),
                         {"i": rid}).scalar_one() == pid


# --------------------------------------------------------------------------- role hygiene
def test_gate_role_is_a_noinherit_login_without_memberships(db: Any) -> None:
    from reva.cli.bootstrap import RUNTIME_ROLES, verify_runtime_memberships, verify_runtime_role_attributes

    if db.gate is None:
        pytest.skip("REVA_GATE_DB_PASSWORD not configured")
    assert "reva_gate" in RUNTIME_ROLES
    probe = f"gate_probe_{uuid.uuid4().hex[:8]}"
    admin = create_engine(db.settings.migration_url().set(database=db.name), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            verify_runtime_role_attributes(c, ["reva_gate"])
            verify_runtime_memberships(c)
            c.exec_driver_sql(f"CREATE ROLE {probe} NOLOGIN")
            c.exec_driver_sql(f"GRANT {probe} TO reva_gate WITH INHERIT TRUE, SET TRUE")
            with pytest.raises(ConfigurationError, match="reva_gate -> gate_probe"):
                verify_runtime_memberships(c)
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {probe}")
        admin.dispose()


def test_bootstrap_requires_the_gate_password_only_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from pydantic import SecretStr

    from reva.settings import Settings

    for k in list(os.environ):
        if k.startswith("REVA_"):
            monkeypatch.delenv(k)
    base = {"reva_api_db_password": "a", "reva_worker_db_password": "w", "reva_sim_db_password": "s",
            "reva_migration_database_url": "postgresql://o:p@h/d"}
    local = Settings(_env_file=None, reva_env="local", **base)  # type: ignore[call-arg]
    assert local.required_for("bootstrap") == []  # fail closed: no gate login, nothing auto-promoted
    assert local.required_for("gate") == ["REVA_GATE_DB_PASSWORD"]
    assert local.model_copy(update={"reva_gate_db_password": SecretStr("g")}).runtime_url("gate").username \
        == "reva_gate"
    prod = Settings(_env_file=None, reva_env="production", reva_object_backend="s3",  # type: ignore[call-arg]
                    reva_s3_bucket="b", reva_s3_access_key_id="AKIAEXAMPLE", reva_s3_secret_access_key="x" * 20,
                    reva_session_secret="s" * 48, reva_public_base_url="https://reva.example.org",
                    reva_db_sslmode="verify-full", **base)
    assert "REVA_GATE_DB_PASSWORD" in prod.required_for("bootstrap")
    assert prod.required_for("gate") == ["REVA_GATE_DB_PASSWORD", "REVA_MIGRATION_DATABASE_URL must not be present "
                                         "in a runtime process"]


# --------------------------------------------------------------------------- gate entrypoint (start_gate.py)
def _start_gate() -> Any:
    import importlib
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module("start_gate")


GATE_PROD = {
    "PATH": "/usr/bin:/bin", "HOME": "/home/reva", "REVA_ENV": "production", "REVA_PROCESS_ROLE": "gate",
    "REVA_DB_HOST": "db.internal", "REVA_DB_PORT": "5432", "REVA_DB_SSLMODE": "require", "REVA_DB_NAME": "reva",
    "REVA_OBJECT_BACKEND": "s3", "REVA_S3_BUCKET": "b", "REVA_S3_REGION": "eu-central-1", "REVA_S3_PREFIX": "reva/",
    "REVA_S3_ACCESS_KEY_ID": "AKIA-GATE", "REVA_S3_SECRET_ACCESS_KEY": "gate-s3-secret-value",
    "REVA_GATE_DB_PASSWORD": "gate-pw-@:/?#-value", "REVA_CPU_BUDGET_THREADS": "1",
    "HTTPS_PROXY": "http://proxy.internal:3128",
}


def test_start_gate_child_env_holds_only_gate_secrets() -> None:
    sg = _start_gate()
    env = {**GATE_PROD, "REVA_WORKER_DB_PASSWORD": "worker-pw-value", "REVA_SIM_DB_PASSWORD": "sim-pw-value",
           "REVA_API_DB_PASSWORD": "api-pw-value", "REVA_SESSION_SECRET": "s" * 48,
           "REVA_COMTRADE_API_KEY": "comtrade-key-value"}
    command, child, summary, settings = sg.prepare(env)
    assert command[-2:] == ["-m", "reva.gate.service"] and summary["process_role"] == "gate"
    assert child["REVA_PROCESS_ROLE"] == "gate" and child["REVA_GATE_DB_PASSWORD"] == GATE_PROD["REVA_GATE_DB_PASSWORD"]
    for k in ("REVA_WORKER_DB_PASSWORD", "REVA_SIM_DB_PASSWORD", "REVA_API_DB_PASSWORD", "REVA_SESSION_SECRET",
              "REVA_COMTRADE_API_KEY", "HTTPS_PROXY"):
        assert k not in child, k
    assert settings.runtime_url("gate").username == "reva_gate"


def test_start_gate_fails_closed() -> None:
    sg = _start_gate()
    with pytest.raises(sg.ConfigError, match="migration-owner"):
        sg.prepare({**GATE_PROD, "REVA_MIGRATION_DATABASE_URL": "postgresql://o:p@h/d"})
    with pytest.raises(sg.ConfigError, match="REVA_GATE_DB_PASSWORD"):
        sg.prepare({k: v for k, v in GATE_PROD.items() if k != "REVA_GATE_DB_PASSWORD"})
    with pytest.raises(sg.ConfigError, match="REVA_PROCESS_ROLE=worker"):
        sg.prepare({**GATE_PROD, "REVA_PROCESS_ROLE": "worker"})
    # the worker entrypoint never forwards the gate's password
    import importlib

    re_ = importlib.import_module("_runtime_env")
    child, _ = re_.build_child_env({**GATE_PROD, "REVA_PROCESS_ROLE": "worker", "REVA_WORKER_DB_PASSWORD": "w",
                                    "REVA_SIM_DB_PASSWORD": "s"}, "worker")
    assert "REVA_GATE_DB_PASSWORD" not in child


def test_blueprint_has_an_independent_gate_service() -> None:
    import importlib
    import sys
    from pathlib import Path

    import yaml

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    vb = importlib.import_module("validate_blueprint")
    doc = yaml.safe_load((scripts.parent / "render.yaml").read_text())
    assert vb.reva_checks(doc) == []
    gate = next(s for s in doc["services"] if s["name"] == "reva-gate")
    gate["envVars"].append({"key": "REVA_WORKER_DB_PASSWORD", "fromService": {
        "type": "web", "name": "reva-api", "envVarKey": "REVA_WORKER_DB_PASSWORD"}})
    worker = next(s for s in doc["services"] if s["name"] == "reva-worker")
    worker["envVars"].append({"key": "REVA_GATE_DB_PASSWORD", "fromService": {
        "type": "web", "name": "reva-api", "envVarKey": "REVA_GATE_DB_PASSWORD"}})
    problems = " | ".join(vb.reva_checks(doc))
    assert "reva-gate must not receive" in problems and "reva-worker must not receive REVA_GATE_DB_PASSWORD" in problems
