"""Safe archive extraction: no path traversal, bounded size and ratio, no ambiguity.

Members are returned keyed by basename, so any ambiguity is an error rather than
a silent "last one wins": duplicate member names (after path normalisation),
case-insensitive collisions between member paths, and two wanted members with
the same (case-folded) basename are all rejected. Corrupt, truncated or
size-lying archives fail as typed errors (``UnsafeRequest``/``SourceResponseError``),
never as raw ``zipfile``/``zlib`` exceptions.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
import zlib

from reva.kernel.errors import SourceResponseError, UnsafeRequest


def _normalised(name: str) -> str:
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or (len(name) >= 2 and name[1] == ":")  # drive-letter paths
        or ".." in name.split("/")
    ):
        raise UnsafeRequest(f"unsafe archive member path {name!r}")
    norm = posixpath.normpath(name)
    if norm in (".", "") or norm.startswith("../"):
        raise UnsafeRequest(f"unsafe archive member path {name!r}")
    return norm


def read_zip_members(
    data: bytes, wanted_suffixes: tuple[str, ...], *, max_total: int = 64 * 1024 * 1024, max_ratio: int = 100
) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    out_folded: dict[str, str] = {}
    total = 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError, OSError) as e:
        raise SourceResponseError("archive is not a readable zip file", retryable=False, kind="corrupt_archive") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > 1000:
            raise UnsafeRequest("too many archive members")
        seen: dict[str, str] = {}
        for info in infos:
            name = info.filename
            norm = _normalised(name)
            folded = norm.casefold()
            if info.is_dir():
                continue
            if folded in seen:
                raise UnsafeRequest(f"ambiguous archive member {name!r} (duplicates {seen[folded]!r})")
            seen[folded] = name
        for info in infos:
            name = info.filename
            if info.is_dir() or not name.lower().endswith(wanted_suffixes):
                continue
            base = posixpath.basename(_normalised(name))
            if base.casefold() in out_folded:
                raise UnsafeRequest(
                    f"ambiguous archive member {name!r}: basename collides with {out_folded[base.casefold()]!r}"
                )
            if info.flag_bits & 0x1:
                raise UnsafeRequest(f"encrypted archive member {name!r}")
            if info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size:
                raise UnsafeRequest(f"archive member size mismatch for {name}")
            if info.compress_size and info.file_size / max(1, info.compress_size) > max_ratio:
                raise UnsafeRequest(f"suspicious compression ratio in {name}")
            total += info.file_size
            if total > max_total:
                raise UnsafeRequest("archive expands beyond the allowed size")
            try:
                with zf.open(info) as f:
                    content = f.read(info.file_size + 1)
            except (
                zipfile.BadZipFile,
                zlib.error,
                EOFError,
                ValueError,
                NotImplementedError,
                RuntimeError,
                OSError,
            ) as e:
                raise SourceResponseError(
                    f"corrupt archive member {name!r}", retryable=False, kind="corrupt_archive"
                ) from e
            if len(content) != info.file_size:
                raise UnsafeRequest(f"archive member size mismatch for {name}")
            out[base] = content
            out_folded[base.casefold()] = name
    return out
