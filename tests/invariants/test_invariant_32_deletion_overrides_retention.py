"""Invariant 32: no claim of legal retention overriding required deletion of restricted data.

V1 cannot store restricted data at all (``artifact_v1_public_only``). The
append-only machinery that protects scientific records (``meta.reject_mutation``)
has an explicit, reviewed deletion path: a member of ``reva_restricted_deletion``
holding a reviewed deletion ticket can delete an immutable row, so retention
can never make a required deletion impossible. Runtime roles can never use that
path (``tests/security/test_roles_grants.py::test_deletion_ticket_does_not_help_runtime_roles``).

Everything here runs in one transaction that is rolled back.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.db


def _insert_artifact(conn) -> str:
    sha = uuid.uuid4().hex * 2
    conn.execute(text("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, "
                      "rights) VALUES (:s, 'text/plain', 1, 'inv32', 'public_aggregate', '{}')"), {"s": sha})
    return sha


def test_restricted_privacy_classes_cannot_be_stored_in_v1(db) -> None:
    for cls in ("restricted_microdata", "sensitive_restricted"):
        with db.owner.connect() as conn, conn.begin() as tx:
            with pytest.raises(DBAPIError, match="artifact_v1_public_only"):
                conn.execute(text("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, "
                                  "privacy_class, rights) VALUES (:s, 'text/csv', 1, 'inv32', :p, '{}')"),
                             {"s": uuid.uuid4().hex * 2, "p": cls})
            tx.rollback()


def test_reviewed_deletion_ticket_can_delete_an_immutable_record(db) -> None:
    with db.owner.connect() as conn:
        member = conn.execute(text(
            "SELECT pg_has_role(session_user, 'reva_restricted_deletion', 'MEMBER')")).scalar_one()
    if not member:
        pytest.skip("migration owner is not a member of reva_restricted_deletion in this cluster")
    with db.owner.connect() as conn:
        tx = conn.begin()
        try:
            sha = _insert_artifact(conn)
            sp = conn.begin_nested()
            with pytest.raises(DBAPIError, match="Immutable scientific record"):
                conn.execute(text("DELETE FROM meta.artifact WHERE sha256 = :s"), {"s": sha})
            sp.rollback()
            conn.execute(text("SELECT set_config('reva.reviewed_deletion_ticket', 'INV32-REVIEWED', true)"))
            deleted = conn.execute(text("DELETE FROM meta.artifact WHERE sha256 = :s"), {"s": sha}).rowcount
            assert deleted == 1
            # the ticket permits DELETE only; an in-place UPDATE is still refused
            sha2 = _insert_artifact(conn)
            sp = conn.begin_nested()
            with pytest.raises(DBAPIError, match="Immutable scientific record"):
                conn.execute(text("UPDATE meta.artifact SET media_type = 'x' WHERE sha256 = :s"), {"s": sha2})
            sp.rollback()
        finally:
            tx.rollback()
