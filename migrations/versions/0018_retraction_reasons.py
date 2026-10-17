"""V3 honest retraction reasons (agent FIXA, audit N1 of docs/dev/review_v3_causal_audit2.md).

Every retraction written so far (4,264 ``kg.causal_claim_retraction`` rows, 1,635 ``kg.statement_retraction`` rows)
says ``removed_at_source``, although in every case the provider file was byte-identical and REVA's connector or
mapping changed (PA-X context reading, Archigos coups, ESRB cells, ...). The reason was hard-coded by the loaders.
The rows are append-only and are never edited; this migration lets new rows say what happened and lets a corrected
reason be recorded beside an old row, with its evidence.

What this migration adds
------------------------
* Reasons. ``kg.causal_claim_retraction.reason`` and ``kg.statement_retraction.reason`` also accept
  ``reclassified_by_reva`` (the source row is still there — same archived raw bytes, or a row-presence check by
  the caller — but REVA's connector / mapping no longer yields the claim or statement) and
  ``absent_from_delivery`` (the claim or statement is absent from a new delivery whose raw bytes changed; whether
  the provider removed the row was not checked). ``removed_at_source`` stays for a row verified absent from a new
  provider revision. Existing rows are re-validated by the new CHECK, not changed.
* ``detail jsonb`` (nullable) on both retraction tables: the basis of the reason (``raw_unchanged`` /
  ``row_absent_from_new_revision`` / ``row_present_in_new_revision`` / ``raw_changed_row_not_checked``), the raw
  sha256 before / in the delivery, the source revision and connector from -> to. NULL on the rows written before
  this migration (nothing is backfilled: ADD COLUMN, no UPDATE).
* ``kg.retraction_annotation`` — append-only (``meta.reject_mutation``) corrections of a retraction's reason: target
  table + retraction id, the reason as recorded on the row (restated and checked), the corrected reason, the
  evidence (object, e.g. the raw sha256 of the retracted row and of the retracting load), a note, the tool that
  derived it; server ``recorded_at`` / ``recorded_by``. A BEFORE INSERT trigger refuses an annotation whose target
  row does not exist, whose origin or recorded reason differ from the row, or whose corrected reason is not valid
  for the target table or equals the recorded one. One annotation per (target, retraction, corrected reason).
  ``reva_worker`` inserts (``reva causal annotate-retractions``), the readers select.
* ``kg.retraction_reason_current`` — every retraction of both tables with its recorded reason, the corrected reason
  of its latest annotation (if any) and the effective reason.

Revision ID: 0018_retraction_reasons
Revises: 0017_links_retention
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0018_retraction_reasons"
down_revision = "0017_links_retention"
branch_labels = None
depends_on = None

CLAIM_REASONS = (
    "removed_at_source",
    "source_statement_retracted",
    "no_longer_causal",
    "reclassified_by_reva",
    "absent_from_delivery",
)
STATEMENT_REASONS = ("deprecated_at_source", "removed_at_source", "reclassified_by_reva", "absent_from_delivery")
ALL_REASONS = tuple(dict.fromkeys(CLAIM_REASONS + STATEMENT_REASONS))


def _in(vals: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in vals)


SQL_REASONS = rf"""
ALTER TABLE kg.causal_claim_retraction DROP CONSTRAINT causal_claim_retraction_reason_check;
ALTER TABLE kg.causal_claim_retraction ADD CONSTRAINT causal_claim_retraction_reason_check
  CHECK (reason IN ({_in(CLAIM_REASONS)}));
ALTER TABLE kg.causal_claim_retraction ADD COLUMN detail jsonb
  CHECK (detail IS NULL OR (jsonb_typeof(detail) = 'object' AND octet_length(detail::text) <= 8192));
ALTER TABLE kg.statement_retraction DROP CONSTRAINT statement_retraction_reason_check;
ALTER TABLE kg.statement_retraction ADD CONSTRAINT statement_retraction_reason_check
  CHECK (reason IN ({_in(STATEMENT_REASONS)}));
ALTER TABLE kg.statement_retraction ADD COLUMN detail jsonb
  CHECK (detail IS NULL OR (jsonb_typeof(detail) = 'object' AND octet_length(detail::text) <= 8192));
"""

SQL_TABLE = rf"""
CREATE TABLE kg.retraction_annotation (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target text NOT NULL CHECK (target IN ('causal_claim_retraction','statement_retraction')),
  retraction_id bigint NOT NULL,
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  recorded_reason text NOT NULL CHECK (recorded_reason IN ({_in(ALL_REASONS)})),
  corrected_reason text NOT NULL CHECK (corrected_reason IN ({_in(ALL_REASONS)})),
  evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object' AND octet_length(evidence::text) <= 16384),
  note text NOT NULL CHECK (length(btrim(note)) BETWEEN 1 AND 2000),
  annotator text NOT NULL CHECK (length(annotator) BETWEEN 1 AND 100),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user,
  CHECK (corrected_reason <> recorded_reason),
  UNIQUE (target, retraction_id, corrected_reason)
);
CREATE INDEX retraction_annotation_target ON kg.retraction_annotation(target, retraction_id, id);

CREATE FUNCTION kg.check_retraction_annotation() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE o text; r text;
BEGIN
  IF NEW.target = 'causal_claim_retraction' THEN
    SELECT x.origin, x.reason INTO o, r FROM kg.causal_claim_retraction x WHERE x.id = NEW.retraction_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'kg.retraction_annotation: no kg.causal_claim_retraction %', NEW.retraction_id;
    END IF;
    IF NEW.corrected_reason NOT IN ({_in(CLAIM_REASONS)}) THEN
      RAISE EXCEPTION 'kg.retraction_annotation: % is not a causal claim retraction reason', NEW.corrected_reason;
    END IF;
  ELSE
    SELECT x.origin, x.reason INTO o, r FROM kg.statement_retraction x WHERE x.id = NEW.retraction_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'kg.retraction_annotation: no kg.statement_retraction %', NEW.retraction_id;
    END IF;
    IF NEW.corrected_reason NOT IN ({_in(STATEMENT_REASONS)}) THEN
      RAISE EXCEPTION 'kg.retraction_annotation: % is not a statement retraction reason', NEW.corrected_reason;
    END IF;
  END IF;
  IF o <> NEW.origin OR r <> NEW.recorded_reason THEN
    RAISE EXCEPTION 'kg.retraction_annotation must restate the origin and reason of % %', NEW.target,
      NEW.retraction_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER b01_check_retraction_annotation BEFORE INSERT ON kg.retraction_annotation
  FOR EACH ROW EXECUTE FUNCTION kg.check_retraction_annotation();
CREATE TRIGGER a01_recorded_by BEFORE INSERT ON kg.retraction_annotation
  FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_by();
"""

SQL_VIEW = r"""
CREATE VIEW kg.retraction_reason_current AS
  WITH r AS (
    SELECT 'causal_claim_retraction'::text AS target, x.id AS retraction_id, x.origin, x.load_id,
           x.claim_key AS record_key, x.reason AS recorded_reason, x.detail
      FROM kg.causal_claim_retraction x
    UNION ALL
    SELECT 'statement_retraction'::text, x.id, x.origin, x.load_id, x.statement_key, x.reason, x.detail
      FROM kg.statement_retraction x)
  SELECT r.target, r.retraction_id, r.origin, r.load_id, r.record_key, r.recorded_reason, r.detail,
         a.id AS annotation_id, a.corrected_reason, COALESCE(a.corrected_reason, r.recorded_reason) AS effective_reason
    FROM r
    LEFT JOIN LATERAL (SELECT n.id, n.corrected_reason FROM kg.retraction_annotation n
                        WHERE n.target = r.target AND n.retraction_id = r.retraction_id
                        ORDER BY n.id DESC LIMIT 1) a ON true;
"""

SQL_GRANTS = r"""
REVOKE ALL ON kg.retraction_annotation, kg.retraction_reason_current FROM PUBLIC;
REVOKE ALL ON FUNCTION kg.check_retraction_annotation() FROM PUBLIC;
GRANT SELECT ON kg.retraction_annotation, kg.retraction_reason_current TO reva_api, reva_worker, reva_sim;
GRANT INSERT ON kg.retraction_annotation TO reva_worker;
GRANT USAGE ON SEQUENCE kg.retraction_annotation_id_seq TO reva_worker;
GRANT EXECUTE ON FUNCTION kg.check_retraction_annotation() TO reva_worker;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    GRANT SELECT, DELETE ON kg.retraction_annotation TO reva_restricted_deletion;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_REASONS)
    op.execute(SQL_TABLE)
    op.execute(immutable("kg.retraction_annotation"))
    op.execute(server_recorded_at("kg"))
    op.execute(SQL_VIEW)
    op.execute(SQL_GRANTS)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
