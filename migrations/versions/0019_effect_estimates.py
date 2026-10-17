"""V5 effect estimates (agent PRED-EFFECT; spec docs/roadmap/v5_prediction_engine.md, decision 3).

Effect sizes enter REVA **only as attributed numbers**: a published estimate (citation verbatim, locator, verbatim
quote, archived source file), a REVA estimation run (run id, code version, data snapshot, as-of) or an accounting
identity / physical law. REVA asserts no effect. Contract: ``docs/dev/v5_effect_interface.md``.

Tables (schema ``kg``, same runtime roles as the causal tables of 0015; everything append-only)
---------------------------------------------------------------------------------------------
* ``kg.effect_load`` — one row per write batch: ``kind`` (``publication`` / ``reva_run`` / ``identity``), source
  key, dataset (curation file or run family), origin, raw artifact sha256s (each a verified ``meta.artifact``;
  at least one for ``publication``), retrieval time, loader version, parameters; server ``recorded_at`` /
  ``recorded_by``. Rows of the tables below must be inserted in the transaction that created their load.
* ``kg.effect_estimate`` — estimate versions: cause (atlas concept / effect variable / event type / KG event) and
  effect (atlas concept / effect variable) with the source's verbatim definitions and their transformations,
  measure + unit (unit allowed for the measure), shock size for responses, sign (= sign of the point), point and
  the number as printed, uncertainty (SE, interval + level + kind, archived draws), horizon and lag profile
  (``irf``), scope, method, identification, meta-analysis facts, source (kind, key, citation, locator, verbatim
  quote, selector, transcription status, raw sha256 in the load, REVA run or identity), knowledge date
  ``as_of``, REVA's applicability note (labelled), verbatim source caveats, optional causal claim keys, rights,
  curation. A changed estimate is a new row; ``reinstates_row_id`` marks one that came back after a retraction.
* ``kg.effect_estimate_retraction`` — an estimate left the current view (reason + detail).

View ``kg.effect_estimate_current``: latest row per (estimate key, origin), not retracted, with the load's
``recorded_at``.

Grants: ``reva_worker`` inserts and reads; ``reva_api`` / ``reva_sim`` read; ``reva_gate`` nothing; functions
revoked from PUBLIC; the restricted-deletion role covers the tables.

Revision ID: 0019_effect_estimates
Revises: 0018_retraction_reasons
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0019_effect_estimates"
down_revision = "0018_retraction_reasons"
branch_labels = None
depends_on = None

SOURCE_KEY = r"'^[a-z0-9][a-z0-9_.-]*$'"
ESTIMATE_KEY = r"'^(pub|run|idn):[A-Za-z0-9_.:-]+$'"  # + 3..300 chars after the prefix (length CHECK)
ENTITY_ID = r"'^(Q[1-9][0-9]*|[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]+)$'"
VAR_ID = r"'^[a-z0-9][a-z0-9_.-]{0,199}$'"
TRANSFORMS = (
    "log_change",
    "percent_change",
    "pp_change",
    "level_change",
    "share_of_gdp_change",
    "std_dev_shock",
    "occurrence",
    "level",
    "log_level",
    "growth_rate",
    "gap",
)
MEASURE_UNITS = {
    "elasticity": ("ratio", "percent_per_percent"),
    "pass_through": ("ratio", "percent_per_percent"),
    "semi_elasticity": ("percent_per_pp", "percent_per_unit"),
    "slope": ("pp_per_pp", "pp_per_percent", "unit_per_unit"),
    "multiplier": ("currency_per_currency", "ratio"),
    "response": ("percent", "percentage_points", "log_points"),
    "identity_coefficient": None,
}
UNITS = (
    "ratio",
    "percent_per_percent",
    "percent_per_pp",
    "percent_per_unit",
    "pp_per_pp",
    "pp_per_percent",
    "unit_per_unit",
    "currency_per_currency",
    "percent",
    "percentage_points",
    "log_points",
)
METHODS = (
    "meta_analysis",
    "rct",
    "iv",
    "did",
    "rdd",
    "synthetic_control",
    "event_study",
    "local_projection",
    "var",
    "svar",
    "bvar",
    "dsge",
    "structural_model",
    "input_output",
    "panel_regression",
    "time_series_regression",
    "cross_section_regression",
    "gravity",
    "calibration",
    "accounting_identity",
    "physical_law",
    "other",
)
RETRACTION_REASONS = ("withdrawn_by_curator", "removed_at_source", "absent_from_delivery", "run_invalidated")


def _in(vals: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in vals)


def _key(col: str) -> str:
    return f"({col} ~ {SOURCE_KEY} AND length({col}) <= 100)"


def _sha(col: str) -> str:
    return f"(length({col}) = 64 AND {col} !~ '[^0-9a-f]')"


def _obj(col: str) -> str:
    return f"jsonb_typeof({col}) = 'object'"


def _txt(col: str, hi: int = 4000, null: bool = True) -> str:
    base = f"length(btrim({col})) BETWEEN 1 AND {hi}"
    return f"({col} IS NULL OR {base})" if null else f"({base})"


_UNIT_RULES = " OR ".join(
    f"(measure = '{m}'" + (f" AND unit IN ({_in(u)}))" if u else ")") for m, u in MEASURE_UNITS.items()
)

SQL_TABLES = rf"""
CREATE TABLE kg.effect_load (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('publication','reva_run','identity')),
  source_key text NOT NULL CHECK {_key("source_key")},
  dataset text NOT NULL CHECK {_key("dataset")},
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  raw_sha256s text[] NOT NULL DEFAULT '{{}}' CHECK (cardinality(raw_sha256s) <= 10000),
  retrieved_at timestamptz NOT NULL,
  loader_version text NOT NULL CHECK (length(loader_version) BETWEEN 1 AND 100),
  parameters jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("parameters")} AND octet_length(parameters::text) <= 1048576),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user,
  CHECK (retrieved_at <= recorded_at + interval '5 minutes'),
  CHECK (kind <> 'publication' OR cardinality(raw_sha256s) >= 1)
);

CREATE TABLE kg.effect_estimate (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.effect_load,
  estimate_key text NOT NULL CHECK (estimate_key ~ {ESTIMATE_KEY} AND length(estimate_key) BETWEEN 7 AND 304),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  content_sha256 text NOT NULL CHECK {_sha("content_sha256")},
  cause_kind text NOT NULL CHECK (cause_kind IN ('atlas_concept','variable','event_type','event')),
  cause_id text NOT NULL CHECK (length(cause_id) BETWEEN 1 AND 200),
  cause_definition text NOT NULL CHECK {_txt("cause_definition", null=False)},
  cause_transform text NOT NULL CHECK (cause_transform IN ({_in(TRANSFORMS)})),
  effect_kind text NOT NULL CHECK (effect_kind IN ('atlas_concept','variable')),
  effect_id text NOT NULL CHECK (effect_id ~ {VAR_ID}),
  effect_definition text NOT NULL CHECK {_txt("effect_definition", null=False)},
  effect_transform text NOT NULL CHECK (effect_transform IN ({_in(TRANSFORMS)})
                                        AND effect_transform NOT IN ('occurrence','std_dev_shock')),
  measure text NOT NULL CHECK (measure IN ({_in(tuple(MEASURE_UNITS))})),
  unit text NOT NULL CHECK (unit IN ({_in(UNITS)})),
  shock_size numeric CHECK (shock_size IS NULL OR shock_size <> 0),
  shock_unit text CHECK (shock_unit IS NULL OR length(shock_unit) BETWEEN 1 AND 100),
  sign text NOT NULL CHECK (sign IN ('positive','negative','zero','ambiguous')),
  point numeric,
  point_kind text CHECK (point_kind IS NULL OR point_kind IN ('point','mean','median')),
  as_printed text CHECK (as_printed IS NULL OR length(as_printed) BETWEEN 1 AND 100),
  se numeric CHECK (se IS NULL OR se > 0),
  ci_low numeric,
  ci_high numeric,
  ci_level numeric CHECK (ci_level IS NULL OR (ci_level > 0 AND ci_level < 1)),
  interval_kind text CHECK (interval_kind IS NULL OR interval_kind IN
                            ('confidence','credible','range_across_estimates','interquartile','min_max')),
  draws jsonb CHECK (draws IS NULL OR {_obj("draws")}),
  uncertainty_status text NOT NULL CHECK (uncertainty_status IN ('reported','partial','not_reported')),
  horizon_kind text NOT NULL CHECK (horizon_kind IN
                                    ('impact','at_horizon','cumulative','peak','average','long_run','unspecified')),
  horizon_value numeric CHECK (horizon_value IS NULL OR horizon_value >= 0),
  horizon_unit text CHECK (horizon_unit IS NULL OR horizon_unit IN ('day','month','quarter','year')),
  irf jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(irf) = 'array' AND jsonb_array_length(irf) <= 400),
  scope jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("scope")}),
  method text NOT NULL CHECK (method IN ({_in(METHODS)})),
  identification text CHECK {_txt("identification")},
  meta jsonb CHECK (meta IS NULL OR {_obj("meta")}),
  source_kind text NOT NULL CHECK (source_kind IN ('publication','reva_run','identity')),
  source_key text NOT NULL CHECK {_key("source_key")},
  dataset text NOT NULL CHECK {_key("dataset")},
  citation jsonb CHECK (citation IS NULL OR {_obj("citation")}),
  locator text CHECK {_txt("locator", 1000)},
  quote text CHECK {_txt("quote")},
  selector jsonb CHECK (selector IS NULL OR {_obj("selector")}),
  transcription text NOT NULL CHECK (transcription IN
                                     ('machine_readable','transcribed','transcribed_double_checked','computed')),
  raw_sha256 text CHECK (raw_sha256 IS NULL OR {_sha("raw_sha256")}),
  run jsonb CHECK (run IS NULL OR {_obj("run")}),
  identity jsonb CHECK (identity IS NULL OR {_obj("identity")}),
  as_of date NOT NULL,
  applicability text CHECK {_txt("applicability")},
  source_caveats text CHECK {_txt("source_caveats")},
  causal_claim_keys text[] NOT NULL DEFAULT '{{}}' CHECK (cardinality(causal_claim_keys) <= 100),
  rights jsonb CHECK (rights IS NULL OR {_obj("rights")}),
  curation jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK ({_obj("curation")}),
  retrieved_at timestamptz NOT NULL,
  reinstates_row_id bigint,
  -- ids by kind
  CHECK (cause_kind = 'event' OR cause_id ~ {VAR_ID}),
  CHECK (cause_kind <> 'event' OR cause_id ~ {ENTITY_ID}),
  -- the unit is allowed for the measure; a response states its shock
  CHECK ({_UNIT_RULES}),
  CHECK ((shock_size IS NULL) = (shock_unit IS NULL)),
  CHECK (measure <> 'response' OR shock_size IS NOT NULL),
  -- sign = sign of the point
  CHECK (point IS NULL OR sign = CASE WHEN point > 0 THEN 'positive' WHEN point < 0 THEN 'negative' ELSE 'zero' END),
  CHECK ((point IS NULL) = (point_kind IS NULL)),
  -- intervals: both bounds, ordered, around the point, with a kind; a level for confidence/credible intervals
  CHECK ((ci_low IS NULL) = (ci_high IS NULL)),
  CHECK (ci_low IS NULL OR ci_low <= ci_high),
  CHECK (ci_low IS NULL OR point IS NULL OR (ci_low <= point AND point <= ci_high)),
  CHECK ((ci_low IS NULL) = (interval_kind IS NULL)),
  CHECK (interval_kind IS NULL OR interval_kind NOT IN ('confidence','credible') OR ci_level IS NOT NULL),
  CHECK (ci_level IS NULL OR ci_low IS NOT NULL),
  CHECK (uncertainty_status <> 'reported'
         OR se IS NOT NULL OR draws IS NOT NULL OR interval_kind IN ('confidence','credible')),
  CHECK (uncertainty_status <> 'not_reported' OR (se IS NULL AND ci_low IS NULL AND draws IS NULL)),
  -- horizon
  CHECK ((horizon_value IS NULL) = (horizon_unit IS NULL)),
  CHECK (horizon_kind NOT IN ('long_run','unspecified') OR horizon_value IS NULL),
  -- source kind requirements
  CHECK (source_kind <> 'publication' OR (citation IS NOT NULL AND locator IS NOT NULL AND raw_sha256 IS NOT NULL
                                          AND transcription <> 'computed' AND run IS NULL)),
  CHECK (source_kind <> 'publication' OR transcription = 'machine_readable' OR (quote IS NOT NULL AND as_printed IS NOT NULL)),
  CHECK (transcription <> 'machine_readable' OR selector IS NOT NULL),
  CHECK (source_kind <> 'reva_run' OR (run IS NOT NULL AND transcription = 'computed')),
  CHECK (source_kind <> 'identity' OR (identity IS NOT NULL AND transcription = 'computed')),
  CHECK (draws IS NULL OR raw_sha256 IS NOT NULL OR draws ? 'raw_sha256'),
  CONSTRAINT effect_estimate_version UNIQUE (estimate_key, origin, load_id)
);
CREATE INDEX effect_estimate_latest ON kg.effect_estimate(estimate_key, origin, id);
CREATE INDEX effect_estimate_cause ON kg.effect_estimate(cause_id, effect_id);
CREATE INDEX effect_estimate_effect ON kg.effect_estimate(effect_id);

CREATE TABLE kg.effect_estimate_retraction (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id bigint NOT NULL REFERENCES kg.effect_load,
  estimate_row_id bigint NOT NULL REFERENCES kg.effect_estimate,
  estimate_key text NOT NULL,
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  reason text NOT NULL CHECK (reason IN ({_in(RETRACTION_REASONS)})),
  detail jsonb CHECK (detail IS NULL OR ({_obj("detail")} AND octet_length(detail::text) <= 8192)),
  UNIQUE (estimate_row_id)
);
"""

SQL_VIEWS = r"""
CREATE VIEW kg.effect_estimate_current AS
  SELECT e.*, l.recorded_at
    FROM kg.effect_estimate e JOIN kg.effect_load l ON l.id = e.load_id
   WHERE NOT EXISTS (SELECT 1 FROM kg.effect_estimate n
                      WHERE n.estimate_key = e.estimate_key AND n.origin = e.origin AND n.id > e.id)
     AND NOT EXISTS (SELECT 1 FROM kg.effect_estimate_retraction r WHERE r.estimate_row_id = e.id);
"""

SQL_TRIGGERS = r"""
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON kg.effect_load
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();

-- A load's raw artifacts are listed once and are verified, located artifacts.
CREATE FUNCTION kg.check_effect_load() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE n int; bad text;
BEGIN
  SELECT count(DISTINCT x) INTO n FROM unnest(NEW.raw_sha256s) x;
  IF n <> cardinality(NEW.raw_sha256s) OR array_position(NEW.raw_sha256s, NULL) IS NOT NULL THEN
    RAISE EXCEPTION 'kg.effect_load raw_sha256s must be distinct and non-null';
  END IF;
  SELECT x INTO bad FROM unnest(NEW.raw_sha256s) x WHERE NOT meta.artifact_is_verified(x) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.effect_load raw artifact % has no verified storage location', bad;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER effect_load_derived_from_archive BEFORE INSERT ON kg.effect_load
  FOR EACH ROW EXECUTE FUNCTION kg.check_effect_load();

-- Estimate rows restate their load (kind, origin, source, dataset, retrieval time), cite one of its raw
-- artifacts, and are inserted in the transaction that created the load. Statement-level: one check per INSERT.
CREATE FUNCTION kg.check_effect_estimate_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n JOIN kg.effect_load l ON l.id = n.load_id
   WHERE l.origin <> n.origin OR l.kind <> n.source_kind
      OR (l.source_key, l.dataset, l.retrieved_at) IS DISTINCT FROM (n.source_key, n.dataset, n.retrieved_at)
      OR (n.raw_sha256 IS NOT NULL AND NOT (n.raw_sha256 = ANY (l.raw_sha256s)))
      OR (n.draws ? 'raw_sha256' AND NOT ((n.draws->>'raw_sha256') = ANY (l.raw_sha256s)))
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.effect_estimate row % does not match its load (kind, origin, source, dataset, raw artifact)', bad;
  END IF;
  SELECT n.id INTO bad FROM new_rows n
   WHERE n.reinstates_row_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM kg.effect_estimate c
                       JOIN kg.effect_estimate_retraction r ON r.estimate_row_id = c.id
                      WHERE c.id = n.reinstates_row_id AND c.estimate_key = n.estimate_key AND c.origin = n.origin)
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.effect_estimate row % reinstates a row that is not a retracted row of the same estimate', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.effect_load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.effect_load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER effect_estimate_rows_match_load AFTER INSERT ON kg.effect_estimate
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_effect_estimate_rows();

CREATE FUNCTION kg.check_effect_retraction_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT n.id INTO bad FROM new_rows n
    JOIN kg.effect_load l ON l.id = n.load_id
    JOIN kg.effect_estimate e ON e.id = n.estimate_row_id
   WHERE (e.estimate_key, e.origin) IS DISTINCT FROM (n.estimate_key, n.origin)
      OR l.origin <> n.origin OR (l.source_key, l.dataset) IS DISTINCT FROM (e.source_key, e.dataset)
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.effect_estimate_retraction % does not match its estimate or load', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.effect_load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.effect_load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER effect_retraction_rows_match_load AFTER INSERT ON kg.effect_estimate_retraction
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_effect_retraction_rows();
"""

EFFECT_TABLES = ("kg.effect_load", "kg.effect_estimate", "kg.effect_estimate_retraction")
EFFECT_VIEWS = ("kg.effect_estimate_current",)
EFFECT_FUNCTIONS = (
    "kg.check_effect_load()",
    "kg.check_effect_estimate_rows()",
    "kg.check_effect_retraction_rows()",
)
EFFECT_SEQUENCES = ("kg.effect_load_id_seq", "kg.effect_estimate_id_seq", "kg.effect_estimate_retraction_id_seq")

SQL_GRANTS = rf"""
REVOKE ALL ON {", ".join(EFFECT_TABLES + EFFECT_VIEWS)} FROM PUBLIC;
REVOKE ALL ON FUNCTION {", ".join(EFFECT_FUNCTIONS)} FROM PUBLIC;
GRANT SELECT ON {", ".join(EFFECT_TABLES + EFFECT_VIEWS)} TO reva_api, reva_worker, reva_sim;
GRANT INSERT ON {", ".join(EFFECT_TABLES)} TO reva_worker;
GRANT USAGE ON SEQUENCE {", ".join(EFFECT_SEQUENCES)} TO reva_worker;
GRANT EXECUTE ON FUNCTION {", ".join(EFFECT_FUNCTIONS)} TO reva_worker;
"""

SQL_RESTRICTED_DELETION = rf"""
DO $$
DECLARE t text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    FOREACH t IN ARRAY ARRAY[{", ".join(f"'{t}'" for t in EFFECT_TABLES)}] LOOP
      EXECUTE format('GRANT SELECT, DELETE ON %s TO reva_restricted_deletion', t);
    END LOOP;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_TABLES)
    op.execute(SQL_VIEWS)
    op.execute(SQL_TRIGGERS)
    op.execute(immutable(*EFFECT_TABLES))
    # recorded_at is server time on kg.effect_load (estimate rows inherit their load's).
    op.execute(server_recorded_at("kg"))
    op.execute(SQL_GRANTS)
    op.execute(SQL_RESTRICTED_DELETION)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
