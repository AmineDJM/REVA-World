"""DDL helpers shared by Alembic migrations."""

from __future__ import annotations


def immutable(*tables: str) -> str:
    """Attach the append-only trigger to fully-qualified tables."""
    out = []
    for t in tables:
        name = "immutable_record"
        out.append(
            f"CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {t} "
            "FOR EACH ROW EXECUTE FUNCTION meta.reject_mutation();"
        )
        out.append(
            f"CREATE TRIGGER {name}_truncate BEFORE TRUNCATE ON {t} "
            "FOR EACH STATEMENT EXECUTE FUNCTION meta.reject_truncate();"
        )
    return "\n".join(out)


def server_recorded_at(*schemas: str) -> str:
    """Attach ``meta.force_recorded_at()`` (BEFORE INSERT) to every base table in
    ``schemas`` that has a ``recorded_at`` column.

    Runtime roles can then never choose (backdate) ``recorded_at``; only the table
    owner may supply a value. The trigger is named ``a00_…`` so it fires before
    every other BEFORE INSERT trigger (PostgreSQL fires them in name order) and
    those triggers see the server value. Idempotent per table; later migrations
    that add tables call it again with the new schema(s).
    """
    if not schemas or not all(s.isidentifier() for s in schemas):
        raise ValueError("schemas must be plain identifiers")
    arr = ",".join(f"'{s}'" for s in schemas)
    return f"""
DO $$
DECLARE t record;
BEGIN
  FOR t IN
    SELECT n.nspname AS sch, c.relname AS tbl
    FROM pg_catalog.pg_attribute a
    JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE a.attname = 'recorded_at' AND NOT a.attisdropped AND c.relkind IN ('r', 'p')
      AND n.nspname = ANY (ARRAY[{arr}])
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger g
                      WHERE g.tgrelid = c.oid AND g.tgname = 'a00_server_recorded_at')
    ORDER BY 1, 2
  LOOP
    EXECUTE format('CREATE TRIGGER a00_server_recorded_at BEFORE INSERT ON %I.%I '
                   'FOR EACH ROW EXECUTE FUNCTION meta.force_recorded_at()', t.sch, t.tbl);
  END LOOP;
END $$;
"""
