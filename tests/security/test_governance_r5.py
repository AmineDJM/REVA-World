"""R5 governance regressions (docs/dev/review_db_fixes.md): R5-01, R5-06, R5-09, R5-10, R5-12, R5-16.

Every check uses the real login roles against the session test database; negative checks run in
rolled-back transactions and are paired with positive controls showing what the role may still do.
The rules key on the *login* (``session_user``), not on the format of identity strings.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from test_db_hardening import PROMOTION_SQL, activity, build_world, evaluation, new_artifact, tx

from reva.kernel.errors import ConfigurationError

pytestmark = pytest.mark.db

DENIED = "governance|permission denied"


GATE_DATASET = "gate.ds"
GATE_MANIFEST = "sha256:" + "6" * 64


def gate_connector_versions(db: Any, n: int = 2) -> list[UUID]:
    """Connector versions that declare the manifest/dataset the test scopes name (R6-02 binding)."""
    caps = json.dumps({"template": "gate.test", "datasets": [GATE_DATASET], "manifest_sha256": GATE_MANIFEST})
    with db.worker.begin() as c:
        src = c.execute(text("SELECT source_id FROM evidence.connector_version LIMIT 1")).scalar_one()
        sha = c.execute(
            text("SELECT artifact_sha256 FROM meta.artifact_location WHERE namespace = 'raw' LIMIT 1")
        ).scalar_one()
        return [
            UUID(
                str(
                    c.execute(
                        text(
                            "INSERT INTO evidence.connector_version(source_id, name, version, spec_sha256, code_digest, "
                            "declared_capabilities) VALUES (:s, :n, '1.0.0', :sha, 'sha256:x', CAST(:caps AS jsonb)) RETURNING id"
                        ),
                        {"s": src, "n": f"gate.test.{uuid.uuid4().hex[:10]}", "sha": sha, "caps": caps},
                    ).scalar_one()
                )
            )
            for _ in range(n)
        ]


@pytest.fixture(scope="module")
def w(db: Any) -> dict[str, Any]:
    world = build_world(db)
    world["gate_cvs"] = gate_connector_versions(db)
    return world


def refused(c: Connection, sql: str, params: dict[str, Any] | None = None, match: str = DENIED) -> None:
    with pytest.raises(DBAPIError, match=match), c.begin_nested():
        c.execute(text(sql), params or {})


def concept_ref(c: Connection, concept: Any) -> str:
    return c.execute(
        text("SELECT concept_id || '@' || version FROM ontology.concept_version WHERE id = :c"), {"c": concept}
    ).scalar_one()


def other_connector_version(c: Connection, w: dict[str, Any]) -> UUID:
    return c.execute(
        text("SELECT id FROM evidence.connector_version WHERE id <> :c ORDER BY name LIMIT 1"),
        {"c": w["obs"]["connector_version_id"]},
    ).scalar_one()


def promotion_proposal(
    c: Connection, w: dict[str, Any], scope: dict[str, Any], ptype: str = "connector_promotion"
) -> UUID:
    """A committed worker proposal (GATE: the independent gate login must be able to see it)."""
    _ = c
    with w["db"].worker.begin() as wc:
        return UUID(
            str(
                wc.execute(
                    text(
                        "INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, author, summary) "
                        "VALUES (:t, :s, 1, 'agent:discovery', CAST(:sum AS jsonb)) RETURNING id"
                    ),
                    {"t": ptype, "s": w["verified"], "sum": json.dumps({"scope": scope})},
                ).scalar_one()
            )
        )


def gate(
    c: Connection,
    w: dict[str, Any],
    pid: UUID,
    decision: str = "pass",
    policy: str = "promotion-1",
    scope: dict[str, Any] | None = None,
) -> UUID:
    """A gate result recorded (committed) by the independent gate login reva_gate (GATE). Its derived
    scope (R6-02) defaults to the proposal's recorded scope, as the gate derives it for a sound proposal."""
    _ = c
    with w["db"].gate.begin() as gc:
        if scope is None:
            scope = gc.execute(text("SELECT summary->'scope' FROM ops.proposal WHERE id = :p"), {"p": pid}).scalar_one()
        return UUID(
            str(
                gc.execute(
                    text(
                        "SELECT ops.record_gate_result(:p, 'promotion', :v, :d, :s, 'gate-evaluator:promotion', "
                        "CAST(:sc AS jsonb))"
                    ),
                    {"p": pid, "v": policy, "d": decision, "s": w["verified"], "sc": json.dumps(scope)},
                ).scalar_one()
            )
        )


# =========================================================================== R5-01
def test_worker_cannot_self_approve_registry_rows(w: dict[str, Any]) -> None:
    """The R5 forgeries (each one succeeded before) are refused for the worker login."""
    db, o = w["db"], w["obs"]
    with tx(db.worker) as c:
        agent = activity(c, "review", "agent:discovery")
        cv = other_connector_version(c, w)
        refused(
            c,
            "INSERT INTO meta.activity(kind, origin, code_digest, parameters, environment_digest, actor, "
            "started_at, ended_at) VALUES ('x', 'review', 'x', '{}', 'x', 'human:alice', now(), now())",
        )
        refused(
            c,
            "INSERT INTO meta.activity(kind, origin, code_digest, parameters, environment_digest, actor, "
            "started_at, ended_at) VALUES ('x', 'review', 'x', '{}', 'x', 'Maintainer:seed', now(), now())",
        )
        refused(
            c,
            """INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability,
                        decision, policy_version, review_activity_id)
                      VALUES (:cv, :c, '{}', 'eligible', 'forged', :a)""",
            {"cv": cv, "c": o["concept_version_id"], "a": agent},
        )
        refused(
            c,
            """INSERT INTO ontology.mapping(mapping_key, source_ref, target_concept_id, mapping_kind, version,
                        transform_spec, applicability, approval, review_activity_id)
                      VALUES ('r5.forged', '{}', :c, 'exact', '9', '{}', '{}', 'approved', :a)""",
            {"c": o["concept_version_id"], "a": agent},
        )
        refused(
            c,
            """INSERT INTO model.version(model_key, version, kind, spec_sha256, code_digest, input_ports,
                        output_ports, applicability, assumptions, approval_status, provenance_activity_id)
                      VALUES ('r5.forged', '9', 'forecast', :s, 'x', '[]', '[]', '{}', '[]', 'approved', :a)""",
            {"s": w["verified"], "a": agent},
        )
        refused(
            c,
            """INSERT INTO evidence.claim(claim_key, version, card, evidence_kind, status,
                        extraction_method, provenance_activity_id)
                      VALUES ('r5.forged', 1, '{}', 'experimental', 'reviewed', 'manual', :a)""",
            {"a": agent},
        )
        for actor in ("human:admin", "token:human:alice", "admin:cli", " HUMAN:x"):
            refused(
                c, "INSERT INTO ops.audit(actor, action, target, details) VALUES (:a, 'x', '{}', '{}')", {"a": actor}
            )
        refused(
            c,
            "INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, author) "
            "VALUES ('x', :s, 1, 'human:alice')",
            {"s": w["verified"]},
        )
        # positive controls: fail-safe and attributable automated states remain writable
        c.execute(
            text("""INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability,
                            decision, policy_version, review_activity_id)
                          VALUES (:cv, :c, '{}', 'quarantine', 'p', :a)"""),
            {"cv": cv, "c": o["concept_version_id"], "a": agent},
        )
        c.execute(
            text("""INSERT INTO ontology.mapping(mapping_key, source_ref, target_concept_id, mapping_kind,
                            version, transform_spec, applicability, approval, review_activity_id)
                          VALUES ('r5.cand', '{}', :c, 'exact', '9', '{}', '{}', 'candidate', :a),
                                 ('r5.incompat', '{}', :c, 'incompatible', '9', '{}', '{}', 'approved', :a)"""),
            {"c": o["concept_version_id"], "a": agent},
        )
        c.execute(
            text("""INSERT INTO model.version(model_key, version, kind, spec_sha256, code_digest, input_ports,
                            output_ports, applicability, assumptions, approval_status, provenance_activity_id)
                          VALUES ('r5.cand', '9', 'forecast', :s, 'x', '[]', '[]', '{}', '[]', 'candidate', :a)"""),
            {"s": w["verified"], "a": agent},
        )
        c.execute(
            text(
                "INSERT INTO ops.audit(actor, action, target, details) "
                "VALUES ('gate-evaluator:promotion', 'x', '{}', '{}')"
            )
        )


def test_human_attribution_is_accepted_from_api_and_owner(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.api) as c:
        c.execute(
            text("INSERT INTO ops.audit(actor, action, target, details) VALUES ('token:human:alice', 'x', '{}', '{}')")
        )
    with tx(db.owner) as c:
        activity(c, "review", "maintainer:registry-seed")


def test_worker_cannot_forge_human_promotions(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.worker) as c:
        ev = evaluation(c, w, w["mv"], "operational_as_known")
        refused(
            c,
            "SELECT model.record_promotion(:m, '{}'::jsonb, 'promote', :e, 'any', 'human:alice', '[]'::jsonb)",
            {"m": w["mv"], "e": ev},
        )
        refused(c, PROMOTION_SQL, {"m": w["mv"], "d": "promote", "e": ev, "p": "p", "r": "human:alice"})
        # automated decisions remain possible and attributable to the worker login
        pid = c.execute(
            text(
                "SELECT model.record_promotion(:m, '{}'::jsonb, 'promote', :e, 'p', "
                "'gate-evaluator:tournament', '[]'::jsonb)"
            ),
            {"m": w["mv"], "e": ev},
        ).scalar_one()
        assert (
            c.execute(text("SELECT recorded_by FROM model.promotion WHERE id = :p"), {"p": pid}).scalar_one()
            == "reva_worker"
        )


def test_recorded_by_is_the_login_and_cannot_be_supplied(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.worker) as c:
        aid = c.execute(
            text(
                "INSERT INTO ops.audit(actor, action, target, details, recorded_by) "
                "VALUES ('agent:x', 'x', '{}', '{}', 'reva_api') RETURNING id"
            )
        ).scalar_one()
        assert (
            c.execute(text("SELECT recorded_by FROM ops.audit WHERE id = :i"), {"i": aid}).scalar_one() == "reva_worker"
        )


def _scope(c: Connection, w: dict[str, Any], subjects: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "concept": concept_ref(c, w["obs"]["concept_version_id"]),
        "subject_ids": subjects,
        "years": {"from": 2020, "to": 2023},
        "mapping_key": "r5.map",
        "dataset": GATE_DATASET,
        "manifest_sha256": GATE_MANIFEST,
        **extra,
    }


APPROVE_ELIG = "SELECT evidence.approve_eligibility(:p, :g, :cv, :c, CAST(:ap AS jsonb), :pv, :a)"


def test_eligibility_approval_requires_a_bound_passing_gate(w: dict[str, Any]) -> None:
    db, o = w["db"], w["obs"]
    subj = [str(o["subject_id"])]
    with tx(db.worker) as c:
        cv, other = w["gate_cvs"]  # (R6-02) both declare the manifest/dataset the scope names
        pid = promotion_proposal(c, w, _scope(c, w, subj))
        g = gate(c, w, pid)
        act = activity(c, "review", "gate-evaluator:promotion")
        app = {"subject_ids": subj, "years": {"from": 2020, "to": 2023}, "gate_result_id": str(g)}

        def args(**kw: Any) -> dict[str, Any]:
            base = {
                "p": pid,
                "g": g,
                "cv": cv,
                "c": o["concept_version_id"],
                "ap": json.dumps(app),
                "pv": "promotion-1",
                "a": act,
            }
            return {**base, **kw}

        bad = [
            (args(ap=json.dumps({**app, "subject_ids": [str(uuid.uuid4())]})), "subject_ids"),
            (args(ap=json.dumps({**app, "years": {"from": 1900, "to": 2100}})), "years"),
            (args(ap=json.dumps({k: v for k, v in app.items() if k != "subject_ids"})), "subject_ids"),
            (args(pv="other-policy"), "ran policy"),
            (
                args(g=(fake := uuid.uuid4()), ap=json.dumps({**app, "gate_result_id": str(fake)})),
                "neither a passing gate",
            ),
            (args(g=uuid.uuid4()), "cites another gate result"),
            (args(a=activity(c, "review", "agent:discovery")), "not to the approver"),
        ]
        for params, match in bad:
            refused(c, APPROVE_ELIG, params, match=match)
        eid = c.execute(text(APPROVE_ELIG), args()).scalar_one()
        assert (
            c.execute(
                text("SELECT evidence.is_eligible(:cv, :c, :s, clock_timestamp(), 2021)"),
                {"cv": cv, "c": o["concept_version_id"], "s": o["subject_id"]},
            ).scalar_one()
            is True
        )
        assert (
            c.execute(text("SELECT recorded_by FROM evidence.eligibility WHERE id = :e"), {"e": eid}).scalar_one()
            == "reva_worker"
        )
        assert (
            c.execute(
                text(
                    "SELECT actor FROM ops.audit WHERE action = 'eligibility.approve' "
                    "AND target->>'eligibility_id' = :e"
                ),
                {"e": str(eid)},
            ).scalar_one()
            == "gate-evaluator:promotion"
        )
        # one gate pass approves one (connector version, concept): no laundering onto another connector
        refused(c, APPROVE_ELIG, args(cv=other), match="already approved another")
        # re-applying it to the same target is idempotent (append-only history)
        c.execute(text(APPROVE_ELIG), args())
        # a failing gate, or a pass later superseded by a fail, approves nothing
        pid2 = promotion_proposal(c, w, _scope(c, w, subj))
        g_fail = gate(c, w, pid2, "fail")
        refused(
            c,
            APPROVE_ELIG,
            args(p=pid2, g=g_fail, ap=json.dumps({**app, "gate_result_id": str(g_fail)})),
            match="not a passing gate",
        )
        g_pass = gate(c, w, pid2)
        gate(c, w, pid2, "needs_review")
        refused(
            c,
            APPROVE_ELIG,
            args(p=pid2, g=g_pass, ap=json.dumps({**app, "gate_result_id": str(g_pass)})),
            match="supersedes",
        )
        # the proposal type and the concept in its recorded scope must match
        pid3 = promotion_proposal(c, w, _scope(c, w, subj), ptype="mapping_equivalence")
        g3 = gate(c, w, pid3)
        refused(
            c, APPROVE_ELIG, args(p=pid3, g=g3, ap=json.dumps({**app, "gate_result_id": str(g3)})), match="not one of"
        )
        pid4 = promotion_proposal(c, w, _scope(c, w, subj, concept="other.concept@1"))
        g4 = gate(c, w, pid4)
        refused(
            c,
            APPROVE_ELIG,
            args(p=pid4, g=g4, ap=json.dumps({**app, "gate_result_id": str(g4)})),
            match="does not cover concept",
        )


def test_mapping_approval_requires_bound_gate(w: dict[str, Any]) -> None:
    db, o = w["db"], w["obs"]
    sql = (
        "SELECT ontology.approve_mapping(:p, :g, :k, '1', '{}'::jsonb, :c, 'exact', '{}'::jsonb, '{}'::jsonb, "
        "'promotion-1', :a)"
    )
    with tx(db.worker) as c:
        pid = promotion_proposal(c, w, _scope(c, w, [str(o["subject_id"])]))
        g = gate(c, w, pid)
        act = activity(c, "review", "gate-evaluator:promotion")
        refused(
            c,
            sql,
            {"p": pid, "g": g, "k": "r5.other", "c": o["concept_version_id"], "a": act},
            match="does not cover mapping",
        )
        mid = c.execute(
            text(sql), {"p": pid, "g": g, "k": "r5.map", "c": o["concept_version_id"], "a": act}
        ).scalar_one()
        assert (
            c.execute(text("SELECT approval FROM ontology.mapping WHERE id = :m"), {"m": mid}).scalar_one()
            == "approved"
        )


def test_model_approval_requires_a_human_review_recorded_by_the_api(w: dict[str, Any]) -> None:
    db = w["db"]
    with db.worker.begin() as c:  # committed: the API reviews it in its own transaction
        pid = promotion_proposal(c, w, {}, ptype="model_registration")
    sql = (
        "SELECT model.approve_version(:p, :g, :k, '1', 'forecast', :s, 'x', '[]'::jsonb, '[]'::jsonb, "
        "'{}'::jsonb, '[]'::jsonb, :a)"
    )
    with tx(db.worker) as c:  # no approval yet
        act = activity(c, "review", "worker:model-registry")
        refused(
            c,
            sql,
            {"p": pid, "g": uuid.uuid4(), "k": "r5.m", "s": w["verified"], "a": act},
            match="neither a passing gate",
        )
    with db.api.begin() as c:
        rid = c.execute(
            text("SELECT ops.review_proposal(:p, 'approve', 'human:maintainer', 'ok')"), {"p": pid}
        ).scalar_one()
    with tx(db.worker) as c:
        act = activity(c, "review", "worker:model-registry")
        vid = c.execute(text(sql), {"p": pid, "g": rid, "k": "r5.m", "s": w["verified"], "a": act}).scalar_one()
        assert (
            c.execute(text("SELECT approval_status FROM model.version WHERE id = :v"), {"v": vid}).scalar_one()
            == "approved"
        )
        assert (
            c.execute(
                text("SELECT actor FROM ops.audit WHERE action = 'model.approve' AND target->>'model_version_id' = :v"),
                {"v": str(vid)},
            ).scalar_one()
            == "human:maintainer"
        )
        # one review registers one model version
        refused(
            c, sql, {"p": pid, "g": rid, "k": "r5.m2", "s": w["verified"], "a": act}, match="already approved another"
        )


def test_reviews_and_restores_are_checked(w: dict[str, Any]) -> None:
    db, o = w["db"], w["obs"]
    with tx(db.owner) as c:  # the admin CLI may review; the worker may not (grant + login check)
        pid = promotion_proposal(c, w, {})
        c.execute(text("SELECT ops.review_proposal(:p, 'approve', 'human:admin', '')"), {"p": pid})
    with tx(db.worker) as c:
        act = activity(c, "review", "gate-evaluator:promotion")
        refused(
            c,
            "SELECT evidence.restore_eligibility(:cv, :c, :rb, :a)",
            {"cv": o["connector_version_id"], "c": o["concept_version_id"], "rb": uuid.uuid4(), "a": act},
            match="not retired by supersession",
        )
    with tx(db.sim) as c:
        refused(
            c, "SELECT evidence.approve_eligibility(NULL, NULL, NULL, NULL, '{}', 'p', NULL)", match="permission denied"
        )


def test_approval_functions_are_owned_by_the_nologin_registry_role(w: dict[str, Any]) -> None:
    with w["db"].owner.connect() as c:
        rows = c.execute(
            text(
                """SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner), p.prosecdef,
                      has_function_privilege('public', p.oid, 'EXECUTE')
               FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE p.proname IN ('approve_eligibility','restore_eligibility','approve_mapping',
                                   'approve_version','consume_approval')"""
            )
        ).all()
        login = c.execute(text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'reva_registry'")).scalar_one()
    assert len(rows) == 5 and not login
    assert all(owner == "reva_registry" and not public for _, owner, _, public in rows)


# =========================================================================== R5-06
def test_worker_prunes_only_expired_session_revocations(w: dict[str, Any]) -> None:
    db = w["db"]
    live, dead = f"r5-live-{uuid.uuid4().hex}", f"r5-dead-{uuid.uuid4().hex}"
    with db.api.begin() as c:
        c.execute(
            text(
                "INSERT INTO ops.session_revocation(session_id, expires_at) VALUES "
                "(:l, clock_timestamp() + interval '1 hour'), (:d, clock_timestamp() - interval '1 hour')"
            ),
            {"l": live, "d": dead},
        )
    with db.worker.begin() as c:
        assert c.execute(text("DELETE FROM ops.session_revocation WHERE session_id = :s"), {"s": live}).rowcount == 0
        assert c.execute(text("DELETE FROM ops.session_revocation WHERE session_id = :s"), {"s": dead}).rowcount == 1
    with db.api.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM ops.session_revocation WHERE session_id = :s"), {"s": live}
            ).scalar_one()
            == 1
        )


# =========================================================================== R5-12
def test_worker_cannot_submit_jobs_as_an_api_principal(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        refused(
            c,
            "SELECT ops.submit_job('discovery', '{}'::jsonb, :k, 10, 'human:admin')",
            {"k": f"r5-{uuid.uuid4().hex}"},
            match="permission denied",
        )


def test_idempotency_namespace_is_injective(w: dict[str, Any]) -> None:
    k = uuid.uuid4().hex[:12]
    sql = "SELECT ops.submit_job('simulation', CAST(:p AS jsonb), :k, 0, :c)"
    with tx(w["db"].api) as c:
        a = c.execute(text(sql), {"p": '{"a": 1}', "k": f"m:simulation:{k}", "c": "token:alice"}).scalar_one()
        b = c.execute(text(sql), {"p": '{"a": 1}', "k": k, "c": "token:alice:simulation:m"}).scalar_one()
        assert a != b
        # keys with ':' or '%' are still stable for retries
        assert c.execute(text(sql), {"p": '{"a": 1}', "k": f"m:simulation:{k}", "c": "token:alice"}).scalar_one() == a
        pct = c.execute(text(sql), {"p": '{"a": 1}', "k": f"m%3Asimulation%3A{k}", "c": "token:alice"}).scalar_one()
        assert pct != a


# =========================================================================== R5-16
def test_sim_cannot_claim_foreign_outputs_or_model_activities(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.sim) as c:
        act = activity(c, "simulation", "sim:r5")
        refused(
            c,
            "INSERT INTO meta.activity_output(activity_id, artifact_sha256) VALUES (:a, :s)",
            {"a": act, "s": w["verified"]},
            match="row-level security",
        )
        refused(
            c,
            "INSERT INTO meta.activity(kind, origin, code_digest, parameters, environment_digest, actor, "
            "started_at, ended_at) VALUES ('x', 'model', 'x', '{}', 'x', 'sim:x', now(), now())",
            match="row-level security",
        )
    with db.owner.begin() as c:  # an artifact nobody produced yet
        sha = new_artifact(c)
    with tx(db.sim) as c:
        act = activity(c, "simulation", "sim:r5")
        c.execute(
            text("INSERT INTO meta.activity_output(activity_id, artifact_sha256) VALUES (:a, :s)"), {"a": act, "s": sha}
        )


# =========================================================================== R5-09 / R5-10
def test_bootstrap_rejects_memberships_held_by_runtime_roles(db: Any) -> None:
    from reva.cli.bootstrap import verify_runtime_memberships

    probe = f"r5_probe_{uuid.uuid4().hex[:8]}"
    admin = create_engine(db.settings.migration_url().set(database=db.name), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            verify_runtime_memberships(c)  # clean cluster passes
            c.exec_driver_sql(f"CREATE ROLE {probe} NOLOGIN")
            c.exec_driver_sql(f"GRANT {probe} TO reva_worker WITH INHERIT TRUE, SET TRUE")
            with pytest.raises(ConfigurationError, match="reva_worker -> r5_probe"):
                verify_runtime_memberships(c)
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {probe}")
        admin.dispose()


def test_deletion_needs_explicit_set_role(w: dict[str, Any]) -> None:
    db = w["db"]
    with tx(db.owner) as c:
        sha = new_artifact(c)
        c.execute(text("SELECT set_config('reva.reviewed_deletion_ticket', 'R5-10', true)"))
        # implicit membership (superuser / CREATEROLE owner) is no longer enough
        refused(c, "DELETE FROM meta.artifact WHERE sha256 = :s", {"s": sha}, match="Immutable scientific record")
        c.execute(text("SET LOCAL ROLE reva_restricted_deletion"))
        assert c.execute(text("DELETE FROM meta.artifact WHERE sha256 = :s"), {"s": sha}).rowcount == 1
        c.execute(text("SELECT set_config('reva.reviewed_deletion_ticket', '', true)"))
        sha2 = c.execute(text("SELECT sha256 FROM meta.artifact LIMIT 1")).scalar_one()
        refused(c, "DELETE FROM meta.artifact WHERE sha256 = :s", {"s": sha2}, match="Immutable scientific record")


# =========================================================================== seeding
def test_registry_seed_runs_as_owner_and_runtime_roles_only_confirm_it(w: dict[str, Any], monkeypatch) -> None:
    import reva.cli.seed as seed_mod

    db = w["db"]
    # The owner seeds (or confirms) exactly the current registry first: on a shared session database the
    # registry files may have changed since bootstrap. A runtime role may then only confirm it.
    assert seed_mod.seed(db.owner, db.store)["status"] in ("seeded", "already_seeded")
    assert seed_mod.seed(db.worker, db.store)["status"] == "already_seeded"
    with db.owner.connect() as c:
        assert (
            c.execute(
                text("SELECT recorded_by FROM meta.activity WHERE kind = 'registry.seed' ORDER BY recorded_at LIMIT 1")
            ).scalar_one()
            != "reva_worker"
        )
    monkeypatch.setattr(seed_mod, "seed_fingerprint", lambda reg, contact_email=None: "changed-registry")
    with pytest.raises(ConfigurationError, match="migration owner"):
        seed_mod.seed(db.worker, db.store)


def test_connector_version_content_is_immutable(w: dict[str, Any]) -> None:
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.cli.seed import register_connector_version
    from reva.kernel.errors import ImmutableRecordViolation

    spec = builtin_connectors()["worldbank.wdi.indicators"].describe()
    with tx(w["db"].owner) as c:
        act = activity(c)
        assert register_connector_version(c, w["db"].store, spec, act) == w["obs"]["connector_version_id"]
        changed = spec.model_copy(update={"code_digest": "sha256:" + "0" * 64})
        with pytest.raises(ImmutableRecordViolation):
            register_connector_version(c, w["db"].store, changed, act)
