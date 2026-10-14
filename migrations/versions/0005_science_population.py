"""Scientific evidence (studies, versioned claims/cards, effects), population
datasets/diagnostics and compression certificates. Extends belief.input with
reviewed claims.

Revision ID: 0005_science_population
Revises: 0004_models_sim
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable

revision = "0005_science_population"
down_revision = "0004_models_sim"
branch_labels = None
depends_on = None

SQL = r"""
CREATE TABLE evidence.study (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  doi text,
  metadata jsonb NOT NULL,
  source_artifact_sha256 text REFERENCES meta.artifact,
  acquisition_id uuid REFERENCES evidence.acquisition,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX study_doi ON evidence.study(lower(doi));

CREATE TABLE evidence.claim (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_key text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  card jsonb NOT NULL,
  evidence_kind text NOT NULL CHECK (evidence_kind IN
    ('experimental','quasi_experimental','observational','mechanistic',
     'structural_calibration','expert_judgment','hypothesis')),
  status text NOT NULL CHECK (status IN ('candidate','reviewed','rejected','retracted')),
  extraction_method text NOT NULL,
  supersedes_id uuid REFERENCES evidence.claim,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (claim_key, version),
  -- An LLM/automated extraction is candidate-only; review is a separate record.
  CHECK (extraction_method NOT LIKE 'llm%' OR status IN ('candidate','rejected'))
);

CREATE TABLE evidence.effect (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id uuid NOT NULL REFERENCES evidence.claim,
  study_id uuid NOT NULL REFERENCES evidence.study,
  estimand jsonb NOT NULL,
  estimate jsonb NOT NULL,
  uncertainty jsonb NOT NULL,
  dependency_family text NOT NULL,
  source_span jsonb NOT NULL CHECK (jsonb_typeof(source_span) = 'object' AND source_span <> '{}'::jsonb),
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity
);

ALTER TABLE belief.input ADD COLUMN claim_id uuid REFERENCES evidence.claim;
ALTER TABLE belief.input DROP CONSTRAINT input_exactly_one;
ALTER TABLE belief.input ADD CONSTRAINT input_exactly_one
  CHECK (num_nonnulls(observation_id, prior_estimate_id, claim_id) = 1);

CREATE OR REPLACE FUNCTION belief.check_input_claim() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE st text; rec timestamptz; cutoff timestamptz;
BEGIN
  IF NEW.claim_id IS NULL THEN RETURN NEW; END IF;
  SELECT status, recorded_at INTO st, rec FROM evidence.claim WHERE id = NEW.claim_id;
  SELECT rel.knowledge_cutoff INTO cutoff FROM belief.estimate e JOIN belief.release rel ON rel.id = e.release_id
   WHERE e.id = NEW.estimate_id;
  IF st <> 'reviewed' THEN
    RAISE EXCEPTION 'only reviewed claims may inform beliefs (claim status %)', st;
  END IF;
  IF rec > cutoff THEN
    RAISE EXCEPTION 'claim recorded after the release cutoff';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER input_claim BEFORE INSERT ON belief.input
  FOR EACH ROW EXECUTE FUNCTION belief.check_input_claim();

-- Recreate the generic input trigger to skip the claim path.
CREATE OR REPLACE FUNCTION belief.check_input() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r belief.release; o evidence.observation; prior_cutoff timestamptz; ref_year integer;
BEGIN
  IF NEW.claim_id IS NOT NULL THEN RETURN NEW; END IF;
  SELECT rel.* INTO r FROM belief.estimate e JOIN belief.release rel ON rel.id = e.release_id
   WHERE e.id = NEW.estimate_id;
  IF NEW.observation_id IS NOT NULL THEN
    SELECT * INTO o FROM evidence.observation WHERE id = NEW.observation_id;
    IF o.recorded_at > r.knowledge_cutoff THEN
      RAISE EXCEPTION 'observation % recorded after release cutoff', o.id;
    END IF;
    -- (R-07) eligibility is evaluated inside the decision's scope: subject and the
    -- support year (UTC year of lower(valid_range); 0 = unknown, which no
    -- years-scoped decision covers, so it fails closed).
    SELECT CASE WHEN sp.valid_range IS NULL OR lower_inf(sp.valid_range) OR NOT isfinite(lower(sp.valid_range))
                THEN 0 ELSE extract(year FROM lower(sp.valid_range) AT TIME ZONE 'UTC')::int END
      INTO ref_year FROM world.support sp WHERE sp.id = o.support_id;
    IF NOT evidence.is_eligible(o.connector_version_id, o.concept_version_id, o.subject_id, r.knowledge_cutoff,
                                COALESCE(ref_year, 0)) THEN
      RAISE EXCEPTION 'observation % is not eligible for concept % under connector %',
        o.id, o.concept_version_id, o.connector_version_id;
    END IF;
    IF EXISTS (SELECT 1 FROM evidence.observation_retraction t
               WHERE t.observation_id = o.id AND t.recorded_at <= r.knowledge_cutoff) THEN
      RAISE EXCEPTION 'observation % was retracted before the cutoff', o.id;
    END IF;
    -- observation.origin equals its acquisition's origin (evidence.check_revision)
    IF r.origin = 'live' AND o.origin <> 'live' THEN
      RAISE EXCEPTION 'fixture-origin observation cannot enter a live release';
    END IF;
  ELSE
    SELECT rel.knowledge_cutoff INTO prior_cutoff FROM belief.estimate e
      JOIN belief.release rel ON rel.id = e.release_id WHERE e.id = NEW.prior_estimate_id;
    IF prior_cutoff > r.knowledge_cutoff THEN
      RAISE EXCEPTION 'prior estimate comes from a later knowledge cutoff';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- Population recipes and materialisations (records are Parquet artifacts,
-- never PostgreSQL rows).
CREATE TABLE model.population_dataset (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_id uuid NOT NULL UNIQUE REFERENCES meta.context,
  recipe_sha256 text NOT NULL REFERENCES meta.artifact,
  materialisation_sha256 text REFERENCES meta.artifact,
  supported_attributes jsonb NOT NULL,
  record_count integer CHECK (record_count IS NULL OR record_count BETWEEN 1 AND 100000),
  diagnostics jsonb NOT NULL,
  seed_plan jsonb NOT NULL,
  privacy_class text NOT NULL CHECK (privacy_class = 'synthetic_public_aggregate_derived'),
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION model.check_population() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM meta.require_context_mode(NEW.context_id, 'population');
  RETURN NEW;
END $$;
CREATE TRIGGER population_context BEFORE INSERT ON model.population_dataset
  FOR EACH ROW EXECUTE FUNCTION model.check_population();

CREATE TABLE model.compression_certificate (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  population_dataset_id uuid NOT NULL REFERENCES model.population_dataset,
  operation text NOT NULL CHECK (operation IN ('coarsen','refine','stratify')),
  task_scope jsonb NOT NULL,
  certificate jsonb NOT NULL,
  output_sha256 text NOT NULL REFERENCES meta.artifact,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""


def upgrade() -> None:
    op.execute(SQL)
    op.execute(
        immutable(
            "evidence.study",
            "evidence.claim",
            "evidence.effect",
            "model.population_dataset",
            "model.compression_certificate",
        )
    )


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
