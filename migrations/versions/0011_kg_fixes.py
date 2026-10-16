"""V3 review fixes for the KG store (agent REVFIX; docs/dev/review_v3.md KG-1, SEC-2).

KG-1 — a retracted statement of a source without revision ids can come back
---------------------------------------------------------------------------
A statement is identified by ``(statement_key, origin, source_revision)``. For sources that give no
revision id (WDQS rows, UCDP/UNGA/CShapes dataset statements: ``source_revision`` NULL) a statement
retracted after one delivery omitted it could never be stored again: the identity (NULLS NOT DISTINCT)
refused a second NULL-revision row. Rule added here:

* ``kg.statement.reinstates_row_id`` (nullable) — set only on a *reinstatement*: a revision-less
  delivery re-asserting a statement whose latest stored row is retracted by a retraction recorded from
  a delivery retrieved *before* this one. It names that retracted row.
* The identity becomes ``(statement_key, origin, source_revision, reinstates_row_id)`` NULLS NOT
  DISTINCT: ordinary rows keep exactly the old identity (NULL there); each reinstatement is unique per
  retracted row, so load → retract → reload → retract → reload works and re-running the same reload is
  idempotent (the reinstated row is then the latest, non-retracted row: ``unchanged``). The
  ``(statement_key, origin)`` prefix still serves the current-view anti-join.
* ``kg.check_statement_rows()`` additionally refuses a row whose ``reinstates_row_id`` is not a retracted
  row of the same key and origin.

SEC-2 — read grants on ``kg`` match the threat model
----------------------------------------------------
0009 granted SELECT on every ``kg`` table to ``reva_gate``, whose read set ``docs/threat_model.md``
limits to its decision inputs ("nothing else"). The gate never reads the world graph: USAGE and SELECT
are revoked. ``reva_sim`` keeps read access (world-graph context for simulations; read-only, CC0 data)
and the threat model now says so.

Revision ID: 0011_kg_fixes
Revises: 0010_atlas_ops
"""

from alembic import op

revision = "0011_kg_fixes"
down_revision = "0010_atlas_ops"
branch_labels = None
depends_on = None

SQL_REINSTATEMENT = r"""
ALTER TABLE kg.statement ADD COLUMN reinstates_row_id bigint;
ALTER TABLE kg.statement DROP CONSTRAINT statement_version_identity;
ALTER TABLE kg.statement ADD CONSTRAINT statement_version_identity
  UNIQUE NULLS NOT DISTINCT (statement_key, origin, source_revision, reinstates_row_id);
COMMENT ON COLUMN kg.statement.reinstates_row_id IS
  'Set only when a revision-less delivery re-asserts a retracted statement: the retracted kg.statement row';

CREATE OR REPLACE FUNCTION kg.check_statement_rows() RETURNS trigger LANGUAGE plpgsql
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
  SELECT n.id INTO bad FROM new_rows n
   WHERE n.reinstates_row_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM kg.statement s JOIN kg.statement_retraction r ON r.statement_row_id = s.id
                      WHERE s.id = n.reinstates_row_id AND s.statement_key = n.statement_key
                        AND s.origin = n.origin)
   LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.statement row % reinstates a row that is not a retracted row of the same statement', bad;
  END IF;
  SELECT l.id INTO bad FROM kg.load l WHERE l.id IN (SELECT DISTINCT load_id FROM new_rows)
     AND NOT meta.inserted_in_current_xact(l.xmin) LIMIT 1;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'kg.load % is sealed: its rows must be inserted in the transaction that created it', bad;
  END IF;
  RETURN NULL;
END $$;
"""

SQL_GATE_GRANTS = r"""
REVOKE SELECT ON ALL TABLES IN SCHEMA kg FROM reva_gate;
REVOKE USAGE ON SCHEMA kg FROM reva_gate;
"""


def upgrade() -> None:
    op.execute(SQL_REINSTATEMENT)
    op.execute(SQL_GATE_GRANTS)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
