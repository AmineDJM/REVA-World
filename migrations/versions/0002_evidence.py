"""Evidence: sources, metadata versions, connectors, acquisitions, observations,
revision lineage, retractions and concept-specific eligibility.

Revision ID: 0002_evidence
Revises: 0001_kernel
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable

revision = "0002_evidence"
down_revision = "0001_kernel"
branch_labels = None
depends_on = None

SQL = r"""
-- Initial registrations are immutable; metadata/rights changes append versions.
CREATE TABLE evidence.source (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_key text NOT NULL UNIQUE,
  title text NOT NULL,
  provider_url text NOT NULL,
  lineage_family text NOT NULL,
  rights jsonb NOT NULL,
  metadata jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE evidence.source_metadata_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  version integer NOT NULL CHECK (version > 0),
  metadata jsonb NOT NULL,
  rights jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (source_id, version),
  UNIQUE (id, source_id)
);

CREATE FUNCTION evidence.check_metadata_version_sequence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE latest integer;
BEGIN
  SELECT max(version) INTO latest FROM evidence.source_metadata_version WHERE source_id = NEW.source_id;
  IF NEW.version <> COALESCE(latest, 0) + 1 THEN
    RAISE EXCEPTION 'source metadata versions must be appended contiguously (latest %, got %)', latest, NEW.version;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER metadata_version_sequence BEFORE INSERT ON evidence.source_metadata_version
  FOR EACH ROW EXECUTE FUNCTION evidence.check_metadata_version_sequence();

CREATE TABLE evidence.connector_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  name text NOT NULL,
  version text NOT NULL,
  spec_sha256 text NOT NULL REFERENCES meta.artifact,
  code_digest text NOT NULL,
  declared_capabilities jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (name, version),
  UNIQUE (id, source_id)
);

CREATE TABLE evidence.acquisition (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  connector_version_id uuid NOT NULL,
  source_metadata_version_id uuid NOT NULL,
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  request_fingerprint text NOT NULL,
  raw_sha256 text NOT NULL REFERENCES meta.artifact,
  retrieved_at timestamptz NOT NULL,
  response_metadata jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  idempotency_key text NOT NULL UNIQUE,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  -- (1) an acquisition's connector belongs to its declared source
  FOREIGN KEY (connector_version_id, source_id) REFERENCES evidence.connector_version (id, source_id),
  -- (4) acquisition manifests pin the used source metadata version of the same source
  FOREIGN KEY (source_metadata_version_id, source_id) REFERENCES evidence.source_metadata_version (id, source_id),
  UNIQUE (id, source_id, connector_version_id)
);
CREATE INDEX acquisition_source_time ON evidence.acquisition(source_id, retrieved_at);

CREATE FUNCTION evidence.check_acquisition_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT meta.artifact_is_verified(NEW.raw_sha256) THEN
    RAISE EXCEPTION 'acquisition raw artifact % has no verified storage location', NEW.raw_sha256;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER acquisition_artifact BEFORE INSERT ON evidence.acquisition
  FOR EACH ROW EXECUTE FUNCTION evidence.check_acquisition_artifact();

CREATE TABLE evidence.observation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES evidence.source,
  connector_version_id uuid NOT NULL REFERENCES evidence.connector_version,
  acquisition_id uuid NOT NULL,
  -- Lineages are per origin (R-04): fixture and live records of the same
  -- logical key never share a revision chain. Must equal the acquisition's
  -- origin (enforced by evidence.check_revision; filled from it when omitted).
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  logical_key text NOT NULL,
  revision_no integer NOT NULL CHECK (revision_no > 0),
  source_revision_id text,
  supersedes_id uuid,
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
  raw_selector jsonb NOT NULL CHECK (jsonb_typeof(raw_selector) = 'object' AND raw_selector ? 'artifact_sha256'),
  parser_version text NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  semantic_hash text NOT NULL,
  record_hash text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (source_id, origin, logical_key, revision_no),
  UNIQUE (acquisition_id, logical_key, connector_version_id),
  UNIQUE (id, source_id, origin, logical_key),
  UNIQUE (supersedes_id),
  CHECK (supersedes_id IS NULL OR supersedes_id <> id),
  CHECK ((revision_no = 1) = (supersedes_id IS NULL)),
  CHECK (source_available_at IS NULL OR availability_basis IS NOT NULL),
  CHECK (jsonb_typeof(value) = 'object' AND value ? 'kind'),
  CHECK (uncertainty ? 'status'),
  -- Missing is typed; a missing value is never a number.
  CHECK (value->>'kind' <> 'missing' OR (value ? 'reason' AND NOT value ? 'value')),
  -- (2) observation's source/connector/acquisition agree
  FOREIGN KEY (acquisition_id, source_id, connector_version_id)
    REFERENCES evidence.acquisition (id, source_id, connector_version_id),
  -- (3) supersedes points to the same source/origin/logical key
  FOREIGN KEY (supersedes_id, source_id, origin, logical_key)
    REFERENCES evidence.observation (id, source_id, origin, logical_key)
);
CREATE INDEX observation_revision_lookup
  ON evidence.observation(source_id, origin, logical_key, revision_no DESC);
CREATE INDEX observation_series
  ON evidence.observation(subject_id, concept_version_id, support_id);

-- (3) supersedes has exactly the preceding revision number: lineage is a
-- strictly decreasing chain, therefore acyclic.
CREATE FUNCTION evidence.check_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prev integer; acq evidence.acquisition;
BEGIN
  SELECT * INTO acq FROM evidence.acquisition WHERE id = NEW.acquisition_id;
  -- The lineage origin is the acquisition's origin; never caller-chosen.
  IF NEW.origin IS NULL THEN
    NEW.origin := acq.origin;
  ELSIF NEW.origin IS DISTINCT FROM acq.origin THEN
    RAISE EXCEPTION 'observation origin % does not match its acquisition origin %', NEW.origin, acq.origin;
  END IF;
  IF NEW.supersedes_id IS NOT NULL THEN
    SELECT revision_no INTO prev FROM evidence.observation WHERE id = NEW.supersedes_id;
    IF prev IS NULL OR prev <> NEW.revision_no - 1 THEN
      RAISE EXCEPTION 'supersedes must reference revision % of the same lineage', NEW.revision_no - 1;
    END IF;
  END IF;
  -- The raw selector must point at the acquisition's verified raw bytes.
  IF NEW.raw_selector->>'artifact_sha256' IS DISTINCT FROM acq.raw_sha256 THEN
    RAISE EXCEPTION 'raw_selector.artifact_sha256 must match the acquisition raw artifact';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER observation_revision BEFORE INSERT ON evidence.observation
  FOR EACH ROW EXECUTE FUNCTION evidence.check_revision();

CREATE TABLE evidence.observation_retraction (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  observation_id uuid NOT NULL REFERENCES evidence.observation,
  reason text NOT NULL,
  activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX retraction_observation ON evidence.observation_retraction(observation_id);

-- (6) concept/scope-specific eligibility per connector version; append-only
-- decisions, latest decision wins.
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
CREATE INDEX eligibility_lookup ON evidence.eligibility(connector_version_id, concept_version_id, recorded_at DESC);

-- (6) Applicability scope (R-07, R5-11). The schema is explicit and validated
-- at insert (evidence.check_eligibility_applicability); allowed keys:
--   scope keys (enforced by evidence.is_eligible):
--     subject_ids      array of entity UUID strings: the subject must be listed;
--     years            {"from": int, "to": int} (inclusive range) or
--                      {"in": [int, ...]} (explicit set): the record's reference
--                      year (world.support_ref_year) must be covered; an unknown
--                      year is never covered;
--     classifications  array of classification codes (e.g. HS revisions H4..H6):
--                      the record's classification (source_flags->>'classificationCode')
--                      must be listed; an unknown classification is never covered.
--   informational keys (recorded, never matched): subjects, geo, mapping_key,
--     gate_result_id, rollback_sha256, superseded_by, reapproved_by,
--     restored_by_rollback_of, reason.
-- Any other key, list-form years or non-integer years are rejected at insert.
CREATE FUNCTION evidence.check_eligibility_applicability() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ap jsonb := NEW.applicability; k text; y jsonb; v jsonb;
BEGIN
  IF ap IS NULL OR jsonb_typeof(ap) <> 'object' THEN
    RAISE EXCEPTION 'eligibility applicability must be a JSON object' USING ERRCODE = '22023';
  END IF;
  FOR k IN SELECT jsonb_object_keys(ap) LOOP
    IF k <> ALL (ARRAY['subject_ids','years','classifications','subjects','geo','mapping_key','gate_result_id',
                       'rollback_sha256','superseded_by','reapproved_by','restored_by_rollback_of','reason']) THEN
      RAISE EXCEPTION 'eligibility applicability key % is not part of the scope schema', k USING ERRCODE = '22023';
    END IF;
  END LOOP;
  IF ap ? 'subject_ids' THEN
    IF jsonb_typeof(ap->'subject_ids') <> 'array' THEN
      RAISE EXCEPTION 'applicability.subject_ids must be an array of entity UUIDs' USING ERRCODE = '22023';
    END IF;
    FOR v IN SELECT jsonb_array_elements(ap->'subject_ids') LOOP
      IF jsonb_typeof(v) <> 'string'
         OR (v #>> '{}') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'applicability.subject_ids must hold lower-case entity UUID strings (got %)', v
          USING ERRCODE = '22023';
      END IF;
    END LOOP;
  END IF;
  IF ap ? 'years' THEN
    y := ap->'years';
    IF jsonb_typeof(y) <> 'object' THEN
      RAISE EXCEPTION 'applicability.years must be {"from": y0, "to": y1} or {"in": [y, ...]} (got %)', y
        USING ERRCODE = '22023';
    END IF;
    IF (SELECT array_agg(x ORDER BY x) FROM jsonb_object_keys(y) x) = ARRAY['from','to'] THEN
      FOREACH v IN ARRAY ARRAY[y->'from', y->'to'] LOOP
        IF jsonb_typeof(v) <> 'number' OR (v #>> '{}')::numeric <> trunc((v #>> '{}')::numeric)
           OR (v #>> '{}')::numeric NOT BETWEEN 1 AND 9999 THEN
          RAISE EXCEPTION 'applicability.years bounds must be integer years (got %)', v USING ERRCODE = '22023';
        END IF;
      END LOOP;
      IF (y->>'from')::int > (y->>'to')::int THEN
        RAISE EXCEPTION 'applicability.years.from must not exceed years.to (got %)', y USING ERRCODE = '22023';
      END IF;
    ELSIF (SELECT array_agg(x) FROM jsonb_object_keys(y) x) = ARRAY['in'] THEN
      IF jsonb_typeof(y->'in') <> 'array' OR jsonb_array_length(y->'in') = 0 THEN
        RAISE EXCEPTION 'applicability.years.in must be a non-empty array of integer years' USING ERRCODE = '22023';
      END IF;
      FOR v IN SELECT jsonb_array_elements(y->'in') LOOP
        IF jsonb_typeof(v) <> 'number' OR (v #>> '{}')::numeric <> trunc((v #>> '{}')::numeric)
           OR (v #>> '{}')::numeric NOT BETWEEN 1 AND 9999 THEN
          RAISE EXCEPTION 'applicability.years.in must hold integer years (got %)', v USING ERRCODE = '22023';
        END IF;
      END LOOP;
    ELSE
      RAISE EXCEPTION 'applicability.years must be {"from": y0, "to": y1} or {"in": [y, ...]} (got %)', y
        USING ERRCODE = '22023';
    END IF;
  END IF;
  IF ap ? 'classifications' THEN
    IF jsonb_typeof(ap->'classifications') <> 'array' OR jsonb_array_length(ap->'classifications') = 0 THEN
      RAISE EXCEPTION 'applicability.classifications must be a non-empty array of codes' USING ERRCODE = '22023';
    END IF;
    FOR v IN SELECT jsonb_array_elements(ap->'classifications') LOOP
      IF jsonb_typeof(v) <> 'string' OR (v #>> '{}') = '' THEN
        RAISE EXCEPTION 'applicability.classifications must hold non-empty strings (got %)', v
          USING ERRCODE = '22023';
      END IF;
    END LOOP;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER applicability_valid BEFORE INSERT ON evidence.eligibility
  FOR EACH ROW EXECUTE FUNCTION evidence.check_eligibility_applicability();

-- The latest decision among those whose scope covers the record wins, so a
-- decision scoped to other subjects/years/classifications never affects this
-- record. ref_year / classification NULL mean "unknown": a decision scoped on
-- that dimension then does not cover the record (fails closed, R5-03/R5-11);
-- only decisions without that scope key apply. Unknown keys (impossible after
-- the insert check) make a decision non-matching.
CREATE FUNCTION evidence.is_eligible(cv uuid, concept uuid, subject uuid, at timestamptz,
                                     ref_year integer DEFAULT NULL, classification text DEFAULT NULL)
  RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT COALESCE((
    SELECT e.decision = 'eligible'
    FROM evidence.eligibility e
    WHERE e.connector_version_id = cv AND e.concept_version_id = concept AND e.recorded_at <= at
      AND jsonb_typeof(e.applicability) = 'object'
      AND NOT EXISTS (SELECT 1 FROM jsonb_object_keys(e.applicability) k
                      WHERE k <> ALL (ARRAY['subject_ids','years','classifications','subjects','geo','mapping_key',
                                            'gate_result_id','rollback_sha256','superseded_by','reapproved_by',
                                            'restored_by_rollback_of','reason']))
      AND (NOT e.applicability ? 'subject_ids'
           OR (jsonb_typeof(e.applicability->'subject_ids') = 'array'
               AND e.applicability->'subject_ids' ? subject::text))
      AND (NOT e.applicability ? 'years'
           OR CASE WHEN ref_year IS NULL OR jsonb_typeof(e.applicability->'years') IS DISTINCT FROM 'object'
                     THEN false
                   WHEN jsonb_typeof(e.applicability->'years'->'from') = 'number'
                        AND jsonb_typeof(e.applicability->'years'->'to') = 'number'
                     THEN ref_year BETWEEN (e.applicability->'years'->>'from')::numeric
                                       AND (e.applicability->'years'->>'to')::numeric
                   WHEN jsonb_typeof(e.applicability->'years'->'in') = 'array'
                     THEN e.applicability->'years'->'in' @> jsonb_build_array(ref_year)
                   ELSE false
              END)
      AND (NOT e.applicability ? 'classifications'
           OR (classification IS NOT NULL
               AND jsonb_typeof(e.applicability->'classifications') = 'array'
               AND e.applicability->'classifications' @> jsonb_build_array(classification)))
    ORDER BY e.recorded_at DESC, e.id DESC LIMIT 1), false)
$$;

-- The single eligibility authority for one observation (R5-03): used by the
-- belief.input trigger and by belief selection, so a row selection admits is a
-- row the trigger accepts. Reference year = world.support_ref_year(support);
-- classification = source_flags->>'classificationCode' (NULL when absent).
CREATE FUNCTION evidence.observation_is_eligible(obs uuid, at timestamptz) RETURNS boolean
  LANGUAGE sql STABLE AS $$
  SELECT COALESCE((
    SELECT evidence.is_eligible(o.connector_version_id, o.concept_version_id, o.subject_id, at,
                                world.support_ref_year(o.support_id), o.source_flags->>'classificationCode')
    FROM evidence.observation o WHERE o.id = obs), false)
$$;
"""


def upgrade() -> None:
    op.execute(SQL)
    op.execute(
        immutable(
            "evidence.source",
            "evidence.source_metadata_version",
            "evidence.connector_version",
            "evidence.acquisition",
            "evidence.observation",
            "evidence.observation_retraction",
            "evidence.eligibility",
        )
    )


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
