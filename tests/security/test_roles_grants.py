"""Database principals, grants, row security and integrity triggers (§4, §8, §21, §24.1).

Every check runs with the real login roles (reva_api / reva_worker / reva_sim)
against a private database: a WHERE clause is not a security boundary, grants
and triggers are. Each negative check runs in its own transaction that is
always rolled back; positive controls show the negative checks fail for the
stated reason and not because the statement was malformed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres import repositories as repo
from reva.belief.publication import publish_release
from reva.cli.seed import entity_id
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin

pytestmark = pytest.mark.db

RUNTIME = ("api", "worker", "sim")
LOGIN = {"api": "reva_api", "worker": "reva_worker", "sim": "reva_sim"}


def _helper() -> Any:
    name = "reva_h2_isolated_db"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "fixtures" / "adversarial" / "isolated_db.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@contextmanager
def tx(engine: Engine) -> Iterator[Connection]:
    """A transaction that is always rolled back."""
    with engine.connect() as c:
        t = c.begin()
        try:
            yield c
        finally:
            if t.is_active:
                t.rollback()


def fails(engine: Engine, sql: str, params: dict[str, Any] | None = None, match: str = "permission denied") -> None:
    with tx(engine) as c, pytest.raises(DBAPIError, match=match):
        c.execute(text(sql), params or {})


def ok(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    with tx(engine) as c:
        r = c.execute(text(sql), params or {})
        return r.fetchall() if r.returns_rows else r.rowcount


@dataclass
class World:
    db: Any
    release_id: UUID
    unpublished_release_id: UUID
    manifest_sha: str
    onto_sha: str
    model_sha: str
    method_version_id: UUID
    obs: dict[str, Any]
    raw_sha: str
    wb_source: UUID
    wb_smv: UUID
    comtrade_cv: UUID
    trade_flow: UUID

    def engine(self, role: str) -> Engine:
        return getattr(self.db, role)


@pytest.fixture(scope="module")
def w(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    with _helper().isolated_db(tmp_path_factory) as d:
        wb = builtin_connectors()["worldbank.wdi.indicators"]
        rep = asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), d.store, d.worker, persist_cursor=False))
        assert rep.inserted == 200, rep.as_dict()
        rel = publish_release(
            d.worker, d.store, cutoff=datetime.now(UTC), origin=Origin.test_fixture, environment="test"
        )
        with d.owner.connect() as c:
            r = c.execute(text("SELECT * FROM belief.release WHERE id = :r"), {"r": rel.release_id}).mappings().one()
            obs = dict(
                c.execute(
                    text(
                        """SELECT o.*, c.concept_id FROM evidence.observation o
                   JOIN ontology.concept_version c ON c.id = o.concept_version_id
                   WHERE o.logical_key = 'wb:SP.POP.TOTL:FRA:2020'"""
                    )
                )
                .mappings()
                .one()
            )
            raw_sha = c.execute(
                text("SELECT raw_sha256 FROM evidence.acquisition WHERE id = :a"), {"a": obs["acquisition_id"]}
            ).scalar_one()
            wb_src = c.execute(text("SELECT id FROM evidence.source WHERE source_key='worldbank.wdi'")).scalar_one()
            smv = c.execute(
                text("SELECT id FROM evidence.source_metadata_version WHERE source_id=:s"), {"s": wb_src}
            ).scalar_one()
            ct_cv = c.execute(
                text("SELECT id FROM evidence.connector_version WHERE name='un.comtrade.public_exact'")
            ).scalar_one()
            mv = c.execute(text("SELECT id FROM model.version WHERE model_key='belief.source_select'")).scalar_one()
            tf = repo.concept_version_id(c, "predicate.trade_flow")
        with d.api.begin() as c:  # the API may append audit records (and nothing else)
            repo.audit(c, "api:test", "h2.probe", {"kind": "test"}, {})
        # An unpublished (committed) release: a valid belief context, no publication row.
        with d.worker.begin() as c:
            act = repo.simple_activity("belief.publication", "belief", "test:h2", {"draft": True})
            repo.record_activity(c, act)
            ctx = repo.create_context(
                c,
                mode="belief",
                origin="test_fixture",
                manifest_sha256=r["manifest_sha256"],
                knowledge_cutoff=datetime.now(UTC),
            )
            unpub = c.execute(
                text(
                    """INSERT INTO belief.release(context_id, origin, manifest_sha256, ontology_manifest_sha256,
                     model_manifest_sha256, knowledge_cutoff, method, provenance_activity_id)
                   SELECT :c, 'test_fixture', :m, :om, :mm, knowledge_cutoff, 'draft', :a
                   FROM meta.context WHERE id = :c RETURNING id"""
                ),
                {
                    "c": ctx,
                    "m": r["manifest_sha256"],
                    "om": r["ontology_manifest_sha256"],
                    "mm": r["model_manifest_sha256"],
                    "a": act.id,
                },
            ).scalar_one()
        yield World(
            d,
            rel.release_id,
            UUID(str(unpub)),
            r["manifest_sha256"],
            r["ontology_manifest_sha256"],
            r["model_manifest_sha256"],
            UUID(str(mv)),
            obs,
            raw_sha,
            UUID(str(wb_src)),
            UUID(str(smv)),
            UUID(str(ct_cv)),
            tf,
        )


def activity(c: Connection, origin: str = "belief") -> UUID:
    act = repo.simple_activity(f"test.{origin}", origin, "test:h2", {})
    repo.record_activity(c, act)
    return act.id


def draft_estimate(c: Connection, w: World, *, origin: str, cutoff: datetime, with_input: bool = False) -> UUID:
    act = activity(c)
    ctx = repo.create_context(c, mode="belief", origin=origin, manifest_sha256=w.manifest_sha, knowledge_cutoff=cutoff)
    rid = c.execute(
        text(
            """INSERT INTO belief.release(context_id, origin, manifest_sha256, ontology_manifest_sha256,
             model_manifest_sha256, knowledge_cutoff, method, provenance_activity_id)
           VALUES (:c, :o, :m, :om, :mm, :cut, 'test', :a) RETURNING id"""
        ),
        {"c": ctx, "o": origin, "m": w.manifest_sha, "om": w.onto_sha, "mm": w.model_sha, "cut": cutoff, "a": act},
    ).scalar_one()
    eid = c.execute(
        text(
            """INSERT INTO belief.estimate(release_id, subject_id, concept_version_id, support_id, method_version_id,
             modality, value, uncertainty, provenance_activity_id)
           VALUES (:r, :s, :c, :sp, :m, 'selected_report', CAST(:v AS jsonb), CAST(:u AS jsonb), :a)
           RETURNING id"""
        ),
        {
            "r": rid,
            "s": w.obs["subject_id"],
            "c": w.obs["concept_version_id"],
            "sp": w.obs["support_id"],
            "m": w.method_version_id,
            "v": json.dumps(w.obs["value"]),
            "u": json.dumps(w.obs["uncertainty"]),
            "a": act,
        },
    ).scalar_one()
    if with_input:
        add_input(c, eid, w.obs["id"])
    return UUID(str(eid))


def add_input(c: Connection, estimate_id: UUID, observation_id: Any) -> None:
    c.execute(
        text("INSERT INTO belief.input(estimate_id, observation_id) VALUES (:e, :o)"),
        {"e": estimate_id, "o": observation_id},
    )


def release_of(c: Connection, estimate_id: UUID) -> UUID:
    return UUID(
        str(c.execute(text("SELECT release_id FROM belief.estimate WHERE id=:e"), {"e": estimate_id}).scalar_one())
    )


# =========================================================================== role attributes
def test_runtime_roles_are_unprivileged_noinherit_logins(w: World) -> None:
    with w.db.owner.connect() as c:
        rows = (
            c.execute(
                text(
                    "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, "
                    "rolcanlogin FROM pg_roles WHERE rolname = ANY(:r)"
                ),
                {"r": list(LOGIN.values())},
            )
            .mappings()
            .all()
        )
    assert {r["rolname"] for r in rows} == set(LOGIN.values())
    for r in rows:
        assert r["rolcanlogin"], r
        assert not any(
            (
                r["rolsuper"],
                r["rolinherit"],
                r["rolcreaterole"],
                r["rolcreatedb"],
                r["rolreplication"],
                r["rolbypassrls"],
            )
        ), r


def test_runtime_roles_own_nothing_and_hold_no_powerful_memberships(w: World) -> None:
    with w.db.owner.connect() as c:
        owned = (
            c.execute(
                text(
                    """SELECT 'class:' || c.oid::regclass::text FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner
                 WHERE r.rolname = ANY(:r)
               UNION ALL SELECT 'proc:' || p.oid::regprocedure::text FROM pg_proc p
                 JOIN pg_roles r ON r.oid = p.proowner WHERE r.rolname = ANY(:r)
               UNION ALL SELECT 'schema:' || n.nspname FROM pg_namespace n
                 JOIN pg_roles r ON r.oid = n.nspowner WHERE r.rolname = ANY(:r)
               UNION ALL SELECT 'type:' || t.typname FROM pg_type t
                 JOIN pg_roles r ON r.oid = t.typowner WHERE r.rolname = ANY(:r)
               UNION ALL SELECT 'database:' || d.datname FROM pg_database d
                 JOIN pg_roles r ON r.oid = d.datdba WHERE r.rolname = ANY(:r)"""
                ),
                {"r": list(LOGIN.values())},
            )
            .scalars()
            .all()
        )
        member_of = (
            c.execute(
                text(
                    """SELECT m.rolname || ' in ' || g.rolname FROM pg_auth_members am
               JOIN pg_roles m ON m.oid = am.member JOIN pg_roles g ON g.oid = am.roleid
               WHERE m.rolname = ANY(:r)"""
                ),
                {"r": list(LOGIN.values())},
            )
            .scalars()
            .all()
        )
        owner = c.execute(text("SELECT current_user")).scalar_one()
    assert owned == []
    forbidden = {
        "reva_restricted_deletion",
        "pg_read_all_data",
        "pg_write_all_data",
        "pg_execute_server_program",
        "pg_read_server_files",
        "pg_write_server_files",
        owner,
        *LOGIN.values(),
    }
    assert [m for m in member_of if m.split(" in ")[1] in forbidden] == []


@pytest.mark.parametrize("role", RUNTIME)
@pytest.mark.parametrize(
    "sql,match",
    [
        ("CREATE TABLE public.h2_probe(x int)", "permission denied for schema public"),
        ("CREATE TABLE evidence.h2_probe(x int)", "permission denied for schema evidence"),
        ("CREATE TABLE ops.h2_probe(x int)", "permission denied for schema ops"),
        ("CREATE VIEW belief.h2_probe AS SELECT 1", "permission denied for schema belief"),
        ("CREATE FUNCTION public.h2_probe() RETURNS int LANGUAGE sql AS 'SELECT 1'", "permission denied"),
        ("CREATE SCHEMA h2_probe", "permission denied for database"),
        ("CREATE EXTENSION IF NOT EXISTS hstore", "permission denied"),
        ("CREATE ROLE h2_probe", "permission denied"),
        ("ALTER ROLE CURRENT_USER BYPASSRLS", "permission denied"),
        ("ALTER TABLE evidence.observation DISABLE TRIGGER immutable_record", "must be owner"),
        ("ALTER TABLE meta.context DISABLE ROW LEVEL SECURITY", "must be owner"),
        ("SET ROLE reva_restricted_deletion", "permission denied"),
    ],
)
def test_runtime_roles_cannot_change_schema_or_privileges(w: World, role: str, sql: str, match: str) -> None:
    fails(w.engine(role), sql, match=match)


# =========================================================================== append-only history
SCIENTIFIC_TABLES = (
    "evidence.observation",
    "evidence.acquisition",
    "evidence.source",
    "evidence.source_metadata_version",
    "evidence.eligibility",
    "belief.release",
    "belief.estimate",
    "belief.input",
    "ops.publication",
    "meta.artifact",
    "meta.artifact_location",
    "meta.activity",
    "meta.context",
    "world.entity",
    "world.alias",
    "world.support",
    "ontology.concept_version",
    "model.version",
    "ops.audit",
)
SET_FOR = {
    "evidence.observation": "parser_version = parser_version",
    "evidence.acquisition": "origin = 'live'",
    "evidence.source": "title = title",
    "evidence.source_metadata_version": "version = version",
    "evidence.eligibility": "decision = 'eligible'",
    "belief.release": "origin = 'live'",
    "belief.estimate": "value = value",
    "belief.input": "observation_id = observation_id",
    "ops.publication": "published_at = published_at",
    "meta.artifact": "privacy_class = privacy_class",
    "meta.artifact_location": "storage_uri = storage_uri",
    "meta.activity": "actor = actor",
    "meta.context": "origin = 'live'",
    "world.entity": "label = label",
    "world.alias": "confidence_kind = confidence_kind",
    "world.support": "aggregation = aggregation",
    "ontology.concept_version": "definition = definition",
    "model.version": "approval_status = 'approved'",
    "ops.audit": "actor = actor",
}


@pytest.mark.parametrize("role", RUNTIME)
@pytest.mark.parametrize("table", SCIENTIFIC_TABLES)
def test_runtime_roles_cannot_update_delete_truncate_history(w: World, role: str, table: str) -> None:
    e = w.engine(role)
    fails(e, f"UPDATE {table} SET {SET_FOR[table]}")
    fails(e, f"DELETE FROM {table}")
    fails(e, f"TRUNCATE {table}")


@pytest.mark.parametrize("table", SCIENTIFIC_TABLES)
def test_owner_cannot_mutate_immutable_records_either(w: World, table: str) -> None:
    with w.db.owner.connect() as c:
        assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() > 0, "need rows to prove triggers"
    fails(w.db.owner, f"UPDATE {table} SET {SET_FOR[table]}", match="Immutable scientific record")
    fails(w.db.owner, f"DELETE FROM {table}", match="Immutable scientific record")
    fails(w.db.owner, f"TRUNCATE {table} CASCADE", match="TRUNCATE forbidden")


def test_deletion_ticket_does_not_help_runtime_roles(w: World) -> None:
    for role in RUNTIME:
        with tx(w.engine(role)) as c, pytest.raises(DBAPIError, match="permission denied"):
            c.execute(text("SELECT set_config('reva.reviewed_deletion_ticket', 'TICKET-1', true)"))
            c.execute(text("DELETE FROM evidence.observation WHERE logical_key = 'wb:SP.POP.TOTL:FRA:2020'"))


def test_where_clause_is_not_the_boundary(w: World) -> None:
    # Even a statement matching zero rows is refused: the privilege is checked, not the rows.
    for role in RUNTIME:
        fails(w.engine(role), "UPDATE evidence.observation SET parser_version = 'x' WHERE false")
        fails(w.engine(role), "DELETE FROM belief.estimate WHERE false")


# =========================================================================== simulation isolation
@pytest.mark.parametrize(
    "table",
    [
        "evidence.observation",
        "evidence.source",
        "evidence.acquisition",
        "evidence.eligibility",
        "belief.estimate",
        "belief.release",
        "belief.input",
        "ops.publication",
        "world.relation",
        "world.entity",
        "world.alias",
        "ontology.concept_version",
        "evidence.claim",
    ],
)
def test_sim_cannot_insert_evidence_or_beliefs(w: World, table: str) -> None:
    fails(w.db.sim, f"INSERT INTO {table} DEFAULT VALUES")


def test_sim_cannot_create_belief_context_but_can_create_simulation_context(w: World) -> None:
    sql = (
        "INSERT INTO meta.context(mode, origin, manifest_sha256, knowledge_cutoff) "
        "VALUES (:m, 'test_fixture', :sha, clock_timestamp()) RETURNING id"
    )
    for mode in ("belief", "evidence", "population", "quarantine"):
        fails(w.db.sim, sql, {"m": mode, "sha": w.manifest_sha}, match="row-level security")
    assert ok(w.db.sim, sql, {"m": "simulation", "sha": w.manifest_sha})  # positive control
    assert ok(w.db.sim, sql, {"m": "evaluation", "sha": w.manifest_sha})


def test_sim_cannot_record_source_or_belief_activities(w: World) -> None:
    sql = (
        "INSERT INTO meta.activity(kind, origin, code_digest, parameters, environment_digest, actor, "
        "started_at, ended_at) VALUES ('x', :o, 'c', '{}', 'e', 'test', now(), now()) RETURNING id"
    )
    for origin in ("source", "normalisation", "belief", "review"):
        fails(w.db.sim, sql, {"o": origin}, match="row-level security")
    assert ok(w.db.sim, sql, {"o": "simulation"})
    fails(w.db.worker, sql, {"o": "simulation"}, match="row-level security")  # and the converse


def test_sim_object_locations_limited_to_scenario_namespaces(w: World) -> None:
    sql = (
        "INSERT INTO meta.artifact_location(artifact_sha256, storage_uri, namespace, verified_byte_size, "
        "checksum_verified_at) SELECT sha256, 'file:///x/' || :ns, :ns, byte_size, now() "
        "FROM meta.artifact WHERE sha256 = :s"
    )
    for ns in ("raw", "normalised", "belief", "quarantine"):
        fails(w.db.sim, sql, {"ns": ns, "s": w.manifest_sha}, match="row-level security")
    # Nor may it re-home an existing belief/raw artifact into its own namespace.
    fails(w.db.sim, sql, {"ns": "scenarios", "s": w.manifest_sha}, match="row-level security")
    fails(w.db.sim, sql, {"ns": "scenarios", "s": w.raw_sha}, match="row-level security")
    # Positive control: an artifact the simulation role creates itself, in this transaction
    # (R5-05: only through sim.register_artifact, which derives the metadata from the bytes).
    with tx(w.db.sim) as c:
        doc = json.dumps({"schema": "reva-simulation-result/1", "nonce": uuid.uuid4().hex}).encode()
        sha = c.execute(text("SELECT sim.register_artifact(:b)"), {"b": doc}).scalar_one()
        c.execute(text(sql), {"ns": "scenarios", "s": sha})
    with tx(w.db.worker) as c:
        sha = _artifact(c)
        with pytest.raises(DBAPIError, match="row-level security"):
            c.execute(text(sql), {"ns": "scenarios", "s": sha})


def test_worker_cannot_create_simulation_context(w: World) -> None:
    sql = (
        "INSERT INTO meta.context(mode, origin, manifest_sha256, knowledge_cutoff) "
        "VALUES (:m, 'test_fixture', :sha, clock_timestamp())"
    )
    fails(w.db.worker, sql, {"m": "simulation", "sha": w.manifest_sha}, match="row-level security")
    ok(w.db.worker, sql, {"m": "belief", "sha": w.manifest_sha})


def test_worker_cannot_write_simulation_outputs(w: World) -> None:
    for table in ("sim.scenario", "sim.run", "sim.result", "sim.run_outcome"):
        fails(w.db.worker, f"INSERT INTO {table} DEFAULT VALUES")


# =========================================================================== API narrow functions
@pytest.mark.parametrize(
    "table",
    [
        "evidence.observation",
        "evidence.acquisition",
        "belief.estimate",
        "belief.release",
        "belief.input",
        "sim.scenario",
        "sim.run",
        "sim.result",
        "meta.artifact",
        "meta.context",
        "meta.activity",
        "ops.job",
        "ops.publication",
        "model.version",
        "model.evaluation",
        "world.relation",
    ],
)
def test_api_cannot_insert_directly(w: World, table: str) -> None:
    fails(w.db.api, f"INSERT INTO {table} DEFAULT VALUES")


@pytest.mark.parametrize(
    "call",
    [
        "SELECT ops.complete_job(gen_random_uuid(), 'w', 1, 'succeeded', '{}'::jsonb, NULL)",
        "SELECT ops.assert_lease(gen_random_uuid(), 'w', 1)",
        "SELECT ops.heartbeat(gen_random_uuid(), 'w', 1, 60, '{}'::jsonb)",
    ],
)
def test_api_cannot_call_worker_queue_functions(w: World, call: str) -> None:
    fails(w.db.api, call, match="permission denied for function")


def test_sim_cannot_submit_or_review(w: World) -> None:
    fails(
        w.db.sim,
        "SELECT ops.submit_job('simulation', '{}'::jsonb, 'h2-sim-submit-1', 0, 'sim')",
        match="permission denied for function",
    )
    fails(
        w.db.sim,
        "SELECT ops.review_proposal(gen_random_uuid(), 'approve', 'human:x', '')",
        match="permission denied for function",
    )


@pytest.mark.parametrize("job_type", ["shell", "publish_beliefs", "migrate", "'; DROP TABLE ops.job; --", ""])
def test_submit_job_rejects_unknown_types(w: World, job_type: str) -> None:
    fails(
        w.db.api,
        "SELECT ops.submit_job(:t, '{}'::jsonb, :k, 0, 'api')",
        {"t": job_type, "k": f"h2-{uuid.uuid4().hex}"},
        match="cannot be submitted",
    )


def test_submit_job_idempotency_key_binds_the_request(w: World) -> None:
    key = f"h2-idem-{uuid.uuid4().hex}"
    sql = "SELECT ops.submit_job('replay', CAST(:p AS jsonb), :k, 0, 'api:test')"
    with w.db.api.begin() as c:
        first = c.execute(text(sql), {"p": '{"cutoff": "2024-01-01"}', "k": key}).scalar_one()
    with w.db.api.begin() as c:
        again = c.execute(text(sql), {"p": '{"cutoff": "2024-01-01"}', "k": key}).scalar_one()
    assert first == again  # safe retry
    fails(w.db.api, sql, {"p": '{"cutoff": "2025-01-01"}', "k": key}, match="idempotency key reused")
    # Keys are scoped by creator and job type: the same client key for another type is a new job,
    # never a silent alias of the replay job.
    with tx(w.db.api) as c:
        other = c.execute(
            text("SELECT ops.submit_job('simulation', CAST(:p AS jsonb), :k, 0, 'api:test')"),
            {"p": '{"cutoff": "2024-01-01"}', "k": key},
        ).scalar_one()
    assert other != first
    fails(w.db.api, sql, {"p": "{}", "k": "short"}, match="invalid idempotency key")
    fails(
        w.db.api,
        "SELECT ops.submit_job('replay', '{}'::jsonb, :k, 50, 'api')",
        {"k": f"h2-{uuid.uuid4().hex}"},
        match="priority",
    )


def test_security_definer_functions_pin_search_path(w: World) -> None:
    with w.db.owner.connect() as c:
        rows = (
            c.execute(
                text(
                    """SELECT p.oid::regprocedure::text AS f, p.proconfig FROM pg_proc p
               JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE p.prosecdef AND n.nspname IN ('meta','ontology','world','evidence','belief','model','sim',
                                                    'ops','public')"""
                )
            )
            .mappings()
            .all()
        )
    assert rows, "expected narrow SECURITY DEFINER functions"
    for r in rows:
        cfg = r["proconfig"] or []
        assert any(x.startswith("search_path=") and "public" not in x for x in cfg), r


# =========================================================================== belief inputs
def test_belief_input_positive_control(w: World) -> None:
    with tx(w.db.worker) as c:
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=datetime.now(UTC))
        add_input(c, eid, w.obs["id"])  # eligible, fixture into fixture release, before cutoff: accepted


def test_belief_input_rejects_fixture_observation_in_live_release(w: World) -> None:
    with tx(w.db.worker) as c:
        eid = draft_estimate(c, w, origin="live", cutoff=datetime.now(UTC))
        with pytest.raises(DBAPIError, match="fixture-origin observation cannot enter a live release"):
            add_input(c, eid, w.obs["id"])


def test_belief_input_rejects_observation_recorded_after_cutoff(w: World) -> None:
    with tx(w.db.worker) as c:
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=w.obs["recorded_at"] - timedelta(seconds=1))
        with pytest.raises(DBAPIError, match="recorded after release cutoff"):
            add_input(c, eid, w.obs["id"])


def test_belief_input_rejects_non_eligible_observation(w: World) -> None:
    with tx(w.db.worker) as c:
        act = activity(c, "review")
        c.execute(
            text(
                """INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                 policy_version, review_activity_id) VALUES (:cv, :c, '{}', 'retired', 'h2-test', :a)"""
            ),
            {"cv": w.obs["connector_version_id"], "c": w.obs["concept_version_id"], "a": act},
        )
        cutoff = c.execute(text("SELECT clock_timestamp()")).scalar_one()
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=cutoff)
        with pytest.raises(DBAPIError, match="is not eligible"):
            add_input(c, eid, w.obs["id"])


def test_belief_input_rejects_retracted_observation(w: World) -> None:
    with tx(w.db.worker) as c:
        act = activity(c, "review")
        c.execute(
            text(
                "INSERT INTO evidence.observation_retraction(observation_id, reason, activity_id) "
                "VALUES (:o, 'h2 test', :a)"
            ),
            {"o": w.obs["id"], "a": act},
        )
        cutoff = c.execute(text("SELECT clock_timestamp()")).scalar_one()
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=cutoff)
        with pytest.raises(DBAPIError, match="retracted"):
            add_input(c, eid, w.obs["id"])


def test_belief_input_cannot_reference_simulation_or_evaluation_outputs(w: World) -> None:
    with w.db.owner.connect() as c:
        targets = set(
            c.execute(
                text(
                    "SELECT confrelid::regclass::text FROM pg_constraint WHERE conrelid = 'belief.input'::regclass "
                    "AND contype = 'f'"
                )
            ).scalars()
        )
    assert targets == {"belief.estimate", "evidence.observation", "evidence.claim"}
    with tx(w.db.worker) as c:
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=datetime.now(UTC))
        # Rejected by the admissibility trigger (or the FK): a sim.result id is not an observation.
        with pytest.raises(DBAPIError, match=r"foreign key|is not eligible"):
            add_input(c, eid, uuid.uuid4())


# =========================================================================== publication
def test_publication_requires_advisory_lock(w: World) -> None:
    with tx(w.db.worker) as c:
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=datetime.now(UTC), with_input=True)
        with pytest.raises(DBAPIError, match="advisory lock"):
            c.execute(
                text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
                {"r": release_of(c, eid), "a": activity(c)},
            )
    with tx(w.db.worker) as c:  # positive control: same release, lock held -> published
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=datetime.now(UTC), with_input=True)
        repo.take_publication_lock(c)
        c.execute(
            text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
            {"r": release_of(c, eid), "a": activity(c)},
        )


def test_publication_rejects_estimates_without_provenance(w: World) -> None:
    with tx(w.db.worker) as c:
        repo.take_publication_lock(c)
        with pytest.raises(DBAPIError, match="has no estimates"):
            c.execute(
                text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
                {"r": w.unpublished_release_id, "a": activity(c)},
            )
    with tx(w.db.worker) as c:
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=datetime.now(UTC), with_input=False)
        repo.take_publication_lock(c)
        with pytest.raises(DBAPIError, match="without inputs"):
            c.execute(
                text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
                {"r": release_of(c, eid), "a": activity(c)},
            )


def test_publication_rejects_unverified_artifacts(w: World) -> None:
    with tx(w.db.worker) as c:
        eid = draft_estimate(c, w, origin="test_fixture", cutoff=datetime.now(UTC), with_input=True)
        fake = uuid.uuid4().hex * 2  # registered metadata, but never written/verified in a store
        c.execute(
            text(
                "INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, "
                "rights) VALUES (:s, 'application/x-parquet', 10, 't', 'public_aggregate', '{}')"
            ),
            {"s": fake},
        )
        rid = release_of(c, eid)
        c.execute(
            text("INSERT INTO belief.release_artifact(release_id, artifact_sha256, role) VALUES (:r, :s, 'estimates')"),
            {"r": rid, "s": fake},
        )
        repo.take_publication_lock(c)
        with pytest.raises(DBAPIError, match="unverified artifact"):
            c.execute(
                text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
                {"r": rid, "a": activity(c)},
            )


def test_release_requires_belief_mode_context(w: World) -> None:
    with tx(w.db.worker) as c:
        act = activity(c)
        ctx = repo.create_context(
            c,
            mode="evaluation",
            origin="test_fixture",
            manifest_sha256=w.manifest_sha,
            knowledge_cutoff=datetime.now(UTC),
        )
        with pytest.raises(DBAPIError, match="belief-mode context"):
            c.execute(
                text(
                    """INSERT INTO belief.release(context_id, origin, manifest_sha256, ontology_manifest_sha256,
                     model_manifest_sha256, knowledge_cutoff, method, provenance_activity_id)
                   SELECT id, origin, :m, :m, :m, knowledge_cutoff, 'x', :a FROM meta.context WHERE id = :c"""
                ),
                {"c": ctx, "m": w.manifest_sha, "a": act},
            )


# =========================================================================== scenarios
def _scenario_spec(baseline: UUID, title: str = "h2 roles test") -> Any:
    from reva.simulation.spec import validate_scenario_spec

    data = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "scenario.example.json").read_text())
    data["baseline_release_id"] = str(baseline)
    data["title"] = title
    return validate_scenario_spec(data)


def test_scenario_rejects_unpublished_baseline_direct_insert(w: World) -> None:
    sql_ctx = (
        "INSERT INTO meta.context(mode, origin, manifest_sha256, knowledge_cutoff) "
        "VALUES ('simulation', 'test_fixture', :sha, clock_timestamp()) RETURNING id"
    )
    sql_scn = (
        "INSERT INTO sim.scenario(context_id, baseline_release_id, spec, spec_digest, spec_sha256, "
        "created_by) VALUES (:c, :b, CAST(:spec AS jsonb), :d, :sha, 'sim')"
    )
    with tx(w.db.sim) as c:  # (R5-02) scenarios are created only through sim.create_scenario
        ctx = c.execute(text(sql_ctx), {"sha": w.manifest_sha}).scalar_one()
        with pytest.raises(DBAPIError, match="permission denied for table scenario"):
            c.execute(
                text(sql_scn),
                {
                    "c": ctx,
                    "b": w.release_id,
                    "d": uuid.uuid4().hex,
                    "sha": w.manifest_sha,
                    "spec": json.dumps({"baseline_release_id": str(w.release_id)}),
                },
            )
    # The trigger still guards every path (exercised as the owner): an unpublished baseline is
    # refused; a published one passes that check and then fails the spec-bytes binding, because
    # spec_sha256 here is not the sha of inline canonical bytes encoding the spec.
    for baseline, expect_ok in ((w.unpublished_release_id, False), (w.release_id, True)):
        with tx(w.db.owner) as c:
            ctx = c.execute(text(sql_ctx), {"sha": w.manifest_sha}).scalar_one()
            params = {
                "c": ctx,
                "b": baseline,
                "spec": json.dumps({"baseline_release_id": str(baseline)}),
                "d": uuid.uuid4().hex,
                "sha": w.manifest_sha,
            }
            with pytest.raises(DBAPIError, match="verified canonical bytes" if expect_ok else "is not published"):
                c.execute(text(sql_scn), params)


def test_scenario_spec_baseline_must_match_pinned_baseline(w: World) -> None:
    with tx(w.db.owner) as c:  # (R5-02) the sim role has no INSERT on sim.scenario; the trigger guards all paths
        ctx = c.execute(
            text(
                "INSERT INTO meta.context(mode, origin, manifest_sha256, knowledge_cutoff) "
                "VALUES ('simulation', 'test_fixture', :sha, now()) RETURNING id"
            ),
            {"sha": w.manifest_sha},
        ).scalar_one()
        with pytest.raises(DBAPIError, match="does not match pinned baseline"):
            c.execute(
                text(
                    "INSERT INTO sim.scenario(context_id, baseline_release_id, spec, spec_digest, "
                    "spec_sha256, created_by) VALUES (:c, :b, CAST(:s AS jsonb), :d, :sha, 'sim')"
                ),
                {
                    "c": ctx,
                    "b": w.release_id,
                    "s": json.dumps({"baseline_release_id": str(uuid.uuid4())}),
                    "d": uuid.uuid4().hex,
                    "sha": w.manifest_sha,
                },
            )


def test_create_scenario_function_checks_publication(w: World) -> None:
    sql = "SELECT sim.create_scenario(CAST(:spec AS jsonb), :bytes, :digest, 'api:test')"
    bad = _scenario_spec(w.unpublished_release_id)
    fails(
        w.db.api,
        sql,
        {"spec": json.dumps(bad.as_json()), "bytes": bad.canonical_bytes(), "digest": bad.digest()},
        match="baseline release is not published",
    )
    good = _scenario_spec(w.release_id)
    with tx(w.db.api) as c:
        sid = c.execute(
            text(sql), {"spec": json.dumps(good.as_json()), "bytes": good.canonical_bytes(), "digest": good.digest()}
        ).scalar_one()
        assert sid is not None
    # Canonical bytes must encode the spec (no smuggled payload).
    fails(
        w.db.api,
        sql,
        {"spec": json.dumps(good.as_json()), "bytes": b'{"x":1}', "digest": good.digest()},
        match="canonical bytes do not encode",
    )


def test_create_scenario_does_not_alias_different_specs(w: World) -> None:
    sql = "SELECT sim.create_scenario(CAST(:spec AS jsonb), :bytes, :digest, 'api:test')"
    a = _scenario_spec(w.release_id, "scenario A")
    b = _scenario_spec(w.release_id, "scenario B")
    with tx(w.db.api) as c:
        sid_a = c.execute(
            text(sql), {"spec": json.dumps(a.as_json()), "bytes": a.canonical_bytes(), "digest": a.digest()}
        ).scalar_one()
        # Spec B with A's digest must not resolve to scenario A.
        try:
            sid_b = c.execute(
                text(sql), {"spec": json.dumps(b.as_json()), "bytes": b.canonical_bytes(), "digest": a.digest()}
            ).scalar_one()
        except DBAPIError:
            return
        assert sid_b != sid_a


# =========================================================================== relations / graph
def _relation(c: Connection, w: World) -> UUID:
    return UUID(
        str(
            c.execute(
                text(
                    """INSERT INTO world.relation(predicate_id, layer, support_id, evidence_observation_id,
             existence_uncertainty, component, provenance_activity_id)
           VALUES (:p, 'trade.crude_petroleum', :s, :o, '{"status":"unquantified"}', '{}', :a) RETURNING id"""
                ),
                {"p": w.trade_flow, "s": w.obs["support_id"], "o": w.obs["id"], "a": activity(c, "normalisation")},
            ).scalar_one()
        )
    )


def _participant(c: Connection, rid: UUID, ordinal: int, role: str, entity: str) -> None:
    c.execute(
        text(
            "INSERT INTO world.relation_participant(relation_id, ordinal, role_concept_id, entity_id, "
            "direction) VALUES (:r, :o, :role, :e, 'undirected')"
        ),
        {"r": rid, "o": ordinal, "role": repo.concept_version_id(c, role), "e": entity_id(entity)},
    )


def test_relation_signature_positive_control(w: World) -> None:
    with tx(w.db.worker) as c:
        rid = _relation(c, w)
        _participant(c, rid, 0, "role.reporter", "FRA")
        _participant(c, rid, 1, "role.partner", "WORLD")
        _participant(c, rid, 2, "role.product", "HS2709")
        c.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # runs the deferred cardinality check now


def test_relation_rejects_role_outside_signature(w: World) -> None:
    with tx(w.db.worker) as c:
        rid = _relation(c, w)
        with pytest.raises(DBAPIError, match="not permitted by the predicate signature"):
            _participant(c, rid, 0, "entity.country", "FRA")  # a non-role concept used as a role


def test_relation_rejects_wrong_entity_kind(w: World) -> None:
    with tx(w.db.worker) as c:
        rid = _relation(c, w)
        with pytest.raises(DBAPIError, match="cannot fill role"):
            _participant(c, rid, 0, "role.product", "FRA")  # a country is not a product
    with tx(w.db.worker) as c:
        rid = _relation(c, w)
        with pytest.raises(DBAPIError, match="cannot fill role"):
            _participant(c, rid, 0, "role.reporter", "WORLD")  # the world aggregate cannot report


@pytest.mark.parametrize(
    "parts",
    [
        [("role.reporter", "FRA")],  # partner/product missing
        [("role.reporter", "FRA"), ("role.reporter", "USA"), ("role.partner", "WORLD"), ("role.product", "HS2709")],
    ],
)
def test_relation_cardinality_checked_at_commit(w: World, parts: list[tuple[str, str]]) -> None:
    with pytest.raises(DBAPIError, match="participants, outside"), w.db.worker.begin() as c:
        rid = _relation(c, w)
        for i, (role, ent) in enumerate(parts):
            _participant(c, rid, i, role, ent)
    with w.db.owner.connect() as c:  # nothing was committed
        assert (
            c.execute(
                text("SELECT count(*) FROM world.relation WHERE evidence_observation_id = :o"), {"o": w.obs["id"]}
            ).scalar_one()
            == 0
        )


# =========================================================================== provenance
def _artifact(c: Connection) -> str:
    sha = uuid.uuid4().hex * 2
    c.execute(
        text(
            "INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights) "
            "VALUES (:s, 'application/json', 1, 't', 'public_aggregate', '{}')"
        ),
        {"s": sha},
    )
    return sha


def _io(c: Connection, kind: str, act: UUID, sha: str) -> None:
    if kind == "in":
        n = c.execute(text("SELECT count(*) FROM meta.activity_input WHERE activity_id=:a"), {"a": act}).scalar_one()
        c.execute(
            text("INSERT INTO meta.activity_input(activity_id, ordinal, artifact_sha256) VALUES (:a, :o, :s)"),
            {"a": act, "o": n, "s": sha},
        )
    else:
        c.execute(
            text("INSERT INTO meta.activity_output(activity_id, artifact_sha256) VALUES (:a, :s)"), {"a": act, "s": sha}
        )


def test_provenance_cycles_rejected(w: World) -> None:
    with tx(w.db.worker) as c:
        a, b = _artifact(c), _artifact(c)
        x, y = activity(c, "model"), activity(c, "model")
        _io(c, "in", x, b)
        _io(c, "out", x, a)  # x: b -> a
        _io(c, "in", y, a)  # y consumes a
        with pytest.raises(DBAPIError, match="provenance cycle"):
            _io(c, "out", y, b)  # y: a -> b would close b -> a -> b
    with tx(w.db.worker) as c:
        a = _artifact(c)
        x = activity(c, "model")
        _io(c, "out", x, a)
        with pytest.raises(DBAPIError, match="provenance cycle"):
            _io(c, "in", x, a)  # self-consumption
    with tx(w.db.worker) as c:  # longer chain closed from the input side
        s = [_artifact(c) for _ in range(4)]
        acts = [activity(c, "model") for _ in range(3)]
        for i, act in enumerate(acts):
            _io(c, "in", act, s[i])
            _io(c, "out", act, s[i + 1])
        z = activity(c, "model")
        _io(c, "out", z, s[0])
        with pytest.raises(DBAPIError, match="provenance cycle"):
            _io(c, "in", z, s[3])


# =========================================================================== cross-table consistency
def test_acquisition_connector_must_belong_to_source(w: World) -> None:
    sql = (
        "INSERT INTO evidence.acquisition(source_id, connector_version_id, source_metadata_version_id, origin, "
        "request_fingerprint, raw_sha256, retrieved_at, response_metadata, provenance_activity_id, "
        "idempotency_key) VALUES (:s, :cv, :smv, 'test_fixture', 'fp', :sha, now(), '{}', :a, :k)"
    )
    with tx(w.db.worker) as c:
        with pytest.raises(DBAPIError, match="foreign key"):
            c.execute(
                text(sql),
                {
                    "s": w.wb_source,
                    "cv": w.comtrade_cv,
                    "smv": w.wb_smv,
                    "sha": w.raw_sha,
                    "a": activity(c, "source"),
                    "k": uuid.uuid4().hex,
                },
            )


def test_acquisition_requires_verified_raw_bytes(w: World) -> None:
    sql = (
        "INSERT INTO evidence.acquisition(source_id, connector_version_id, source_metadata_version_id, origin, "
        "request_fingerprint, raw_sha256, retrieved_at, response_metadata, provenance_activity_id, "
        "idempotency_key) VALUES (:s, :cv, :smv, 'test_fixture', 'fp', :sha, now(), '{}', :a, :k)"
    )
    with tx(w.db.worker) as c:
        sha = _artifact(c)  # metadata only, no verified location
        with pytest.raises(DBAPIError, match="no verified storage location"):
            c.execute(
                text(sql),
                {
                    "s": w.wb_source,
                    "cv": w.obs["connector_version_id"],
                    "smv": w.wb_smv,
                    "sha": sha,
                    "a": activity(c, "source"),
                    "k": uuid.uuid4().hex,
                },
            )


def test_observation_must_agree_with_its_acquisition(w: World) -> None:
    with tx(w.db.worker) as c:
        with pytest.raises(DBAPIError, match="foreign key"):
            c.execute(
                text(
                    """INSERT INTO evidence.observation(source_id, connector_version_id, acquisition_id, logical_key,
                     revision_no, subject_id, concept_version_id, support_id, value, uncertainty, source_flags,
                     raw_selector, parser_version, provenance_activity_id, semantic_hash, record_hash)
                   SELECT source_id, :cv, acquisition_id, 'h2:test', 1, subject_id, concept_version_id, support_id,
                     value, uncertainty, source_flags, raw_selector, parser_version, provenance_activity_id,
                     'x', 'y' FROM evidence.observation WHERE id = :o"""
                ),
                {"cv": w.comtrade_cv, "o": w.obs["id"]},
            )


def test_missing_value_cannot_carry_a_number(w: World) -> None:
    with tx(w.db.worker) as c:
        with pytest.raises(DBAPIError, match="check constraint"):
            c.execute(
                text(
                    """INSERT INTO evidence.observation(source_id, connector_version_id, acquisition_id, logical_key,
                     revision_no, subject_id, concept_version_id, support_id, value, uncertainty, source_flags,
                     raw_selector, parser_version, provenance_activity_id, semantic_hash, record_hash)
                   SELECT source_id, connector_version_id, acquisition_id, 'h2:missing', 1, subject_id,
                     concept_version_id, support_id, '{"kind":"missing","reason":"unavailable","value":"0"}',
                     uncertainty, source_flags, raw_selector, parser_version, provenance_activity_id, 'x', 'y'
                   FROM evidence.observation WHERE id = :o"""
                ),
                {"o": w.obs["id"]},
            )


@pytest.mark.parametrize("cls", ["restricted_microdata", "sensitive_restricted", "clinical", ""])
def test_restricted_privacy_classes_rejected(w: World, cls: str) -> None:
    fails(
        w.db.worker,
        "INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights) "
        "VALUES (:s, 'text/csv', 1, 't', :p, '{}')",
        {"s": uuid.uuid4().hex * 2, "p": cls},
        match="check constraint",
    )


# =========================================================================== readers see only published releases
def test_release_readers_ignore_unpublished_releases(w: World) -> None:
    from reva.belief.snapshots import release_as_of

    with w.db.api.connect() as c:
        rel = release_as_of(c, datetime.now(UTC))
        assert rel is not None and rel["id"] == w.release_id  # the newer unpublished draft is invisible
        assert release_as_of(c, datetime.now(UTC), "test_fixture")["id"] == w.release_id
        assert release_as_of(c, datetime.now(UTC), "live") is None
    queries = pytest.importorskip("reva.world.queries")
    with w.db.api.connect() as c:
        ctx = queries.release_context_as_of(c, datetime.now(UTC))
        assert ctx is not None and str(w.release_id) in json.dumps(ctx.as_dict(), default=str)
        assert str(w.unpublished_release_id) not in json.dumps(ctx.as_dict(), default=str)


def test_release_by_id_does_not_serve_unpublished_releases(w: World) -> None:
    from reva.belief.snapshots import release_by_id

    with w.db.api.connect() as c:
        assert release_by_id(c, w.release_id) is not None
        assert release_by_id(c, w.unpublished_release_id) is None
