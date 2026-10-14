"""Shared streaming/hash helpers for object stores."""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import BinaryIO

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
