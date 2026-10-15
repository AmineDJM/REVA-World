"""Model evaluation/promotion, scenarios, runs and results with typed context boundaries.

Migration policy (review finding R-15): no database had been deployed when the
security review fixes were made, so the canonical migration set stays exactly
0001..0007 (the names the specification mandates) and these files were fixed in
place, including the ``cache_miss`` run-outcome status below. From the first
deployment on, an applied migration is never edited again: every later schema
change is a new revision (0008+). A database created from an earlier draft of
these files (e.g. a developer database) must be recreated, not upgraded.

Revision ID: 0004_models_sim
Revises: 0003_belief_graph
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable

revision = "0004_models_sim"
down_revision = "0003_belief_graph"
branch_labels = None
depends_on = None

SQL = r"""
CREATE TABLE model.evaluation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version_id uuid NOT NULL REFERENCES model.version,
  context_id uuid NOT NULL REFERENCES meta.context,
  evaluation_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  replay_mode text NOT NULL CHECK (replay_mode IN
    ('operational_as_known','available_then_reconstruction','revised_data_retrospective','analytic_verification')),
  task_scope jsonb NOT NULL,
  metrics jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION model.check_evaluation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM meta.require_context_mode(NEW.context_id, 'evaluation');
  RETURN NEW;
END $$;
CREATE TRIGGER evaluation_context BEFORE INSERT ON model.evaluation
  FOR EACH ROW EXECUTE FUNCTION model.check_evaluation();

CREATE TABLE model.promotion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version_id uuid NOT NULL REFERENCES model.version,
  task_scope jsonb NOT NULL,
  decision text NOT NULL CHECK (decision IN ('promote','reject','rollback','retire','keep_incumbent')),
  evaluation_id uuid NOT NULL REFERENCES model.evaluation,
  policy_version text NOT NULL,
  reviewer text NOT NULL,
  reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- (R-01, invariant 15) A promotion decision cites an evaluation of the same
-- model version; anything other than reject/keep_incumbent needs a replay-based
-- evaluation (analytic verification alone never promotes), a non-empty policy
-- version and a gate evaluator or named human reviewer. Applies to direct
-- inserts and to model.record_promotion.
CREATE FUNCTION model.check_promotion() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ev model.evaluation;
BEGIN
  SELECT * INTO ev FROM model.evaluation WHERE id = NEW.evaluation_id;
  IF ev.id IS NULL OR ev.model_version_id IS DISTINCT FROM NEW.model_version_id THEN
    RAISE EXCEPTION 'promotion of model version % must cite an evaluation of that model version',
      NEW.model_version_id;
  END IF;
  IF NEW.decision NOT IN ('reject','keep_incumbent') AND ev.replay_mode = 'analytic_verification' THEN
    RAISE EXCEPTION 'decision % requires a replay-based evaluation (not analytic_verification)', NEW.decision;
  END IF;
  IF NEW.reviewer !~ '^(gate-evaluator|human):[^[:space:]]+$' THEN
    RAISE EXCEPTION 'promotion reviewer must be gate-evaluator:<name> or human:<name>';
  END IF;
  IF btrim(NEW.policy_version) = '' THEN
    RAISE EXCEPTION 'promotion requires a policy version';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER promotion_checked BEFORE INSERT ON model.promotion
  FOR EACH ROW EXECUTE FUNCTION model.check_promotion();

-- (7) scenarios live in simulation contexts and pin a *published* baseline release.
CREATE TABLE sim.scenario (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_id uuid NOT NULL UNIQUE REFERENCES meta.context,
  parent_scenario_id uuid REFERENCES sim.scenario,
  baseline_release_id uuid NOT NULL REFERENCES belief.release,
  spec jsonb NOT NULL,
  -- Identity is the server-verified SHA-256 of the canonical spec bytes;
  -- spec_digest (the client's logical hash) is advisory metadata only.
  spec_digest text NOT NULL,
  spec_sha256 text NOT NULL UNIQUE REFERENCES meta.artifact,
  created_by text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX scenario_spec_digest ON sim.scenario(spec_digest);
CREATE FUNCTION sim.check_scenario() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM meta.require_context_mode(NEW.context_id, 'simulation');
  IF NOT EXISTS (SELECT 1 FROM ops.publication WHERE release_id = NEW.baseline_release_id) THEN
    RAISE EXCEPTION 'baseline release % is not published', NEW.baseline_release_id;
  END IF;
  IF NEW.spec->>'baseline_release_id' IS DISTINCT FROM NEW.baseline_release_id::text THEN
    RAISE EXCEPTION 'scenario spec baseline does not match pinned baseline';
  END IF;
  -- (R5-02) The stored spec must be exactly what its identity hashes. The
  -- canonical bytes live inline (CHECK sha256(content) = artifact_sha256), so
  -- this binds spec_sha256 -> verified bytes -> parsed jsonb = NEW.spec, and
  -- the artifact's recorded size to the bytes. Canonical JSON is not
  -- recomputed in SQL (float formatting); the server hashes the bytes instead.
  IF NOT EXISTS (
       SELECT 1 FROM meta.artifact_inline i JOIN meta.artifact a ON a.sha256 = i.artifact_sha256
        WHERE i.artifact_sha256 = NEW.spec_sha256
          AND a.byte_size = octet_length(i.content)
          AND convert_from(i.content, 'UTF8')::jsonb = NEW.spec) THEN
    RAISE EXCEPTION 'scenario spec does not match its verified canonical bytes (spec_sha256 %)', NEW.spec_sha256;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER scenario_context BEFORE INSERT ON sim.scenario
  FOR EACH ROW EXECUTE FUNCTION sim.check_scenario();

-- (R5-05) The simulation role registers artifact metadata only through this
-- function, never by INSERT: sha256 and byte size are computed here from the
-- bytes, the schema label is derived from the document's own "schema" field
-- (simulation result and model manifests only), and media type, privacy class
-- and rights are fixed. Nothing is caller-chosen, so a registration made with
-- simulation credentials can never carry a wrong size or a foreign label for
-- bytes that another writer (worker, API) registers later: the worker is not
-- blocked, and the metadata it finds is what it would have written itself.
CREATE FUNCTION sim.register_artifact(p_content bytea) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE doc jsonb; label text; sha text; size bigint; stored bigint;
BEGIN
  IF p_content IS NULL OR octet_length(p_content) > 33554432 THEN
    RAISE EXCEPTION 'simulation artifact content is missing or larger than 32 MiB';
  END IF;
  BEGIN
    doc := convert_from(p_content, 'UTF8')::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'simulation artifact is not a UTF-8 JSON document';
  END;
  IF jsonb_typeof(doc) <> 'object'
     OR COALESCE(doc->>'schema', '') !~ '^reva-(simulation-result|model-manifest)/[0-9]+$' THEN
    RAISE EXCEPTION 'the simulation role may register only simulation result and model manifests';
  END IF;
  label := substr(doc->>'schema', 6);
  sha := encode(sha256(p_content), 'hex');
  size := octet_length(p_content);
  INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights)
  VALUES (sha, 'application/json', size, label, 'public_aggregate', '{"licence":"internal-derived"}')
  ON CONFLICT (sha256) DO NOTHING;
  SELECT byte_size INTO stored FROM meta.artifact WHERE sha256 = sha;
  IF stored IS DISTINCT FROM size THEN
    RAISE EXCEPTION 'artifact % size mismatch (% vs %)', sha, stored, size;
  END IF;
  RETURN sha;
END $$;

CREATE TABLE sim.run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id uuid NOT NULL REFERENCES sim.scenario,
  fidelity text NOT NULL CHECK (fidelity IN ('L0','L1','L2','L3','L4')),
  model_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  ontology_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  idempotency_key text NOT NULL UNIQUE,
  seed_plan jsonb NOT NULL,
  execution_profile jsonb NOT NULL,
  job_id uuid,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Run outcome is an append-only record (status history lives in ops.job).
CREATE TABLE sim.run_outcome (
  run_id uuid PRIMARY KEY REFERENCES sim.run,
  status text NOT NULL CHECK (status IN ('succeeded','failed','unsupported','missing_input','cache_miss')),
  output_manifest_sha256 text REFERENCES meta.artifact,
  output_digest text,
  applicability jsonb NOT NULL,
  problems jsonb NOT NULL DEFAULT '[]'::jsonb,
  cost jsonb NOT NULL,
  cache_hit_of uuid REFERENCES sim.run,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (status <> 'succeeded' OR output_digest IS NOT NULL)
);

CREATE TABLE sim.result (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES sim.run,
  output_key text NOT NULL,
  subject_id uuid REFERENCES world.entity,
  variant text NOT NULL DEFAULT 'L1',
  value jsonb NOT NULL CHECK (jsonb_typeof(value) = 'object' AND value ? 'kind'),
  uncertainty jsonb NOT NULL CHECK (uncertainty ? 'status'),
  applicability jsonb NOT NULL,
  artifact_sha256 text REFERENCES meta.artifact,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  UNIQUE (run_id, output_key, subject_id, variant)
);
"""


def upgrade() -> None:
    op.execute(SQL)
    op.execute(
        immutable(
            "model.evaluation",
            "model.promotion",
            "sim.scenario",
            "sim.run",
            "sim.run_outcome",
            "sim.result",
        )
    )


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
