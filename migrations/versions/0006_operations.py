"""Operations: durable job queue with leases/fencing, schedules, dependency
invalidation, outbox, audit, budgets, proposals, gates, connector health, and the
narrow functions runtime roles use instead of broad table writes.

Revision ID: 0006_operations
Revises: 0005_science_population
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable

revision = "0006_operations"
down_revision = "0005_science_population"
branch_labels = None
depends_on = None

SQL = r"""
CREATE SEQUENCE ops.fencing_seq AS bigint;

-- Operational tables are mutable; scientific records are not.
CREATE TABLE ops.job (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_type text NOT NULL,
  payload jsonb NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN
    ('queued','running','succeeded','failed','cancel_requested','cancelled','dead')),
  priority integer NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
  available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  worker_id text,
  lease_until timestamptz,
  fencing_token bigint NOT NULL DEFAULT 0,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
  checkpoint jsonb,
  checkpoint_sha256 text REFERENCES meta.artifact,
  result_ref jsonb,
  last_error jsonb,
  progress jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by text NOT NULL DEFAULT 'system',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at timestamptz,
  CHECK (status <> 'running' OR (worker_id IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX job_pending ON ops.job(priority DESC, available_at, created_at) WHERE status = 'queued';
CREATE INDEX job_running_lease ON ops.job(lease_until) WHERE status IN ('running','cancel_requested');

CREATE TABLE ops.schedule (
  schedule_key text PRIMARY KEY,
  job_type text NOT NULL,
  parameters jsonb NOT NULL,
  cadence_spec jsonb NOT NULL CHECK ((cadence_spec->>'interval_seconds')::bigint >= 60),
  next_due_at timestamptz NOT NULL,
  last_slot timestamptz,
  enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE ops.dependency (
  upstream_key text NOT NULL,
  downstream_key text NOT NULL,
  version text NOT NULL,
  approximation_policy jsonb NOT NULL,
  PRIMARY KEY (upstream_key, downstream_key, version),
  CHECK (upstream_key <> downstream_key)
);
CREATE TABLE ops.dirty_node (
  node_key text PRIMARY KEY,
  since_sequence bigint NOT NULL,
  accumulated_error_bound jsonb NOT NULL,
  reason text NOT NULL DEFAULT 'upstream_changed',
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_type text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  processed_at timestamptz
);
CREATE INDEX outbox_pending ON ops.outbox(id) WHERE processed_at IS NULL;

CREATE TABLE ops.connector_cursor (
  connector_name text PRIMARY KEY,
  cursor jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Two-pass orphan GC for staged objects (never published source history).
CREATE TABLE ops.gc_candidate (
  storage_uri text PRIMARY KEY,
  first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  confirmed_scans integer NOT NULL DEFAULT 1
);

CREATE TABLE ops.audit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor text NOT NULL,
  action text NOT NULL,
  target jsonb NOT NULL,
  details jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.budget_ledger (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_key text NOT NULL,
  operation_id text NOT NULL UNIQUE,
  unit text NOT NULL,
  reserved numeric NOT NULL CHECK (reserved >= 0),
  consumed numeric CHECK (consumed >= 0),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.proposal (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_type text NOT NULL,
  manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  permission_tier integer NOT NULL CHECK (permission_tier BETWEEN 0 AND 3),
  summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  author text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.proposal_review (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_id uuid NOT NULL REFERENCES ops.proposal,
  decision text NOT NULL CHECK (decision IN ('approve','reject','request_changes')),
  reviewer text NOT NULL,
  comment text NOT NULL DEFAULT '',
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.gate_result (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_id uuid NOT NULL REFERENCES ops.proposal,
  gate_name text NOT NULL,
  policy_version text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('pass','fail','blocked')),
  evidence_sha256 text NOT NULL REFERENCES meta.artifact,
  evaluator text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  -- Gate results come from the separately controlled evaluator, never the proposer.
  CHECK (evaluator LIKE 'gate-evaluator:%')
);
CREATE FUNCTION ops.check_gate_evaluator() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM ops.proposal p WHERE p.id = NEW.proposal_id
             AND 'gate-evaluator:' || p.author = NEW.evaluator) THEN
    RAISE EXCEPTION 'the proposer cannot evaluate its own proposal';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER gate_independent BEFORE INSERT ON ops.gate_result
  FOR EACH ROW EXECUTE FUNCTION ops.check_gate_evaluator();

CREATE TABLE ops.connector_health (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  connector_version_id uuid NOT NULL REFERENCES evidence.connector_version,
  metrics jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('healthy','degraded','quarantined','unknown')),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.api_token (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_sha256 text NOT NULL UNIQUE,
  scopes text[] NOT NULL,
  label text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  revoked_at timestamptz
);

-- ---------------------------------------------------------------------------
-- Narrow SECURITY DEFINER functions. search_path is fixed; arguments validated.
-- ---------------------------------------------------------------------------
CREATE FUNCTION ops.submit_job(p_type text, p_payload jsonb, p_idempotency_key text,
                               p_priority integer, p_created_by text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE jid uuid;
BEGIN
  IF p_type NOT IN ('simulation','replay','discovery','ingest','evaluation','population','health') THEN
    RAISE EXCEPTION 'job type % cannot be submitted through the API', p_type;
  END IF;
  IF length(p_idempotency_key) < 8 OR length(p_idempotency_key) > 200 THEN
    RAISE EXCEPTION 'invalid idempotency key';
  END IF;
  IF p_priority < -10 OR p_priority > 10 THEN
    RAISE EXCEPTION 'priority out of API range';
  END IF;
  INSERT INTO ops.job(job_type, payload, idempotency_key, priority, created_by)
  VALUES (p_type, p_payload, p_idempotency_key, p_priority, p_created_by)
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id INTO jid;
  IF jid IS NULL THEN
    SELECT id INTO jid FROM ops.job WHERE idempotency_key = p_idempotency_key
      AND job_type = p_type AND payload = p_payload;
    IF jid IS NULL THEN
      RAISE EXCEPTION 'idempotency key reused with a different request';
    END IF;
  END IF;
  RETURN jid;
END $$;

CREATE FUNCTION ops.request_cancel(p_job uuid, p_actor text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM ops.job WHERE id = p_job FOR UPDATE;
  IF st IS NULL THEN RAISE EXCEPTION 'job not found'; END IF;
  IF st = 'queued' THEN
    UPDATE ops.job SET status = 'cancelled', finished_at = clock_timestamp(), updated_at = clock_timestamp() WHERE id = p_job;
    st := 'cancelled';
  ELSIF st = 'running' THEN
    UPDATE ops.job SET status = 'cancel_requested', updated_at = clock_timestamp() WHERE id = p_job;
    st := 'cancel_requested';
  END IF;
  INSERT INTO ops.audit(actor, action, target, details)
  VALUES (p_actor, 'job.cancel', jsonb_build_object('job_id', p_job), jsonb_build_object('status', st));
  RETURN st;
END $$;

-- Lock the job row and verify owner, fencing token and unexpired lease using
-- the database clock. Called inside the result-publication transaction.
CREATE FUNCTION ops.assert_lease(p_job uuid, p_worker text, p_token bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE j record;
BEGIN
  SELECT worker_id, fencing_token, lease_until, status INTO j FROM ops.job WHERE id = p_job FOR UPDATE;
  IF j IS NULL OR j.worker_id IS DISTINCT FROM p_worker OR j.fencing_token <> p_token
     OR j.lease_until IS NULL OR j.lease_until <= clock_timestamp()
     OR j.status NOT IN ('running','cancel_requested') THEN
    RAISE EXCEPTION 'stale lease for job % (token %)', p_job, p_token USING ERRCODE = 'RV001';
  END IF;
END $$;

CREATE FUNCTION ops.heartbeat(p_job uuid, p_worker text, p_token bigint, p_lease_seconds integer,
                              p_progress jsonb) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE st text;
BEGIN
  PERFORM ops.assert_lease(p_job, p_worker, p_token);
  UPDATE ops.job SET lease_until = clock_timestamp() + make_interval(secs => p_lease_seconds),
         progress = COALESCE(p_progress, progress), updated_at = clock_timestamp()
   WHERE id = p_job RETURNING status INTO st;
  RETURN st;
END $$;

CREATE FUNCTION ops.complete_job(p_job uuid, p_worker text, p_token bigint, p_status text,
                                 p_result jsonb, p_error jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF p_status NOT IN ('succeeded','failed','cancelled') THEN
    RAISE EXCEPTION 'invalid completion status %', p_status;
  END IF;
  PERFORM ops.assert_lease(p_job, p_worker, p_token);
  UPDATE ops.job SET status = p_status, result_ref = p_result, last_error = p_error,
         lease_until = NULL, finished_at = clock_timestamp(), updated_at = clock_timestamp()
   WHERE id = p_job;
END $$;

CREATE FUNCTION ops.review_proposal(p_proposal uuid, p_decision text, p_reviewer text, p_comment text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE rid uuid; tier integer; author text;
BEGIN
  SELECT permission_tier, p.author INTO tier, author FROM ops.proposal p WHERE id = p_proposal;
  IF tier IS NULL THEN RAISE EXCEPTION 'proposal not found'; END IF;
  IF author = p_reviewer THEN RAISE EXCEPTION 'authors cannot review their own proposals'; END IF;
  IF p_reviewer NOT LIKE 'human:%' THEN RAISE EXCEPTION 'reviews must be recorded by a named human reviewer'; END IF;
  INSERT INTO ops.proposal_review(proposal_id, decision, reviewer, comment)
  VALUES (p_proposal, p_decision, p_reviewer, left(p_comment, 4000)) RETURNING id INTO rid;
  INSERT INTO ops.audit(actor, action, target, details)
  VALUES (p_reviewer, 'proposal.review', jsonb_build_object('proposal_id', p_proposal),
          jsonb_build_object('decision', p_decision, 'tier', tier));
  RETURN rid;
END $$;

-- Immutable scenario definitions created by the API without object-store write
-- rights: canonical bytes are stored inline and verified by SHA-256 in-database.
CREATE FUNCTION sim.create_scenario(p_spec jsonb, p_canonical bytea, p_spec_digest text, p_created_by text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE sha text; ctx uuid; sid uuid; rel record;
BEGIN
  SELECT id INTO sid FROM sim.scenario WHERE spec_digest = p_spec_digest;
  IF sid IS NOT NULL THEN RETURN sid; END IF;
  IF convert_from(p_canonical, 'UTF8')::jsonb <> p_spec THEN
    RAISE EXCEPTION 'canonical bytes do not encode the scenario spec';
  END IF;
  SELECT r.id, r.origin INTO rel FROM belief.release r
    JOIN ops.publication p ON p.release_id = r.id
   WHERE r.id = (p_spec->>'baseline_release_id')::uuid;
  IF rel.id IS NULL THEN RAISE EXCEPTION 'baseline release is not published'; END IF;
  sha := encode(sha256(p_canonical), 'hex');
  INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights)
  VALUES (sha, 'application/json', octet_length(p_canonical), 'scenario-spec/' || (p_spec->>'schema_version'),
          'public_aggregate', '{"licence":"internal-derived"}')
  ON CONFLICT (sha256) DO NOTHING;
  INSERT INTO meta.artifact_inline(artifact_sha256, content) VALUES (sha, p_canonical)
  ON CONFLICT (artifact_sha256) DO NOTHING;
  INSERT INTO meta.context(mode, origin, manifest_sha256, knowledge_cutoff)
  VALUES ('simulation', rel.origin, sha, (p_spec->>'knowledge_cutoff')::timestamptz) RETURNING id INTO ctx;
  INSERT INTO sim.scenario(context_id, baseline_release_id, spec, spec_digest, spec_sha256, created_by)
  VALUES (ctx, rel.id, p_spec, p_spec_digest, sha, p_created_by) RETURNING id INTO sid;
  INSERT INTO ops.audit(actor, action, target, details)
  VALUES (p_created_by, 'scenario.create', jsonb_build_object('scenario_id', sid), jsonb_build_object('spec_sha256', sha));
  RETURN sid;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)
    op.execute(
        immutable(
            "ops.audit",
            "ops.proposal",
            "ops.proposal_review",
            "ops.gate_result",
            "ops.connector_health",
        )
    )


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
