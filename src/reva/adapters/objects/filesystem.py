"""Filesystem ArtifactStore for local development and tests.

Objects are content-addressed by the sha256 of their **raw** bytes. Since V6 (DISKZ) a stored
file may hold a zstd-compressed wrapper of those bytes (``compression.py``): new objects are
written compressed when it frees space, existing ones are migrated by ``reva objects compress``.
Every read path decodes transparently and re-verifies the sha256 of the decoded bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from reva.adapters.objects import compression as cz
from reva.adapters.objects.common import (
    CHUNK,
    check_namespace,
    object_key,
    spool_and_hash,
    verify,
)
from reva.kernel.errors import IntegrityFailure
from reva.ports.storage import ObjectHead, StoredObject


class FilesystemArtifactStore:
    def __init__(
        self,
        root: Path | str,
        prefix: str = "reva/",
        default_max_bytes: int = 1 << 30,
        *,
        compress: bool = True,
        compression_level: int = cz.DEFAULT_LEVEL,
        min_saving_pct: float = cz.DEFAULT_MIN_SAVING_PCT,
    ):
        self.root = Path(root).resolve()
        self.prefix = prefix
        self.default_max_bytes = default_max_bytes
        self.compress = compress
        self.compression_level = compression_level
        self.min_saving_pct = min_saving_pct
        self.staging = self.root / ".staging"
        self.staging.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, sha256: str) -> Path:
        return self.root / object_key(self.prefix, namespace, sha256)

    def uri(self, namespace: str, sha256: str) -> str:
        return f"file://{self._path(namespace, sha256)}"

    def _maybe_compress(self, tmp: str, sha: str, size: int) -> str | None:
        """A staged compressed copy of ``tmp`` when it pays off (or is required), else None."""
        with open(tmp, "rb") as f:
            head = f.read(cz.SAMPLE_BYTES)
        forced = cz.must_wrap(head)
        if not forced and not (self.compress and cz.worth_trying(head, size, self.min_saving_pct)):
            return None
        fd, ctmp = tempfile.mkstemp(prefix="reva-zst-", dir=str(self.staging))
        try:
            with open(tmp, "rb") as src, os.fdopen(fd, "wb") as out:
                got_sha, got_size = cz.compress_into(src, out, size, sha, self.compression_level)
            verify(sha, size, got_sha, got_size)
            if not forced and not cz.pays_off(size, os.path.getsize(ctmp), self.min_saving_pct):
                os.unlink(ctmp)
                return None
            # Verify the staged wrapper decodes to exactly the raw bytes before it can be linked.
            dec_sha, dec_size = cz.hash_object_file(ctmp)
            verify(sha, size, dec_sha, dec_size)
        except BaseException:
            try:
                os.unlink(ctmp)
            except FileNotFoundError:
                pass
            raise
        return ctmp

    def put(self, stream: BinaryIO, namespace: str, media_type: str, *, max_bytes: int | None = None) -> StoredObject:
        check_namespace(namespace)
        tmp, sha, size = spool_and_hash(stream, str(self.staging), max_bytes or self.default_max_bytes)
        ctmp: str | None = None
        try:
            final = self._path(namespace, sha)
            final.parent.mkdir(parents=True, exist_ok=True)
            created = False
            if not final.exists():
                ctmp = self._maybe_compress(tmp, sha, size)
                # link() fails if the target exists: an atomic conditional create.
                try:
                    os.link(ctmp or tmp, final)
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
            # Verify the actual (decoded) bytes at the address (never metadata), also for reuse.
            got_sha, got_size = cz.hash_object_file(final)
            verify(sha, size, got_sha, got_size)  # never overwrite; mismatch is an IntegrityFailure
        finally:
            os.unlink(tmp)
            if ctmp is not None:
                os.unlink(ctmp)
        return StoredObject(sha, size, namespace, self.uri(namespace, sha), media_type)

    def put_bytes(self, data: bytes, namespace: str, media_type: str) -> StoredObject:
        return self.put(io.BytesIO(data), namespace, media_type, max_bytes=max(len(data), 1))

    def head(self, namespace: str, sha256: str) -> ObjectHead | None:
        """Raw byte size (the size the address was computed over), also for compressed objects."""
        p = self._path(namespace, sha256)
        try:
            size = cz.object_raw_size(p)
        except FileNotFoundError:
            return None
        return ObjectHead(sha256, size, self.uri(namespace, sha256))

    def get(self, namespace: str, sha256: str) -> bytes:
        p = self._path(namespace, sha256)
        if not p.exists():
            raise FileNotFoundError(f"object {namespace}/{sha256} not found")
        data = cz.decode_bytes(p.read_bytes())
        if hashlib.sha256(data).hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)
        return data

    def open(self, namespace: str, sha256: str) -> Iterator[bytes]:
        """Stream the (decoded) object; raises IntegrityFailure after the last chunk if the bytes don't hash
        to their address (callers must not commit anything derived before the iterator is exhausted)."""
        p = self._path(namespace, sha256)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            hdr = cz.read_header(f)
            for chunk in cz.iter_raw_from(f, hdr, CHUNK):
                h.update(chunk)
                yield chunk
        if h.hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)

    def range_read(self, namespace: str, sha256: str, start: int, length: int) -> bytes:
        if start < 0 or length < 0:
            raise ValueError("invalid range")
        with open(self._path(namespace, sha256), "rb") as f:
            hdr = cz.read_header(f)
            if hdr is None:
                f.seek(start)
                return f.read(length)
            # Compressed: decode sequentially up to the range (bounded memory, O(start)).
            out = bytearray()
            pos = 0
            end = start + length
            for chunk in cz.iter_raw_from(f, hdr, CHUNK):
                nxt = pos + len(chunk)
                if nxt > start and pos < end:
                    out += chunk[max(0, start - pos) : min(len(chunk), end - pos)]
                pos = nxt
                if pos >= end:
                    break
            return bytes(out)

    def is_compressed(self, namespace: str, sha256: str) -> bool:
        return cz.is_wrapped(self._path(namespace, sha256))

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
