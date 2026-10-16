"""V3 causal claims (agent CAUSCORE; spec docs/roadmap/v3_causality.md).

REVA never asserts causation. These tables hold what *sources* assert, how REVA graded it, and quarantined
candidates, in schema ``kg`` (same runtime roles and grants as the world graph). Everything is append-only.

Tables
------
* ``kg.causal_load`` — one row per write batch: ``kind`` (``kg_statements`` = claims derived from
  ``kg.statement_current``; ``dataset`` = expert-coded dataset rows; ``grading`` = a grader run;
  ``candidates`` = text-mined / model-proposed candidates), source key, dataset, origin, raw artifact sha256s
  (each a verified ``meta.artifact``; required for datasets and candidates), retrieval time, KG watermark,
  loader version, parameters (codebook, grading-rules version...), server ``recorded_at`` / ``recorded_by``.
  Rows of the tables below must be inserted in the transaction that created their load.
* ``kg.causal_claim`` — claim versions: cause and effect ids (direction normalised), relation (closed
  vocabulary), family, polarity, stance (``asserts`` / ``denies``), asserting source (source key, tier
  T1–T4, dataset, revision, KG statement key/id/row, row selector, raw sha256, references verbatim, upstream
  keys used for corroboration dedupe, T2 citation, T1 codebook), verbatim mechanism, scope, explicit
  ``contests`` and source-given disputes. A KG-derived claim (T3/T4, key ``kg:<statement key>``) must
  restate its ``kg.statement`` row (key, origin, source, raw artifact, retrieval time). A changed claim is a
  new row; ``reinstates_row_id`` marks a claim that came back after a retraction.
* ``kg.causal_claim_retraction`` — the source dropped the claim (or its KG statement left the current view).
* ``kg.causal_grade`` — grades as their own rows: claim row, grading-rules version + sha256, grade A–E, the
  rule ids that produced it, temporal check (+ the dates used), corroboration (+ groups), contesting claims
  and the sha256 of all inputs (the grader writes a row only when the inputs changed).
* ``kg.causal_candidate`` — T5/T6 candidates, separate from claims: extractor id/version, verbatim source
  sentence and its revision (required), raw sha256.
* ``kg.causal_review`` — maintainer reviews of claims / candidates. **No runtime role may insert** (agents
  cannot record reviews; a candidate's ``promotion_status`` changes only by a maintainer's review).

Views: ``kg.causal_claim_current`` (latest row per (claim key, origin), not retracted, with
``review_status``), ``kg.causal_grade_current`` (latest grade per claim row, with ``graded_at``),
``kg.causal_candidate_current`` (with ``promotion_status``).

Grants (0009/0011/0012): ``reva_worker`` inserts (not reviews); ``reva_api`` / ``reva_sim`` read;
``reva_gate`` nothing; functions revoked from PUBLIC; the restricted-deletion role covers the tables.

Revision ID: 0015_causal
Revises: 0014_recorded_at_window
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0015_causal"
down_revision = "0014_recorded_at_window"
branch_labels = None
depends_on = None

QID = r"'^(Q[1-9][0-9]*|[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]+)$'"
SOURCE_KEY = r"'^[a-z0-9][a-z0-9_.-]*$'"
RELATIONS = (
    "has_cause",
    "immediate_cause",
    "underlying_cause",
    "contributing_factor",
    "has_effect",
    "enabled",
    "precipitated",
    "prevented_or_delayed",
    "reaction_to",
    "coded_trigger",
    "coded_outcome",
)
_REL = ", ".join(f"'{r}'" for r in RELATIONS)


def _qid(col: str) -> str:
    return f"({col} ~ {QID} AND length({col}) <= 161)"


def _key(col: str) -> str:
    return f"({col} ~ {SOURCE_KEY} AND length({col}) <= 100)"


def _sha(col: str) -> str:
    return f"(length({col}) = 64 AND {col} !~ '[^0-9a-f]')"


def _obj(col: str) -> str:
    return f"jsonb_typeof({col}) = 'object'"


SQL_TABLES = rf"""
CREATE TABLE kg.causal_load (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('kg_statements','dataset','grading','candidates')),
  source_key text NOT NULL CHECK {_key("source_key")},
  dataset text CHECK (dataset IS NULL OR {_key("dataset")}),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  raw_sha256s text[] NOT NULL DEFAULT '{{}}' CHECK (cardinality(raw_sha256s) <= 10000),
  retrieved_at timestamptz,
  kg_load_watermark bigint,
  loader_version text NOT NULL CHECK (length(loader_version) BETWEEN 1 AND 100),
  parameters jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("parameters")}),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user,
  CHECK (retrieved_at IS NULL OR retrieved_at <= recorded_at + interval '5 minutes'),
  CHECK ((kind IN ('dataset','candidates')) = (cardinality(raw_sha256s) >= 1)),
  CHECK (kind NOT IN ('dataset','candidates') OR retrieved_at IS NOT NULL),
  CHECK ((kind = 'dataset') = (dataset IS NOT NULL))
);

CREATE TABLE kg.causal_claim (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.causal_load,
  claim_key text NOT NULL CHECK (length(claim_key) BETWEEN 4 AND 400),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  content_sha256 text NOT NULL CHECK {_sha("content_sha256")},
  cause_id text NOT NULL CHECK {_qid("cause_id")},
  effect_id text NOT NULL CHECK {_qid("effect_id")},
  relation text NOT NULL CHECK (relation IN ({_REL})),
  family text NOT NULL CHECK (family IN ('cause','prevention')),
  polarity text CHECK (polarity IS NULL OR polarity IN ('promotes','inhibits')),
  stance text NOT NULL DEFAULT 'asserts' CHECK (stance IN ('asserts','denies')),
  asserted_on text CHECK (asserted_on IS NULL OR asserted_on IN ('cause','effect')),
  source_key text NOT NULL CHECK {_key("source_key")},
  source_tier text NOT NULL CHECK (source_tier IN ('T1','T2','T3','T4')),
  dataset text CHECK (dataset IS NULL OR {_key("dataset")}),
  source_revision text CHECK (source_revision IS NULL OR length(source_revision) BETWEEN 1 AND 100),
  statement_key text CHECK (statement_key IS NULL OR length(statement_key) BETWEEN 3 AND 300),
  statement_id text CHECK (statement_id IS NULL OR length(statement_id) BETWEEN 1 AND 200),
  statement_row_id bigint REFERENCES kg.statement,
  selector jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("selector")}),
  raw_sha256 text CHECK (raw_sha256 IS NULL OR {_sha("raw_sha256")}),
  refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(refs) = 'array'),
  upstream_keys text[] NOT NULL DEFAULT '{{}}' CHECK (cardinality(upstream_keys) <= 200),
  citation jsonb CHECK (citation IS NULL OR {_obj("citation")}),
  codebook jsonb CHECK (codebook IS NULL OR {_obj("codebook")}),
  mechanism text CHECK (mechanism IS NULL OR length(mechanism) BETWEEN 1 AND 4000),
  scope jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("scope")}),
  qualifiers jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("qualifiers")}),
  contests text[] NOT NULL DEFAULT '{{}}' CHECK (cardinality(contests) <= 100),
  disputed_by jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(disputed_by) = 'array'),
  retrieved_at timestamptz NOT NULL,
  reinstates_row_id bigint,
  -- KG-derived claims (T3/T4) restate their kg.statement row; dataset claims (T1/T2) cite a raw artifact.
  CHECK ((source_tier IN ('T3','T4')) = (statement_row_id IS NOT NULL)),
  CHECK ((statement_row_id IS NULL) = (statement_key IS NULL)),
  CHECK (starts_with(claim_key, 'kg:') = (statement_row_id IS NOT NULL)),
  CHECK (statement_row_id IS NOT NULL OR (raw_sha256 IS NOT NULL AND dataset IS NOT NULL)),
  CHECK (source_tier <> 'T1' OR codebook IS NOT NULL),
  CHECK (source_tier <> 'T2' OR citation IS NOT NULL),
  CHECK ((family = 'prevention') = (relation = 'prevented_or_delayed' OR polarity IS NOT DISTINCT FROM 'inhibits')),
  CONSTRAINT causal_claim_version UNIQUE (claim_key, origin, load_id)
);
CREATE INDEX causal_claim_latest ON kg.causal_claim(claim_key, origin, id);
CREATE INDEX causal_claim_cause ON kg.causal_claim(cause_id, family);
CREATE INDEX causal_claim_effect ON kg.causal_claim(effect_id, family);
CREATE INDEX causal_claim_statement ON kg.causal_claim(statement_row_id) WHERE statement_row_id IS NOT NULL;

CREATE TABLE kg.causal_claim_retraction (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.causal_load,
  claim_row_id bigint NOT NULL REFERENCES kg.causal_claim,
  claim_key text NOT NULL,
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  reason text NOT NULL CHECK (reason IN ('removed_at_source','source_statement_retracted','no_longer_causal')),
  UNIQUE (claim_row_id)
);

CREATE TABLE kg.causal_grade (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.causal_load,
  claim_row_id bigint NOT NULL REFERENCES kg.causal_claim,
  claim_key text NOT NULL,
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  rules_version text NOT NULL CHECK (length(rules_version) BETWEEN 1 AND 50),
  rules_sha256 text NOT NULL CHECK {_sha("rules_sha256")},
  grade text NOT NULL CHECK (grade IN ('A','B','C','D','E')),
  rule_ids text[] NOT NULL CHECK (cardinality(rule_ids) BETWEEN 1 AND 50),
  temporal_check text NOT NULL CHECK (temporal_check IN ('consistent','overlapping','inconsistent','undetermined')),
  temporal jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("temporal")}),
  corroboration integer NOT NULL CHECK (corroboration >= 1),
  corroborating_claim_keys text[] NOT NULL DEFAULT '{{}}',
  upstream_groups jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(upstream_groups) = 'array'),
  contested_by text[] NOT NULL DEFAULT '{{}}',
  inputs_sha256 text NOT NULL CHECK {_sha("inputs_sha256")},
  UNIQUE (claim_row_id, load_id)
);
CREATE INDEX causal_grade_latest ON kg.causal_grade(claim_row_id, id);

CREATE TABLE kg.causal_candidate (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.causal_load,
  candidate_key text NOT NULL CHECK (length(candidate_key) BETWEEN 4 AND 400),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  content_sha256 text NOT NULL CHECK {_sha("content_sha256")},
  cause_id text NOT NULL CHECK {_qid("cause_id")},
  effect_id text NOT NULL CHECK {_qid("effect_id")},
  relation text NOT NULL CHECK (relation IN ({_REL})),
  family text NOT NULL CHECK (family IN ('cause','prevention')),
  polarity text CHECK (polarity IS NULL OR polarity IN ('promotes','inhibits')),
  tier text NOT NULL CHECK (tier IN ('T5','T6')),
  extractor_id text NOT NULL CHECK {_key("extractor_id")},
  extractor_version text NOT NULL CHECK (length(extractor_version) BETWEEN 1 AND 100),
  source_key text NOT NULL CHECK {_key("source_key")},
  source_revision text NOT NULL CHECK (length(source_revision) BETWEEN 1 AND 100),
  source_sentence text NOT NULL CHECK (length(source_sentence) BETWEEN 1 AND 4000),
  selector jsonb NOT NULL CHECK ({_obj("selector")}),
  raw_sha256 text NOT NULL CHECK {_sha("raw_sha256")},
  mechanism text CHECK (mechanism IS NULL OR length(mechanism) BETWEEN 1 AND 4000),
  retrieved_at timestamptz NOT NULL,
  CHECK ((family = 'prevention') = (relation = 'prevented_or_delayed' OR polarity IS NOT DISTINCT FROM 'inhibits')),
  CONSTRAINT causal_candidate_version UNIQUE (candidate_key, origin, content_sha256)
);
CREATE INDEX causal_candidate_latest ON kg.causal_candidate(candidate_key, origin, id);
CREATE INDEX causal_candidate_cause ON kg.causal_candidate(cause_id);
CREATE INDEX causal_candidate_effect ON kg.causal_candidate(effect_id);

CREATE TABLE kg.causal_review (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target text NOT NULL CHECK (target IN ('claim','candidate')),
  target_key text NOT NULL CHECK (length(target_key) BETWEEN 4 AND 400),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  status text NOT NULL CHECK (status IN ('accepted','rejected','needs_revision','promoted','not_promoted')),
  note text CHECK (note IS NULL OR length(note) BETWEEN 1 AND 4000),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user,
  CHECK (target = 'candidate' OR status IN ('accepted','rejected','needs_revision')),
  CHECK (target = 'claim' OR status IN ('promoted','rejected','not_promoted'))
);
CREATE INDEX causal_review_latest ON kg.causal_review(target, target_key, origin, id);
"""

SQL_VIEWS = r"""
CREATE VIEW kg.causal_claim_current AS
  SELECT c.*, l.recorded_at,
         COALESCE((SELECT r.status FROM kg.causal_review r
                    WHERE r.target = 'claim' AND r.target_key = c.claim_key AND r.origin = c.origin
                    ORDER BY r.id DESC LIMIT 1), 'unreviewed') AS review_status
    FROM kg.causal_claim c JOIN kg.causal_load l ON l.id = c.load_id
   WHERE NOT EXISTS (SELECT 1 FROM kg.causal_claim n
                      WHERE n.claim_key = c.claim_key AND n.origin = c.origin AND n.id > c.id)
     AND NOT EXISTS (SELECT 1 FROM kg.causal_claim_retraction r WHERE r.claim_row_id = c.id);

CREATE VIEW kg.causal_grade_current AS
  SELECT g.*, l.recorded_at AS graded_at
    FROM kg.causal_grade g JOIN kg.causal_load l ON l.id = g.load_id
   WHERE NOT EXISTS (SELECT 1 FROM kg.causal_grade n WHERE n.claim_row_id = g.claim_row_id AND n.id > g.id);

CREATE VIEW kg.causal_candidate_current AS
  SELECT c.*, l.recorded_at,
         COALESCE((SELECT r.status FROM kg.causal_review r
                    WHERE r.target = 'candidate' AND r.target_key = c.candidate_key AND r.origin = c.origin
                    ORDER BY r.id DESC LIMIT 1), 'not_promoted') AS promotion_status
    FROM kg.causal_candidate c JOIN kg.causal_load l ON l.id = c.load_id
   WHERE NOT EXISTS (SELECT 1 FROM kg.causal_candidate n
                      WHERE n.candidate_key = c.candidate_key AND n.origin = c.origin AND n.id > c.id);
"""

SQL_TRIGGERS = r"""
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON kg.causal_load
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON kg.causal_review
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();

-- A load's raw artifacts are listed once and are verified, located artifacts.
CREATE FUNCTION kg.check_causal_load() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE n int; bad text;
BEGIN
  SELECT count(DISTINCT x) INTO n FROM unnest(NEW.raw_sha256s) x;
  IF n <> cardinality(NEW.raw_sha256s) OR array_position(NEW.raw_sha256s, NULL) IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_load raw_sha256s must be distinct and non-null';
  END IF;
  SELECT x INTO bad FROM unnest(NEW.raw_sha256s) x WHERE NOT meta.artifact_is_verified(x) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_load raw artifact % has no verified storage location', bad;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER causal_load_derived_from_archive BEFORE INSERT ON kg.causal_load
  FOR EACH ROW EXECUTE FUNCTION kg.check_causal_load();

-- Claim rows restate their load (and, for KG-derived claims, their kg.statement row) and are inserted in the
-- transaction that created the load. Statement-level: one check per INSERT.
CREATE FUNCTION kg.check_causal_claim_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n JOIN kg.causal_load l ON l.id = n.load_id
   WHERE l.origin <> n.origin
      OR l.kind NOT IN ('kg_statements','dataset')
      OR (l.kind = 'kg_statements') <> (n.statement_row_id IS NOT NULL)
      OR (l.kind = 'dataset' AND ((l.source_key, l.dataset, l.retrieved_at) IS DISTINCT FROM
                                  (n.source_key, n.dataset, n.retrieved_at)
                                  OR NOT (n.raw_sha256 = ANY (l.raw_sha256s))))
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_claim row % does not match its load (kind, origin, source, dataset, raw artifact)', bad;
  END IF;
  SELECT n.id INTO bad FROM new_rows n LEFT JOIN kg.statement s ON s.id = n.statement_row_id
   WHERE n.statement_row_id IS NOT NULL
     AND (s.id IS NULL
          OR (s.statement_key, s.statement_id, s.origin, s.source_key, s.retrieved_at)
             IS DISTINCT FROM (n.statement_key, n.statement_id, n.origin, n.source_key, n.retrieved_at)
          OR s.raw_sha256 IS DISTINCT FROM n.raw_sha256
          OR s.source_revision IS DISTINCT FROM n.source_revision
          OR n.claim_key <> 'kg:' || s.statement_key)
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_claim row % does not restate its kg.statement row', bad;
  END IF;
  SELECT n.id INTO bad FROM new_rows n
   WHERE n.reinstates_row_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM kg.causal_claim c JOIN kg.causal_claim_retraction r ON r.claim_row_id = c.id
                      WHERE c.id = n.reinstates_row_id AND c.claim_key = n.claim_key AND c.origin = n.origin)
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_claim row % reinstates a row that is not a retracted row of the same claim', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.causal_load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER causal_claim_rows_match_load AFTER INSERT ON kg.causal_claim
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_causal_claim_rows();

CREATE FUNCTION kg.check_causal_retraction_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n
    JOIN kg.causal_load l ON l.id = n.load_id
    JOIN kg.causal_claim c ON c.id = n.claim_row_id
   WHERE (c.claim_key, c.origin) IS DISTINCT FROM (n.claim_key, n.origin)
      OR l.origin <> n.origin OR l.kind NOT IN ('kg_statements','dataset')
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_claim_retraction % does not match its claim or load', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.causal_load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER causal_retraction_rows_match_load AFTER INSERT ON kg.causal_claim_retraction
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_causal_retraction_rows();

CREATE FUNCTION kg.check_causal_grade_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n
    JOIN kg.causal_load l ON l.id = n.load_id
    LEFT JOIN kg.causal_claim c ON c.id = n.claim_row_id
   WHERE c.id IS NULL OR (c.claim_key, c.origin) IS DISTINCT FROM (n.claim_key, n.origin)
      OR l.origin <> n.origin OR l.kind <> 'grading'
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_grade % does not match its claim or grading load', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.causal_load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER causal_grade_rows_match_load AFTER INSERT ON kg.causal_grade
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_causal_grade_rows();

CREATE FUNCTION kg.check_causal_candidate_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n JOIN kg.causal_load l ON l.id = n.load_id
   WHERE l.kind <> 'candidates' OR l.origin <> n.origin
      OR (l.source_key, l.retrieved_at) IS DISTINCT FROM (n.source_key, n.retrieved_at)
      OR NOT (n.raw_sha256 = ANY (l.raw_sha256s))
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_candidate row % does not match its load (kind, origin, source, raw artifact)', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.causal_load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.causal_load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER causal_candidate_rows_match_load AFTER INSERT ON kg.causal_candidate
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_causal_candidate_rows();
"""

CAUSAL_TABLES = (
    "kg.causal_load",
    "kg.causal_claim",
    "kg.causal_claim_retraction",
    "kg.causal_grade",
    "kg.causal_candidate",
    "kg.causal_review",
)
#: The worker writes everything except reviews (agents cannot record reviews).
WORKER_INSERT = tuple(t for t in CAUSAL_TABLES if t != "kg.causal_review")
CAUSAL_VIEWS = ("kg.causal_claim_current", "kg.causal_grade_current", "kg.causal_candidate_current")
CAUSAL_FUNCTIONS = (
    "kg.check_causal_load()",
    "kg.check_causal_claim_rows()",
    "kg.check_causal_retraction_rows()",
    "kg.check_causal_grade_rows()",
    "kg.check_causal_candidate_rows()",
)

SQL_GRANTS = rf"""
REVOKE ALL ON {", ".join(CAUSAL_TABLES + CAUSAL_VIEWS)} FROM PUBLIC;
REVOKE ALL ON FUNCTION {", ".join(CAUSAL_FUNCTIONS)} FROM PUBLIC;
GRANT SELECT ON {", ".join(CAUSAL_TABLES + CAUSAL_VIEWS)} TO reva_api, reva_worker, reva_sim;
GRANT INSERT ON {", ".join(WORKER_INSERT)} TO reva_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA kg TO reva_worker;
REVOKE USAGE ON SEQUENCE kg.causal_review_id_seq FROM reva_worker;
GRANT EXECUTE ON FUNCTION {", ".join(CAUSAL_FUNCTIONS)} TO reva_worker;
"""

SQL_RESTRICTED_DELETION = rf"""
DO $$
DECLARE t text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    FOREACH t IN ARRAY ARRAY[{", ".join(f"'{t}'" for t in CAUSAL_TABLES)}] LOOP
      EXECUTE format('GRANT SELECT, DELETE ON %s TO reva_restricted_deletion', t);
    END LOOP;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_TABLES)
    op.execute(SQL_VIEWS)
    op.execute(SQL_TRIGGERS)
    op.execute(immutable(*CAUSAL_TABLES))
    # recorded_at is server time on kg.causal_load and kg.causal_review (batch rows inherit their load's).
    op.execute(server_recorded_at("kg"))
    op.execute(SQL_GRANTS)
    op.execute(SQL_RESTRICTED_DELETION)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
