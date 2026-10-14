-- REVA logical reference schema, PostgreSQL 17 + PostGIS.
-- Implement as the seven Alembic migrations in the architecture document.
-- This is a concrete baseline, not the completed application/security layer.
-- Runtime login creation, passwords, narrow SECURITY DEFINER functions,
-- publication validation and per-role grants must be implemented and tested.
BEGIN;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS meta;
CREATE SCHEMA IF NOT EXISTS ontology;
CREATE SCHEMA IF NOT EXISTS world;
CREATE SCHEMA IF NOT EXISTS evidence;
CREATE SCHEMA IF NOT EXISTS belief;
CREATE SCHEMA IF NOT EXISTS model;
CREATE SCHEMA IF NOT EXISTS sim;
CREATE SCHEMA IF NOT EXISTS ops;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE TABLE meta.artifact (
  sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  media_type text NOT NULL,
  byte_size bigint NOT NULL CHECK (byte_size >= 0),
  schema_version text NOT NULL,
  privacy_class text NOT NULL CHECK (privacy_class IN
    ('public_aggregate','synthetic_public_aggregate_derived','restricted_microdata','sensitive_restricted')),
  rights jsonb NOT NULL CHECK (jsonb_typeof(rights) = 'object'),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE meta.artifact_location (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  storage_uri text NOT NULL,
  namespace text NOT NULL CHECK (namespace IN
    ('raw','normalised','belief','models','scenarios','evaluation','quarantine')),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (artifact_sha256, storage_uri)
);
CREATE TABLE meta.activity (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL,
  origin text NOT NULL CHECK (origin IN
    ('source','normalisation','belief','model','simulation','evaluation','review')),
  code_digest text NOT NULL,
  parameters jsonb NOT NULL,
  environment_digest text NOT NULL,
  actor text NOT NULL,
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (ended_at >= started_at)
);
CREATE TABLE meta.activity_input (
  activity_id uuid NOT NULL REFERENCES meta.activity,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  selector jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (activity_id, ordinal)
);
CREATE TABLE meta.activity_output (
  activity_id uuid NOT NULL REFERENCES meta.activity,
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  PRIMARY KEY (activity_id, artifact_sha256)
);
CREATE TABLE meta.context (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mode text NOT NULL CHECK (mode IN
    ('evidence','belief','simulation','evaluation','population','quarantine')),
  parent_context_id uuid REFERENCES meta.context,
  manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  knowledge_cutoff timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (parent_context_id IS NULL OR parent_context_id <> id)
);

CREATE TABLE ontology.concept_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id text NOT NULL,
  version text NOT NULL,
  kind text NOT NULL CHECK (kind IN
    ('entity_type','quantity','predicate','role','category','unit','component')),
  definition text NOT NULL,
  semantic_profile jsonb NOT NULL,
  schema_sha256 text REFERENCES meta.artifact,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  UNIQUE (concept_id, version)
);
CREATE TABLE ontology.mapping (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_ref jsonb NOT NULL,
  target_concept_id uuid NOT NULL REFERENCES ontology.concept_version,
  mapping_kind text NOT NULL CHECK (mapping_kind IN
    ('exact','narrower','broader','overlap','weighted_crosswalk','approximate_transform','incompatible')),
  version text NOT NULL,
  transform_spec jsonb NOT NULL,
  applicability jsonb NOT NULL,
  review_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE world.entity (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind_concept_id uuid NOT NULL REFERENCES ontology.concept_version,
  namespace text NOT NULL,
  referent_mode text NOT NULL CHECK (referent_mode IN ('real','abstract')),
  label text NOT NULL,
  created_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE world.alias (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid NOT NULL REFERENCES world.entity,
  provider text NOT NULL,
  external_key text NOT NULL,
  valid_range daterange,
  mapping_version text NOT NULL,
  confidence_kind text NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX alias_lookup ON world.alias(provider, external_key, recorded_at);
CREATE TABLE world.support (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  temporal_extent jsonb NOT NULL,
  valid_range tstzrange,
  spatial_spec jsonb NOT NULL,
  geometry_4326 geometry(Geometry,4326),
  population_spec jsonb NOT NULL,
  aggregation text NOT NULL,
  resolution jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  support_digest text NOT NULL UNIQUE,
  CHECK (valid_range IS NULL OR NOT isempty(valid_range))
);
CREATE INDEX support_geometry ON world.support USING gist(geometry_4326);
CREATE INDEX support_time ON world.support USING gist(valid_range);

CREATE TABLE evidence.source (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_key text NOT NULL UNIQUE,
  title text NOT NULL,
  provider_url text NOT NULL,
  lineage_family text NOT NULL,
  rights jsonb NOT NULL,
  metadata jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity
);
CREATE TABLE evidence.connector_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  name text NOT NULL,
  version text NOT NULL,
  spec_sha256 text NOT NULL REFERENCES meta.artifact,
  code_digest text NOT NULL,
  declared_capabilities jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (name, version)
);
CREATE TABLE evidence.source_metadata_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  version integer NOT NULL CHECK (version>0),
  metadata jsonb NOT NULL,
  rights jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (source_id,version)
);
CREATE TABLE evidence.acquisition (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  connector_version_id uuid NOT NULL REFERENCES evidence.connector_version,
  source_metadata_version_id uuid NOT NULL REFERENCES evidence.source_metadata_version,
  request_fingerprint text NOT NULL,
  raw_sha256 text NOT NULL REFERENCES meta.artifact,
  retrieved_at timestamptz NOT NULL,
  response_metadata jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  idempotency_key text NOT NULL UNIQUE,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE evidence.observation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  connector_version_id uuid NOT NULL REFERENCES evidence.connector_version,
  acquisition_id uuid NOT NULL REFERENCES evidence.acquisition,
  logical_key text NOT NULL,
  revision_no integer NOT NULL CHECK (revision_no > 0),
  source_revision_id text,
  supersedes_id uuid REFERENCES evidence.observation,
  subject_id uuid NOT NULL REFERENCES world.entity,
  concept_version_id uuid NOT NULL REFERENCES ontology.concept_version,
  support_id uuid NOT NULL REFERENCES world.support,
  value jsonb NOT NULL,
  uncertainty jsonb NOT NULL,
  source_flags jsonb NOT NULL,
  observed_at timestamptz,
  source_published_at timestamptz,
  source_available_at timestamptz,
  availability_basis jsonb,
  raw_selector jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  semantic_hash text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (source_id, logical_key, revision_no),
  UNIQUE (acquisition_id, logical_key, connector_version_id),
  CHECK (supersedes_id IS NULL OR supersedes_id <> id),
  CHECK (source_available_at IS NULL OR availability_basis IS NOT NULL),
  CHECK (jsonb_typeof(value) = 'object'),
  CHECK (uncertainty ? 'status')
);
CREATE INDEX observation_revision_lookup
  ON evidence.observation(source_id,logical_key,recorded_at DESC,revision_no DESC);
CREATE INDEX observation_series
  ON evidence.observation(subject_id,concept_version_id,support_id);
CREATE TABLE evidence.observation_retraction (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  observation_id uuid NOT NULL REFERENCES evidence.observation,
  reason text NOT NULL,
  activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE evidence.eligibility (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  connector_version_id uuid NOT NULL REFERENCES evidence.connector_version,
  concept_version_id uuid NOT NULL REFERENCES ontology.concept_version,
  applicability jsonb NOT NULL,
  decision text NOT NULL CHECK (decision IN ('eligible','quarantine','retired')),
  policy_version text NOT NULL,
  review_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE evidence.study (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  doi text,
  metadata jsonb NOT NULL,
  source_artifact_sha256 text REFERENCES meta.artifact,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE evidence.claim (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_key text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  card jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('candidate','reviewed','rejected','retracted')),
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (claim_key,version)
);
CREATE TABLE evidence.effect (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id uuid NOT NULL REFERENCES evidence.claim,
  study_id uuid NOT NULL REFERENCES evidence.study,
  estimand jsonb NOT NULL,
  estimate jsonb NOT NULL,
  uncertainty jsonb NOT NULL,
  dependency_family text NOT NULL,
  source_span jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity
);

CREATE TABLE model.version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_key text NOT NULL,
  version text NOT NULL,
  spec_sha256 text NOT NULL REFERENCES meta.artifact,
  code_digest text NOT NULL,
  input_ports jsonb NOT NULL,
  output_ports jsonb NOT NULL,
  applicability jsonb NOT NULL,
  assumptions jsonb NOT NULL,
  training_manifest_sha256 text REFERENCES meta.artifact,
  training_cutoff timestamptz,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (model_key,version)
);
CREATE TABLE belief.release (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_id uuid NOT NULL UNIQUE REFERENCES meta.context,
  manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  ontology_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  model_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  knowledge_cutoff timestamptz NOT NULL,
  method text NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE belief.estimate (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  release_id uuid NOT NULL REFERENCES belief.release,
  subject_id uuid NOT NULL REFERENCES world.entity,
  concept_version_id uuid NOT NULL REFERENCES ontology.concept_version,
  support_id uuid NOT NULL REFERENCES world.support,
  method_version_id uuid NOT NULL REFERENCES model.version,
  modality text NOT NULL CHECK (modality IN ('selected_report','estimated_state','forecast')),
  value jsonb NOT NULL,
  uncertainty jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  UNIQUE (release_id,subject_id,concept_version_id,support_id,modality)
);
CREATE TABLE belief.input (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  estimate_id uuid NOT NULL REFERENCES belief.estimate,
  observation_id uuid REFERENCES evidence.observation,
  prior_estimate_id uuid REFERENCES belief.estimate,
  claim_id uuid REFERENCES evidence.claim,
  CHECK (num_nonnulls(observation_id,prior_estimate_id,claim_id)=1),
  CHECK (prior_estimate_id IS NULL OR prior_estimate_id<>estimate_id)
);
CREATE TABLE ops.publication (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  release_id uuid NOT NULL UNIQUE REFERENCES belief.release,
  published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  activity_id uuid NOT NULL REFERENCES meta.activity
);
CREATE TABLE world.relation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  predicate_id uuid NOT NULL REFERENCES ontology.concept_version,
  layer text NOT NULL,
  support_id uuid NOT NULL REFERENCES world.support,
  release_id uuid REFERENCES belief.release,
  evidence_observation_id uuid REFERENCES evidence.observation,
  existence_uncertainty jsonb NOT NULL,
  strength jsonb,
  component jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  CHECK (num_nonnulls(release_id,evidence_observation_id)=1)
);
CREATE TABLE world.relation_participant (
  relation_id uuid NOT NULL REFERENCES world.relation,
  ordinal integer NOT NULL CHECK (ordinal>=0),
  role_concept_id uuid NOT NULL REFERENCES ontology.concept_version,
  entity_id uuid NOT NULL REFERENCES world.entity,
  direction text NOT NULL CHECK (direction IN ('in','out','undirected')),
  PRIMARY KEY (relation_id,ordinal)
);
CREATE INDEX participant_lookup ON world.relation_participant(entity_id,role_concept_id,relation_id);
CREATE TABLE world.occurrence (
  entity_id uuid PRIMARY KEY REFERENCES world.entity,
  event_type_id uuid NOT NULL REFERENCES ontology.concept_version,
  support_id uuid NOT NULL REFERENCES world.support,
  component jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity
);

CREATE TABLE sim.scenario (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_id uuid NOT NULL UNIQUE REFERENCES meta.context,
  parent_scenario_id uuid REFERENCES sim.scenario,
  baseline_release_id uuid NOT NULL REFERENCES belief.release,
  spec jsonb NOT NULL,
  spec_digest text NOT NULL UNIQUE,
  created_by text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE sim.run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id uuid NOT NULL REFERENCES sim.scenario,
  model_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  idempotency_key text NOT NULL UNIQUE,
  seed_plan jsonb NOT NULL,
  execution_profile jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE sim.result (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES sim.run,
  output_key text NOT NULL,
  value jsonb NOT NULL,
  uncertainty jsonb NOT NULL,
  applicability jsonb NOT NULL,
  artifact_sha256 text REFERENCES meta.artifact,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  UNIQUE (run_id,output_key)
);
CREATE TABLE model.evaluation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version_id uuid NOT NULL REFERENCES model.version,
  evaluation_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  replay_mode text NOT NULL CHECK (replay_mode IN
    ('operational_as_known','available_then_reconstruction','revised_data_retrospective','analytic_verification')),
  metrics jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE model.promotion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version_id uuid NOT NULL REFERENCES model.version,
  task_scope jsonb NOT NULL,
  decision text NOT NULL CHECK (decision IN ('promote','reject','rollback','retire')),
  evaluation_id uuid NOT NULL REFERENCES model.evaluation,
  policy_version text NOT NULL,
  reviewer text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Operational tables are mutable; scientific records above are not.
CREATE TABLE ops.job (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_type text NOT NULL,
  payload jsonb NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  status text NOT NULL CHECK (status IN
    ('queued','running','succeeded','failed','cancel_requested','cancelled','dead')),
  priority integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  worker_id text,
  lease_until timestamptz,
  fencing_token bigint NOT NULL DEFAULT 0,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0),
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts>0),
  checkpoint_sha256 text REFERENCES meta.artifact,
  result_ref jsonb,
  last_error jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX job_pending ON ops.job(priority DESC,available_at,created_at)
  WHERE status='queued';
CREATE TABLE ops.schedule (
  schedule_key text PRIMARY KEY,
  job_type text NOT NULL,
  parameters jsonb NOT NULL,
  cadence_spec jsonb NOT NULL,
  next_due_at timestamptz NOT NULL,
  enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE ops.dependency (
  upstream_key text NOT NULL,
  downstream_key text NOT NULL,
  version text NOT NULL,
  approximation_policy jsonb NOT NULL,
  PRIMARY KEY (upstream_key,downstream_key,version)
);
CREATE TABLE ops.dirty_node (
  node_key text PRIMARY KEY,
  since_sequence bigint NOT NULL,
  accumulated_error_bound jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
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
  reserved numeric NOT NULL CHECK(reserved>=0),
  consumed numeric CHECK(consumed>=0),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.proposal (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_type text NOT NULL,
  manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  permission_tier integer NOT NULL CHECK(permission_tier BETWEEN 0 AND 3),
  author text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.gate_result (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_id uuid NOT NULL REFERENCES ops.proposal,
  gate_name text NOT NULL,
  policy_version text NOT NULL,
  decision text NOT NULL CHECK(decision IN ('pass','fail','blocked')),
  evidence_sha256 text NOT NULL REFERENCES meta.artifact,
  evaluator text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.connector_health (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  connector_version_id uuid NOT NULL REFERENCES evidence.connector_version,
  metrics jsonb NOT NULL,
  status text NOT NULL CHECK(status IN ('healthy','degraded','quarantined','unknown')),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION meta.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Immutable scientific record: append a new version or retraction';
END;
$$;
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT table_schema,table_name FROM information_schema.tables
    WHERE table_schema IN ('meta','ontology','world','evidence','belief','model','sim')
      AND table_type='BASE TABLE'
  LOOP
    EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON %I.%I FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation()',r.table_schema,r.table_name);
  END LOOP;
END;
$$;
-- Also protect audit/publication/proposal/gate records against update/delete.
CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON ops.audit
  FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation();
CREATE TRIGGER immutable_publication BEFORE UPDATE OR DELETE ON ops.publication
  FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation();
CREATE TRIGGER immutable_proposal BEFORE UPDATE OR DELETE ON ops.proposal
  FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation();
CREATE TRIGGER immutable_gate BEFORE UPDATE OR DELETE ON ops.gate_result
  FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation();
COMMIT;

