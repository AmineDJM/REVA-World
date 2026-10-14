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
  spec_digest text NOT NULL UNIQUE,
  spec_sha256 text NOT NULL REFERENCES meta.artifact,
  created_by text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION sim.check_scenario() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM meta.require_context_mode(NEW.context_id, 'simulation');
  IF NOT EXISTS (SELECT 1 FROM ops.publication WHERE release_id = NEW.baseline_release_id) THEN
    RAISE EXCEPTION 'baseline release % is not published', NEW.baseline_release_id;
  END IF;
  IF NEW.spec->>'baseline_release_id' IS DISTINCT FROM NEW.baseline_release_id::text THEN
    RAISE EXCEPTION 'scenario spec baseline does not match pinned baseline';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER scenario_context BEFORE INSERT ON sim.scenario
  FOR EACH ROW EXECUTE FUNCTION sim.check_scenario();

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
