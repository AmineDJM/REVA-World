"""V3 world knowledge graph storage (agent KGCORE).

Schema ``kg``: typed, dated, sourced statements between entities of every kind
(states, empires, people, events, treaties, languages, ...), Wikidata-backed and
open to other sources (UCDP, UN votes, CShapes...). Everything is append-only:
a new source revision is a new row; the *current view* is derived.

Tables
------
* ``kg.load`` — one row per loaded batch: the provenance manifest (source key,
  origin, every raw artifact sha256 the batch was parsed from — each must be a
  verified ``meta.artifact`` —, retrieval time, loader version, server
  ``recorded_at``, ``recorded_by`` = login). Entity, label, statement and
  retraction rows reference their load and must be inserted in the *same
  transaction* as it (checked by statement-level triggers), so a load's
  ``recorded_at`` is the recording time of every row of the batch. Per-row
  ``recorded_at`` columns are deliberately absent from the bulk tables: the
  server-time trigger would cost a catalog lookup per row on 10^6-row loads.
* ``kg.entity`` — entity snapshots (qid, kind, descriptions, aliases, sitelinks,
  source revision, content sha256); ``kg.entity_label`` — labels and aliases per
  language (search index).
* ``kg.property`` / ``kg.property_version`` — the property allow-list (pid,
  value kind) and its versioned naming (REVA predicate, inverse name, fr/en
  labels). Statements must use an allow-listed property.
* ``kg.statement`` — subject, property, object entity *or* typed literal value,
  temporal qualifiers start / end / point in time with precision (raw time
  strings + Wikibase precision 0-14; ``valid_lo``/``valid_hi`` are derived
  inclusive dates used for filtering, NULL when undated or outside the
  PostgreSQL date range), other qualifiers and references (jsonb), rank
  (preferred/normal only: deprecated statements never enter), statement key
  (``<source_key>:<statement id>`` or ``<source_key>:h:<content hash>``), source
  revision, raw artifact sha256, retrieval time.
* ``kg.statement_retraction`` — a source deprecated or removed a statement: the
  retracted row leaves the current view; nothing is deleted.

Views: ``kg.property_current``, ``kg.entity_current``, ``kg.statement_current``
(latest row per (statement key, origin), not retracted; a NOT EXISTS anti-join
on the (statement_key, origin, ...) identity index, so neighbour and time
predicates push down into the view).

Bulk-load cost decisions (measured, docs/dev/v3_kgcore.md): ``kg.statement``'s
load and property references are verified by the statement-level trigger
(one set-based query per INSERT) instead of two per-row foreign-key probes;
CHECK patterns avoid bounded regex repetitions; no GiST range index (time
filters always ride on a subject/object/property index).

Grants (0007/0008 patterns): reva_worker inserts; reva_api, reva_sim and reva_gate
read only; functions are revoked from PUBLIC; the restricted-deletion role covers
the immutable tables.

Revision ID: 0009_kg
Revises: 0008_v2
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0009_kg"
down_revision = "0008_v2"
branch_labels = None
depends_on = None

# CHECK patterns. Bounded repetitions ({0,128}, {64}) cost 5-15x more per row in PostgreSQL's regex engine
# than an unbounded pattern plus a length() test (measured: 10 us vs 1 us per value), so lengths are
# checked separately. The loader validates the finer bounds (reva.kg.model).
QID = r"'^(Q[1-9][0-9]*|[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]+)$'"
PID = r"'^(P[1-9][0-9]*|[a-z][a-z0-9_]*:[a-z][a-z0-9_]*)$'"
SOURCE_KEY = r"'^[a-z0-9][a-z0-9_.-]*$'"
NAME = r"'^[a-z][a-z0-9_]*$'"


def _qid(col: str) -> str:
    return f"({col} ~ {QID} AND length({col}) <= 161)"


def _pid(col: str) -> str:
    return f"({col} ~ {PID} AND length({col}) <= 97)"


def _source_key(col: str) -> str:
    return f"({col} ~ {SOURCE_KEY} AND length({col}) <= 100)"


def _name(col: str) -> str:
    return f"({col} ~ {NAME} AND length({col}) <= 64)"


def _sha(col: str) -> str:
    return f"(length({col}) = 64 AND {col} !~ '[^0-9a-f]')"


SQL_TABLES = rf"""
CREATE SCHEMA IF NOT EXISTS kg;

CREATE TABLE kg.load (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_key text NOT NULL CHECK {_source_key('source_key')},
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  raw_sha256s text[] NOT NULL CHECK (cardinality(raw_sha256s) BETWEEN 1 AND 10000),
  retrieved_at timestamptz NOT NULL,
  loader_version text NOT NULL CHECK (length(loader_version) BETWEEN 1 AND 100),
  parameters jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(parameters) = 'object'),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user,
  CHECK (retrieved_at <= recorded_at + interval '5 minutes')
);

CREATE TABLE kg.property (
  pid text PRIMARY KEY CHECK {_pid('pid')},
  value_kind text NOT NULL CHECK (value_kind IN
    ('item','time','quantity','string','monolingualtext','external_id','globe_coordinate','url','other')),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user
);

CREATE TABLE kg.property_version (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pid text NOT NULL REFERENCES kg.property,
  reva_predicate text NOT NULL CHECK {_name('reva_predicate')},
  inverse_name text CHECK (inverse_name IS NULL OR {_name('inverse_name')}),
  label_en text NOT NULL CHECK (length(label_en) BETWEEN 1 AND 200),
  label_fr text CHECK (label_fr IS NULL OR length(label_fr) BETWEEN 1 AND 200),
  inverse_label_en text CHECK (inverse_label_en IS NULL OR length(inverse_label_en) BETWEEN 1 AND 200),
  inverse_label_fr text CHECK (inverse_label_fr IS NULL OR length(inverse_label_fr) BETWEEN 1 AND 200),
  definition_sha256 text NOT NULL CHECK {_sha('definition_sha256')},
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user,
  UNIQUE (pid, definition_sha256)
);
CREATE INDEX property_version_latest ON kg.property_version(pid, id);

CREATE TABLE kg.entity (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.load,
  qid text NOT NULL CHECK {_qid('qid')},
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  kind text NOT NULL CHECK {_name('kind')},
  descriptions jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(descriptions) = 'object'),
  aliases jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(aliases) = 'object'),
  sitelinks integer CHECK (sitelinks IS NULL OR sitelinks >= 0),
  content_sha256 text NOT NULL CHECK {_sha('content_sha256')},
  source_key text NOT NULL CHECK {_source_key('source_key')},
  source_revision text CHECK (source_revision IS NULL OR length(source_revision) BETWEEN 1 AND 100),
  retrieved_at timestamptz NOT NULL,
  CONSTRAINT entity_version_identity UNIQUE NULLS NOT DISTINCT (qid, origin, source_revision, content_sha256)
);
CREATE INDEX entity_latest ON kg.entity(qid, origin, id);

CREATE TABLE kg.entity_label (
  entity_id bigint NOT NULL REFERENCES kg.entity,
  qid text NOT NULL,
  origin text NOT NULL,
  lang text NOT NULL CHECK (lang ~ '^[a-z][a-z][a-z]?(-[a-z0-9]+)*$' AND length(lang) <= 32),
  is_alias boolean NOT NULL,
  label text NOT NULL CHECK (length(label) BETWEEN 1 AND 500),
  PRIMARY KEY (entity_id, is_alias, lang, label)
);
CREATE INDEX entity_label_prefix ON kg.entity_label(lower(label) text_pattern_ops, lang);

CREATE TABLE kg.statement (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL,
  statement_key text NOT NULL CHECK (length(statement_key) BETWEEN 3 AND 300),
  statement_id text CHECK (statement_id IS NULL OR length(statement_id) BETWEEN 1 AND 200),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  content_sha256 text NOT NULL CHECK {_sha('content_sha256')},
  subject_qid text NOT NULL CHECK {_qid('subject_qid')},
  -- load_id and property_pid are checked set-wise by kg.check_statement_rows() (one query per INSERT
  -- statement instead of two foreign-key probes per row; rows are never updated or deleted).
  property_pid text NOT NULL CHECK {_pid('property_pid')},
  object_qid text CHECK (object_qid IS NULL OR {_qid('object_qid')}),
  value jsonb CHECK (value IS NULL OR (jsonb_typeof(value) = 'object' AND jsonb_typeof(value->'type') = 'string')),
  start_time text CHECK (start_time IS NULL OR length(start_time) BETWEEN 1 AND 64),
  start_precision smallint CHECK (start_precision BETWEEN 0 AND 14),
  end_time text CHECK (end_time IS NULL OR length(end_time) BETWEEN 1 AND 64),
  end_precision smallint CHECK (end_precision BETWEEN 0 AND 14),
  pit_time text CHECK (pit_time IS NULL OR length(pit_time) BETWEEN 1 AND 64),
  pit_precision smallint CHECK (pit_precision BETWEEN 0 AND 14),
  time_calendars jsonb CHECK (time_calendars IS NULL OR jsonb_typeof(time_calendars) = 'object'),
  valid_lo date,
  valid_hi date,
  value_time_lo date,
  value_time_hi date,
  qualifiers jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(qualifiers) = 'object'),
  rank text NOT NULL CHECK (rank IN ('preferred','normal')),
  refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(refs) = 'array'),
  source_key text NOT NULL CHECK {_source_key('source_key')},
  source_revision text CHECK (source_revision IS NULL OR length(source_revision) BETWEEN 1 AND 100),
  raw_sha256 text CHECK (raw_sha256 IS NULL OR {_sha('raw_sha256')}),
  retrieved_at timestamptz NOT NULL,
  CHECK ((object_qid IS NULL) <> (value IS NULL)),
  CHECK ((start_time IS NULL) = (start_precision IS NULL)),
  CHECK ((end_time IS NULL) = (end_precision IS NULL)),
  CHECK ((pit_time IS NULL) = (pit_precision IS NULL)),
  CHECK (valid_lo IS NULL OR valid_hi IS NULL OR valid_lo <= valid_hi),
  CHECK (value_time_lo IS NULL OR value_time_hi IS NULL OR value_time_lo <= value_time_hi),
  CHECK (starts_with(statement_key, source_key || ':')),
  CONSTRAINT statement_version_identity UNIQUE NULLS NOT DISTINCT (statement_key, origin, source_revision)
);
-- Neighbours (outgoing / incoming), by property and time. Time filters always come with a subject,
-- object or property (the API has no global "everything in this window" scan), so validity is filtered
-- on these index paths; a GiST range index was measured to cost more at load than it saved.
-- The latest revision per key (current-view anti-join) uses statement_version_identity's
-- (statement_key, origin) prefix: keys have very few revisions.
CREATE INDEX statement_subject ON kg.statement(subject_qid, property_pid);
CREATE INDEX statement_object ON kg.statement(object_qid, property_pid) WHERE object_qid IS NOT NULL;
CREATE INDEX statement_property_time ON kg.statement(property_pid, valid_lo);

CREATE TABLE kg.statement_retraction (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.load,
  statement_row_id bigint NOT NULL REFERENCES kg.statement,
  statement_key text NOT NULL,
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  reason text NOT NULL CHECK (reason IN ('deprecated_at_source','removed_at_source')),
  source_revision text CHECK (source_revision IS NULL OR length(source_revision) BETWEEN 1 AND 100),
  UNIQUE (statement_row_id)
);
"""

SQL_VIEWS = r"""
CREATE VIEW kg.property_current AS
  SELECT p.pid, p.value_kind, v.reva_predicate, v.inverse_name, v.label_en, v.label_fr, v.inverse_label_en,
         v.inverse_label_fr, v.definition_sha256, v.recorded_at
    FROM kg.property p
    JOIN kg.property_version v ON v.pid = p.pid
   WHERE NOT EXISTS (SELECT 1 FROM kg.property_version n WHERE n.pid = v.pid AND n.id > v.id);

CREATE VIEW kg.entity_current AS
  SELECT e.*, l.recorded_at
    FROM kg.entity e JOIN kg.load l ON l.id = e.load_id
   WHERE NOT EXISTS (SELECT 1 FROM kg.entity n WHERE n.qid = e.qid AND n.origin = e.origin AND n.id > e.id);

CREATE VIEW kg.statement_current AS
  SELECT s.*, l.recorded_at
    FROM kg.statement s JOIN kg.load l ON l.id = s.load_id
   WHERE NOT EXISTS (SELECT 1 FROM kg.statement n
                      WHERE n.statement_key = s.statement_key AND n.origin = s.origin AND n.id > s.id)
     AND NOT EXISTS (SELECT 1 FROM kg.statement_retraction r WHERE r.statement_row_id = s.id);
"""

SQL_TRIGGERS = r"""
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON kg.load
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON kg.property
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON kg.property_version
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();

-- A load is derived from archived raw bytes: every raw artifact is listed once
-- and is a verified, located artifact.
CREATE FUNCTION kg.check_load() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE n int; bad text;
BEGIN
  SELECT count(DISTINCT x) INTO n FROM unnest(NEW.raw_sha256s) x;
  IF n <> cardinality(NEW.raw_sha256s) OR array_position(NEW.raw_sha256s, NULL) IS NOT NULL THEN
    RAISE EXCEPTION 'kg.load raw_sha256s must be distinct and non-null';
  END IF;
  SELECT x INTO bad FROM unnest(NEW.raw_sha256s) x WHERE NOT meta.artifact_is_verified(x) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.load raw artifact % has no verified storage location', bad;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER load_derived_from_archive BEFORE INSERT ON kg.load
  FOR EACH ROW EXECUTE FUNCTION kg.check_load();

-- Batch rows restate their load (source, origin, retrieval time) and are inserted in the
-- transaction that created it, so the load's server recorded_at is theirs. Statement-level
-- (transition tables): one check per INSERT, not per row.
CREATE FUNCTION kg.check_entity_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n JOIN kg.load l ON l.id = n.load_id
   WHERE (l.origin, l.source_key, l.retrieved_at) IS DISTINCT FROM (n.origin, n.source_key, n.retrieved_at)
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.entity row % does not match its load', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER entity_rows_match_load AFTER INSERT ON kg.entity
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_entity_rows();

CREATE FUNCTION kg.check_label_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.entity_id INTO bad FROM new_rows n JOIN kg.entity e ON e.id = n.entity_id
   WHERE (e.qid, e.origin) IS DISTINCT FROM (n.qid, n.origin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.entity_label row does not match entity %', bad;
  END IF;
  SELECT e.id INTO bad FROM kg.entity e WHERE e.id IN (SELECT DISTINCT entity_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(e.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.entity % is sealed: its labels must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER label_rows_match_entity AFTER INSERT ON kg.entity_label
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_label_rows();

CREATE FUNCTION kg.check_statement_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n LEFT JOIN kg.load l ON l.id = n.load_id
   WHERE l.id IS NULL
      OR (l.origin, l.source_key, l.retrieved_at) IS DISTINCT FROM (n.origin, n.source_key, n.retrieved_at)
      OR (n.raw_sha256 IS NOT NULL AND NOT (n.raw_sha256 = ANY (l.raw_sha256s)))
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.statement row % does not match its load (source, origin, retrieval time, raw artifact)', bad;
  END IF;
  SELECT n.id INTO bad FROM new_rows n
   WHERE NOT EXISTS (SELECT 1 FROM kg.property p WHERE p.pid = n.property_pid) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.statement row % uses a property that is not allow-listed in kg.property', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER statement_rows_match_load AFTER INSERT ON kg.statement
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_statement_rows();

CREATE FUNCTION kg.check_retraction_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n
    JOIN kg.load l ON l.id = n.load_id
    JOIN kg.statement s ON s.id = n.statement_row_id
   WHERE (s.statement_key, s.origin, s.source_key) IS DISTINCT FROM (n.statement_key, n.origin, l.source_key)
      OR l.origin <> n.origin OR l.retrieved_at < s.retrieved_at
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.statement_retraction % does not match its statement or load', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER retraction_rows_match_load AFTER INSERT ON kg.statement_retraction
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_retraction_rows();
"""

#: Label search: trigram index when pg_trgm can be created (it is a trusted extension, so a
#: non-superuser owner with CREATE on the database may install it); otherwise the lower(label)
#: btree prefix index above is the only search index and substring search scans.
SQL_TRGM = r"""
DO $$
DECLARE sch text;
BEGIN
  BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
  EXCEPTION WHEN insufficient_privilege OR feature_not_supported OR undefined_file THEN
    RAISE NOTICE 'pg_trgm unavailable: kg label search uses the prefix index only';
  END;
  SELECT n.nspname INTO sch FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
   WHERE e.extname = 'pg_trgm';
  IF sch IS NOT NULL THEN
    EXECUTE format('CREATE INDEX entity_label_trgm ON kg.entity_label USING gin (lower(label) %I.gin_trgm_ops)',
                   sch);
  END IF;
END $$;
"""

KG_TABLES = (
    "kg.load",
    "kg.property",
    "kg.property_version",
    "kg.entity",
    "kg.entity_label",
    "kg.statement",
    "kg.statement_retraction",
)
KG_FUNCTIONS = (
    "kg.check_load()",
    "kg.check_entity_rows()",
    "kg.check_label_rows()",
    "kg.check_statement_rows()",
    "kg.check_retraction_rows()",
)

SQL_GRANTS = rf"""
REVOKE ALL ON SCHEMA kg FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA kg FROM PUBLIC;
REVOKE ALL ON FUNCTION {", ".join(KG_FUNCTIONS)} FROM PUBLIC;
GRANT USAGE ON SCHEMA kg TO reva_api, reva_worker, reva_sim, reva_gate;
GRANT SELECT ON ALL TABLES IN SCHEMA kg TO reva_api, reva_worker, reva_sim, reva_gate;
GRANT INSERT ON {", ".join(KG_TABLES)} TO reva_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA kg TO reva_worker;
GRANT EXECUTE ON FUNCTION {", ".join(KG_FUNCTIONS)} TO reva_worker;
"""

#: (R5-10) as in 0007/0008: the restricted-deletion role holds DELETE on immutable tables.
SQL_RESTRICTED_DELETION = rf"""
DO $$
DECLARE t text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA kg TO reva_restricted_deletion';
    FOREACH t IN ARRAY ARRAY[{", ".join(f"'{t}'" for t in KG_TABLES)}] LOOP
      EXECUTE format('GRANT SELECT, DELETE ON %s TO reva_restricted_deletion', t);
    END LOOP;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_TABLES)
    op.execute(SQL_VIEWS)
    op.execute(SQL_TRIGGERS)
    op.execute(SQL_TRGM)
    op.execute(immutable(*KG_TABLES))
    # recorded_at is server time on kg.load and the property tables (bulk rows inherit their load's).
    op.execute(server_recorded_at("kg"))
    op.execute(SQL_GRANTS)
    op.execute(SQL_RESTRICTED_DELETION)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
