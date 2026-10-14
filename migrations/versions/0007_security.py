"""Security: separate NOINHERIT runtime roles, precise grants, row security on
shared provenance tables. Passwords/LOGIN are set by scripts/bootstrap_runtime.py.

A WHERE clause is not a security boundary: these grants are.

Revision ID: 0007_security
Revises: 0006_operations
"""

from alembic import op

from reva.adapters.postgres.ddl import server_recorded_at

revision = "0007_security"
down_revision = "0006_operations"
branch_labels = None
depends_on = None

SCIENTIFIC = ("meta", "ontology", "world", "evidence", "belief", "model", "sim")
ALL_SCHEMAS = (*SCIENTIFIC, "ops")

SQL_ROLES = r"""
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['reva_api','reva_worker','reva_sim'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION', r);
    ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r AND (rolinherit OR rolsuper OR rolcreatedb
                  OR rolcreaterole OR rolbypassrls OR rolreplication)) THEN
      -- Roles are cluster-global; only touch the catalog row when needed so parallel
      -- bootstraps of other databases do not race on it ("tuple concurrently updated").
      EXECUTE format('ALTER ROLE %I NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION', r);
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
        "meta.require_context_mode(uuid,text), evidence.is_eligible(uuid,uuid,uuid,timestamptz,integer), "
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
    # and promotions keep a direct grant for now, guarded by BEFORE INSERT
    # triggers; ops.record_gate_result / model.record_promotion are the
    # checked path callers should migrate to.
    s.append("REVOKE INSERT ON ops.api_token, ops.proposal_review, ops.session_revocation FROM reva_worker;")
    s.append(
        "GRANT UPDATE ON ops.job, ops.schedule, ops.dirty_node, ops.outbox, ops.connector_cursor, "
        "ops.gc_candidate, ops.budget_ledger, ops.worker_presence TO reva_worker;"
    )
    s.append("GRANT DELETE ON ops.dirty_node, ops.gc_candidate, ops.session_revocation TO reva_worker;")
    s.append("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO reva_worker;")
    s.append(
        "GRANT EXECUTE ON FUNCTION ops.assert_lease(uuid,text,bigint), "
        "ops.heartbeat(uuid,text,bigint,integer,jsonb), "
        "ops.complete_job(uuid,text,bigint,text,jsonb,jsonb), "
        "ops.submit_job(text,jsonb,text,integer,text), "
        "ops.record_gate_result(uuid,text,text,text,text,text), "
        "model.record_promotion(uuid,jsonb,text,uuid,text,text,jsonb) TO reva_worker;"
    )

    # --- reva_sim: read pinned inputs; write only sim/evaluation outputs.
    for sch in SCIENTIFIC:
        s.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO reva_sim;")
    s.append("GRANT SELECT ON ops.publication TO reva_sim;")
    s.append("GRANT INSERT ON ALL TABLES IN SCHEMA sim TO reva_sim;")
    s.append(
        "GRANT INSERT ON meta.artifact, meta.artifact_location, meta.activity, meta.activity_input, "
        "meta.activity_output, meta.context, model.evaluation TO reva_sim;"
    )
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
CREATE POLICY activity_sim_insert ON meta.activity FOR INSERT TO reva_sim
  WITH CHECK (origin IN ('simulation','evaluation','model'));
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
-- to simulation/evaluation/model activities (never to source, normalisation,
-- belief or review activities); the worker never to simulation activities.
ALTER TABLE meta.activity_input ENABLE ROW LEVEL SECURITY;
CREATE POLICY activity_input_read ON meta.activity_input FOR SELECT TO reva_api, reva_worker, reva_sim
  USING (true);
CREATE POLICY activity_input_sim_insert ON meta.activity_input FOR INSERT TO reva_sim
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_input.activity_id)
              IN ('simulation','evaluation','model'));
CREATE POLICY activity_input_worker_insert ON meta.activity_input FOR INSERT TO reva_worker
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_input.activity_id) <> 'simulation');

ALTER TABLE meta.activity_output ENABLE ROW LEVEL SECURITY;
CREATE POLICY activity_output_read ON meta.activity_output FOR SELECT TO reva_api, reva_worker, reva_sim
  USING (true);
CREATE POLICY activity_output_sim_insert ON meta.activity_output FOR INSERT TO reva_sim
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_output.activity_id)
              IN ('simulation','evaluation','model'));
CREATE POLICY activity_output_worker_insert ON meta.activity_output FOR INSERT TO reva_worker
  WITH CHECK ((SELECT a.origin FROM meta.activity a WHERE a.id = activity_output.activity_id) <> 'simulation');
"""


def upgrade() -> None:
    op.execute(SQL_ROLES)
    op.execute(_grants())
    op.execute(SQL_RLS)
    # (R-08) recorded_at is server time on every table that has it.
    op.execute(server_recorded_at(*ALL_SCHEMAS))


def downgrade() -> None:
    raise NotImplementedError("security migrations are forward-only")
