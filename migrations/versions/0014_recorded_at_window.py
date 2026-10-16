"""Server-generated timestamps (R-08): accept the column default evaluated earlier in the same statement.

``meta.force_recorded_at()`` (0001) and ``ops.force_published_at()`` (0003) rejected a non-null value more
than one second away from ``clock_timestamp()`` at trigger time. The column default (``clock_timestamp()``)
is itself such a value, evaluated before the BEFORE INSERT triggers run; under load (CPU starvation, lock
waits inside the statement) the gap exceeded one second and a legitimate insert failed with "recorded_at is
server-generated" (observed on ``ops.budget_ledger``; a retry succeeded).

The check now accepts a supplied value only inside the current statement's window
``[statement_timestamp() - 1 s, clock_timestamp() + 1 s]``. A default evaluated during this statement always
lies in it, whatever the delay before the trigger; a caller-chosen value from before the statement (a
backdate) or from the future is rejected as before. The one-second margins only absorb a stepped wall clock
and keep the window a superset of the old one. Either way the stored value is overwritten with the server
clock, so no caller can choose it (R-08). Owner bypass (keyed on ``session_user``, R5-14), SECURITY INVOKER,
the owner and the grants (``CREATE OR REPLACE`` keeps owner and ACL) are unchanged; every catalog reference
stays ``pg_catalog``-qualified. No table changes.

Revision ID: 0014_recorded_at_window
Revises: 0013_links_ops
"""

from alembic import op

revision = "0014_recorded_at_window"
down_revision = "0013_links_ops"
branch_labels = None
depends_on = None

FORCE_WINDOW = r"""
CREATE OR REPLACE FUNCTION meta.force_recorded_at() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE server_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles r ON r.oid = c.relowner
             WHERE c.oid = TG_RELID AND r.rolname = session_user) THEN
    RETURN NEW;
  END IF;
  -- (R-08) Only a value produced during this statement (the column default) is accepted; it is
  -- replaced by the server clock regardless.
  IF NEW.recorded_at IS NOT NULL
     AND (NEW.recorded_at < pg_catalog.statement_timestamp() - interval '1 second'
          OR NEW.recorded_at > server_now + interval '1 second') THEN
    RAISE EXCEPTION 'recorded_at is server-generated: %.% does not accept a caller-supplied value (%)',
      TG_TABLE_SCHEMA, TG_TABLE_NAME, NEW.recorded_at USING ERRCODE = '42501';
  END IF;
  NEW.recorded_at := server_now;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION ops.force_published_at() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE server_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles r ON r.oid = c.relowner
             WHERE c.oid = TG_RELID AND r.rolname = session_user) THEN
    RETURN NEW;
  END IF;
  IF NEW.published_at IS NOT NULL
     AND (NEW.published_at < pg_catalog.statement_timestamp() - interval '1 second'
          OR NEW.published_at > server_now + interval '1 second') THEN
    RAISE EXCEPTION 'published_at is server-generated: a caller-supplied value is not accepted (%)',
      NEW.published_at USING ERRCODE = '42501';
  END IF;
  NEW.published_at := server_now;
  RETURN NEW;
END $$;
"""

#: 0001_kernel / 0003_belief_graph definitions (restored on downgrade).
FORCE_ONE_SECOND = r"""
CREATE OR REPLACE FUNCTION meta.force_recorded_at() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE server_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles r ON r.oid = c.relowner
             WHERE c.oid = TG_RELID AND r.rolname = session_user) THEN
    RETURN NEW;
  END IF;
  IF NEW.recorded_at IS NOT NULL
     AND abs(extract(epoch FROM (NEW.recorded_at - server_now))) > 1 THEN
    RAISE EXCEPTION 'recorded_at is server-generated: %.% does not accept a caller-supplied value (%)',
      TG_TABLE_SCHEMA, TG_TABLE_NAME, NEW.recorded_at USING ERRCODE = '42501';
  END IF;
  NEW.recorded_at := server_now;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION ops.force_published_at() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE server_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles r ON r.oid = c.relowner
             WHERE c.oid = TG_RELID AND r.rolname = session_user) THEN
    RETURN NEW;
  END IF;
  IF NEW.published_at IS NOT NULL
     AND abs(extract(epoch FROM (NEW.published_at - server_now))) > 1 THEN
    RAISE EXCEPTION 'published_at is server-generated: a caller-supplied value is not accepted (%)',
      NEW.published_at USING ERRCODE = '42501';
  END IF;
  NEW.published_at := server_now;
  RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.execute(FORCE_WINDOW)


def downgrade() -> None:
    op.execute(FORCE_ONE_SECOND)
