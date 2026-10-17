"""V3 link store retention (agent LINKRET): governed pruning of old link materialisations.

The materialised link store (0012_links_store) is append-only and nothing ever removed an old materialisation
(one full live materialisation is ~1.2–1.4 GB of ``kg.link_row``). Link rows are a **derived cache**: every row is
re-derivable from its materialisation's recorded inputs (atlas snapshot sha256, KG watermark, taxonomy sha256,
method version, top-k), and the materialisation row itself — the provenance record — is never deleted.

What this migration adds
------------------------
* ``kg.link_prune`` — append-only (``meta.reject_mutation``) record, one row per pruned materialisation: the run
  id, the reason, the number of link rows deleted and their size (``pg_column_size`` of each row), the policy in
  force (keep N, the kept materialisations and why). Server ``recorded_at`` / ``recorded_by``. Readers
  (``reva_api``, ``reva_worker``, ``reva_sim``) may SELECT it (a pruned materialisation is never served or
  reused); only the owner inserts.
* ``kg.prune_link_materialisation(id, run, reason, policy)`` — the only deletion path for link rows outside the
  reviewed restricted-deletion ticket: owner only (EXECUTE revoked from PUBLIC, granted to nobody; the body also
  refuses any non-owner session). It records the ``kg.link_prune`` row, then deletes that materialisation's link
  rows and checks the count, in the caller's transaction.
* ``kg.link_row``'s ``immutable_record`` trigger is split: UPDATE stays refused unconditionally; DELETE is refused
  unless a ``kg.link_prune`` row for that very materialisation was inserted **in the current transaction**
  (``kg.link_prune_in_xact``) — so link rows can only disappear together with their audit record. TRUNCATE stays
  refused. The reviewed restricted-deletion path (``reva_restricted_deletion`` + ticket) is unchanged.
* Guards on ``kg.link_prune`` (BEFORE INSERT): owner only; never the latest materialisation of its lineage
  (same atlas origin and KG origin) nor the latest live-served one (atlas live/none + KG live — what production
  reads); ``rows_deleted`` ≤ the materialisation's ``link_count``. A deferred constraint trigger refuses the commit
  if any link row of a pruned materialisation remains (a prune record always means "rows gone").

The retention policy itself (keep N, current/pinned/referenced materialisations) is in ``reva.links.retention``
(``reva links prune``); the database enforces the invariants above whatever the caller.

Revision ID: 0017_links_retention
Revises: 0016_causal_ops
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable, server_recorded_at

revision = "0017_links_retention"
down_revision = "0016_causal_ops"
branch_labels = None
depends_on = None

SQL_TABLE = r"""
CREATE TABLE kg.link_prune (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  materialisation_id bigint NOT NULL UNIQUE REFERENCES kg.link_materialisation,
  run_id uuid NOT NULL,
  rows_deleted bigint NOT NULL CHECK (rows_deleted >= 0),
  row_bytes bigint NOT NULL CHECK (row_bytes >= 0),
  reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
  policy jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(policy) = 'object' AND octet_length(policy::text) <= 65536),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  recorded_by text NOT NULL DEFAULT session_user
);
CREATE INDEX link_prune_run ON kg.link_prune(run_id);
"""

SQL_FUNCTIONS = r"""
-- Was a prune record for this materialisation inserted by the current transaction? (link_row DELETE gate;
-- SECURITY DEFINER: the trigger's WHEN clause runs as the deleting role, e.g. reva_restricted_deletion, which
-- may not call meta.inserted_in_current_xact itself. It only answers a boolean.)
CREATE FUNCTION kg.link_prune_in_xact(p_id bigint) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM kg.link_prune p
                  WHERE p.materialisation_id = p_id AND meta.inserted_in_current_xact(p.xmin))
$$;

CREATE FUNCTION kg.check_link_prune() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE m kg.link_materialisation;
BEGIN
  IF meta.session_principal() <> 'owner' THEN
    RAISE EXCEPTION 'only the owner may prune link materialisations (%)', session_user
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT * INTO m FROM kg.link_materialisation WHERE id = NEW.materialisation_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no link materialisation %', NEW.materialisation_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM kg.link_materialisation n
                  WHERE n.id > m.id AND n.atlas_origin IS NOT DISTINCT FROM m.atlas_origin
                    AND n.kg_origin IS NOT DISTINCT FROM m.kg_origin) THEN
    RAISE EXCEPTION 'link materialisation % is the latest of its lineage (atlas %, KG %): it is served and cannot be pruned',
      m.id, coalesce(m.atlas_origin, 'none'), coalesce(m.kg_origin, 'any');
  END IF;
  IF coalesce(m.atlas_origin, 'live') = 'live' AND m.kg_origin = 'live'
     AND NOT EXISTS (SELECT 1 FROM kg.link_materialisation n
                      WHERE n.id > m.id AND coalesce(n.atlas_origin, 'live') = 'live' AND n.kg_origin = 'live') THEN
    RAISE EXCEPTION 'link materialisation % is the latest live materialisation (production serves it) and cannot be pruned',
      m.id;
  END IF;
  IF NEW.rows_deleted > m.link_count THEN
    RAISE EXCEPTION 'prune of link materialisation % records % rows; it stored %', m.id, NEW.rows_deleted, m.link_count;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER link_prune_guard BEFORE INSERT ON kg.link_prune
  FOR EACH ROW EXECUTE FUNCTION kg.check_link_prune();

CREATE FUNCTION kg.check_link_prune_complete() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM kg.link_row r WHERE r.materialisation_id = NEW.materialisation_id) THEN
    RAISE EXCEPTION 'link materialisation % is recorded as pruned but link rows remain', NEW.materialisation_id;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER link_prune_complete AFTER INSERT ON kg.link_prune
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION kg.check_link_prune_complete();

-- The prune itself: record, delete, verify — in the caller's transaction (owner only).
CREATE FUNCTION kg.prune_link_materialisation(p_id bigint, p_run uuid, p_reason text, p_policy jsonb)
RETURNS TABLE (rows_deleted bigint, row_bytes bigint) LANGUAGE plpgsql
  SET search_path = pg_catalog, pg_temp AS $$
DECLARE n bigint; b bigint; d bigint;
BEGIN
  IF meta.session_principal() <> 'owner' THEN
    RAISE EXCEPTION 'only the owner may prune link materialisations (%)', session_user
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  PERFORM 1 FROM kg.link_materialisation WHERE id = p_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no link materialisation %', p_id;
  END IF;
  SELECT count(*), coalesce(sum(pg_column_size(r.*)), 0) INTO n, b
    FROM kg.link_row r WHERE r.materialisation_id = p_id;
  INSERT INTO kg.link_prune(materialisation_id, run_id, rows_deleted, row_bytes, reason, policy)
  VALUES (p_id, p_run, n, b, p_reason, coalesce(p_policy, '{}'::jsonb));
  DELETE FROM kg.link_row r WHERE r.materialisation_id = p_id;
  GET DIAGNOSTICS d = ROW_COUNT;
  IF d <> n THEN
    RAISE EXCEPTION 'prune of link materialisation %: counted % rows, deleted %', p_id, n, d;
  END IF;
  RETURN QUERY SELECT n, b;
END $$;
"""

#: link_row: UPDATE refused as before; DELETE refused unless pruned (with its record) in this transaction.
SQL_LINK_ROW_TRIGGERS = r"""
DROP TRIGGER immutable_record ON kg.link_row;
CREATE TRIGGER immutable_record BEFORE UPDATE ON kg.link_row
  FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation();
CREATE TRIGGER immutable_record_delete BEFORE DELETE ON kg.link_row
  FOR EACH ROW WHEN (NOT kg.link_prune_in_xact(OLD.materialisation_id)) EXECUTE FUNCTION meta.reject_mutation();
"""

SQL_GRANTS = r"""
REVOKE ALL ON kg.link_prune FROM PUBLIC;
REVOKE ALL ON FUNCTION kg.link_prune_in_xact(bigint), kg.check_link_prune(), kg.check_link_prune_complete(),
  kg.prune_link_materialisation(bigint, uuid, text, jsonb) FROM PUBLIC;
GRANT SELECT ON kg.link_prune TO reva_api, reva_worker, reva_sim;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    GRANT SELECT, DELETE ON kg.link_prune TO reva_restricted_deletion;
    -- the link_row DELETE trigger's WHEN clause runs as the deleting role
    GRANT EXECUTE ON FUNCTION kg.link_prune_in_xact(bigint) TO reva_restricted_deletion;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL_TABLE)
    op.execute(SQL_FUNCTIONS)
    op.execute(immutable("kg.link_prune"))
    op.execute(server_recorded_at("kg"))
    op.execute(SQL_LINK_ROW_TRIGGERS)
    op.execute(SQL_GRANTS)


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
