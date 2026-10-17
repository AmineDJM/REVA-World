"""In-place, verified, resumable compression of an existing filesystem object archive (V6 DISKZ).

``reva objects compress`` walks ``<root>/<prefix><namespace>/sha256/aa/bb/<sha>`` and, file by file:

1. skips files that are already wrapped (this is what makes a re-run resume where it stopped),
   hard-linked, younger than ``min_age_s``, outside the content-addressed layout, or not worth it
   (one-block files, already-compressed formats by magic bytes, ratio test on the first MiB);
2. stream-compresses the file into the store's ``.staging`` directory (same filesystem) while
   hashing the source: if the source bytes do not hash to the file's address the object is
   reported ``corrupt`` and **left untouched**;
3. fsyncs the temporary file, verifies it by decoding it and re-hashing (sha256 + size of the raw
   bytes), copies the original mode/timestamps (orphan GC ages objects by mtime);
4. re-stats the original: if it changed meanwhile (replaced/deleted by someone else) the
   temporary file is discarded; otherwise ``os.replace`` swaps it in atomically and the directory is
   fsynced. Concurrent readers either hold the old inode (raw bytes) or open the new one (decoded
   transparently by ``FilesystemArtifactStore``); no reader can observe a partial file.

An object is never deleted or replaced without a verified replacement.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import zstandard

from reva.adapters.objects import compression as cz
from reva.adapters.objects.common import object_key
from reva.ports.storage import NAMESPACES


@dataclass
class CompressReport:
    dry_run: bool
    root: str
    statuses: Counter[str] = field(default_factory=Counter)
    #: allocated bytes (st_blocks*512) of the files that were (or, dry run, would be) compressed
    bytes_before: int = 0
    bytes_after: int = 0
    raw_bytes_compressed: int = 0
    by_namespace: dict[str, dict[str, int]] = field(default_factory=dict)
    corrupt: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "root": self.root,
            "files": sum(self.statuses.values()),
            "statuses": dict(sorted(self.statuses.items())),
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "bytes_freed": self.bytes_before - self.bytes_after,
            "gb_freed": round((self.bytes_before - self.bytes_after) / 1e9, 3),
            "raw_bytes_compressed": self.raw_bytes_compressed,
            "by_namespace": self.by_namespace,
            "corrupt": self.corrupt[:100],
            "errors": self.errors[:100],
            "seconds": round(self.seconds, 1),
        }


def iter_object_files(
    root: Path, prefix: str, namespaces: tuple[str, ...] = NAMESPACES
) -> Iterator[tuple[str, str, Path]]:
    """(namespace, sha256, path) of every file in the content-addressed layout (sorted, deterministic)."""
    for ns in namespaces:
        base = root / prefix / ns / "sha256"
        if not base.is_dir():
            continue
        for a in sorted(os.listdir(base)):
            da = base / a
            if not da.is_dir():
                continue
            for b in sorted(os.listdir(da)):
                db = da / b
                if not db.is_dir():
                    continue
                for name in sorted(os.listdir(db)):
                    yield ns, name, db / name


class _Counting:
    """Write-only sink that only counts (dry run)."""

    def __init__(self) -> None:
        self.n = 0

    def write(self, b: bytes) -> int:
        self.n += len(b)
        return len(b)

    def flush(self) -> None:
        pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compress_object_file(
    path: Path,
    namespace: str,
    sha: str,
    *,
    root: Path,
    prefix: str,
    staging: Path,
    level: int = cz.DEFAULT_LEVEL,
    min_saving_pct: float = cz.DEFAULT_MIN_SAVING_PCT,
    min_age_s: float = 0.0,
    dry_run: bool = False,
    now: float | None = None,
    before_replace: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Compress one stored object in place. Returns ``{"status", "before", "after", "raw"}``."""
    try:
        if object_key(prefix, namespace, sha) != str(path.relative_to(root)):
            return {"status": "skipped_layout"}
    except ValueError:
        return {"status": "skipped_layout"}
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return {"status": "vanished"}
    tmp: str | None = None
    try:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return {"status": "skipped_not_file"}
        head = f.read(cz.SAMPLE_BYTES)
        if cz.parse_header(head[: cz.HEADER_SIZE]) is not None:
            return {"status": "already_compressed"}
        if st.st_nlink > 1:
            return {"status": "skipped_hardlinked"}
        if min_age_s > 0 and (now or time.time()) - st.st_mtime < min_age_s:
            return {"status": "skipped_young"}
        forced = cz.must_wrap(head)
        if not forced:
            if cz.allocated(st.st_size) <= cz.BLOCK:
                return {"status": "skipped_small"}
            if cz.looks_precompressed(head):
                return {"status": "skipped_precompressed"}
            if cz.sample_saving_pct(head) < min_saving_pct:
                return {"status": "skipped_ratio"}
        f.seek(0)
        before = st.st_blocks * 512
        if dry_run:
            sink = _Counting()
            try:
                got_sha, got_size = cz.compress_into(f, sink, st.st_size, sha, level)  # type: ignore[arg-type]
            except zstandard.ZstdError:
                return {"status": "corrupt"}
            if (got_sha, got_size) != (sha, st.st_size):
                return {"status": "corrupt"}
            if not forced and not cz.pays_off(st.st_size, sink.n, min_saving_pct):
                return {"status": "skipped_ratio_full"}
            return {"status": "would_compress", "before": before, "after": cz.allocated(sink.n), "raw": st.st_size}
        fd, tmp = tempfile.mkstemp(prefix="reva-zmig-", dir=str(staging))
        try:
            with os.fdopen(fd, "wb") as out:
                got_sha, got_size = cz.compress_into(f, out, st.st_size, sha, level)
        except zstandard.ZstdError:
            return {"status": "corrupt"}  # source size changed under us / unreadable: leave it untouched
        if (got_sha, got_size) != (sha, st.st_size):
            return {"status": "corrupt"}  # the stored bytes do not hash to their address: never rewrite them
        stored = os.path.getsize(tmp)
        if not forced and not cz.pays_off(st.st_size, stored, min_saving_pct):
            return {"status": "skipped_ratio_full"}
        # Verify the replacement by decoding it from disk (sha256 + size of the raw bytes).
        if cz.hash_object_file(tmp) != (sha, st.st_size):
            return {"status": "error_verify"}
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        if before_replace is not None:
            before_replace(path)
        try:
            cur = os.stat(path)
        except FileNotFoundError:
            return {"status": "vanished"}  # deleted meanwhile (e.g. orphan GC): never resurrect it
        if (cur.st_ino, cur.st_size, cur.st_mtime_ns) != (st.st_ino, st.st_size, st.st_mtime_ns):
            return {"status": "changed_during"}
        os.replace(tmp, path)
        tmp = None
        _fsync_dir(path.parent)
        after = os.stat(path).st_blocks * 512
        return {"status": "compressed", "before": before, "after": after, "raw": st.st_size}
    finally:
        f.close()
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def compress_tree(
    root: Path | str,
    prefix: str = "reva/",
    *,
    namespaces: tuple[str, ...] = NAMESPACES,
    level: int = cz.DEFAULT_LEVEL,
    min_saving_pct: float = cz.DEFAULT_MIN_SAVING_PCT,
    min_age_s: float = 0.0,
    dry_run: bool = False,
    limit: int | None = None,
    log: TextIO | None = None,
    progress: Callable[[CompressReport], None] | None = None,
    progress_every: int = 500,
) -> CompressReport:
    """Compress every eligible object under ``root`` (see module docstring). Resumable and idempotent."""
    root = Path(root).resolve()
    staging = root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    rep = CompressReport(dry_run=dry_run, root=str(root))
    t0 = time.time()
    done = 0
    for ns, sha, path in iter_object_files(root, prefix, namespaces):
        try:
            r = compress_object_file(
                path,
                ns,
                sha,
                root=root,
                prefix=prefix,
                staging=staging,
                level=level,
                min_saving_pct=min_saving_pct,
                min_age_s=min_age_s,
                dry_run=dry_run,
                now=t0,
            )
        except Exception as e:
            r = {"status": "error", "error": f"{type(e).__name__}: {e}"[:200]}
        status = r["status"]
        rep.statuses[status] += 1
        nsd = rep.by_namespace.setdefault(ns, {"files": 0, "compressed": 0, "bytes_before": 0, "bytes_after": 0})
        nsd["files"] += 1
        if status in ("compressed", "would_compress"):
            rep.bytes_before += r["before"]
            rep.bytes_after += r["after"]
            rep.raw_bytes_compressed += r["raw"]
            nsd["compressed"] += 1
            nsd["bytes_before"] += r["before"]
            nsd["bytes_after"] += r["after"]
        elif status == "corrupt":
            rep.corrupt.append(str(path))
        elif status.startswith("error"):
            rep.errors.append(f"{path}: {r.get('error', status)}")
        if log is not None and (status in ("compressed", "would_compress", "corrupt") or status.startswith("error")):
            log.write(json.dumps({"namespace": ns, "sha256": sha, **r}) + "\n")
            log.flush()
        done += 1
        if progress is not None and done % progress_every == 0:
            rep.seconds = time.time() - t0
            progress(rep)
        if limit is not None and rep.statuses["compressed"] + rep.statuses["would_compress"] >= limit:
            break
    rep.seconds = time.time() - t0
    return rep


def verify_tree(root: Path | str, prefix: str = "reva/", *, namespaces: tuple[str, ...] = NAMESPACES) -> dict[str, Any]:
    """Decode and re-hash every object: its raw bytes must hash to its address."""
    root = Path(root).resolve()
    counts: Counter[str] = Counter()
    bad: list[str] = []
    raw_bytes = stored_bytes = 0
    for _ns, sha, path in iter_object_files(root, prefix, namespaces):
        try:
            wrapped = cz.is_wrapped(path)
            got_sha, size = cz.hash_object_file(path)
        except FileNotFoundError:
            counts["vanished"] += 1
            continue
        except Exception as e:  # IntegrityFailure from a broken wrapper, OSError
            counts["bad"] += 1
            bad.append(f"{path}: {type(e).__name__}")
            continue
        if got_sha != sha:
            counts["bad"] += 1
            bad.append(str(path))
            continue
        counts["ok_compressed" if wrapped else "ok_raw"] += 1
        raw_bytes += size
        stored_bytes += os.stat(path).st_blocks * 512
    return {
        "root": str(root),
        "counts": dict(counts),
        "raw_bytes": raw_bytes,
        "allocated_bytes": stored_bytes,
        "bad": bad[:100],
        "ok": not bad,
    }
