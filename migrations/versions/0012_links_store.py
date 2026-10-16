"""V3 materialised link store (agent LINKS2).

Country–country "neurons" (``reva.links``) are derived from one atlas snapshot plus the KG as loaded up to a
watermark. Deriving them on every ``/v1/links/{iso3}`` request means re-reading millions of atlas cells (UNGA
votes, IMF IMTS, UN DESA migrant stock); the store keeps one materialisation per (atlas snapshot, KG watermark,
method version), written once by the worker and read by the API.

Tables (schema ``kg``: the links are a derived view of the world graph; same runtime roles and grants)
------------------------------------------------------------------------------------------------------
* ``kg.link_materialisation`` — one row per materialisation run: method version, taxonomy sha256, the atlas
  snapshot (manifest sha256 + origin) and KG watermark (highest ``kg.load`` id and its ``recorded_at``) it was
  derived from, the KG origin read (NULL = live and test_fixture), parameters (top-K...) and the derivation
  report (per link type: count, skipped reasons). ``recorded_at`` / ``recorded_by`` are server-side.
* ``kg.link_row`` — one row per link: type, facet (``issue=co``, ``section=XVI``...), endpoints (node ids:
  QID, ``iso3:<KEY>`` or a dataset entity) and their ISO3 keys when they are crosswalk countries, direction,
  year bounds (inclusive, NULL = open/undated) plus the exact temporal JSON, weight as the source's decimal
  string (``weight_num`` only orders rows) and unit, ``via`` nodes, evidence (non-empty JSON array of KG
  statement ids or atlas raw sha256 + selector), basis (method, formula, counts) and the evidence origin.

Both tables are append-only (``meta.reject_mutation``). Link rows must be inserted in the transaction that
created their materialisation (statement-level trigger, as for ``kg.load``), so a materialisation is complete
or absent. Grants: ``reva_worker`` inserts; ``reva_api`` / ``reva_sim`` read; ``reva_gate`` nothing (0011).

Revision ID: 0012_links_store
Revises: 0011_kg_fixes
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0012_links_store"
down_revision = "0011_kg_fixes"
branch_labels = None
depends_on = None

NODE = r"'^(Q[1-9][0-9]*|[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]+)$'"


def _node(col: str) -> str:
    return f"({col} ~ {NODE} AND length({col}) <= 161)"


SQL_TABLES = rf"""
CREATE TABLE kg.link_materialisation (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  method_version text NOT NULL CHECK (length(method_version) BETWEEN 1 AND 100),
  taxonomy_sha256 text NOT NULL CHECK (length(taxonomy_sha256) = 64 AND taxonomy_sha256 !~ '[^0-9a-f]'),
  atlas_snapshot_sha256 text CHECK (atlas_snapshot_sha256 IS NULL
    OR (length(atlas_snapshot_sha256) = 64 AND atlas_snapshot_sha256 !~ '[^0-9a-f]')),
  atlas_origin text CHECK (atlas_origin IS NULL OR atlas_origin IN ('live','test_fixture')),
  kg_origin text CHECK (kg_origin IS NULL OR kg_origin IN ('live','test_fixture')),
  kg_load_watermark bigint,
  kg_recorded_watermark timestamptz,
  parameters jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(parameters) = 'object'),
  report jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(report) = 'object'),
  link_count integer NOT NULL CHECK (link_count >= 0),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user,
  CHECK ((atlas_snapshot_sha256 IS NULL) = (atlas_origin IS NULL))
);
CREATE INDEX link_materialisation_snapshot ON kg.link_materialisation(atlas_snapshot_sha256, id);

CREATE TABLE kg.link_row (
  materialisation_id bigint NOT NULL REFERENCES kg.link_materialisation,
  link_type text NOT NULL CHECK (link_type ~ '^[a-z][a-z0-9_]*$' AND length(link_type) <= 64),
  facet text NOT NULL DEFAULT '' CHECK (length(facet) <= 100),
  subject text NOT NULL CHECK {_node("subject")},
  object text NOT NULL CHECK {_node("object")},
  subject_iso3 text CHECK (subject_iso3 IS NULL OR subject_iso3 ~ '^[A-Z]{{3}}$'),
  object_iso3 text CHECK (object_iso3 IS NULL OR object_iso3 ~ '^[A-Z]{{3}}$'),
  directed boolean NOT NULL,
  year_lo integer,
  year_hi integer,
  temporal jsonb NOT NULL CHECK (jsonb_typeof(temporal) = 'object'),
  weight text CHECK (weight IS NULL OR length(weight) BETWEEN 1 AND 64),
  weight_num double precision,
  unit text CHECK (unit IS NULL OR length(unit) BETWEEN 1 AND 200),
  via text[] NOT NULL DEFAULT '{{}}',
  evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) >= 1),
  basis jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(basis) = 'object'),
  origin text NOT NULL CHECK (origin IN ('live','test_fixture','mixed','unknown')),
  CHECK (subject <> object),
  CHECK ((weight IS NULL) = (weight_num IS NULL)),
  CHECK (weight IS NULL OR unit IS NOT NULL),
  CHECK (year_lo IS NULL OR year_hi IS NULL OR year_lo <= year_hi)
);
CREATE INDEX link_row_subject_iso3 ON kg.link_row(materialisation_id, subject_iso3, link_type)
  WHERE subject_iso3 IS NOT NULL;
CREATE INDEX link_row_object_iso3 ON kg.link_row(materialisation_id, object_iso3, link_type)
  WHERE object_iso3 IS NOT NULL;
CREATE INDEX link_row_subject ON kg.link_row(materialisation_id, subject);
CREATE INDEX link_row_object ON kg.link_row(materialisation_id, object);

CREATE FUNCTION kg.check_link_rows() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE bad bigint;
BEGIN
  SELECT m.id INTO bad FROM kg.link_materialisation m
   WHERE m.id IN (SELECT DISTINCT materialisation_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(m.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.link_materialisation % is sealed: its link rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER link_rows_match_materialisation AFTER INSERT ON kg.link_row
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION kg.check_link_rows();
"""

LINK_TABLES = ("kg.link_materialisation", "kg.link_row")

SQL_GRANTS = rf"""
REVOKE ALL ON {", ".join(LINK_TABLES)} FROM PUBLIC;
REVOKE ALL ON FUNCTION kg.check_link_rows() FROM PUBLIC;
GRANT SELECT ON {", ".join(LINK_TABLES)} TO reva_api, reva_worker, reva_sim;
GRANT INSERT ON {", ".join(LINK_TABLES)} TO reva_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA kg TO reva_worker;
GRANT EXECUTE ON FUNCTION kg.check_link_rows() TO reva_worker;
"""

SQL_RESTRICTED_DELETION = rf"""
DO $$
DECLARE t text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    FOREACH t IN ARRAY ARRAY[{", ".join(f"'{t}'" for t in LINK_TABLES)}] LOOP
      EXECUTE format('GRANT SELECT, DELETE ON %s TO reva_restricted_deletion', t);
    END LOOP;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_TABLES)
    op.execute(immutable(*LINK_TABLES))
    op.execute(server_recorded_at("kg"))
    op.execute(SQL_GRANTS)
    op.execute(SQL_RESTRICTED_DELETION)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
