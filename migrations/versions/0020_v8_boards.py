"""V8 console boards & watch rules (agent V8API; contract ``docs/dev/v8_api_interface.md`` §3).

User state of the console, not science: a board is an ordered list of pinned references (entity, series, forecast
question, decision, note) plus watch rules, owned by one principal (its audit identity, e.g. ``human:admin`` or
``token:<label>``). Boards hold **references only, never values** (values are always read live with their source).

Table ``ops.v8_board`` (mutable user state; one row per (owner, id)): ``doc`` is the board document
(``{items: [...], rules: [...]}``, ≤ 512 KiB, validated by the API), ``version`` an optimistic-concurrency counter.

Writes go only through SECURITY DEFINER functions callable by ``reva_api`` (create / update with expected
version / delete), each checking the session principal; ``reva_api`` may only SELECT the table. ``reva_worker`` /
``reva_sim`` / ``reva_gate`` have no access. Functions are revoked from PUBLIC.

Revision ID: 0020_v8_boards
Revises: 0019_effect_estimates
"""

from alembic import op

revision = "0020_v8_boards"
down_revision = "0019_effect_estimates"
branch_labels = None
depends_on = None

SQL_TABLE = r"""
CREATE TABLE ops.v8_board (
  owner text NOT NULL CHECK (length(owner) BETWEEN 1 AND 200),
  id text NOT NULL CHECK (id ~ '^[A-Za-z0-9_-]{1,64}$'),
  title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
  doc jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(doc) = 'object' AND octet_length(doc::text) <= 524288),
  version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner, id),
  CHECK (updated_at >= created_at)
);
CREATE INDEX v8_board_recent ON ops.v8_board (owner, updated_at DESC);
"""

SQL_FUNCTIONS = r"""
CREATE FUNCTION ops.v8_board_create(p_owner text, p_id text, p_title text, p_doc jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE r ops.v8_board;
BEGIN
  IF meta.session_principal() NOT IN ('reva_api', 'owner') THEN
    RAISE EXCEPTION 'board writes are an API action' USING ERRCODE = 'insufficient_privilege';
  END IF;
  INSERT INTO ops.v8_board (owner, id, title, doc) VALUES (p_owner, p_id, p_title, p_doc) RETURNING * INTO r;
  RETURN to_jsonb(r);
END $$;

-- Returns the new row, or NULL when the board does not exist or the expected version is stale.
CREATE FUNCTION ops.v8_board_update(p_owner text, p_id text, p_title text, p_doc jsonb, p_version integer)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE r ops.v8_board;
BEGIN
  IF meta.session_principal() NOT IN ('reva_api', 'owner') THEN
    RAISE EXCEPTION 'board writes are an API action' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE ops.v8_board
     SET title = p_title, doc = p_doc, version = version + 1, updated_at = clock_timestamp()
   WHERE owner = p_owner AND id = p_id AND version = p_version
  RETURNING * INTO r;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN to_jsonb(r);
END $$;

CREATE FUNCTION ops.v8_board_delete(p_owner text, p_id text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF meta.session_principal() NOT IN ('reva_api', 'owner') THEN
    RAISE EXCEPTION 'board writes are an API action' USING ERRCODE = 'insufficient_privilege';
  END IF;
  DELETE FROM ops.v8_board WHERE owner = p_owner AND id = p_id;
  RETURN FOUND;
END $$;
"""

SQL_GRANTS = r"""
REVOKE ALL ON ops.v8_board FROM PUBLIC;
GRANT SELECT ON ops.v8_board TO reva_api;
REVOKE ALL ON FUNCTION ops.v8_board_create(text, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops.v8_board_update(text, text, text, jsonb, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops.v8_board_delete(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.v8_board_create(text, text, text, jsonb),
                          ops.v8_board_update(text, text, text, jsonb, integer),
                          ops.v8_board_delete(text, text) TO reva_api;
"""


def upgrade() -> None:
    op.execute(SQL_TABLE)
    op.execute(SQL_FUNCTIONS)
    op.execute(SQL_GRANTS)


def downgrade() -> None:
    op.execute("DROP TABLE ops.v8_board")
    op.execute("DROP FUNCTION ops.v8_board_create(text, text, text, jsonb)")
    op.execute("DROP FUNCTION ops.v8_board_update(text, text, text, jsonb, integer)")
    op.execute("DROP FUNCTION ops.v8_board_delete(text, text)")
