"""Security: separate NOINHERIT runtime roles, precise grants, row security on
shared provenance tables. Passwords/LOGIN are set by scripts/bootstrap_runtime.py.

A WHERE clause is not a security boundary: these grants are.

Revision ID: 0007_security
Revises: 0006_operations
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0007_security"
down_revision = "0006_operations"
branch_labels = None
depends_on = None

SCIENTIFIC = ("meta", "ontology", "world", "evidence", "belief", "model", "sim")
ALL_SCHEMAS = (*SCIENTIFIC, "ops")

SQL_ROLES = r"""
-- Works for a superuser and for a non-superuser CREATEROLE owner (managed
-- PostgreSQL, PG16+ semantics): such an owner may not even *mention*
-- SUPERUSER/CREATEDB/CREATEROLE/BYPASSRLS/REPLICATION in CREATE/ALTER ROLE, so
-- roles are created with the (false) defaults and the attributes are verified.
DO $$
DECLARE r text; bad text;
BEGIN
  FOREACH r IN ARRAY ARRAY['reva_api','reva_worker','reva_sim'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      BEGIN
        EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT', r);
      EXCEPTION WHEN duplicate_object OR unique_violation THEN
        NULL;  -- created concurrently by another bootstrap; verified below
      END;
    END IF;
    -- Roles are cluster-global; only touch the catalog row when needed so parallel
    -- bootstraps of other databases do not race on it ("tuple concurrently updated").
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r AND rolinherit) THEN
      EXECUTE format('ALTER ROLE %I NOINHERIT', r);
    END IF;
    SELECT string_agg(a, ', ') INTO bad FROM pg_roles p,
      LATERAL (VALUES ('SUPERUSER', p.rolsuper), ('CREATEDB', p.rolcreatedb), ('CREATEROLE', p.rolcreaterole),
                      ('BYPASSRLS', p.rolbypassrls), ('REPLICATION', p.rolreplication)) v(a, on_)
     WHERE p.rolname = r AND v.on_;
    IF bad IS NOT NULL THEN
      RAISE EXCEPTION 'runtime role % holds forbidden attributes: % (a superuser must remove them)', r, bad;
    END IF;
  END LOOP;
END $$;
"""


def _grants() -> str:
    s = []
    db_revoke = "REVOKE ALL ON DATABASE {db} FROM PUBLIC;"
    s.append(
        "DO $$ BEGIN EXECUTE format('" + db_revoke.replace("{db}", "%I") + "', current_database()); END $$;"
    )
    s.append(
        "DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO reva_api, reva_worker, reva_sim',"
        " current_database()); END $$;"
    )
    for sch in ALL_SCHEMAS:
        s.append(f"REVOKE ALL ON SCHEMA {sch} FROM PUBLIC;")
        s.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {sch} FROM PUBLIC;")
        s.append(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {sch} FROM PUBLIC;")
        s.append(f"GRANT USAGE ON SCHEMA {sch} TO reva_api, reva_worker, reva_sim;")
    # Trigger/check helper functions must remain callable by the invoking role.
    s.append(
        "GRANT EXECUTE ON FUNCTION meta.artifact_is_verified(text), meta.activity_reaches(uuid,uuid), "
        "meta.require_context_mode(uuid,text), evidence.is_eligible(uuid,uuid,uuid,timestamptz,integer,text), "
        "evidence.observation_is_eligible(uuid,timestamptz), world.support_ref_year(uuid), "
        "meta.inserted_in_current_xact(xid), world.assert_relation_cardinality(uuid) "
        "TO reva_api, reva_worker, reva_sim;"
    )
    s.append("GRANT USAGE ON SCHEMA public TO reva_api, reva_worker, reva_sim;")

    # --- reva_api: read approved projections; writes only via narrow functions.
    for sch in SCIENTIFIC:
        s.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO reva_api;")
    s.append(
        "GRANT SELECT ON ops.job, ops.schedule, ops.publication, ops.connector_health, ops.proposal, "
        "ops.proposal_review, ops.gate_result, ops.api_token, ops.budget_ledger, ops.dirty_node, "
        "ops.connector_cursor, ops.worker_presence TO reva_api;"
    )
    s.append("GRANT INSERT ON ops.audit TO reva_api;")
    # (R2-09) session revocations are recorded by the API and pruned by the worker.
    s.append("GRANT SELECT, INSERT ON ops.session_revocation TO reva_api;")
    s.append(
        "GRANT EXECUTE ON FUNCTION ops.submit_job(text,jsonb,text,integer,text), "
        "ops.request_cancel(uuid,text), ops.review_proposal(uuid,text,text,text), "
        "sim.create_scenario(jsonb,bytea,text,text) TO reva_api;"
    )

    # --- reva_worker: ingestion, belief publication, registry, queue.
    for sch in ALL_SCHEMAS:
        s.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO reva_worker;")
    for sch in ("meta", "ontology", "world", "evidence", "belief", "model", "ops"):
        s.append(f"GRANT INSERT ON ALL TABLES IN SCHEMA {sch} TO reva_worker;")
    # (R-01) Authentication and human-review rows are never written with worker
    # credentials: tokens are minted by the migration owner (admin CLI), reviews
    # only through ops.review_proposal (granted to reva_api alone). Gate results
    # and promotions keep a direct grant, guarded by BEFORE INSERT triggers
    # (identity format) and by meta.enforce_governance (who may attribute to a
    # human; R5-01, see SQL_GOVERNANCE). Approved/eligible/reviewed registry
    # states are written only by the reva_registry approval functions.
    s.append("REVOKE INSERT ON ops.api_token, ops.proposal_review, ops.session_revocation FROM reva_worker;")
    s.append(
        "GRANT UPDATE ON ops.job, ops.schedule, ops.dirty_node, ops.outbox, ops.connector_cursor, "
        "ops.gc_candidate, ops.budget_ledger, ops.worker_presence TO reva_worker;"
    )
    # (R5-06) session_revocation DELETE is limited to expired rows by RLS (SQL_GOVERNANCE).
    s.append("GRANT DELETE ON ops.dirty_node, ops.gc_candidate, ops.session_revocation TO reva_worker;")
    s.append("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO reva_worker;")
    # (R5-12) The worker does not submit API jobs: ops.submit_job is the API's
    # path (creator = the authenticated API principal); internal jobs are
    # enqueued directly with their own key prefixes.
    s.append(
        "GRANT EXECUTE ON FUNCTION ops.assert_lease(uuid,text,bigint), "
        "ops.heartbeat(uuid,text,bigint,integer,jsonb), "
        "ops.complete_job(uuid,text,bigint,text,jsonb,jsonb), "
        "ops.record_gate_result(uuid,text,text,text,text,text), "
        "model.record_promotion(uuid,jsonb,text,uuid,text,text,jsonb) TO reva_worker;"
    )

    # --- reva_sim: read pinned inputs; write only sim/evaluation outputs.
    for sch in SCIENTIFIC:
        s.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO reva_sim;")
    s.append("GRANT SELECT ON ops.publication TO reva_sim;")
    s.append("GRANT INSERT ON ALL TABLES IN SCHEMA sim TO reva_sim;")
    # (R5-02) Scenarios are created only by sim.create_scenario (API), which
    # stores and verifies the canonical spec bytes; the worker has no sim INSERT.
    s.append("REVOKE INSERT ON sim.scenario FROM reva_sim;")
    # (R5-05) No direct meta.artifact INSERT: artifact metadata (sha, size,
    # label) is derived from the bytes by sim.register_artifact.
    s.append(
        "GRANT INSERT ON meta.artifact_location, meta.activity, meta.activity_input, "
        "meta.activity_output, meta.context, model.evaluation TO reva_sim;"
    )
    s.append("GRANT EXECUTE ON FUNCTION sim.register_artifact(bytea) TO reva_sim;")
    s.append(
        "GRANT EXECUTE ON FUNCTION ops.assert_lease(uuid,text,bigint), "
        "ops.heartbeat(uuid,text,bigint,integer,jsonb), "
        "ops.complete_job(uuid,text,bigint,text,jsonb,jsonb) TO reva_sim;"
    )
    return "\n".join(s)


SQL_RLS = r"""
-- Row security: simulation credentials may only create simulation/evaluation
-- contexts and simulation/evaluation/model activities. The worker may not
-- create simulation contexts. Owner (migration role) is unaffected.
ALTER TABLE meta.context ENABLE ROW LEVEL SECURITY;
CREATE POLICY context_read ON meta.context FOR SELECT TO reva_api, reva_worker, reva_sim USING (true);
CREATE POLICY context_sim_insert ON meta.context FOR INSERT TO reva_sim
  WITH CHECK (mode IN ('simulation','evaluation'));
CREATE POLICY context_worker_insert ON meta.context FOR INSERT TO reva_worker
  WITH CHECK (mode <> 'simulation');

ALTER TABLE meta.activity ENABLE ROW LEVEL SECURITY;
CREATE POLICY activity_read ON meta.activity FOR SELECT TO reva_api, reva_worker, reva_sim USING (true);
-- (R5-16) 'model' activities are the worker's (population.materialise); the
-- simulation role has only simulation/evaluation provenance.
CREATE POLICY activity_sim_insert ON meta.activity FOR INSERT TO reva_sim
  WITH CHECK (origin IN ('simulation','evaluation'));
CREATE POLICY activity_worker_insert ON meta.activity FOR INSERT TO reva_worker
  WITH CHECK (origin <> 'simulation');

ALTER TABLE meta.artifact_location ENABLE ROW LEVEL SECURITY;
CREATE POLICY location_read ON meta.artifact_location FOR SELECT TO reva_api, reva_worker, reva_sim USING (true);
-- (R-18) The simulation role may register a location only in its own
-- namespaces, never for an artifact that already lives elsewhere (raw source
-- bytes, normalised/belief/model artifacts) or is stored inline (scenario
-- specs), and only for an artifact it created in this transaction or one that
-- already has a scenarios/evaluation location (content-addressed reuse).
CREATE POLICY location_sim_insert ON meta.artifact_location FOR INSERT TO reva_sim
  WITH CHECK (
    namespace IN ('scenarios','evaluation')
    AND NOT EXISTS (SELECT 1 FROM meta.artifact_inline i
                    WHERE i.artifact_sha256 = artifact_location.artifact_sha256)
    AND NOT EXISTS (SELECT 1 FROM meta.artifact_location l
                    WHERE l.artifact_sha256 = artifact_location.artifact_sha256
                      AND l.namespace NOT IN ('scenarios','evaluation'))
    AND (meta.inserted_in_current_xact((SELECT a.xmin FROM meta.artifact a
                                        WHERE a.sha256 = artifact_location.artifact_sha256))
         OR EXISTS (SELECT 1 FROM meta.artifact_location l
                    WHERE l.artifact_sha256 = artifact_location.artifact_sha256
                      AND l.namespace IN ('scenarios','evaluation'))));
CREATE POLICY location_worker_insert ON meta.artifact_location FOR INSERT TO reva_worker
  WITH CHECK (namespace <> 'scenarios');

-- (R-18) Provenance edges: the simulation role may attach inputs/outputs only
-- to simulation/evaluation activities (never to source, normalisation, belief,
-- model or review activities); the worker never to simulation activities.
-- (R5-16) The simulation role may not claim to have produced an artifact that
-- a non-simulation/evaluation activity produced (e.g. raw source bytes).
ALTER TABLE meta.activity_input ENABLE ROW LEVEL SECURITY;
CREATE POLICY activity_input_read ON meta.activity_input FOR SELECT TO reva_api, reva_worker, reva_sim
  USING (true);
CREATE POLICY activity_input_sim_insert ON meta.activity_input FOR INSERT TO reva_sim
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_input.activity_id)
              IN ('simulation','evaluation'));
CREATE POLICY activity_input_worker_insert ON meta.activity_input FOR INSERT TO reva_worker
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_input.activity_id) <> 'simulation');

ALTER TABLE meta.activity_output ENABLE ROW LEVEL SECURITY;
CREATE POLICY activity_output_read ON meta.activity_output FOR SELECT TO reva_api, reva_worker, reva_sim
  USING (true);
CREATE POLICY activity_output_sim_insert ON meta.activity_output FOR INSERT TO reva_sim
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_output.activity_id)
              IN ('simulation','evaluation')
              AND NOT EXISTS (SELECT 1 FROM meta.activity_output o JOIN meta.activity a ON a.id = o.activity_id
                              WHERE o.artifact_sha256 = activity_output.artifact_sha256
                                AND a.origin NOT IN ('simulation','evaluation')));
CREATE POLICY activity_output_worker_insert ON meta.activity_output FOR INSERT TO reva_worker
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_output.activity_id) <> 'simulation');
"""


#: (R5-01) Tables whose rows carry an unforgeable ``recorded_by`` (the login
#: that wrote the row: ``session_user``, which a SECURITY DEFINER function does
#: not change) and the governance rules of meta.enforce_governance().
RECORDED_BY_TABLES = (
    "meta.activity", "ontology.mapping", "evidence.eligibility", "evidence.claim", "model.version",
    "model.evaluation", "model.promotion", "ops.audit", "ops.proposal", "ops.proposal_review", "ops.gate_result",
)
GOVERNED_TABLES = (
    "meta.activity", "ontology.mapping", "evidence.eligibility", "evidence.claim", "model.version",
    "model.promotion", "ops.audit", "ops.proposal",
)

SQL_GOVERNANCE = r"""
-- ---------------------------------------------------------------------------
-- (R5-01) Governance is enforced on the *login* (session_user), not on the
-- format of identity strings.
--   * Human attribution (actor/reviewer/author 'human:', 'maintainer:',
--     'admin:', also behind 'token:') is accepted only from the API login (the
--     authenticated human channel), the migration owner (admin CLI, registry
--     seed) and the reva_registry approval functions (which verified a recorded
--     human review).
--   * Approving states (eligibility 'eligible', mapping 'approved' except
--     'incompatible', model version 'approved', claim 'reviewed') are written
--     only by the migration owner (reviewed registry seed, admin CLI) or by the
--     reva_registry SECURITY DEFINER functions, which require a recorded
--     passing gate result or a human review (recorded through reva_api) of a
--     proposal whose recorded scope covers exactly what is approved; one
--     approval approves one thing (ops.approval_use).
--   * Everything else the worker may still write is fail-safe (quarantine,
--     retired, candidate, rejected, retracted) or attributable (recorded_by).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reva_registry') THEN
    BEGIN
      CREATE ROLE reva_registry NOLOGIN NOINHERIT;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN
      NULL;  -- created concurrently by another bootstrap
    END;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reva_registry'
             AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR rolreplication)) THEN
    RAISE EXCEPTION 'reva_registry must be a NOLOGIN role without privileged attributes';
  END IF;
  -- A non-superuser CREATEROLE owner (PG16+) holds only ADMIN on roles it
  -- created; making reva_registry an object owner needs SET on it.
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
     AND NOT pg_has_role(current_user, 'reva_registry', 'SET') THEN
    EXECUTE format('GRANT reva_registry TO %I WITH INHERIT FALSE, SET TRUE', current_user);
  END IF;
END $$;

-- Who is writing: the runtime login, the migration owner, or another login.
CREATE FUNCTION meta.role_is_owner(r name) RETURNS boolean LANGUAGE sql STABLE
  SET search_path = pg_catalog, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM pg_roles x WHERE x.rolname = r AND x.rolsuper)
      OR EXISTS (SELECT 1 FROM pg_namespace n JOIN pg_roles x ON x.oid = n.nspowner
                 WHERE n.nspname = 'meta' AND x.rolname = r)
$$;
CREATE FUNCTION meta.session_principal() RETURNS text LANGUAGE sql STABLE
  SET search_path = pg_catalog, pg_temp AS $$
  SELECT CASE WHEN session_user::text IN ('reva_api','reva_worker','reva_sim') THEN session_user::text
              WHEN meta.role_is_owner(session_user) THEN 'owner'
              ELSE 'other' END
$$;
-- 'human:x', 'maintainer:x', 'admin:x' (also 'token:human:x' ...), any case.
CREATE FUNCTION meta.is_human_identity(who text) RETURNS boolean LANGUAGE sql IMMUTABLE
  SET search_path = pg_catalog, pg_temp AS $$
  SELECT COALESCE(who ~* '^\s*(token\s*:\s*)?(human|maintainer|admin)\s*:', false)
$$;

CREATE FUNCTION meta.force_recorded_by() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_by := session_user;
  RETURN NEW;
END $$;

CREATE FUNCTION meta.enforce_governance() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  who text := meta.session_principal();
  human_ok boolean := who IN ('owner','reva_api') OR current_user = 'reva_registry';
  approve_ok boolean := who = 'owner' OR current_user = 'reva_registry';
  tbl text := TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME;
  bad text;
BEGIN
  CASE tbl
    WHEN 'meta.activity' THEN
      IF NOT human_ok AND meta.is_human_identity(NEW.actor) THEN bad := 'a human-attributed activity'; END IF;
    WHEN 'ops.audit' THEN
      IF NOT human_ok AND meta.is_human_identity(NEW.actor) THEN bad := 'a human-attributed audit row'; END IF;
    WHEN 'ops.proposal' THEN
      IF NOT human_ok AND meta.is_human_identity(NEW.author) THEN bad := 'a human-authored proposal'; END IF;
    WHEN 'model.promotion' THEN
      IF NOT human_ok AND meta.is_human_identity(NEW.reviewer) THEN
        bad := 'a promotion attributed to a human reviewer';
      END IF;
    WHEN 'evidence.eligibility' THEN
      IF NOT approve_ok AND NEW.decision = 'eligible' THEN
        bad := 'an eligible decision (use evidence.approve_eligibility with a recorded approval)';
      END IF;
    WHEN 'ontology.mapping' THEN
      IF NOT approve_ok AND NEW.approval = 'approved' AND NEW.mapping_kind <> 'incompatible' THEN
        bad := 'an approved mapping (use ontology.approve_mapping with a recorded approval)';
      END IF;
    WHEN 'model.version' THEN
      IF NOT approve_ok AND NEW.approval_status = 'approved' THEN
        bad := 'an approved model version (use model.approve_version with a recorded approval)';
      END IF;
    WHEN 'evidence.claim' THEN
      IF NOT approve_ok AND NEW.status = 'reviewed' THEN bad := 'a reviewed claim'; END IF;
    ELSE
      NULL;
  END CASE;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'governance: % may not record % in %', session_user, bad, tbl
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

-- One approval (a passing gate result or an approving human review) approves
-- one thing per kind; re-applying it to the very same target is idempotent.
CREATE TABLE ops.approval_use (
  approval_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('eligibility','mapping','model_version')),
  proposal_id uuid NOT NULL REFERENCES ops.proposal,
  approver text NOT NULL,
  target jsonb NOT NULL,
  recorded_by text NOT NULL DEFAULT session_user,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (approval_id, kind)
);
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON ops.approval_use
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();

-- Verify a recorded approval of p_proposal and bind it to p_target. Returns
-- the approver identity. Callable only from the reva_registry functions.
CREATE FUNCTION ops.consume_approval(p_proposal uuid, p_approval uuid, p_types text[], p_policy text,
                                     p_kind text, p_target jsonb) RETURNS text
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE p ops.proposal; g ops.gate_result; r ops.proposal_review; who text; used jsonb;
BEGIN
  SELECT * INTO p FROM ops.proposal WHERE id = p_proposal;
  IF p.id IS NULL THEN RAISE EXCEPTION 'approval: proposal % not found', p_proposal; END IF;
  IF NOT (p.proposal_type = ANY(p_types)) THEN
    RAISE EXCEPTION 'approval: proposal % is a %, not one of %', p_proposal, p.proposal_type, p_types;
  END IF;
  SELECT * INTO g FROM ops.gate_result WHERE id = p_approval;
  IF g.id IS NOT NULL THEN
    IF g.proposal_id <> p_proposal OR g.decision <> 'pass' OR g.evaluator NOT LIKE 'gate-evaluator:%' THEN
      RAISE EXCEPTION 'approval: gate result % is not a passing gate result for proposal %', p_approval, p_proposal;
    END IF;
    IF p_policy IS NOT NULL AND g.policy_version IS DISTINCT FROM p_policy THEN
      RAISE EXCEPTION 'approval: gate result % ran policy %, not %', p_approval, g.policy_version, p_policy;
    END IF;
    IF EXISTS (SELECT 1 FROM ops.gate_result x WHERE x.proposal_id = p_proposal AND x.decision <> 'pass'
               AND (x.recorded_at, x.id) > (g.recorded_at, g.id)) THEN
      RAISE EXCEPTION 'approval: a later gate decision supersedes gate result %', p_approval;
    END IF;
    who := g.evaluator;
  ELSE
    SELECT * INTO r FROM ops.proposal_review WHERE id = p_approval;
    IF r.id IS NULL OR r.proposal_id <> p_proposal OR r.decision <> 'approve' OR r.reviewer NOT LIKE 'human:%' THEN
      RAISE EXCEPTION 'approval: % is neither a passing gate result nor an approving human review of proposal %',
        p_approval, p_proposal;
    END IF;
    IF NOT (r.recorded_by = 'reva_api' OR meta.role_is_owner(r.recorded_by::name)) THEN
      RAISE EXCEPTION 'approval: review % was not recorded through the API', p_approval;
    END IF;
    IF EXISTS (SELECT 1 FROM ops.proposal_review x WHERE x.proposal_id = p_proposal AND x.decision <> 'approve'
               AND (x.recorded_at, x.id) > (r.recorded_at, r.id)) THEN
      RAISE EXCEPTION 'approval: a later review supersedes review %', p_approval;
    END IF;
    who := r.reviewer;
  END IF;
  INSERT INTO ops.approval_use(approval_id, kind, proposal_id, approver, target)
  VALUES (p_approval, p_kind, p_proposal, who, p_target) ON CONFLICT DO NOTHING;
  SELECT target INTO used FROM ops.approval_use WHERE approval_id = p_approval AND kind = p_kind;
  IF used IS DISTINCT FROM p_target THEN
    RAISE EXCEPTION 'approval: % already approved another % (%)', p_approval, p_kind, used;
  END IF;
  RETURN who;
END $$;

CREATE FUNCTION ops.require_review_activity(p_activity uuid, p_actor text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE a record;
BEGIN
  SELECT origin, actor INTO a FROM meta.activity WHERE id = p_activity;
  IF a.origin IS DISTINCT FROM 'review' THEN
    RAISE EXCEPTION 'approval: activity % is not a review activity', p_activity;
  END IF;
  IF p_actor IS NOT NULL AND a.actor IS DISTINCT FROM p_actor THEN
    RAISE EXCEPTION 'approval: review activity % is attributed to %, not to the approver %', p_activity, a.actor, p_actor;
  END IF;
END $$;

-- Automated (T1) eligibility: requires an approval of a connector_promotion
-- proposal whose recorded scope names this concept, these subject_ids and
-- these years.
CREATE FUNCTION evidence.approve_eligibility(p_proposal uuid, p_approval uuid, p_connector_version uuid,
                                             p_concept uuid, p_applicability jsonb, p_policy_version text,
                                             p_review_activity uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE scope jsonb; cref text; who text; eid uuid;
BEGIN
  IF p_applicability IS NULL OR jsonb_typeof(p_applicability) <> 'object'
     OR octet_length(p_applicability::text) > 65536 THEN
    RAISE EXCEPTION 'applicability must be a JSON object of at most 64 KiB';
  END IF;
  IF p_policy_version IS NULL OR btrim(p_policy_version) = '' OR length(p_policy_version) > 200 THEN
    RAISE EXCEPTION 'invalid policy version';
  END IF;
  IF jsonb_typeof(p_applicability->'subject_ids') IS DISTINCT FROM 'array'
     OR jsonb_array_length(p_applicability->'subject_ids') = 0 THEN
    RAISE EXCEPTION 'approved eligibility must be scoped to explicit subject_ids';
  END IF;
  IF p_applicability ? 'gate_result_id' AND p_applicability->>'gate_result_id' IS DISTINCT FROM p_approval::text THEN
    RAISE EXCEPTION 'applicability cites another gate result';
  END IF;
  SELECT summary->'scope' INTO scope FROM ops.proposal WHERE id = p_proposal;
  SELECT concept_id || '@' || version INTO cref FROM ontology.concept_version WHERE id = p_concept;
  IF cref IS NULL OR scope->>'concept' IS DISTINCT FROM cref THEN
    RAISE EXCEPTION 'proposal scope does not cover concept %', COALESCE(cref, p_concept::text);
  END IF;
  IF NOT COALESCE(jsonb_typeof(scope->'subject_ids') = 'array'
                  AND scope->'subject_ids' @> p_applicability->'subject_ids', false) THEN
    RAISE EXCEPTION 'proposal scope does not cover subject_ids %', p_applicability->'subject_ids';
  END IF;
  IF (scope->'years') IS DISTINCT FROM (p_applicability->'years') THEN
    RAISE EXCEPTION 'proposal scope years % differ from %', scope->'years', p_applicability->'years';
  END IF;
  who := ops.consume_approval(p_proposal, p_approval, ARRAY['connector_promotion'], p_policy_version,
                              'eligibility', jsonb_build_object('connector_version_id', p_connector_version,
                                                                'concept_version_id', p_concept));
  PERFORM ops.require_review_activity(p_review_activity, who);
  INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                                   policy_version, review_activity_id)
  VALUES (p_connector_version, p_concept, p_applicability, 'eligible', p_policy_version, p_review_activity)
  RETURNING id INTO eid;
  INSERT INTO ops.audit(actor, action, target, details)
  VALUES (who, 'eligibility.approve', jsonb_build_object('eligibility_id', eid, 'proposal_id', p_proposal,
                                                         'approval_id', p_approval),
          jsonb_build_object('connector_version_id', p_connector_version, 'concept_version_id', p_concept,
                             'policy_version', p_policy_version));
  RETURN eid;
END $$;

-- Rollback of a superseding version: re-append the previous version's last
-- approved decision, only if it was retired *because* it was superseded by
-- p_rolled_back and p_rolled_back is no longer eligible.
CREATE FUNCTION evidence.restore_eligibility(p_connector_version uuid, p_concept uuid, p_rolled_back uuid,
                                             p_review_activity uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE last evidence.eligibility; prev evidence.eligibility; sup text; actor text; eid uuid;
BEGIN
  SELECT * INTO last FROM evidence.eligibility WHERE connector_version_id = p_connector_version
     AND concept_version_id = p_concept ORDER BY recorded_at DESC, id DESC LIMIT 1;
  IF last.decision IS DISTINCT FROM 'retired'
     OR last.applicability->'superseded_by'->>'connector_version_id' IS DISTINCT FROM p_rolled_back::text THEN
    RAISE EXCEPTION 'restore: connector version % was not retired by supersession of %',
      p_connector_version, p_rolled_back;
  END IF;
  SELECT decision INTO sup FROM evidence.eligibility WHERE connector_version_id = p_rolled_back
     AND concept_version_id = p_concept ORDER BY recorded_at DESC, id DESC LIMIT 1;
  IF sup IS NULL OR sup = 'eligible' THEN
    RAISE EXCEPTION 'restore: superseding version % has not been rolled back', p_rolled_back;
  END IF;
  SELECT * INTO prev FROM evidence.eligibility WHERE connector_version_id = p_connector_version
     AND concept_version_id = p_concept AND decision = 'eligible' ORDER BY recorded_at DESC, id DESC LIMIT 1;
  IF prev.id IS NULL THEN
    RAISE EXCEPTION 'restore: connector version % was never approved', p_connector_version;
  END IF;
  PERFORM ops.require_review_activity(p_review_activity, NULL);
  SELECT a.actor INTO actor FROM meta.activity a WHERE a.id = p_review_activity;
  INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                                   policy_version, review_activity_id)
  VALUES (p_connector_version, p_concept, prev.applicability, 'eligible', prev.policy_version, p_review_activity)
  RETURNING id INTO eid;
  INSERT INTO ops.audit(actor, action, target, details)
  VALUES (actor, 'eligibility.restore', jsonb_build_object('eligibility_id', eid, 'restored_from', prev.id),
          jsonb_build_object('rolled_back_connector_version_id', p_rolled_back));
  RETURN eid;
END $$;

CREATE FUNCTION ontology.approve_mapping(p_proposal uuid, p_approval uuid, p_mapping_key text, p_version text,
                                         p_source_ref jsonb, p_target uuid, p_mapping_kind text,
                                         p_transform_spec jsonb, p_applicability jsonb, p_policy_version text,
                                         p_review_activity uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE scope jsonb; cref text; who text; mid uuid;
BEGIN
  IF p_mapping_key IS NULL OR p_version IS NULL OR length(p_mapping_key) > 300 OR length(p_version) > 100 THEN
    RAISE EXCEPTION 'invalid mapping key or version';
  END IF;
  IF octet_length(COALESCE(p_source_ref, 'null')::text) + octet_length(COALESCE(p_transform_spec, 'null')::text)
     + octet_length(COALESCE(p_applicability, 'null')::text) > 65536 THEN
    RAISE EXCEPTION 'mapping documents larger than 64 KiB';
  END IF;
  SELECT summary->'scope' INTO scope FROM ops.proposal WHERE id = p_proposal;
  SELECT concept_id || '@' || version INTO cref FROM ontology.concept_version WHERE id = p_target;
  IF cref IS NULL OR scope->>'concept' IS DISTINCT FROM cref THEN
    RAISE EXCEPTION 'proposal scope does not cover concept %', COALESCE(cref, p_target::text);
  END IF;
  IF scope->>'mapping_key' IS DISTINCT FROM p_mapping_key THEN
    RAISE EXCEPTION 'proposal scope does not cover mapping %', p_mapping_key;
  END IF;
  who := ops.consume_approval(p_proposal, p_approval, ARRAY['connector_promotion'], p_policy_version,
                              'mapping', jsonb_build_object('mapping_key', p_mapping_key, 'version', p_version));
  PERFORM ops.require_review_activity(p_review_activity, who);
  INSERT INTO ontology.mapping(mapping_key, source_ref, target_concept_id, mapping_kind, version, transform_spec,
                               applicability, approval, review_activity_id)
  VALUES (p_mapping_key, p_source_ref, p_target, p_mapping_kind, p_version, p_transform_spec, p_applicability,
          'approved', p_review_activity)
  RETURNING id INTO mid;
  INSERT INTO ops.audit(actor, action, target, details)
  VALUES (who, 'mapping.approve', jsonb_build_object('mapping_id', mid, 'proposal_id', p_proposal,
                                                     'approval_id', p_approval),
          jsonb_build_object('mapping_key', p_mapping_key, 'version', p_version));
  RETURN mid;
END $$;

CREATE FUNCTION model.approve_version(p_proposal uuid, p_approval uuid, p_model_key text, p_version text,
                                      p_kind text, p_spec_sha256 text, p_code_digest text, p_input_ports jsonb,
                                      p_output_ports jsonb, p_applicability jsonb, p_assumptions jsonb,
                                      p_provenance_activity uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE scope jsonb; who text; vid uuid;
BEGIN
  IF p_model_key IS NULL OR p_version IS NULL OR length(p_model_key) > 300 OR length(p_version) > 100 THEN
    RAISE EXCEPTION 'invalid model key or version';
  END IF;
  IF octet_length(COALESCE(p_input_ports, 'null')::text) + octet_length(COALESCE(p_output_ports, 'null')::text)
     + octet_length(COALESCE(p_applicability, 'null')::text) + octet_length(COALESCE(p_assumptions, 'null')::text)
     > 262144 THEN
    RAISE EXCEPTION 'model documents larger than 256 KiB';
  END IF;
  SELECT summary->'scope' INTO scope FROM ops.proposal WHERE id = p_proposal;
  IF scope ? 'model_spec_sha256' AND scope->>'model_spec_sha256' IS DISTINCT FROM p_spec_sha256 THEN
    RAISE EXCEPTION 'proposal scope names another model spec';
  END IF;
  who := ops.consume_approval(p_proposal, p_approval, ARRAY['model_registration'], NULL,
                              'model_version', jsonb_build_object('model_key', p_model_key, 'version', p_version,
                                                                  'spec_sha256', p_spec_sha256));
  PERFORM ops.require_review_activity(p_provenance_activity, NULL);
  INSERT INTO model.version(model_key, version, kind, spec_sha256, code_digest, input_ports, output_ports,
                            applicability, assumptions, approval_status, provenance_activity_id)
  VALUES (p_model_key, p_version, p_kind, p_spec_sha256, p_code_digest, p_input_ports, p_output_ports,
          p_applicability, p_assumptions, 'approved', p_provenance_activity)
  RETURNING id INTO vid;
  INSERT INTO ops.audit(actor, action, target, details)
  VALUES (who, 'model.approve', jsonb_build_object('model_version_id', vid, 'proposal_id', p_proposal,
                                                   'approval_id', p_approval),
          jsonb_build_object('model_key', p_model_key, 'version', p_version, 'spec_sha256', p_spec_sha256));
  RETURN vid;
END $$;

-- ALTER ... OWNER needs CREATE on the schema for the new owner (unless run by a
-- superuser); reva_registry keeps no CREATE privilege afterwards.
GRANT CREATE ON SCHEMA ops, evidence, ontology, model TO reva_registry;
ALTER FUNCTION ops.consume_approval(uuid,uuid,text[],text,text,jsonb) OWNER TO reva_registry;
ALTER FUNCTION ops.require_review_activity(uuid,text) OWNER TO reva_registry;
ALTER FUNCTION evidence.approve_eligibility(uuid,uuid,uuid,uuid,jsonb,text,uuid) OWNER TO reva_registry;
ALTER FUNCTION evidence.restore_eligibility(uuid,uuid,uuid,uuid) OWNER TO reva_registry;
ALTER FUNCTION ontology.approve_mapping(uuid,uuid,text,text,jsonb,uuid,text,jsonb,jsonb,text,uuid)
  OWNER TO reva_registry;
ALTER FUNCTION model.approve_version(uuid,uuid,text,text,text,text,text,jsonb,jsonb,jsonb,jsonb,uuid)
  OWNER TO reva_registry;
REVOKE CREATE ON SCHEMA ops, evidence, ontology, model FROM reva_registry;

REVOKE ALL ON FUNCTION meta.role_is_owner(name), meta.session_principal(), meta.is_human_identity(text),
  ops.consume_approval(uuid,uuid,text[],text,text,jsonb), ops.require_review_activity(uuid,text),
  evidence.approve_eligibility(uuid,uuid,uuid,uuid,jsonb,text,uuid),
  evidence.restore_eligibility(uuid,uuid,uuid,uuid),
  ontology.approve_mapping(uuid,uuid,text,text,jsonb,uuid,text,jsonb,jsonb,text,uuid),
  model.approve_version(uuid,uuid,text,text,text,text,text,jsonb,jsonb,jsonb,jsonb,uuid) FROM PUBLIC;
-- Helpers called from trigger bodies run as the inserting role.
GRANT EXECUTE ON FUNCTION meta.role_is_owner(name), meta.session_principal(), meta.is_human_identity(text)
  TO reva_api, reva_worker, reva_sim, reva_registry;
GRANT EXECUTE ON FUNCTION evidence.approve_eligibility(uuid,uuid,uuid,uuid,jsonb,text,uuid),
  evidence.restore_eligibility(uuid,uuid,uuid,uuid),
  ontology.approve_mapping(uuid,uuid,text,text,jsonb,uuid,text,jsonb,jsonb,text,uuid),
  model.approve_version(uuid,uuid,text,text,text,text,text,jsonb,jsonb,jsonb,jsonb,uuid) TO reva_worker;

-- reva_registry: exactly what the approval functions read and write.
GRANT USAGE ON SCHEMA meta, ontology, evidence, model, ops TO reva_registry;
GRANT SELECT ON ops.proposal, ops.proposal_review, ops.gate_result, ops.approval_use, meta.activity,
  ontology.concept_version, ontology.mapping, evidence.eligibility, model.version TO reva_registry;
GRANT INSERT ON ops.approval_use, ops.audit, evidence.eligibility, ontology.mapping, model.version
  TO reva_registry;
GRANT EXECUTE ON FUNCTION meta.artifact_is_verified(text), meta.inserted_in_current_xact(xid)
  TO reva_registry;
CREATE POLICY activity_registry_read ON meta.activity FOR SELECT TO reva_registry USING (true);
GRANT SELECT ON ops.approval_use TO reva_api, reva_worker;

-- (R5-06) The worker prunes only *expired* session revocations; deleting a
-- live one would un-revoke a stolen session.
ALTER TABLE ops.session_revocation ENABLE ROW LEVEL SECURITY;
CREATE POLICY revocation_read ON ops.session_revocation FOR SELECT TO reva_api, reva_worker USING (true);
CREATE POLICY revocation_api_insert ON ops.session_revocation FOR INSERT TO reva_api WITH CHECK (true);
CREATE POLICY revocation_worker_prune ON ops.session_revocation FOR DELETE TO reva_worker
  USING (expires_at < clock_timestamp());

-- (R5-10) Deleting immutable scientific history needs an explicit
-- SET ROLE reva_restricted_deletion (a deliberate, separately granted step)
-- plus a reviewed ticket. Implicit membership (a superuser, or the CREATEROLE
-- owner that created the role) is no longer enough.
CREATE OR REPLACE FUNCTION meta.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ticket text := current_setting('reva.reviewed_deletion_ticket', true);
BEGIN
  IF TG_OP = 'DELETE' AND ticket IS NOT NULL AND btrim(ticket) <> ''
     AND current_user = 'reva_restricted_deletion' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'Immutable scientific record %.%: append a new version or retraction',
    TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'P0001';
END;
$$;

-- (R5-11 hygiene, review note) functions created by later migrations are not
-- executable by PUBLIC unless granted explicitly.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
"""


def _governance_triggers() -> str:
    s = []
    for t in RECORDED_BY_TABLES:
        s.append(f"ALTER TABLE {t} ADD COLUMN recorded_by text NOT NULL DEFAULT session_user;")
        s.append(f"CREATE TRIGGER a01_recorded_by BEFORE INSERT ON {t} "
                 "FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();")
    # 'z' sorts after the existing BEFORE INSERT checks, so their more specific
    # errors (identity format, evaluation, policy version) are reported first.
    for t in GOVERNED_TABLES:
        s.append(f"CREATE TRIGGER z_governance BEFORE INSERT ON {t} "
                 "FOR EACH ROW EXECUTE FUNCTION meta.enforce_governance();")
    return "\n".join(s)


#: (R5-10) The restricted-deletion role holds the DELETE privilege itself (it is
#: used through an explicit SET ROLE). Runs last, so it covers every table and
#: row-security policy created by the migrations.
SQL_RESTRICTED_DELETION = r"""
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT n.nspname, c.relname, c.relrowsecurity FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','p') AND n.nspname IN ('meta','ontology','world','evidence','belief','model','sim','ops')
              AND EXISTS (SELECT 1 FROM pg_trigger g WHERE g.tgrelid = c.oid AND NOT g.tgisinternal
                          AND g.tgfoid = 'meta.reject_mutation()'::regprocedure) LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO reva_restricted_deletion', t.nspname);
    EXECUTE format('GRANT SELECT, DELETE ON %I.%I TO reva_restricted_deletion', t.nspname, t.relname);
    IF t.relrowsecurity THEN
      EXECUTE format('CREATE POLICY restricted_deletion ON %I.%I FOR ALL TO reva_restricted_deletion '
                     'USING (true)', t.nspname, t.relname);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_ROLES)
    op.execute(_grants())
    op.execute(SQL_RLS)
    # (R5-01, R5-06, R5-10) governance, approval functions, revocation pruning.
    op.execute(SQL_GOVERNANCE)
    op.execute(_governance_triggers())
    op.execute(immutable("ops.approval_use"))
    # (R-08) recorded_at is server time on every table that has it.
    op.execute(server_recorded_at(*ALL_SCHEMAS))
    op.execute(SQL_RESTRICTED_DELETION)


def downgrade() -> None:
    raise NotImplementedError("security migrations are forward-only")
