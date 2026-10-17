"""Transparent zstd compression of stored objects (V6 DISKZ).

On-disk format of a *compressed* object file (the path is unchanged: the object id stays
the sha256 of the **raw** bytes, never of the compressed bytes)::

    MAGIC (8 bytes) | version u8 | codec u8 | raw_size u64 LE | payload_size u64 LE | raw_sha256 (32 bytes)
    | one zstd frame (payload_size bytes, with content size and frame checksum)

Any file that does not start with ``MAGIC`` holds the raw bytes themselves (legacy objects and
objects for which compression does not pay). Raw bytes that happen to start with ``MAGIC`` are
*always* stored wrapped, so a file starting with ``MAGIC`` is unambiguously a wrapper.

Reads decode transparently and every caller-facing read re-verifies the sha256 of the decoded
bytes against the address (``FilesystemArtifactStore.get/open``; ``hash_object_file`` for
inventory tools). Decoding is bounded: the decoder stops with ``IntegrityFailure`` as soon as it
produces more bytes than the header declares.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import zstandard

from reva.kernel.errors import IntegrityFailure

CHUNK = 1024 * 1024
MAGIC = b"\x89RVZ\r\n\x1a\n"
VERSION = 1
CODEC_ZSTD = 1
_HDR = struct.Struct("<8sBBQQ32s")
HEADER_SIZE = _HDR.size  # 58
_PAYLOAD_OFFSET = 18
DEFAULT_LEVEL = 9
SAMPLE_LEVEL = 3
SAMPLE_BYTES = 1 << 20
DEFAULT_MIN_SAVING_PCT = 10.0
BLOCK = 4096

#: Leading bytes of formats that are already compressed: never worth a zstd pass.
PRECOMPRESSED_MAGIC: tuple[bytes, ...] = (
    b"\x1f\x8b",  # gzip
    b"PK\x03\x04",  # zip (also xlsx/docx/odt/jar)
    b"PK\x05\x06",  # empty zip
    b"\x28\xb5\x2f\xfd",  # zstd
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"7z\xbc\xaf\x27\x1c",  # 7z
    b"Rar!",  # rar
    b"\x04\x22\x4d\x18",  # lz4 frame
    b"\x89PNG",  # png
    b"\xff\xd8\xff",  # jpeg
    b"GIF8",  # gif
    b"OggS",  # ogg
)


@dataclass(frozen=True)
class Header:
    raw_size: int
    raw_sha256: str
    codec: int
    payload_size: int


def encode_header(raw_size: int, raw_sha256: str, payload_size: int = 0) -> bytes:
    return _HDR.pack(MAGIC, VERSION, CODEC_ZSTD, raw_size, payload_size, bytes.fromhex(raw_sha256))


def parse_header(buf: bytes) -> Header | None:
    """Header of a wrapped object, None for a raw object; IntegrityFailure for a broken wrapper."""
    if not buf.startswith(MAGIC):
        return None
    if len(buf) < HEADER_SIZE:
        raise IntegrityFailure("truncated compressed-object header")
    _, ver, codec, size, payload, sha = _HDR.unpack(buf[:HEADER_SIZE])
    if ver != VERSION or codec != CODEC_ZSTD:
        raise IntegrityFailure("unsupported compressed-object format", version=ver, codec=codec)
    return Header(size, sha.hex(), codec, payload)


def read_header(f: BinaryIO) -> Header | None:
    """Read the header of an open object file. Leaves ``f`` at the payload (wrapped) or at 0 (raw)."""
    buf = f.read(HEADER_SIZE)
    hdr = parse_header(buf)
    if hdr is None:
        f.seek(0)
    return hdr


def file_header(path: Path | str) -> Header | None:
    with open(path, "rb") as f:
        return parse_header(f.read(HEADER_SIZE))


def iter_raw_from(f: BinaryIO, header: Header | None, chunk: int = CHUNK) -> Iterator[bytes]:
    """Raw (decoded) chunks of an object file opened at the position ``read_header`` left it.

    Not verified against the address: callers hash what they consume. Bounded: a wrapper that
    decodes to more than its declared size, or a broken zstd frame, raises IntegrityFailure.
    """
    if header is None:
        while data := f.read(chunk):
            yield data
        return
    pos = f.tell()
    end = f.seek(0, os.SEEK_END)
    f.seek(pos)
    if end - pos != header.payload_size:  # truncated, or bytes appended after the frame
        raise IntegrityFailure(
            "compressed object has a wrong payload length", declared=header.payload_size, actual=end - pos
        )
    reader = zstandard.ZstdDecompressor().stream_reader(f, read_size=chunk, read_across_frames=False, closefd=False)
    n = 0
    try:
        while True:
            try:
                data = reader.read(chunk)
            except zstandard.ZstdError as e:
                raise IntegrityFailure("compressed object does not decode", error=str(e)[:200]) from e
            if not data:
                break
            n += len(data)
            if n > header.raw_size:
                raise IntegrityFailure("compressed object decodes past its declared size", declared=header.raw_size)
            yield data
    finally:
        reader.close()  # type: ignore[no-untyped-call]
    if n != header.raw_size:
        raise IntegrityFailure("compressed object decodes to a wrong size", declared=header.raw_size, actual=n)


def iter_object_file(path: Path | str, chunk: int = CHUNK) -> Iterator[bytes]:
    """Raw chunks of a stored object file, raw or wrapped (unverified: hash what you consume)."""
    with open(path, "rb") as f:
        hdr = read_header(f)
        yield from iter_raw_from(f, hdr, chunk)


def decode_bytes(data: bytes) -> bytes:
    """Raw bytes of a whole stored file's content (bounded by the declared size; unverified)."""
    hdr = parse_header(data[:HEADER_SIZE])
    if hdr is None:
        return data
    f = io.BytesIO(data)
    f.seek(HEADER_SIZE)
    return b"".join(iter_raw_from(f, hdr))


def object_raw_size(path: Path | str) -> int:
    """Raw size of a stored object (header for wrapped objects, file size otherwise)."""
    with open(path, "rb") as f:
        hdr = parse_header(f.read(HEADER_SIZE))
        return hdr.raw_size if hdr is not None else os.fstat(f.fileno()).st_size


def hash_object_file(path: Path | str) -> tuple[str, int]:
    """sha256 and size of the decoded raw bytes of a stored object file."""
    h = hashlib.sha256()
    n = 0
    for data in iter_object_file(path):
        h.update(data)
        n += len(data)
    return h.hexdigest(), n


def is_wrapped(path: Path | str) -> bool:
    return file_header(path) is not None


# --------------------------------------------------------------------------- decision
def allocated(n: int, block: int = BLOCK) -> int:
    """Bytes a file of ``n`` bytes occupies on a filesystem with ``block``-sized allocation."""
    return ((n + block - 1) // block) * block


def looks_precompressed(head: bytes) -> bool:
    return any(head.startswith(m) for m in PRECOMPRESSED_MAGIC)


def sample_saving_pct(sample: bytes) -> float:
    """Percentage saved by a fast zstd pass over ``sample`` (0 for empty input)."""
    if not sample:
        return 0.0
    c = len(zstandard.ZstdCompressor(level=SAMPLE_LEVEL).compress(sample))
    return 100.0 * (1.0 - c / len(sample))


def must_wrap(head: bytes) -> bool:
    """Raw bytes beginning with MAGIC are always stored wrapped (keeps the format unambiguous)."""
    return head.startswith(MAGIC)


def worth_trying(head: bytes, raw_size: int, min_saving_pct: float) -> bool:
    """Cheap pre-check (magic bytes + ratio test on the first MiB) before a full compression pass."""
    if allocated(raw_size) <= BLOCK:  # a one-block file cannot free a block
        return False
    if looks_precompressed(head):
        return False
    return sample_saving_pct(head[:SAMPLE_BYTES]) >= min_saving_pct


def pays_off(raw_size: int, stored_size: int, min_saving_pct: float) -> bool:
    """Keep the compressed form only if it frees at least one block and ``min_saving_pct`` percent."""
    return allocated(stored_size) < allocated(raw_size) and stored_size <= raw_size * (1.0 - min_saving_pct / 100.0)


def compress_into(
    src: BinaryIO, out: BinaryIO, raw_size: int, raw_sha256: str, level: int = DEFAULT_LEVEL
) -> tuple[str, int]:
    """Write header + one zstd frame of ``src`` into ``out`` (fsynced). Returns the sha256/size actually read.

    The header records the *expected* sha/size; the caller must compare the returned values with them
    (a mismatch means the source changed or is corrupt, and the output must be discarded).
    """
    start = out.tell() if hasattr(out, "tell") else 0
    out.write(encode_header(raw_size, raw_sha256))
    h = hashlib.sha256()
    n = 0
    cctx = zstandard.ZstdCompressor(level=level, write_content_size=True, write_checksum=True)
    with cctx.stream_writer(out, size=raw_size, closefd=False) as w:
        while data := src.read(CHUNK):
            h.update(data)
            n += len(data)
            w.write(data)
        w.flush(zstandard.FLUSH_FRAME)
    if hasattr(out, "seek"):  # record the payload length (detects truncation and appended bytes)
        end = out.tell()
        out.seek(start + _PAYLOAD_OFFSET)
        out.write(struct.pack("<Q", end - start - HEADER_SIZE))
        out.seek(end)
    out.flush()
    try:
        os.fsync(out.fileno())
    except (AttributeError, io.UnsupportedOperation):
        pass  # in-memory / counting sink
    return h.hexdigest(), n
