"""Kernel: artifacts, activities (acyclic provenance), contexts, ontology, world identity.

Revision ID: 0001_kernel
Revises:
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable

revision = "0001_kernel"
down_revision = None
branch_labels = None
depends_on = None

SQL = r"""
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

-- Membership in this NOLOGIN role is required (together with a reviewed
-- deletion ticket) to bypass append-only protection for legally required
-- restricted-data deletion. Runtime roles are never members.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    CREATE ROLE reva_restricted_deletion NOLOGIN;
  END IF;
END $$;

CREATE FUNCTION meta.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ticket text := current_setting('reva.reviewed_deletion_ticket', true);
BEGIN
  IF TG_OP = 'DELETE' AND ticket IS NOT NULL AND ticket <> ''
     AND pg_has_role(session_user, 'reva_restricted_deletion', 'MEMBER') THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'Immutable scientific record %.%: append a new version or retraction',
    TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'P0001';
END;
$$;

CREATE FUNCTION meta.reject_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'TRUNCATE forbidden on immutable table %.%', TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$;

CREATE TABLE meta.artifact (
  sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  media_type text NOT NULL,
  byte_size bigint NOT NULL CHECK (byte_size >= 0),
  schema_version text NOT NULL,
  privacy_class text NOT NULL CHECK (privacy_class IN
    ('public_aggregate','synthetic_public_aggregate_derived','restricted_microdata','sensitive_restricted')),
  rights jsonb NOT NULL CHECK (jsonb_typeof(rights) = 'object'),
  logical_sha256 text CHECK (logical_sha256 IS NULL OR logical_sha256 ~ '^[0-9a-f]{64}$'),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  -- V1: clinical/sensitive/person-level data are unavailable in this MVP.
  CONSTRAINT artifact_v1_public_only CHECK
    (privacy_class IN ('public_aggregate','synthetic_public_aggregate_derived'))
);

-- A location row is written only after the object store verified bytes and size.
CREATE TABLE meta.artifact_location (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  storage_uri text NOT NULL,
  namespace text NOT NULL CHECK (namespace IN
    ('raw','normalised','belief','models','scenarios','evaluation','quarantine')),
  verified_byte_size bigint NOT NULL CHECK (verified_byte_size >= 0),
  checksum_verified_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (artifact_sha256, storage_uri)
);

-- Small manifests created through narrow API functions (no object-store write
-- credential on the API). Bytes are verified against the declared hash in-DB.
CREATE TABLE meta.artifact_inline (
  artifact_sha256 text PRIMARY KEY REFERENCES meta.artifact,
  content bytea NOT NULL CHECK (octet_length(content) <= 262144),
  CHECK (encode(sha256(content), 'hex') = artifact_sha256)
);

CREATE FUNCTION meta.check_location_size() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected bigint;
BEGIN
  SELECT byte_size INTO expected FROM meta.artifact WHERE sha256 = NEW.artifact_sha256;
  IF expected IS DISTINCT FROM NEW.verified_byte_size THEN
    RAISE EXCEPTION 'artifact % size mismatch (% vs %)', NEW.artifact_sha256, expected, NEW.verified_byte_size;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER location_size BEFORE INSERT ON meta.artifact_location
  FOR EACH ROW EXECUTE FUNCTION meta.check_location_size();

CREATE FUNCTION meta.artifact_is_verified(sha text) RETURNS boolean
  LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM meta.artifact_location WHERE artifact_sha256 = sha)
      OR EXISTS (SELECT 1 FROM meta.artifact_inline WHERE artifact_sha256 = sha)
$$;

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
  selector jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(selector) = 'object'),
  PRIMARY KEY (activity_id, ordinal)
);
CREATE INDEX activity_input_artifact ON meta.activity_input(artifact_sha256);
CREATE TABLE meta.activity_output (
  activity_id uuid NOT NULL REFERENCES meta.activity,
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  PRIMARY KEY (activity_id, artifact_sha256)
);
CREATE INDEX activity_output_artifact ON meta.activity_output(artifact_sha256);

-- Does activity src reach activity dst through output->input edges?
CREATE FUNCTION meta.activity_reaches(src uuid, dst uuid) RETURNS boolean
  LANGUAGE sql STABLE AS $$
  WITH RECURSIVE walk(act, depth) AS (
    SELECT src, 0
    UNION
    SELECT i.activity_id, w.depth + 1
    FROM walk w
    JOIN meta.activity_output o ON o.activity_id = w.act
    JOIN meta.activity_input i ON i.artifact_sha256 = o.artifact_sha256
    WHERE w.depth < 10000
  )
  SELECT EXISTS (SELECT 1 FROM walk WHERE act = dst)
$$;

CREATE FUNCTION meta.check_input_acyclic() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE producer uuid;
BEGIN
  FOR producer IN SELECT activity_id FROM meta.activity_output WHERE artifact_sha256 = NEW.artifact_sha256 LOOP
    IF producer = NEW.activity_id OR meta.activity_reaches(NEW.activity_id, producer) THEN
      RAISE EXCEPTION 'provenance cycle: activity % cannot consume artifact %', NEW.activity_id, NEW.artifact_sha256;
    END IF;
  END LOOP;
  RETURN NEW;
END $$;
CREATE TRIGGER input_acyclic BEFORE INSERT ON meta.activity_input
  FOR EACH ROW EXECUTE FUNCTION meta.check_input_acyclic();

CREATE FUNCTION meta.check_output_acyclic() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE consumer uuid;
BEGIN
  FOR consumer IN SELECT activity_id FROM meta.activity_input WHERE artifact_sha256 = NEW.artifact_sha256 LOOP
    IF consumer = NEW.activity_id OR meta.activity_reaches(consumer, NEW.activity_id) THEN
      RAISE EXCEPTION 'provenance cycle: activity % cannot produce artifact %', NEW.activity_id, NEW.artifact_sha256;
    END IF;
  END LOOP;
  RETURN NEW;
END $$;
CREATE TRIGGER output_acyclic BEFORE INSERT ON meta.activity_output
  FOR EACH ROW EXECUTE FUNCTION meta.check_output_acyclic();

CREATE TABLE meta.context (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mode text NOT NULL CHECK (mode IN
    ('evidence','belief','simulation','evaluation','population','quarantine')),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  parent_context_id uuid REFERENCES meta.context,
  manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  knowledge_cutoff timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (parent_context_id IS NULL OR parent_context_id <> id),
  CHECK (mode NOT IN ('belief','simulation','evaluation') OR knowledge_cutoff IS NOT NULL)
);

CREATE TABLE ontology.concept_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id text NOT NULL,
  version text NOT NULL,
  kind text NOT NULL CHECK (kind IN
    ('entity_type','quantity','predicate','role','category','unit','component')),
  labels jsonb NOT NULL DEFAULT '{}'::jsonb,
  definition text NOT NULL,
  owner text NOT NULL DEFAULT 'reva-maintainers',
  semantic_profile jsonb NOT NULL,
  schema_sha256 text REFERENCES meta.artifact,
  registry_sha256 text NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (concept_id, version),
  CHECK (kind NOT IN ('quantity','predicate') OR semantic_profile <> '{}'::jsonb)
);
CREATE TABLE ontology.mapping (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mapping_key text NOT NULL,
  source_ref jsonb NOT NULL,
  target_concept_id uuid NOT NULL REFERENCES ontology.concept_version,
  mapping_kind text NOT NULL CHECK (mapping_kind IN
    ('exact','narrower','broader','overlap','weighted_crosswalk','approximate_transform','incompatible')),
  version text NOT NULL,
  transform_spec jsonb NOT NULL,
  applicability jsonb NOT NULL,
  approval text NOT NULL CHECK (approval IN ('approved','candidate','rejected')),
  review_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (mapping_key, version)
);

CREATE TABLE world.entity (
  id uuid PRIMARY KEY,
  kind_concept_id uuid NOT NULL REFERENCES ontology.concept_version,
  namespace text NOT NULL,
  referent_mode text NOT NULL CHECK (referent_mode IN ('real','abstract','synthetic','hypothetical')),
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
  confidence_kind text NOT NULL CHECK (confidence_kind IN ('reviewed_exact','provider_declared','candidate')),
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (provider, external_key, mapping_version)
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
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (valid_range IS NULL OR NOT isempty(valid_range))
);
CREATE INDEX support_geometry ON world.support USING gist(geometry_4326);
CREATE INDEX support_time ON world.support USING gist(valid_range);

-- Display geometries (Natural Earth): explicitly not authoritative boundaries.
CREATE TABLE world.display_geometry (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_sha256 text NOT NULL REFERENCES meta.artifact,
  feature_key text NOT NULL,
  entity_id uuid REFERENCES world.entity,
  match_status text NOT NULL CHECK (match_status IN ('reviewed_match','unmatched','disputed','overseas_part')),
  properties jsonb NOT NULL,
  geometry_4326 geometry(Geometry,4326) NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (dataset_sha256, feature_key)
);
CREATE INDEX display_geometry_gist ON world.display_geometry USING gist(geometry_4326);
"""


def upgrade() -> None:
    op.execute(SQL)
    op.execute(
        immutable(
            "meta.artifact",
            "meta.artifact_location",
            "meta.artifact_inline",
            "meta.activity",
            "meta.activity_input",
            "meta.activity_output",
            "meta.context",
            "ontology.concept_version",
            "ontology.mapping",
            "world.entity",
            "world.alias",
            "world.support",
            "world.display_geometry",
        )
    )


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
