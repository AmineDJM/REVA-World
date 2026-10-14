"""Filesystem ArtifactStore: content addressing, immutability, verification, bounded memory."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tracemalloc
from pathlib import Path

import pytest

from reva.adapters.objects.common import object_key
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.kernel.errors import IntegrityFailure, UnsafeRequest
from reva.ports.storage import NAMESPACES


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class ZeroStream(io.RawIOBase):
    """Generates ``total`` bytes lazily; never holds more than one read in memory."""

    def __init__(self, total: int, fail_after: int | None = None) -> None:
        self.total = total
        self.sent = 0
        self.fail_after = fail_after

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError("injected mid-stream failure")
        n = self.total - self.sent if n < 0 else min(n, self.total - self.sent)
        self.sent += n
        return b"\x00" * n


def final_files(store: FilesystemArtifactStore) -> list[Path]:
    return [p for p in store.root.rglob("*") if p.is_file() and ".staging" not in p.parts]


def staged_files(store: FilesystemArtifactStore) -> list[Path]:
    return [p for p in store.staging.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------- basic contract
def test_put_get_head_range_read(store: FilesystemArtifactStore) -> None:
    data = b"0123456789" * 1000
    obj = store.put(io.BytesIO(data), "raw", "text/plain", max_bytes=len(data))
    assert obj.sha256 == sha(data) and obj.byte_size == len(data) and obj.namespace == "raw"
    assert obj.media_type == "text/plain"
    assert store.get("raw", obj.sha256) == data
    head = store.head("raw", obj.sha256)
    assert head is not None and (head.sha256, head.byte_size) == (obj.sha256, len(data))
    assert store.range_read("raw", obj.sha256, 5, 7) == data[5:12]
    assert store.range_read("raw", obj.sha256, len(data) - 3, 100) == data[-3:]
    assert b"".join(store.open("raw", obj.sha256)) == data
    with pytest.raises(ValueError):
        store.range_read("raw", obj.sha256, -1, 2)


def test_content_addressing_and_namespacing(store: FilesystemArtifactStore) -> None:
    data = b'{"a":1}'
    a = store.put_bytes(data, "raw", "application/json")
    b = store.put_bytes(data, "raw", "application/json")
    assert a.storage_uri == b.storage_uri  # idempotent: same bytes, same object
    path = store.root / object_key(store.prefix, "raw", sha(data))
    assert a.storage_uri == f"file://{path}"
    assert path.read_bytes() == data
    # The same bytes in another namespace are a separate object.
    c = store.put_bytes(data, "quarantine", "application/json")
    assert c.storage_uri != a.storage_uri
    assert store.head("belief", sha(data)) is None
    # Objects are read-only on disk.
    assert not (path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_unknown_namespace_and_malformed_addresses_rejected(store: FilesystemArtifactStore) -> None:
    with pytest.raises(ValueError):
        store.put_bytes(b"x", "../../etc", "text/plain")
    with pytest.raises(ValueError):
        store.put_bytes(b"x", "public", "text/plain")
    for bad in ("../../../../etc/passwd", "A" * 64, "a" * 63, "", "a" * 62 + "/."):
        with pytest.raises(ValueError):
            store.head("raw", bad)
        with pytest.raises(ValueError):
            store.get("raw", bad)
    assert set(NAMESPACES) >= {"raw", "quarantine", "belief", "scenarios"}


def test_missing_object(store: FilesystemArtifactStore) -> None:
    assert store.head("raw", "0" * 64) is None
    with pytest.raises(FileNotFoundError):
        store.get("raw", "0" * 64)


# --------------------------------------------------------------------------- immutability
def test_tampered_bytes_detected_on_read(store: FilesystemArtifactStore) -> None:
    data = b"original source bytes"
    obj = store.put_bytes(data, "raw", "text/plain")
    path = store.root / object_key(store.prefix, "raw", obj.sha256)
    os.chmod(path, 0o644)
    path.write_bytes(b"tampered source bytes")
    with pytest.raises(IntegrityFailure) as ei:
        store.get("raw", obj.sha256)
    assert ei.value.code == "integrity_failure"


def test_never_overwrites_different_content(store: FilesystemArtifactStore) -> None:
    data = b"canonical bytes"
    obj = store.put_bytes(data, "raw", "text/plain")
    path = store.root / object_key(store.prefix, "raw", obj.sha256)
    os.chmod(path, 0o644)
    path.write_bytes(b"corrupted!!")
    # Re-putting the true bytes must not silently "repair"/overwrite: it reports the conflict.
    with pytest.raises(IntegrityFailure):
        store.put_bytes(data, "raw", "text/plain")
    assert path.read_bytes() == b"corrupted!!"
    assert staged_files(store) == []


def test_truncated_object_detected(store: FilesystemArtifactStore) -> None:
    data = os.urandom(4096)
    obj = store.put_bytes(data, "normalised", "application/octet-stream")
    path = store.root / object_key(store.prefix, "normalised", obj.sha256)
    os.chmod(path, 0o644)
    with open(path, "r+b") as f:
        f.truncate(100)
    with pytest.raises(IntegrityFailure):
        store.get("normalised", obj.sha256)
    with pytest.raises(IntegrityFailure):
        store.put_bytes(data, "normalised", "application/octet-stream")


# --------------------------------------------------------------------------- bounds
def test_max_bytes_exceeded_leaves_no_object(store: FilesystemArtifactStore) -> None:
    with pytest.raises(UnsafeRequest):
        store.put(ZeroStream(5 * 1024 * 1024), "raw", "application/octet-stream", max_bytes=3 * 1024 * 1024)
    assert final_files(store) == []
    assert staged_files(store) == []


def test_mid_stream_failure_leaves_no_object(store: FilesystemArtifactStore) -> None:
    with pytest.raises(ConnectionResetError):
        store.put(
            ZeroStream(8 * 1024 * 1024, fail_after=3 * 1024 * 1024),
            "raw",
            "application/octet-stream",
            max_bytes=16 * 1024 * 1024,
        )
    assert final_files(store) == []
    assert staged_files(store) == []


def test_streaming_put_memory_is_bounded(store: FilesystemArtifactStore) -> None:
    total = 48 * 1024 * 1024
    tracemalloc.start()
    try:
        obj = store.put(ZeroStream(total), "raw", "application/octet-stream", max_bytes=total)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert obj.byte_size == total
    assert obj.sha256 == hashlib.sha256(b"\x00" * total).hexdigest()
    # One chunk plus overhead, nowhere near the object size.
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes while streaming {total}"


def test_streaming_read_is_chunked(store: FilesystemArtifactStore) -> None:
    total = 5 * 1024 * 1024 + 7
    obj = store.put(ZeroStream(total), "raw", "application/octet-stream", max_bytes=total)
    chunks = list(store.open("raw", obj.sha256))
    assert len(chunks) > 1 and max(len(c) for c in chunks) <= 1024 * 1024
    assert sum(len(c) for c in chunks) == total


# --------------------------------------------------------------------------- orphan deletion
def test_delete_orphan_confined_to_root(store: FilesystemArtifactStore, tmp_path: Path) -> None:
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me")
    for uri in (f"file://{outside}", f"file://{store.root}/../victim.txt", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            store.delete_orphan(uri)
    assert outside.read_text() == "keep me"
    obj = store.put_bytes(b"orphan", "raw", "text/plain")
    store.delete_orphan(obj.storage_uri)
    assert store.head("raw", obj.sha256) is None


def test_list_uris_is_namespace_scoped(store: FilesystemArtifactStore) -> None:
    a = store.put_bytes(b"a", "raw", "text/plain")
    store.put_bytes(b"b", "quarantine", "text/plain")
    uris = [u for u, _ in store.list_uris("raw")]
    assert uris == [a.storage_uri]
