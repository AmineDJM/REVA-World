"""Safe archive extraction: no path traversal, bounded size and ratio."""

from __future__ import annotations

import io
import zipfile

from reva.kernel.errors import UnsafeRequest


def read_zip_members(
    data: bytes, wanted_suffixes: tuple[str, ...], *, max_total: int = 64 * 1024 * 1024, max_ratio: int = 100
) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > 1000:
            raise UnsafeRequest("too many archive members")
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                raise UnsafeRequest(f"unsafe archive member path {name!r}")
            if info.is_dir() or not name.lower().endswith(wanted_suffixes):
                continue
            if info.compress_size and info.file_size / max(1, info.compress_size) > max_ratio:
                raise UnsafeRequest(f"suspicious compression ratio in {name}")
            total += info.file_size
            if total > max_total:
                raise UnsafeRequest("archive expands beyond the allowed size")
            with zf.open(info) as f:
                content = f.read(info.file_size + 1)
            if len(content) != info.file_size:
                raise UnsafeRequest(f"archive member size mismatch for {name}")
            out[name.rsplit("/", 1)[-1]] = content
    return out
