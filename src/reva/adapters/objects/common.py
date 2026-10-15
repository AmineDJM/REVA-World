"""Shared streaming/hash helpers for object stores."""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Any, BinaryIO

from reva.kernel.errors import IntegrityFailure, UnsafeRequest
from reva.ports.storage import NAMESPACES

CHUNK = 1024 * 1024


def check_namespace(namespace: str) -> None:
    if namespace not in NAMESPACES:
        raise ValueError(f"unknown object namespace {namespace!r}")


def object_key(prefix: str, namespace: str, sha256: str) -> str:
    check_namespace(namespace)
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid sha256")
    return f"{prefix}{namespace}/sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"


def spool_and_hash(stream: BinaryIO, spool_dir: str | None, max_bytes: int) -> tuple[str, str, int]:
    """Copy a stream to a bounded temporary file while hashing. Memory: one chunk."""
    h = hashlib.sha256()
    size = 0
    fd, path = tempfile.mkstemp(prefix="reva-stage-", dir=spool_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UnsafeRequest(f"object exceeds max_bytes={max_bytes}")
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        os.unlink(path)
        raise
    return path, h.hexdigest(), size


def hash_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def verify(expected_sha: str, expected_size: int, actual_sha: str, actual_size: int) -> None:
    if expected_sha != actual_sha or expected_size != actual_size:
        raise IntegrityFailure(
            "object verification failed",
            expected_sha256=expected_sha,
            actual_sha256=actual_sha,
            expected_size=expected_size,
            actual_size=actual_size,
        )


# --------------------------------------------------------------------------- GC / reuse exclusion
#: Per-object transaction-level advisory lock shared by GC and publishers.
#: Key text is ``'obj|' || <sha256 hex>``; the lock id is ``hashtextextended(key, 0)``.
OBJECT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"
OBJECT_TRY_LOCK_SQL = "SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"


def object_lock_key(sha256: str) -> str:
    """Advisory-lock key text for a content address (namespace independent).

    Protocol (closes the GC-vs-reuse race, review R-11):

    * GC takes this lock (transaction level) before its final reference
      re-check, age re-check and delete, all in the same transaction.
    * A publisher that registers a ``meta.artifact_location`` for an object --
      in particular one whose bytes already existed (``put`` found and
      re-verified them) -- takes the same lock in the registering transaction
      and then confirms the object is still present (``confirm_object_locked``)
      before inserting the location. The lock is held until commit, so GC
      cannot delete between the confirmation and the commit, and a delete that
      happened earlier is detected by the confirmation.
    """
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid sha256")
    return f"obj|{sha256}"


def lock_object_xact(conn: Any, sha256: str) -> None:
    """Block until the per-object advisory lock is held (released at transaction end)."""
    from sqlalchemy import text

    conn.execute(text(OBJECT_LOCK_SQL), {"k": object_lock_key(sha256)})


def try_lock_object_xact(conn: Any, sha256: str) -> bool:
    from sqlalchemy import text

    return bool(conn.execute(text(OBJECT_TRY_LOCK_SQL), {"k": object_lock_key(sha256)}).scalar_one())


def confirm_object_locked(conn: Any, store: Any, namespace: str, sha256: str, byte_size: int | None = None) -> None:
    """Publisher side: take the object lock, then require the bytes to still exist.

    Call inside the transaction that inserts the artifact location, after
    ``store.put`` returned. Raises ``IntegrityFailure`` (retryable by re-putting)
    if GC removed the object in the meantime.
    """
    lock_object_xact(conn, sha256)
    head = store.head(namespace, sha256)
    if head is None or (byte_size is not None and head.byte_size != byte_size):
        raise IntegrityFailure(
            "object vanished before registration; re-put and retry", namespace=namespace, sha256=sha256, retryable=True
        )
