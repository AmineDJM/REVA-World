"""Database hardening for the core review findings (docs/dev/review_core.md).

R-01 governance forgery, R-02 sealed releases, R-04 per-origin lineages, R-07
scoped eligibility, R-08 server-side recorded_at, R-09 sealed relations, R-17
bootstrap password exposure, R-18 simulation provenance isolation, R-19
transaction-scoped publication lock, R-26 bounded narrow-function arguments.

Every check uses the real login roles against the session test database. Each
negative check runs in a transaction that is rolled back; positive controls
show that the negative checks fail for the stated reason.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres import repositories as repo
from reva.cli.seed import entity_id
from reva.evidence.runner import FixtureHttp, run_connector

pytestmark = pytest.mark.db

FRA_POP = "wb:SP.POP.TOTL:FRA:2020"


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


def activity(c: Connection, origin: str = "belief", actor: str = "test:fx1") -> UUID:
    act = repo.simple_activity(f"test.{origin}", origin, actor, {})
    repo.record_activity(c, act)
    return act.id


def new_artifact(c: Connection) -> str:
    sha = uuid.uuid4().hex * 2
    c.execute(
        text(
            "INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights) "
            "VALUES (:s, 'application/json', 1, 't', 'public_aggregate', '{}')"
        ),
        {"s": sha},
    )
    return sha


@pytest.fixture(scope="module")
def w(db: Any) -> dict[str, Any]:
    wb = builtin_connectors()["worldbank.wdi.indicators"]
    asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), db.store, db.worker, persist_cursor=False))
    with db.owner.connect() as c:
        obs = dict(
            c.execute(
                text(
                    "SELECT * FROM evidence.observation WHERE logical_key = :k AND origin = 'test_fixture' "
                    "ORDER BY revision_no DESC LIMIT 1"
                ),
                {"k": FRA_POP},
            ).mappings().one()
        )
        acq = dict(c.execute(text("SELECT * FROM evidence.acquisition WHERE id = :a"),
                             {"a": obs["acquisition_id"]}).mappings().one())
        verified = c.execute(
            text("SELECT artifact_sha256 FROM meta.artifact_location WHERE namespace = 'raw' LIMIT 1")
        ).scalar_one()
        mv = c.execute(text("SELECT id FROM model.version WHERE model_key = 'belief.source_select'")).scalar_one()
        other_mv = c.execute(
            text("SELECT id FROM model.version WHERE model_key <> 'belief.source_select' LIMIT 1")
        ).scalar_one()
    return {"db": db, "obs": obs, "acq": acq, "verified": verified, "mv": mv, "other_mv": other_mv}


def obs_copy_sql(extra_cols: str = "", extra_vals: str = "") -> str:
    return f"""
        INSERT INTO evidence.observation(source_id, connector_version_id, acquisition_id, logical_key, revision_no,
          supersedes_id, subject_id, concept_version_id, support_id, value, uncertainty, source_flags, raw_selector,
          parser_version, provenance_activity_id, semantic_hash, record_hash{extra_cols})
        SELECT source_id, connector_version_id, COALESCE(CAST(:acq AS uuid), acquisition_id), :lk, :rev,
          CAST(:sup AS uuid), subject_id, concept_version_id, COALESCE(CAST(:support AS uuid), support_id), value,
          uncertainty, source_flags, raw_selector, parser_version, provenance_activity_id, 'x', 'y'{extra_vals}
        FROM evidence.observation WHERE id = :o RETURNING id"""


def copy_obs(c: Connection, w: dict[str, Any], lk: str, *, rev: int = 1, sup: Any = None, acq: Any = None,
             support: Any = None, extra_cols: str = "", extra_vals: str = "", **params: Any) -> UUID:
    return UUID(str(c.execute(
        text(obs_copy_sql(extra_cols, extra_vals)),
        {"o": w["obs"]["id"], "lk": lk, "rev": rev, "sup": sup, "acq": acq, "support": support, **params},
    ).scalar_one()))


def draft_release(c: Connection, w: dict[str, Any], *, origin: str = "test_fixture",
                  cutoff: datetime | None = None) -> UUID:
    act = activity(c)
    cut = cutoff or c.execute(text("SELECT clock_timestamp()")).scalar_one()
    sha = w["verified"]
    ctx = repo.create_context(c, mode="belief", origin=origin, manifest_sha256=sha, knowledge_cutoff=cut)
    return UUID(str(c.execute(
        text(
            """INSERT INTO belief.release(context_id, origin, manifest_sha256, ontology_manifest_sha256,
                 model_manifest_sha256, knowledge_cutoff, method, provenance_activity_id)
               VALUES (:c, :o, :m, :m, :m, :cut, 'test', :a) RETURNING id"""
        ),
        {"c": ctx, "o": origin, "m": sha, "cut": cut, "a": act},
    ).scalar_one()))


def add_estimate(c: Connection, w: dict[str, Any], rid: UUID, *, modality: str = "selected_report",
                 subject: Any = None, support: Any = None) -> UUID:
    o = w["obs"]
    return UUID(str(c.execute(
        text(
            """INSERT INTO belief.estimate(release_id, subject_id, concept_version_id, support_id, method_version_id,
                 modality, value, uncertainty, provenance_activity_id)
               VALUES (:r, :s, :c, :sp, :m, :mod, CAST(:v AS jsonb), CAST(:u AS jsonb), :a) RETURNING id"""
        ),
        {"r": rid, "s": subject or o["subject_id"], "c": o["concept_version_id"], "sp": support or o["support_id"],
         "m": w["mv"], "mod": modality, "v": json.dumps(o["value"]), "u": json.dumps(o["uncertainty"]),
         "a": activity(c)},
    ).scalar_one()))


def add_input(c: Connection, eid: UUID, oid: Any) -> None:
    c.execute(text("INSERT INTO belief.input(estimate_id, observation_id) VALUES (:e, :o)"), {"e": eid, "o": oid})


def publish(c: Connection, rid: UUID) -> None:
    repo.take_publication_lock(c)
    c.execute(text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
              {"r": rid, "a": activity(c)})


def proposal(c: Connection, w: dict[str, Any], author: str = "agent:discovery") -> UUID:
    return UUID(str(c.execute(
        text("INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, author) "
             "VALUES ('connector', :s, 1, :a) RETURNING id"),
        {"s": w["verified"], "a": author},
    ).scalar_one()))


def evaluation(c: Connection, w: dict[str, Any], mv: Any, replay_mode: str = "revised_data_retrospective") -> UUID:
    ctx = repo.create_context(c, mode="evaluation", origin="test_fixture", manifest_sha256=w["verified"],
                              knowledge_cutoff=datetime.now(UTC))
    return UUID(str(c.execute(
        text("""INSERT INTO model.evaluation(model_version_id, context_id, evaluation_manifest_sha256, replay_mode,
                  task_scope, metrics, provenance_activity_id)
                VALUES (:m, :c, :s, :rm, '{}', '{}', :a) RETURNING id"""),
        {"m": mv, "c": ctx, "s": w["verified"], "rm": replay_mode, "a": activity(c, "evaluation")},
    ).scalar_one()))


PROMOTION_SQL = """INSERT INTO model.promotion(model_version_id, task_scope, decision, evaluation_id, policy_version,
                     reviewer) VALUES (:m, '{}', :d, :e, :p, :r)"""


# =========================================================================== R-01 governance forgery
def test_worker_cannot_mint_api_tokens_or_record_human_reviews(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.worker) as c, pytest.raises(DBAPIError, match="permission denied"):
        c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label) "
                       "VALUES (repeat('a', 64), ARRAY['read','review'], 'minted-by-worker')"))
    with tx(db.worker) as c:
        pid = proposal(c, w)
        with pytest.raises(DBAPIError, match="permission denied"):
            c.execute(text("INSERT INTO ops.proposal_review(proposal_id, decision, reviewer) "
                           "VALUES (:p, 'approve', 'human:admin')"), {"p": pid})
    with tx(db.worker) as c:
        pid = proposal(c, w)
        with pytest.raises(DBAPIError, match="permission denied"):
            c.execute(text("SELECT ops.review_proposal(:p, 'approve', 'human:admin', '')"), {"p": pid})
    with tx(db.owner) as c:  # positive control: the migration owner (admin CLI) can mint tokens
        c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label) "
                       "VALUES (repeat('b', 64), ARRAY['read'], 'owner')"))


@pytest.mark.parametrize(
    ("evaluator", "policy", "match"),
    [
        ("gate-evaluator:", "p1", "gate-evaluator|check constraint"),
        ("agent:discovery", "p1", "cannot evaluate its own proposal"),
        ("gate-evaluator:agent:discovery", "p1", "cannot evaluate its own proposal"),
        ("gate-evaluator:promotion", "  ", "policy version"),
        ("anyone", "p1", "gate-evaluator|check constraint"),
    ],
)
def test_direct_gate_result_forgeries_rejected(w: dict[str, Any], evaluator: str, policy: str, match: str) -> None:
    with tx(w["db"].worker) as c:
        pid = proposal(c, w)
        with pytest.raises(DBAPIError, match=match):
            c.execute(text("""INSERT INTO ops.gate_result(proposal_id, gate_name, policy_version, decision,
                              evidence_sha256, evaluator) VALUES (:p, 'promotion', :v, 'pass', :s, :e)"""),
                      {"p": pid, "v": policy, "s": w["verified"], "e": evaluator})


def test_gate_result_function_validates_and_audits(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.worker) as c:
        pid = proposal(c, w)
        gid = c.execute(text("SELECT ops.record_gate_result(:p, 'promotion', 'promotion-1', 'pass', :s, "
                             "'gate-evaluator:promotion')"), {"p": pid, "s": w["verified"]}).scalar_one()
        assert c.execute(text("SELECT count(*) FROM ops.audit WHERE action = 'gate.result' "
                              "AND target->>'gate_result_id' = :g"), {"g": str(gid)}).scalar_one() == 1
    bad = [
        ("promotion", "gate-evaluator:other", w["verified"], "evaluator must be"),
        ("promotion", "gate-evaluator:promotion", "0" * 64, "not a verified artifact"),
        ("bad name!", "gate-evaluator:bad name!", w["verified"], "invalid gate name"),
    ]
    for gate, evaluator, sha, match in bad:
        with tx(db.worker) as c:
            pid = proposal(c, w)
            with pytest.raises(DBAPIError, match=match):
                c.execute(text("SELECT ops.record_gate_result(:p, :g, 'promotion-1', 'pass', :s, :e)"),
                          {"p": pid, "g": gate, "s": sha, "e": evaluator})
    with tx(db.worker) as c:  # the proposer cannot launder itself through the function either
        pid = proposal(c, w, author="promotion")
        with pytest.raises(DBAPIError, match="cannot evaluate its own proposal"):
            c.execute(text("SELECT ops.record_gate_result(:p, 'promotion', 'promotion-1', 'pass', :s, "
                           "'gate-evaluator:promotion')"), {"p": pid, "s": w["verified"]})
    with tx(db.sim) as c, pytest.raises(DBAPIError, match="permission denied"):
        c.execute(text("SELECT ops.record_gate_result(gen_random_uuid(), 'g', 'p', 'pass', :s, "
                       "'gate-evaluator:g')"), {"s": w["verified"]})


def test_model_promotion_requires_matching_replay_evaluation_and_named_reviewer(w: dict[str, Any]) -> None:
    db = w["db"]
    mv, other = w["mv"], w["other_mv"]
    cases = [
        ("promote", "replay", mv, "agent:discovery", "p", "reviewer must be"),
        ("promote", "replay", other, "gate-evaluator:tournament", "p", "must cite an evaluation of that model"),
        ("promote", "analytic", mv, "gate-evaluator:tournament", "p", "replay-based evaluation"),
        ("rollback", "analytic", mv, "human:alice", "p", "replay-based evaluation"),
        ("promote", "replay", mv, "human:alice", " ", "policy version"),
    ]
    for decision, kind, ev_model, reviewer, policy, match in cases:
        with tx(db.worker) as c:
            ev = evaluation(c, w, ev_model, "revised_data_retrospective" if kind == "replay"
                            else "analytic_verification")
            with pytest.raises(DBAPIError, match=match):
                c.execute(text(PROMOTION_SQL), {"m": mv, "d": decision, "e": ev, "p": policy, "r": reviewer})
    # positive controls: a replay-evaluated promotion by the gate evaluator; a reject backed only by analytic
    # verification; and the checked SECURITY DEFINER path (which applies the same trigger).
    with tx(db.worker) as c:
        ev = evaluation(c, w, mv)
        c.execute(text(PROMOTION_SQL), {"m": mv, "d": "promote", "e": ev, "p": "p", "r": "gate-evaluator:tournament"})
        ev2 = evaluation(c, w, mv, "analytic_verification")
        c.execute(text(PROMOTION_SQL), {"m": mv, "d": "reject", "e": ev2, "p": "p", "r": "gate-evaluator:tournament"})
        pid = c.execute(text("SELECT model.record_promotion(:m, '{}'::jsonb, 'promote', :e, 'p', "
                             "'gate-evaluator:tournament', '[]'::jsonb)"), {"m": mv, "e": ev}).scalar_one()
        assert pid is not None
        with pytest.raises(DBAPIError, match="replay-based evaluation"):
            with c.begin_nested():
                c.execute(text("SELECT model.record_promotion(:m, '{}'::jsonb, 'promote', :e, 'p', "
                               "'gate-evaluator:tournament', NULL)"), {"m": mv, "e": ev2})


def test_worker_insert_grants_are_the_reviewed_set(w: dict[str, Any]) -> None:
    """Every table the worker may INSERT into directly (reviewed for forgery risk in R-01)."""
    with w["db"].owner.connect() as c:
        rows = set(c.execute(text(
            """SELECT table_schema || '.' || table_name FROM information_schema.role_table_grants
               WHERE grantee = 'reva_worker' AND privilege_type = 'INSERT'""")).scalars())
    assert "ops.api_token" not in rows and "ops.proposal_review" not in rows
    assert {"ops.gate_result", "model.promotion", "ops.audit", "ops.worker_presence"} <= rows


# =========================================================================== R-02 sealed releases
def test_published_release_is_sealed(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.worker) as c:
        rid = draft_release(c, w)
        eid = add_estimate(c, w, rid)
        add_input(c, eid, w["obs"]["id"])
        # positive control: before publication the release accepts rows
        eid2 = add_estimate(c, w, rid, modality="estimated_state")
        add_input(c, eid2, w["obs"]["id"])
        publish(c, rid)
        for stmt in (
            lambda: add_estimate(c, w, rid, modality="forecast"),
            lambda: add_input(c, eid, w["obs"]["id"]),
            lambda: c.execute(text("INSERT INTO belief.release_artifact(release_id, artifact_sha256, role) "
                                   "VALUES (:r, :s, 'extra')"), {"r": rid, "s": new_artifact(c)}),
        ):
            with pytest.raises(DBAPIError, match="published and sealed"), c.begin_nested():
                stmt()


# =========================================================================== R-04 per-origin lineages
def test_observation_origin_follows_acquisition_and_lineages_are_per_origin(w: dict[str, Any]) -> None:
    db = w["db"]
    acq = w["acq"]
    with tx(db.worker) as c:
        # omitted origin is derived from the acquisition
        oid = copy_obs(c, w, "fx1:origin-derived")
        assert c.execute(text("SELECT origin FROM evidence.observation WHERE id = :o"),
                         {"o": oid}).scalar_one() == "test_fixture"
        with pytest.raises(DBAPIError, match="does not match its acquisition origin"), c.begin_nested():
            copy_obs(c, w, "fx1:origin-mismatch", extra_cols=", origin", extra_vals=", 'live'")
        with pytest.raises(DBAPIError, match="check constraint|does not match"), c.begin_nested():
            copy_obs(c, w, "fx1:origin-bogus", extra_cols=", origin", extra_vals=", 'staging'")
        # live acquisitions of the same source/connector and raw bytes
        def live_acquisition() -> Any:
            return c.execute(
                text("""INSERT INTO evidence.acquisition(source_id, connector_version_id, source_metadata_version_id,
                          origin, request_fingerprint, raw_sha256, retrieved_at, response_metadata,
                          provenance_activity_id, idempotency_key)
                        SELECT source_id, connector_version_id, source_metadata_version_id, 'live',
                          request_fingerprint, raw_sha256, retrieved_at, response_metadata, provenance_activity_id, :k
                        FROM evidence.acquisition WHERE id = :a RETURNING id"""),
                {"a": acq["id"], "k": f"fx1-live-{uuid.uuid4()}"},
            ).scalar_one()

        # the live lineage of the same logical key starts at revision 1 (no clash with the fixture lineage)
        live1 = copy_obs(c, w, FRA_POP, rev=1, acq=live_acquisition())
        assert c.execute(text("SELECT origin FROM evidence.observation WHERE id = :o"),
                         {"o": live1}).scalar_one() == "live"
        second = live_acquisition()
        # a live revision cannot supersede a fixture-origin record
        fixture_head = w["obs"]
        with pytest.raises(DBAPIError, match="foreign key"), c.begin_nested():
            copy_obs(c, w, FRA_POP, rev=fixture_head["revision_no"] + 1, sup=fixture_head["id"], acq=second)
        # but it can supersede its own lineage
        copy_obs(c, w, FRA_POP, rev=2, sup=live1, acq=second)
        # and the per-origin unique key still forbids duplicate revisions inside a lineage
        with pytest.raises(DBAPIError, match="duplicate key|unique"), c.begin_nested():
            copy_obs(c, w, FRA_POP, rev=1, acq=live_acquisition())


def test_live_release_rejects_fixture_observation(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        rid = draft_release(c, w, origin="live")
        eid = add_estimate(c, w, rid)
        with pytest.raises(DBAPIError, match="fixture-origin observation cannot enter a live release"):
            add_input(c, eid, w["obs"]["id"])


# =========================================================================== R-08 server-side recorded_at
def test_runtime_roles_cannot_backdate_recorded_at(w: dict[str, Any]) -> None:
    db = w["db"]
    old = datetime(2001, 1, 1, tzinfo=UTC)
    with tx(db.worker) as c:
        start = c.execute(text("SELECT clock_timestamp()")).scalar_one()
        oid = copy_obs(c, w, "fx1:backdated", extra_cols=", recorded_at", extra_vals=", CAST(:old AS timestamptz)",
                       old=old)
        rec = c.execute(text("SELECT recorded_at FROM evidence.observation WHERE id = :o"), {"o": oid}).scalar_one()
        assert rec >= start
        o = w["obs"]
        eid = c.execute(
            text("""INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability,
                      decision, policy_version, review_activity_id, recorded_at)
                    VALUES (:cv, :c, '{}', 'eligible', 'p', :a, :old) RETURNING recorded_at"""),
            {"cv": o["connector_version_id"], "c": o["concept_version_id"], "a": activity(c, "review"), "old": old},
        ).scalar_one()
        assert eid >= start
        c.execute(text("INSERT INTO ops.audit(actor, action, target, details, recorded_at) "
                       "VALUES ('worker:x', 'x', '{}', '{}', :old)"), {"old": old})
        assert c.execute(text("SELECT min(recorded_at) FROM ops.audit WHERE actor = 'worker:x'")).scalar_one() >= start
    with tx(db.sim) as c:
        start = c.execute(text("SELECT clock_timestamp()")).scalar_one()
        sha = uuid.uuid4().hex * 2
        rec = c.execute(
            text("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights, "
                 "recorded_at) VALUES (:s, 'application/json', 1, 't', 'public_aggregate', '{}', :old) "
                 "RETURNING recorded_at"), {"s": sha, "old": old}).scalar_one()
        assert rec >= start
    with tx(db.owner) as c:  # the owner (migrations, historical test fixtures) may supply the value
        oid = copy_obs(c, w, "fx1:owner-backdated", extra_cols=", recorded_at",
                       extra_vals=", CAST(:old AS timestamptz)", old=old)
        assert c.execute(text("SELECT recorded_at FROM evidence.observation WHERE id = :o"),
                         {"o": oid}).scalar_one() == old


def test_every_recorded_at_table_has_the_server_trigger(w: dict[str, Any]) -> None:
    with w["db"].owner.connect() as c:
        missing = c.execute(text(
            """SELECT n.nspname || '.' || c.relname FROM pg_attribute a
               JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE a.attname = 'recorded_at' AND NOT a.attisdropped AND c.relkind IN ('r','p')
                 AND n.nspname IN ('meta','ontology','world','evidence','belief','model','sim','ops')
                 AND NOT EXISTS (SELECT 1 FROM pg_trigger g WHERE g.tgrelid = c.oid
                                 AND g.tgname = 'a00_server_recorded_at')""")).scalars().all()
    assert missing == []


def test_publication_published_at_is_server_time(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        rid = draft_release(c, w)
        add_input(c, add_estimate(c, w, rid), w["obs"]["id"])
        repo.take_publication_lock(c)
        start = c.execute(text("SELECT clock_timestamp()")).scalar_one()
        pub = c.execute(text("INSERT INTO ops.publication(release_id, activity_id, published_at) "
                             "VALUES (:r, :a, '2001-01-01Z') RETURNING published_at"),
                        {"r": rid, "a": activity(c)}).scalar_one()
        assert pub >= start


# =========================================================================== R-07 scoped eligibility
def _decision(c: Connection, w: dict[str, Any], decision: str, applicability: dict[str, Any]) -> None:
    o = w["obs"]
    c.execute(
        text("""INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                  policy_version, review_activity_id) VALUES (:cv, :c, CAST(:ap AS jsonb), :d, 'fx1', :a)"""),
        {"cv": o["connector_version_id"], "c": o["concept_version_id"], "ap": json.dumps(applicability),
         "d": decision, "a": activity(c, "review")},
    )


def _eligible(c: Connection, w: dict[str, Any], subject: str, year: int | None = None) -> bool:
    o = w["obs"]
    if year is None:  # the 4-argument form keeps working (callers that do not know the year)
        sql = "SELECT evidence.is_eligible(:cv, :c, :s, clock_timestamp())"
    else:
        sql = "SELECT evidence.is_eligible(:cv, :c, :s, clock_timestamp(), :y)"
    return bool(c.execute(text(sql), {"cv": o["connector_version_id"], "c": o["concept_version_id"],
                                      "s": entity_id(subject), "y": year}).scalar_one())


def test_subject_scoped_quarantine_does_not_affect_other_subjects(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        assert _eligible(c, w, "FRA", 2020) and _eligible(c, w, "DZA", 2020)
        _decision(c, w, "quarantine", {"subject_ids": [str(entity_id("DZA"))]})
        assert _eligible(c, w, "FRA", 2020) and _eligible(c, w, "FRA")
        assert not _eligible(c, w, "DZA", 2020) and not _eligible(c, w, "DZA")
        # a global quarantine followed by an eligible decision for CHN only: only CHN is eligible
        _decision(c, w, "quarantine", {})
        _decision(c, w, "eligible", {"subject_ids": [str(entity_id("CHN"))]})
        assert _eligible(c, w, "CHN", 2020)
        assert not _eligible(c, w, "FRA", 2020)


def test_years_outside_the_decision_scope_are_ineligible(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        # the seeded WDI decision is scoped to years [2000, 2024]
        assert _eligible(c, w, "FRA", 2000) and _eligible(c, w, "FRA", 2024)
        assert not _eligible(c, w, "FRA", 1999) and not _eligible(c, w, "FRA", 2025)
        # a single-year scope covers exactly that year; a year-scoped quarantine leaves other years alone
        _decision(c, w, "quarantine", {"years": [2010]})
        assert not _eligible(c, w, "FRA", 2010)
        assert _eligible(c, w, "FRA", 2011)


def test_input_trigger_enforces_the_support_year_scope(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.worker) as c:
        sup = c.execute(text(
            """INSERT INTO world.support(temporal_extent, valid_range, spatial_spec, population_spec, aggregation,
                 resolution, provenance_activity_id, support_digest)
               SELECT temporal_extent, tstzrange('1990-01-01 00:00:00+00', '1991-01-01 00:00:00+00', '[)'),
                 spatial_spec, population_spec, aggregation, resolution, provenance_activity_id, :d
               FROM world.support WHERE id = :s RETURNING id"""),
            {"s": w["obs"]["support_id"], "d": f"fx1-{uuid.uuid4()}"},
        ).scalar_one()
        old = copy_obs(c, w, "fx1:year-1990", support=sup)
        rid = draft_release(c, w)
        with pytest.raises(DBAPIError, match="is not eligible"), c.begin_nested():
            add_input(c, add_estimate(c, w, rid, support=sup), old)
        add_input(c, add_estimate(c, w, rid), w["obs"]["id"])  # positive control: 2020 is in scope


# =========================================================================== R-09 sealed relations
def _relation(c: Connection, w: dict[str, Any]) -> UUID:
    return UUID(str(c.execute(
        text("""INSERT INTO world.relation(predicate_id, layer, support_id, evidence_observation_id,
                  existence_uncertainty, component, provenance_activity_id)
                VALUES (:p, 'test.fx1_sealed', :s, :o, '{"status":"unquantified"}', '{}', :a) RETURNING id"""),
        {"p": repo.concept_version_id(c, "predicate.trade_flow"), "s": w["obs"]["support_id"],
         "o": w["obs"]["id"], "a": activity(c, "normalisation")},
    ).scalar_one()))


def _participant(c: Connection, rid: UUID, ordinal: int, role: str, entity: str) -> None:
    c.execute(
        text("INSERT INTO world.relation_participant(relation_id, ordinal, role_concept_id, entity_id, direction) "
             "VALUES (:r, :o, :role, :e, 'undirected')"),
        {"r": rid, "o": ordinal, "role": repo.concept_version_id(c, role), "e": entity_id(entity)},
    )


def test_participants_cannot_be_added_after_the_relation_committed(w: dict[str, Any]) -> None:
    db = w["db"]
    with db.worker.begin() as c:  # a complete, valid relation (savepoints are part of the same transaction)
        rid = _relation(c, w)
        _participant(c, rid, 0, "role.reporter", "FRA")
        with c.begin_nested():
            _participant(c, rid, 1, "role.partner", "WORLD")
        _participant(c, rid, 2, "role.product", "HS2709")
    for role, ent in (("role.reporter", "DZA"), ("role.partner", "USA")):
        with tx(db.worker) as c, pytest.raises(DBAPIError, match="is sealed"):
            _participant(c, rid, 3, role, ent)
    with db.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM world.relation_participant WHERE relation_id = :r"),
                         {"r": rid}).scalar_one() == 3


def test_participant_insert_rechecks_cardinality(w: dict[str, Any]) -> None:
    with pytest.raises(DBAPIError, match="participants, outside"), w["db"].worker.connect() as c:
        with c.begin():
            rid = _relation(c, w)
            for i, (role, ent) in enumerate((("role.reporter", "FRA"), ("role.partner", "WORLD"),
                                             ("role.product", "HS2709"), ("role.reporter", "USA"))):
                _participant(c, rid, i, role, ent)
            c.execute(text("SET CONSTRAINTS world.participant_cardinality IMMEDIATE"))


# =========================================================================== R-18 simulation provenance
def test_sim_cannot_attach_provenance_to_source_activities(w: dict[str, Any]) -> None:
    db = w["db"]
    src_act = w["acq"]["provenance_activity_id"]
    for table, sql in (
        ("output", "INSERT INTO meta.activity_output(activity_id, artifact_sha256) VALUES (:a, :s)"),
        ("input", "INSERT INTO meta.activity_input(activity_id, ordinal, artifact_sha256) VALUES (:a, 99, :s)"),
    ):
        with tx(db.sim) as c:
            sha = new_artifact(c)
            with pytest.raises(DBAPIError, match="row-level security"):
                c.execute(text(sql), {"a": src_act, "s": sha})
        with tx(db.sim) as c:  # positive control: its own simulation activity
            sha = new_artifact(c)
            c.execute(text(sql), {"a": activity(c, "simulation", "sim:test"), "s": sha})
        with tx(db.worker) as c:  # the worker keeps writing non-simulation provenance
            sha = new_artifact(c)
            c.execute(text(sql), {"a": activity(c, "normalisation"), "s": sha})
    sim_act: UUID
    with db.sim.begin() as c:
        sim_act = activity(c, "simulation", "sim:test")
    with tx(db.worker) as c:  # ... but never onto simulation activities
        sha = new_artifact(c)
        with pytest.raises(DBAPIError, match="row-level security"):
            c.execute(text("INSERT INTO meta.activity_output(activity_id, artifact_sha256) VALUES (:a, :s)"),
                      {"a": sim_act, "s": sha})


LOCATION_SQL = """INSERT INTO meta.artifact_location(artifact_sha256, storage_uri, namespace, verified_byte_size,
                    checksum_verified_at) SELECT sha256, :uri, :ns, byte_size, now() FROM meta.artifact
                  WHERE sha256 = :s"""


def test_sim_cannot_register_locations_for_other_artifacts(w: dict[str, Any]) -> None:
    db = w["db"]
    raw = w["acq"]["raw_sha256"]
    with tx(db.sim) as c, pytest.raises(DBAPIError, match="row-level security"):
        c.execute(text(LOCATION_SQL), {"uri": "s3://attacker/raw", "ns": "scenarios", "s": raw})
    with db.sim.begin() as c:  # an artifact committed earlier without any location
        orphan = new_artifact(c)
    with tx(db.sim) as c, pytest.raises(DBAPIError, match="row-level security"):
        c.execute(text(LOCATION_SQL), {"uri": "s3://attacker/orphan", "ns": "scenarios", "s": orphan})
    with tx(db.sim) as c:  # positive: an artifact created in this transaction, in a sim namespace
        sha = new_artifact(c)
        c.execute(text(LOCATION_SQL), {"uri": f"file:///scenarios/{sha}", "ns": "scenarios", "s": sha})
        with pytest.raises(DBAPIError, match="row-level security"), c.begin_nested():
            c.execute(text(LOCATION_SQL), {"uri": f"file:///belief/{sha}", "ns": "belief", "s": sha})
    with db.sim.begin() as c:  # content-addressed reuse of an existing simulation artifact stays possible
        sha = new_artifact(c)
        c.execute(text(LOCATION_SQL), {"uri": f"file:///evaluation/{sha}", "ns": "evaluation", "s": sha})
    with tx(db.sim) as c:
        c.execute(text(LOCATION_SQL + " ON CONFLICT (artifact_sha256, storage_uri) DO NOTHING"),
                  {"uri": f"file:///evaluation/{sha}", "ns": "evaluation", "s": sha})


# =========================================================================== R-19 publication lock
def test_publication_requires_a_transaction_scoped_lock(w: dict[str, Any]) -> None:
    db = w["db"]
    with db.worker.connect() as c:
        try:
            t = c.begin()
            rid = draft_release(c, w)
            add_input(c, add_estimate(c, w, rid), w["obs"]["id"])
            c.execute(text("SELECT pg_advisory_lock(:k)"), {"k": repo.PUBLICATION_LOCK})  # session-level
            with pytest.raises(DBAPIError, match="transaction-scoped"):
                c.execute(text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
                          {"r": rid, "a": activity(c)})
            t.rollback()
        finally:
            c.execute(text("SELECT pg_advisory_unlock_all()"))
            c.commit()
    with tx(db.worker) as c:  # the two-int4 form (objsubid 2) does not count either
        rid = draft_release(c, w)
        add_input(c, add_estimate(c, w, rid), w["obs"]["id"])
        c.execute(text("SELECT pg_advisory_xact_lock(0, :k)"), {"k": repo.PUBLICATION_LOCK})
        with pytest.raises(DBAPIError, match="advisory lock"):
            c.execute(text("INSERT INTO ops.publication(release_id, activity_id) VALUES (:r, :a)"),
                      {"r": rid, "a": activity(c)})
    with tx(db.worker) as c:  # positive control
        rid = draft_release(c, w)
        add_input(c, add_estimate(c, w, rid), w["obs"]["id"])
        publish(c, rid)


# =========================================================================== R-26 bounded arguments
@pytest.mark.parametrize("seconds", [-5, 0, 9, 3601, 1_000_000_000])
def test_heartbeat_lease_is_bounded(w: dict[str, Any], seconds: int) -> None:
    with tx(w["db"].worker) as c, pytest.raises(DBAPIError, match=r"lease seconds must be within \[10, 3600\]"):
        c.execute(text("SELECT ops.heartbeat(gen_random_uuid(), 'w', 1, :s, '{}'::jsonb)"), {"s": seconds})


def test_heartbeat_in_range_reaches_the_lease_check(w: dict[str, Any]) -> None:
    for seconds in (10, 60, 3600):
        with tx(w["db"].worker) as c, pytest.raises(DBAPIError, match="stale lease"):
            c.execute(text("SELECT ops.heartbeat(gen_random_uuid(), 'w', 1, :s, '{}'::jsonb)"), {"s": seconds})


def test_submit_job_payload_is_bounded(w: dict[str, Any]) -> None:
    db = w["db"]
    big = json.dumps({"blob": "x" * 70_000})
    with tx(db.api) as c, pytest.raises(DBAPIError, match="64 KiB"):
        c.execute(text("SELECT ops.submit_job('health', CAST(:p AS jsonb), :k, 0, 'api:test')"),
                  {"p": big, "k": f"fx1-{uuid.uuid4()}"})
    with tx(db.api) as c:
        assert c.execute(text("SELECT ops.submit_job('health', '{}'::jsonb, :k, 0, 'api:test')"),
                         {"k": f"fx1-{uuid.uuid4()}"}).scalar_one()


# =========================================================================== R-17 bootstrap secrets
def test_scram_verifier_matches_rfc7677() -> None:
    import base64
    import hmac

    from reva.cli.bootstrap import scram_sha256_verifier

    # RFC 7677 section 3 example: password "pencil", salt W22ZaJ0SNY7soEsUEjb6gQ==, 4096 iterations.
    v = scram_sha256_verifier("pencil", salt=base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ=="))
    assert v == ("SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$WG5d8oPm3OtcPnkdi4Uo7BkeZkBFzpcXkuLmtbsT4qY=:"
                 "wfPLwcE6nTWhTAmQ7tl2KeoiWGPlZqQxSrmfPwDl2dU=")
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth = (f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r={nonce}").encode()
    server_key = base64.b64decode(v.rsplit(":", 1)[1])
    # the RFC's server signature v=6rriTRBi... proves the ServerKey part is right
    assert base64.b64encode(hmac.new(server_key, auth, "sha256").digest()).decode() == (
        "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")
    assert "pencil" not in v
    with pytest.raises(Exception, match="printable ASCII"):
        scram_sha256_verifier("pässwörd-with-umlauts")


def test_provision_roles_error_does_not_contain_password(w: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import reva.cli.bootstrap as b
    from reva.kernel.errors import ConfigurationError

    db = w["db"]
    monkeypatch.setattr(b, "ROLE_LOGIN", {"api": "reva_fx1_nonexistent_role"})
    pw = db.settings.reva_api_db_password.get_secret_value()
    with db.owner.connect() as c:
        with pytest.raises(ConfigurationError) as ei:
            b.provision_roles(c, db.settings)
        c.rollback()
    chain = []
    e: BaseException | None = ei.value
    while e is not None:
        chain.append(str(e))
        e = e.__cause__ or e.__context__
    text_ = "\n".join(chain)
    assert pw not in text_ and "SCRAM-SHA-256" not in text_ and "ALTER ROLE" not in text_
    assert "reva_fx1_nonexistent_role" in text_ and "42704" in text_  # undefined_object


def test_provisioned_verifier_lets_the_runtime_role_log_in(w: dict[str, Any]) -> None:
    import reva.cli.bootstrap as b
    from reva.adapters.postgres.engine import make_engine

    db = w["db"]
    with db.owner.connect() as c:
        assert b.provision_roles(c, db.settings) == ["reva_api", "reva_worker", "reva_sim"]
        c.commit()
        stored = c.execute(text("SELECT rolpassword FROM pg_authid WHERE rolname = 'reva_worker'")).scalar_one()
    assert stored.startswith("SCRAM-SHA-256$4096:")
    fresh = make_engine("worker", db.settings)
    try:
        with fresh.connect() as c:
            assert c.execute(text("SELECT current_user")).scalar_one() == "reva_worker"
    finally:
        fresh.dispose()


def test_role_connection_limit_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from reva.cli.bootstrap import role_connection_limit
    from reva.kernel.errors import ConfigurationError

    monkeypatch.delenv("REVA_ROLE_CONNECTION_LIMIT", raising=False)
    assert role_connection_limit(SimpleNamespace(reva_env="production")) == 20
    assert role_connection_limit(SimpleNamespace(reva_env="test")) == -1
    monkeypatch.setenv("REVA_ROLE_CONNECTION_LIMIT", "50")
    assert role_connection_limit(SimpleNamespace(reva_env="production")) == 50
    for bad_env, bad in (("production", "-1"), ("production", "0"), ("test", "0"), ("test", "-2"), ("test", "x")):
        monkeypatch.setenv("REVA_ROLE_CONNECTION_LIMIT", bad)
        with pytest.raises(ConfigurationError):
            role_connection_limit(SimpleNamespace(reva_env=bad_env))


# =========================================================================== lead follow-ups (R2-02, R2-09, G)
def test_gate_result_accepts_needs_review(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        pid = proposal(c, w)
        c.execute(text("SELECT ops.record_gate_result(:p, 'promotion', 'promotion-1', 'needs_review', :s, "
                       "'gate-evaluator:promotion')"), {"p": pid, "s": w["verified"]})
        with pytest.raises(DBAPIError, match="invalid gate decision"), c.begin_nested():
            c.execute(text("SELECT ops.record_gate_result(:p, 'promotion', 'promotion-1', 'approve', :s, "
                           "'gate-evaluator:promotion')"), {"p": pid, "s": w["verified"]})


def test_client_idempotency_keys_are_namespaced(w: dict[str, Any]) -> None:
    db = w["db"]
    key = f"fx1-ns-{uuid.uuid4().hex}"
    with tx(db.worker) as c:  # an internal job whose key a client might guess
        internal = c.execute(text("INSERT INTO ops.job(job_type, payload, idempotency_key, created_by) "
                                  "VALUES ('health', '{}', :k, 'scheduler') RETURNING id"), {"k": key}).scalar_one()
        c.commit()
    try:
        sql = "SELECT ops.submit_job('health', '{}'::jsonb, :k, 0, :a)"
        with db.api.begin() as c:
            mine = c.execute(text(sql), {"k": key, "a": "human:alice"}).scalar_one()
            stored = c.execute(text("SELECT idempotency_key FROM ops.job WHERE id = :j"), {"j": mine}).scalar_one()
        assert mine != internal and stored == f"api:human:alice:health:{key}"
        with db.api.begin() as c:  # safe retry; another principal gets its own job
            assert c.execute(text(sql), {"k": key, "a": "human:alice"}).scalar_one() == mine
            assert c.execute(text(sql), {"k": key, "a": "human:bob"}).scalar_one() not in (mine, internal)
        with tx(db.api) as c, pytest.raises(DBAPIError, match="idempotency key reused"):
            c.execute(text("SELECT ops.submit_job('health', '{\"x\": 1}'::jsonb, :k, 0, 'human:alice')"), {"k": key})
    finally:
        with db.owner.begin() as c:
            c.execute(text("DELETE FROM ops.job WHERE idempotency_key LIKE :k"), {"k": f"%{key}"})


def test_session_revocation_grants(w: dict[str, Any]) -> None:
    db = w["db"]
    sid = f"fx1-session-{uuid.uuid4().hex}"
    with tx(db.api) as c:
        c.execute(text("INSERT INTO ops.session_revocation(session_id, expires_at) "
                       "VALUES (:s, clock_timestamp() + interval '1 hour')"), {"s": sid})
        assert c.execute(text("SELECT count(*) FROM ops.session_revocation WHERE session_id = :s"),
                         {"s": sid}).scalar_one() == 1
    for engine in (db.worker, db.sim):
        with tx(engine) as c, pytest.raises(DBAPIError, match="permission denied"):
            c.execute(text("INSERT INTO ops.session_revocation(session_id, expires_at) "
                           "VALUES (:s, clock_timestamp())"), {"s": sid})
    with tx(db.worker) as c:  # the worker may prune expired rows
        c.execute(text("DELETE FROM ops.session_revocation WHERE expires_at < clock_timestamp()"))
