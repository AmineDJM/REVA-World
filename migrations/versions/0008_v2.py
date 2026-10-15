"""V2 platform: multi-period simulation results and release-pinned dataset inputs.

1. ``sim.result`` gains an optional half-open period ``[period_start, period_end)``
   (UTC dates; both NULL for single-period models, both set with start < end for
   multi-period models). The row identity becomes (run, output, subject, variant,
   period_start) with NULLs not distinct.
2. ``evidence.dataset``: the manifest of a dataset input — a content-addressed
   Parquet table derived from archived raw source bytes (source acquisition ids,
   parser version, table schema, row count, artifact and manifest sha256).
   Append-only; ``recorded_at`` is server time; every source acquisition must
   exist with the dataset's origin, and both artifacts must be verified.
3. ``belief.release_dataset``: a belief release pins the datasets eligible at its
   knowledge cutoff (``recorded_at <= cutoff``, same origin), one per
   (concept, subject). Immutable and sealed with the release exactly like
   estimates (``belief.reject_if_published``: written before ``ops.publication``,
   at READ COMMITTED, under the release lock); the dataset artifact is also
   referenced from ``belief.release_artifact`` (role ``dataset``) so
   ``ops.check_publication`` verifies it.
4. Grants (0007 patterns): the worker writes datasets and pins during
   publication; the simulation and API roles read them. The restricted-deletion
   role covers the new immutable tables as 0007 does for the others.

Revision ID: 0008_v2
Revises: 0007_security
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0008_v2"
down_revision = "0007_security"
branch_labels = None
depends_on = None

SQL_RESULT_PERIOD = r"""
ALTER TABLE sim.result
  ADD COLUMN period_start date,
  ADD COLUMN period_end date,
  ADD CONSTRAINT result_period_half_open CHECK (
    (period_start IS NULL AND period_end IS NULL)
    OR (period_start IS NOT NULL AND period_end IS NOT NULL AND period_start < period_end));

-- Row identity includes the period (single-period rows: NULL, not distinct).
DO $$
DECLARE c text;
BEGIN
  SELECT con.conname INTO c FROM pg_catalog.pg_constraint con
   WHERE con.conrelid = 'sim.result'::regclass AND con.contype = 'u'
     AND (SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_catalog.pg_attribute a
           WHERE a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey))
         = ARRAY['output_key','run_id','subject_id','variant'];
  IF c IS NULL THEN
    RAISE EXCEPTION 'sim.result: V1 unique (run_id, output_key, subject_id, variant) not found';
  END IF;
  EXECUTE format('ALTER TABLE sim.result DROP CONSTRAINT %I', c);
END $$;
ALTER TABLE sim.result ADD CONSTRAINT result_identity
  UNIQUE NULLS NOT DISTINCT (run_id, output_key, subject_id, variant, period_start);
"""

SQL_DATASETS = r"""
CREATE TABLE evidence.dataset (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_key text NOT NULL CHECK (dataset_key ~ '^[a-z0-9][a-z0-9_.-]{0,199}$'),
  concept_id text NOT NULL CHECK (length(concept_id) BETWEEN 1 AND 200),
  subject_key text NOT NULL CHECK (subject_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  source_acquisition_ids uuid[] NOT NULL CHECK (cardinality(source_acquisition_ids) >= 1),
  parser_version text NOT NULL CHECK (length(parser_version) BETWEEN 1 AND 200),
  table_schema jsonb NOT NULL CHECK (jsonb_typeof(table_schema) = 'object'),
  row_count bigint NOT NULL CHECK (row_count >= 0),
  coverage jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(coverage) = 'object'),
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (dataset_key, subject_key, origin, artifact_sha256),
  CHECK (artifact_sha256 <> manifest_sha256)
);
CREATE INDEX dataset_eligibility ON evidence.dataset(concept_id, subject_key, origin, recorded_at);

-- A dataset is derived from archived raw bytes: every source acquisition exists,
-- has the dataset's origin (no fixture bytes behind a live dataset), is listed
-- once, and both the table and its manifest are verified, located artifacts.
CREATE FUNCTION evidence.check_dataset() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n int; bad uuid;
BEGIN
  SELECT count(DISTINCT x) INTO n FROM unnest(NEW.source_acquisition_ids) x;
  IF n <> cardinality(NEW.source_acquisition_ids) OR array_position(NEW.source_acquisition_ids, NULL) IS NOT NULL THEN
    RAISE EXCEPTION 'dataset source_acquisition_ids must be distinct and non-null';
  END IF;
  SELECT x INTO bad FROM unnest(NEW.source_acquisition_ids) x
   WHERE NOT EXISTS (SELECT 1 FROM evidence.acquisition a WHERE a.id = x AND a.origin = NEW.origin)
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'dataset source acquisition % does not exist with origin %', bad, NEW.origin;
  END IF;
  IF NOT meta.artifact_is_verified(NEW.artifact_sha256) OR NOT meta.artifact_is_verified(NEW.manifest_sha256) THEN
    RAISE EXCEPTION 'dataset artifacts must have verified storage locations';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER dataset_derived_from_archive BEFORE INSERT ON evidence.dataset
  FOR EACH ROW EXECUTE FUNCTION evidence.check_dataset();

CREATE TABLE belief.release_dataset (
  release_id uuid NOT NULL REFERENCES belief.release,
  dataset_id uuid NOT NULL REFERENCES evidence.dataset,
  concept_id text NOT NULL,
  subject_key text NOT NULL,
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  PRIMARY KEY (release_id, dataset_id),
  UNIQUE (release_id, concept_id, subject_key)
);
CREATE INDEX release_dataset_dataset ON belief.release_dataset(dataset_id);

-- A pin restates its dataset (no re-pointing) and only pins what was knowable at
-- the release cutoff with the release's origin (mirrors belief.check_input).
CREATE FUNCTION belief.check_release_dataset() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r belief.release; d evidence.dataset;
BEGIN
  SELECT * INTO r FROM belief.release WHERE id = NEW.release_id;
  SELECT * INTO d FROM evidence.dataset WHERE id = NEW.dataset_id;
  IF d.id IS NULL OR r.id IS NULL THEN
    RAISE EXCEPTION 'release dataset pin references a missing release or dataset';
  END IF;
  IF (d.concept_id, d.subject_key, d.artifact_sha256) IS DISTINCT FROM
     (NEW.concept_id, NEW.subject_key, NEW.artifact_sha256) THEN
    RAISE EXCEPTION 'release dataset pin does not match dataset %', d.id;
  END IF;
  IF d.origin <> r.origin THEN
    RAISE EXCEPTION 'dataset % (origin %) cannot enter a % release', d.id, d.origin, r.origin;
  END IF;
  IF d.recorded_at > r.knowledge_cutoff THEN
    RAISE EXCEPTION 'dataset % recorded after release cutoff', d.id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER release_dataset_admissible BEFORE INSERT ON belief.release_dataset
  FOR EACH ROW EXECUTE FUNCTION belief.check_release_dataset();
-- (R-02, R5-07) sealed with the release, exactly like estimates.
CREATE TRIGGER release_sealed BEFORE INSERT ON belief.release_dataset
  FOR EACH ROW EXECUTE FUNCTION belief.reject_if_published();
"""

SQL_GRANTS = r"""
REVOKE ALL ON FUNCTION evidence.check_dataset(), belief.check_release_dataset() FROM PUBLIC;
REVOKE ALL ON evidence.dataset, belief.release_dataset FROM PUBLIC;
GRANT EXECUTE ON FUNCTION evidence.check_dataset(), belief.check_release_dataset()
  TO reva_api, reva_worker, reva_sim;
GRANT SELECT ON evidence.dataset, belief.release_dataset TO reva_api, reva_worker, reva_sim;
GRANT INSERT ON evidence.dataset, belief.release_dataset TO reva_worker;
"""

#: (R5-10) as in 0007: the restricted-deletion role holds DELETE on immutable tables.
SQL_RESTRICTED_DELETION = r"""
DO $$
DECLARE t text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    FOREACH t IN ARRAY ARRAY['evidence.dataset', 'belief.release_dataset'] LOOP
      EXECUTE format('GRANT SELECT, DELETE ON %s TO reva_restricted_deletion', t);
    END LOOP;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_RESULT_PERIOD)
    op.execute(SQL_DATASETS)
    op.execute(immutable("evidence.dataset", "belief.release_dataset"))
    op.execute(server_recorded_at("evidence"))
    op.execute(SQL_GRANTS)
    op.execute(SQL_RESTRICTED_DELETION)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
