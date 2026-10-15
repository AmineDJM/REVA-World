"""Filesystem ArtifactStore for local development and tests."""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from reva.adapters.objects.common import (
    CHUNK,
    check_namespace,
    hash_file,
    object_key,
    spool_and_hash,
    verify,
)
from reva.kernel.errors import IntegrityFailure
from reva.ports.storage import ObjectHead, StoredObject


class FilesystemArtifactStore:
    def __init__(self, root: Path | str, prefix: str = "reva/", default_max_bytes: int = 1 << 30):
        self.root = Path(root).resolve()
        self.prefix = prefix
        self.default_max_bytes = default_max_bytes
        self.staging = self.root / ".staging"
        self.staging.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, sha256: str) -> Path:
        return self.root / object_key(self.prefix, namespace, sha256)

    def uri(self, namespace: str, sha256: str) -> str:
        return f"file://{self._path(namespace, sha256)}"

    def put(
        self, stream: BinaryIO, namespace: str, media_type: str, *, max_bytes: int | None = None
    ) -> StoredObject:
        check_namespace(namespace)
        tmp, sha, size = spool_and_hash(stream, str(self.staging), max_bytes or self.default_max_bytes)
        try:
            final = self._path(namespace, sha)
            final.parent.mkdir(parents=True, exist_ok=True)
            created = False
            if not final.exists():
                # link() fails if the target exists: an atomic conditional create.
                try:
                    os.link(tmp, final)
                    created = True
                except FileExistsError:
                    pass
            if created:
                os.chmod(final, 0o444)
            else:
                # Reuse of an existing object: refresh its age so orphan GC (which
                # re-stats under the per-object lock) treats it as young again. Best
                # effort: the advisory-lock protocol (common.object_lock_key) is the guard.
                try:
                    os.utime(final)
                except PermissionError:
                    pass
            # Verify the actual bytes at the address (never metadata), also for reuse.
            got_sha, got_size = hash_file(str(final))
            verify(sha, size, got_sha, got_size)  # never overwrite; mismatch is an IntegrityFailure
        finally:
            os.unlink(tmp)
        return StoredObject(sha, size, namespace, self.uri(namespace, sha), media_type)

    def put_bytes(self, data: bytes, namespace: str, media_type: str) -> StoredObject:
        return self.put(io.BytesIO(data), namespace, media_type, max_bytes=max(len(data), 1))

    def head(self, namespace: str, sha256: str) -> ObjectHead | None:
        p = self._path(namespace, sha256)
        if not p.exists():
            return None
        return ObjectHead(sha256, p.stat().st_size, self.uri(namespace, sha256))

    def get(self, namespace: str, sha256: str) -> bytes:
        p = self._path(namespace, sha256)
        if not p.exists():
            raise FileNotFoundError(f"object {namespace}/{sha256} not found")
        data = p.read_bytes()
        import hashlib

        if hashlib.sha256(data).hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)
        return data

    def open(self, namespace: str, sha256: str) -> Iterator[bytes]:
        """Stream the object; raises IntegrityFailure after the last chunk if the bytes don't hash to
        their address (callers must not commit anything derived before the iterator is exhausted)."""
        p = self._path(namespace, sha256)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
                yield chunk
        if h.hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)

    def range_read(self, namespace: str, sha256: str, start: int, length: int) -> bytes:
        if start < 0 or length < 0:
            raise ValueError("invalid range")
        with open(self._path(namespace, sha256), "rb") as f:
            f.seek(start)
            return f.read(length)

    def list_uris(self, namespace: str) -> Iterator[tuple[str, float]]:
        base = self.root / self.prefix / namespace
        if not base.exists():
            return
        for p in base.rglob("*"):
            if p.is_file():
                yield f"file://{p}", p.stat().st_mtime

    def uri_mtime(self, storage_uri: str) -> float | None:
        """Current object timestamp (seconds) for a storage URI, or None if absent."""
        path = Path(storage_uri.removeprefix("file://")).resolve()
        if self.root not in path.parents:
            raise ValueError("storage URI outside the object root")
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return None

    def delete_orphan(self, storage_uri: str) -> None:
        path = Path(storage_uri.removeprefix("file://")).resolve()
        if self.root not in path.parents:
            raise ValueError("refusing to delete outside the object root")
        os.chmod(path, 0o644)
        path.unlink(missing_ok=True)
