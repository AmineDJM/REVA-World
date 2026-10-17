"""Repair the table CHECKs of ``kg.causal_claim`` on databases migrated with the first ``0015_causal``.

The first ``0015_causal`` tied the tier to the statement row::

    CHECK ((source_tier IN ('T3','T4')) = (statement_row_id IS NOT NULL))

so a KG-derived T2 claim (a Wikidata causal statement whose reference carries a DOI / ISBN,
``docs/dev/v3_causal_interface.md``) — which restates its ``kg.statement`` row — was refused. The file was then
edited in place to allow T2 statement rows (the two CHECKs on ``statement_row_id`` below), so a fresh bootstrap
and the loader agree, but a database that had already run the first version (``reva_kg_causwd``,
``reva_kg_wdharvest_drift1016``, ``reva_causcand``: ``docs/dev/v3_causfix.md``) kept the stale CHECK: since
CAUSFIX the loader rejects its T2 statement rows one by one (``db_refused:causal_claim_check``), they are never
stored nor graded.

What this migration does
------------------------
* It compares the auto-named table CHECKs of ``kg.causal_claim`` (``causal_claim_check``, ``causal_claim_check1``
  ...) with :data:`TABLE_CHECKS` — the table CHECKs of the current ``0015_causal``, rendered by the server on an
  empty temporary copy of the table. When they are equal (every database bootstrapped from the current
  ``0015_causal``) nothing is changed: no lock beyond the catalog read, no scan.
* Otherwise it drops those CHECKs and adds :data:`TABLE_CHECKS` under the names a fresh bootstrap gives them, so
  the database matches ``migrations/schema_contract.json`` again. They are validated on the existing rows; a row
  of the stale shape satisfies them all (T3/T4 rows have a statement row, T1/T2 rows have none). No row is changed.

Revision ID: 0019_causal_claim_checks
Revises: 0018_retraction_reasons
"""

from alembic import op
from sqlalchemy import Connection, text

revision = "0019_causal_claim_checks"
down_revision = "0018_retraction_reasons"
branch_labels = None
depends_on = None

#: The table CHECKs of ``kg.causal_claim`` in the current ``0015_causal``, in their order there (a unit test keeps
#: the two files in step).
TABLE_CHECKS = (
    "statement_row_id IS NULL OR source_tier IN ('T2','T3','T4')",
    "statement_row_id IS NOT NULL OR source_tier IN ('T1','T2')",
    "(statement_row_id IS NULL) = (statement_key IS NULL)",
    "starts_with(claim_key, 'kg:') = (statement_row_id IS NOT NULL)",
    "statement_row_id IS NOT NULL OR (raw_sha256 IS NOT NULL AND dataset IS NOT NULL)",
    "source_tier <> 'T1' OR codebook IS NOT NULL",
    "source_tier <> 'T2' OR citation IS NOT NULL",
    "(family = 'prevention') = (relation = 'prevented_or_delayed' OR polarity IS NOT DISTINCT FROM 'inhibits')",
)
#: PostgreSQL's names for unnamed table CHECKs: ``<table>_check``, ``<table>_check1``, ...
NAMES = tuple("causal_claim_check" + (str(i) if i else "") for i in range(len(TABLE_CHECKS)))
_PROBE = "reva_0019_causal_claim_probe"


def _checks(conn: Connection, rel: str) -> dict[str, tuple[str, bool]]:
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            text("""SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint
                     WHERE conrelid = CAST(:rel AS regclass) AND contype = 'c'
                       AND conname ~ '^causal_claim_check[0-9]*$'"""),
            {"rel": rel},
        )
    }


def repair(conn: Connection) -> bool:
    """Bring the table CHECKs of ``kg.causal_claim`` to :data:`TABLE_CHECKS`; ``True`` when they were changed."""
    conn.execute(text(f"CREATE TEMPORARY TABLE {_PROBE} (LIKE kg.causal_claim)"))
    try:
        conn.execute(
            text(
                f"ALTER TABLE {_PROBE} "
                + ", ".join(f"ADD CONSTRAINT {n} CHECK ({e})" for n, e in zip(NAMES, TABLE_CHECKS, strict=True))
            )
        )
        want = _checks(conn, _PROBE)
    finally:
        conn.execute(text(f"DROP TABLE {_PROBE}"))
    have = _checks(conn, "kg.causal_claim")
    if have == want:
        return False
    conn.execute(
        text(
            "ALTER TABLE kg.causal_claim "
            + ", ".join(
                [f"DROP CONSTRAINT {n}" for n in sorted(have)]
                + [f"ADD CONSTRAINT {n} CHECK ({e})" for n, e in zip(NAMES, TABLE_CHECKS, strict=True)]
            )
        )
    )
    return True


def upgrade() -> None:
    repair(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
