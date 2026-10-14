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
