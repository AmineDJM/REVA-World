"""Security: separate NOINHERIT runtime roles, precise grants, row security on
shared provenance tables. Passwords/LOGIN are set by scripts/bootstrap_runtime.py.

A WHERE clause is not a security boundary: these grants are.

Revision ID: 0007_security
Revises: 0006_operations
"""

from alembic import op

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
    ELSE
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
        "meta.require_context_mode(uuid,text), evidence.is_eligible(uuid,uuid,uuid,timestamptz) "
        "TO reva_api, reva_worker, reva_sim;"
    )
    s.append("GRANT USAGE ON SCHEMA public TO reva_api, reva_worker, reva_sim;")

    # --- reva_api: read approved projections; writes only via narrow functions.
    for sch in SCIENTIFIC:
        s.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO reva_api;")
    s.append(
        "GRANT SELECT ON ops.job, ops.schedule, ops.publication, ops.connector_health, ops.proposal, "
        "ops.proposal_review, ops.gate_result, ops.api_token, ops.budget_ledger, ops.dirty_node, "
        "ops.connector_cursor TO reva_api;"
    )
    s.append("GRANT INSERT ON ops.audit TO reva_api;")
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
    s.append(
        "GRANT UPDATE ON ops.job, ops.schedule, ops.dirty_node, ops.outbox, ops.connector_cursor, "
        "ops.gc_candidate, ops.budget_ledger TO reva_worker;"
    )
    s.append("GRANT DELETE ON ops.dirty_node, ops.gc_candidate TO reva_worker;")
    s.append("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO reva_worker;")
    s.append(
        "GRANT EXECUTE ON FUNCTION ops.assert_lease(uuid,text,bigint), "
        "ops.heartbeat(uuid,text,bigint,integer,jsonb), "
        "ops.complete_job(uuid,text,bigint,text,jsonb,jsonb), "
        "ops.submit_job(text,jsonb,text,integer,text) TO reva_worker;"
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
CREATE POLICY location_sim_insert ON meta.artifact_location FOR INSERT TO reva_sim
  WITH CHECK (namespace IN ('scenarios','evaluation'));
CREATE POLICY location_worker_insert ON meta.artifact_location FOR INSERT TO reva_worker
  WITH CHECK (namespace <> 'scenarios');
"""


def upgrade() -> None:
    op.execute(SQL_ROLES)
    op.execute(_grants())
    op.execute(SQL_RLS)


def downgrade() -> None:
    raise NotImplementedError("security migrations are forward-only")
