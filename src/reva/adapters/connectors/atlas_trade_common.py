"""Shared plumbing of the trade atlas connectors (V3, agent A-TRADE).

Used by :mod:`atlas_baci` (CEPII BACI), :mod:`atlas_unctad` (UNCTADstat bulk files) and
:mod:`atlas_trade_batis` (OECD-WTO BaTIS). Nothing here fetches anything: those modules request
bytes only through the ATLASCORE run (``run.get`` / ``run.get_range``), archive them with
``run.archive`` and register tables with ``run.register``. This module only holds exact,
provider-neutral helpers:

* **zip by byte ranges**: parse the end-of-central-directory + central directory from an archive
  tail (zip64 aware), compute each member's exact byte range (local header .. next member), and
  inflate a served member *incrementally* (bounded output per step, CRC-32 and size checked
  against the central directory) so a 360 MB CSV member is never held decompressed in memory;
* **7z single-file archives** (UNCTADstat bulk files are ``.csv.7z``): a minimal reader for the
  one layout they use (one folder, one LZMA2 or LZMA coder, one file), with the start-header,
  next-header and data CRCs checked. Anything else is refused (typed error), never guessed;
* **exact money arithmetic**: provider decimals with at most ``scale`` fraction digits as integer
  multiples of ``10**-scale`` (``"122.03"`` -> ``122030`` thousandths) and back, so aggregated
  and derived cells are exact decimal strings (no float round trip);
* small parsing helpers (strict decimal text, canonical JSON, quarter periods).

Every failure is typed (``SourceResponseError`` for provider bytes, ``ValidationProblem`` for
values); nothing is stubbed or silently dropped.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import lzma
import re
import struct
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl

from reva.kernel.errors import SourceResponseError, ValidationProblem

_STRICT_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")


# --------------------------------------------------------------------------- small helpers
def canon(obj: Mapping[str, Any]) -> str:
    """Canonical JSON object text (flags / selector columns)."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def code_digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(m).encode())
    return "sha256:" + h.hexdigest()


def strict_decimal(text: str | None) -> str | None:
    """The provider number as a strict decimal string (verbatim when already strict), ``None`` if empty.

    Other numeric spellings are rewritten exactly through ``Decimal``; ``NaN``/``inf``/``1,000``/text
    raise ``ValidationProblem``."""
    from decimal import Decimal, InvalidOperation

    if text is None:
        return None
    t = text.strip()
    if t == "":
        return None
    if _STRICT_DECIMAL.match(t):
        return t
    try:
        d = Decimal(t)
    except InvalidOperation as e:
        raise ValidationProblem("provider value is not a number", value=t[:40]) from e
    if not d.is_finite():
        raise ValidationProblem("provider value is not finite", value=t[:40])
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    s = "0" if s in ("-0", "") else s
    return s


def scaled_int_expr(col: str, scale: int) -> pl.Expr:
    """Polars expression: a non-negative or negative decimal string with <= ``scale`` fraction
    digits -> exact Int64 multiple of ``10**-scale``. Callers check the text pattern first."""
    parts = pl.col(col).str.split_exact(".", 1)
    whole = parts.struct.field("field_0")
    frac = parts.struct.field("field_1").fill_null("").str.pad_end(scale, "0")
    neg = whole.str.starts_with("-")
    mag = whole.str.strip_prefix("-").cast(pl.Int64) * (10**scale) + frac.cast(pl.Int64)
    return pl.when(neg).then(-mag).otherwise(mag)


def scaled_to_text_expr(col: str, scale: int) -> pl.Expr:
    """Polars expression: exact Int64 multiple of ``10**-scale`` -> strict decimal string
    (trailing fraction zeros dropped, ``-0`` never produced)."""
    v = pl.col(col)
    a = v.abs()
    whole = (a // (10**scale)).cast(pl.String)
    frac = (a % (10**scale)).cast(pl.String).str.pad_start(scale, "0").str.strip_chars_end("0")
    body = pl.when(frac == "").then(whole).otherwise(whole + pl.lit(".") + frac)
    return pl.when(v < 0).then(pl.lit("-") + body).otherwise(body)


def scaled_to_text(n: int, scale: int) -> str:
    sign = "-" if n < 0 else ""
    a = abs(n)
    whole, frac = divmod(a, 10**scale)
    f = str(frac).rjust(scale, "0").rstrip("0")
    return f"{sign}{whole}.{f}" if f else f"{sign}{whole}"


def quarter_period(label: str) -> tuple[date, date]:
    """``"2006Q01"`` -> ``[2006-01-01, 2006-04-01)``."""
    m = re.fullmatch(r"([12][0-9]{3})Q0?([1-4])", label)
    if not m:
        raise SourceResponseError("unexpected quarter label", label=label[:20], retryable=False)
    y, q = int(m.group(1)), int(m.group(2))
    start = date(y, 3 * (q - 1) + 1, 1)
    end = date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)
    return start, end


# --------------------------------------------------------------------------- zip by byte ranges
@dataclass(frozen=True)
class ZipMember:
    name: str
    method: int
    crc32: int
    csize: int
    usize: int
    offset: int  # local header offset
    end: int  # inclusive last byte of this member's range (next member's offset - 1, or cd start - 1)


class ZipError(SourceResponseError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, retryable=False, **details)


def parse_content_range(value: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", (value or "").strip())
    if not m:
        raise ZipError("response has no usable Content-Range", content_range=(value or "")[:80])
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def central_directory(tail: bytes, tail_start: int, total: int) -> list[ZipMember]:
    """Members of a zip archive from its tail bytes (``tail_start`` = absolute offset of ``tail[0]``)."""
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) - i < 22:
        raise ZipError("end of central directory not found in the archive tail")
    n, cd_size, cd_off = struct.unpack("<HII", tail[i + 10 : i + 20])
    j = tail.rfind(b"PK\x06\x06", 0, i)
    if j >= 0 and (cd_off == 0xFFFFFFFF or n == 0xFFFF or cd_size == 0xFFFFFFFF):
        n, cd_size, cd_off = struct.unpack("<QQQ", tail[j + 32 : j + 56])
    p = cd_off - tail_start
    if p < 0 or cd_off + cd_size > total:
        raise ZipError("central directory not inside the fetched tail", cd_offset=cd_off, tail_start=tail_start)
    raw: list[tuple[str, int, int, int, int, int]] = []
    for _ in range(n):
        if tail[p : p + 4] != b"PK\x01\x02":
            raise ZipError("corrupt central directory entry", at=tail_start + p)
        (method,) = struct.unpack("<H", tail[p + 10 : p + 12])
        crc, cs, us, fl, el, cl = struct.unpack("<IIIHHH", tail[p + 16 : p + 34])
        (off,) = struct.unpack("<I", tail[p + 42 : p + 46])
        name = tail[p + 46 : p + 46 + fl].decode("utf-8")
        extra = tail[p + 46 + fl : p + 46 + fl + el]
        q = 0
        while q + 4 <= len(extra):
            hid, hs = struct.unpack("<HH", extra[q : q + 4])
            body, k = extra[q + 4 : q + 4 + hs], 0
            if hid == 1:  # zip64 extended information
                if us == 0xFFFFFFFF:
                    (us,) = struct.unpack("<Q", body[k : k + 8])
                    k += 8
                if cs == 0xFFFFFFFF:
                    (cs,) = struct.unpack("<Q", body[k : k + 8])
                    k += 8
                if off == 0xFFFFFFFF:
                    (off,) = struct.unpack("<Q", body[k : k + 8])
            q += 4 + hs
        raw.append((name, method, crc, cs, us, off))
        p += 46 + fl + el + cl
    raw.sort(key=lambda r: r[5])
    out = []
    for idx, (name, method, crc, cs, us, off) in enumerate(raw):
        end = (raw[idx + 1][5] if idx + 1 < len(raw) else cd_off) - 1
        if end < off + 30 + cs - 1:
            raise ZipError("member range shorter than its compressed size", member=name)
        out.append(ZipMember(name, method, crc, cs, us, off, end))
    return out


def iter_member(
    body: bytes, served_start: int, m: ZipMember, *, step: int = 8 << 20, max_out: int = 1_500_000_000
) -> Iterator[bytes]:
    """Inflate one member from the bytes served for its range, yielding decompressed pieces.

    The local header name must equal the directory name; total size and CRC-32 are checked at the
    end (a mismatch raises after the last piece, so callers must consume the iterator fully before
    trusting what they built)."""
    if served_start != m.offset:
        raise ZipError("range does not start at the member's local header", member=m.name, served=served_start)
    if body[:4] != b"PK\x03\x04":
        raise ZipError("no local file header at the member offset", member=m.name)
    fl, el = struct.unpack("<HH", body[26:30])
    if body[30 : 30 + fl].decode("utf-8") != m.name:
        raise ZipError("local header name differs from the central directory", member=m.name)
    start = 30 + fl + el
    data = memoryview(body)[start : start + m.csize]
    if len(data) != m.csize:
        raise ZipError("range holds fewer bytes than the member's compressed size", member=m.name)
    if m.usize > max_out:
        raise ZipError("member larger than the decompressed size cap", member=m.name, size=m.usize)
    crc, total = 0, 0
    if m.method == 0:
        pieces: Iterator[bytes] = (bytes(data[i : i + step]) for i in range(0, len(data), step))
    elif m.method == 8:
        pieces = _inflate(data, step)
    else:
        raise ZipError("unsupported zip compression method", member=m.name, method=m.method)
    for piece in pieces:
        total += len(piece)
        if total > m.usize:
            raise ZipError("member inflates beyond its declared size", member=m.name)
        crc = zlib.crc32(piece, crc)
        yield piece
    if total != m.usize or crc != m.crc32:
        raise ZipError("member size/CRC-32 mismatch", member=m.name, size=total, expected=m.usize)


def _inflate(data: memoryview, step: int) -> Iterator[bytes]:
    d = zlib.decompressobj(-15)
    pos = 0
    pending = b""
    while True:
        if d.unconsumed_tail:
            pending = d.unconsumed_tail
        elif pos < len(data):
            pending = bytes(data[pos : pos + step])
            pos += step
        else:
            break
        out = d.decompress(pending, step)
        if out:
            yield out
        if d.eof:
            break
    tail = d.flush()
    if tail:
        yield tail
    if not d.eof:
        raise ZipError("truncated deflate stream")


def iter_csv_blocks(pieces: Iterator[bytes], *, header: bytes, block: int = 32 << 20) -> Iterator[bytes]:
    """Re-cut decompressed pieces into blocks of whole lines, each starting with ``header``.

    The first line of the stream must be exactly ``header`` (without newline)."""
    buf = b""
    first = True
    for piece in pieces:
        buf += piece
        if first:
            nl = buf.find(b"\n")
            if nl < 0:
                continue
            got = buf[:nl].rstrip(b"\r")
            if got.lstrip(b"\xef\xbb\xbf") != header:
                raise ZipError("unexpected CSV header", header=got[:120].decode("utf-8", "replace"))
            buf = buf[nl + 1 :]
            first = False
        if len(buf) >= block:
            cut = buf.rfind(b"\n") + 1
            if cut > 0:
                yield header + b"\n" + buf[:cut]
                buf = buf[cut:]
    if first:
        raise ZipError("CSV member has no header line")
    if buf.strip():
        yield header + b"\n" + (buf if buf.endswith(b"\n") else buf + b"\n")


# --------------------------------------------------------------------------- 7z (single file)
class SevenZipError(SourceResponseError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, retryable=False, **details)


_7Z_SIG = b"7z\xbc\xaf\x27\x1c"


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def byte(self) -> int:
        if self.p >= len(self.d):
            raise SevenZipError("7z header truncated")
        b = self.d[self.p]
        self.p += 1
        return b

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise SevenZipError("7z header truncated")
        b = self.d[self.p : self.p + n]
        self.p += n
        return b

    def number(self) -> int:
        first = self.byte()
        mask, value = 0x80, 0
        for i in range(8):
            if first & mask == 0:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def u32(self) -> int:
        return int(struct.unpack("<I", self.take(4))[0])


def _digests(c: _Cursor, n: int) -> list[int | None]:
    all_defined = c.byte()
    if all_defined:
        defined = [True] * n
    else:
        bits: list[bool] = []
        byte, mask = 0, 0
        for _ in range(n):
            if mask == 0:
                byte, mask = c.byte(), 0x80
            bits.append(bool(byte & mask))
            mask >>= 1
        defined = bits
    return [c.u32() if d else None for d in defined]


@dataclass
class _Streams:
    pack_pos: int = 0
    pack_sizes: tuple[int, ...] = ()
    coder_id: bytes = b""
    coder_props: bytes = b""
    unpack_size: int = 0
    folder_crc: int | None = None
    stream_crc: int | None = None


def _streams_info(c: _Cursor) -> _Streams:
    s = _Streams()
    while True:
        t = c.byte()
        if t == 0x00:
            return s
        if t == 0x06:  # PackInfo
            s.pack_pos = c.number()
            n = c.number()
            while True:
                k = c.byte()
                if k == 0x00:
                    break
                if k == 0x09:
                    s.pack_sizes = tuple(c.number() for _ in range(n))
                elif k == 0x0A:
                    _digests(c, n)
                else:
                    raise SevenZipError("unexpected 7z PackInfo property", prop=k)
        elif t == 0x07:  # CodersInfo
            if c.byte() != 0x0B:
                raise SevenZipError("7z CodersInfo without folder list")
            n_folders = c.number()
            if c.byte() != 0 or n_folders != 1:
                raise SevenZipError("only single-folder, inline 7z archives are supported", folders=n_folders)
            n_coders = c.number()
            if n_coders != 1:
                raise SevenZipError("only single-coder 7z folders are supported", coders=n_coders)
            flags = c.byte()
            if flags & 0x10:
                raise SevenZipError("complex 7z coders are not supported")
            s.coder_id = c.take(flags & 0x0F)
            if flags & 0x20:
                s.coder_props = c.take(c.number())
            if c.byte() != 0x0C:
                raise SevenZipError("7z CodersInfo without unpack sizes")
            s.unpack_size = c.number()
            while True:
                k = c.byte()
                if k == 0x00:
                    break
                if k == 0x0A:
                    s.folder_crc = _digests(c, 1)[0]
                else:
                    raise SevenZipError("unexpected 7z CodersInfo property", prop=k)
        elif t == 0x08:  # SubStreamsInfo
            while True:
                k = c.byte()
                if k == 0x00:
                    break
                if k == 0x0D:
                    if c.number() != 1:
                        raise SevenZipError("only one stream per 7z folder is supported")
                elif k == 0x0A:
                    s.stream_crc = _digests(c, 1)[0]
                else:
                    raise SevenZipError("unexpected 7z SubStreamsInfo property", prop=k)
        else:
            raise SevenZipError("unexpected 7z StreamsInfo property", prop=t)


def _files(c: _Cursor) -> list[str]:
    n = c.number()
    names: list[str] = []
    while True:
        t = c.byte()
        if t == 0x00:
            break
        size = c.number()
        data = c.take(size)
        if t == 0x11:  # names: external flag + UTF-16LE zero-terminated
            if data[0] != 0:
                raise SevenZipError("external 7z file names are not supported")
            names = [x for x in data[1:].decode("utf-16-le").split("\x00") if x]
    if len(names) != n:
        raise SevenZipError("7z file name list does not match the file count", files=n, names=len(names))
    return names


def _decode(packed: bytes, coder_id: bytes, props: bytes, size: int, limit: int) -> bytes:
    if size > limit:
        raise SevenZipError("7z content larger than the decompressed size cap", size=size, limit=limit)
    if coder_id == b"\x21":  # LZMA2
        if len(props) != 1 or props[0] > 40:
            raise SevenZipError("bad LZMA2 properties")
        p = props[0]
        dict_size = 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)
        filters: list[dict[str, Any]] = [{"id": lzma.FILTER_LZMA2, "dict_size": dict_size}]
    elif coder_id == b"\x03\x01\x01":  # LZMA
        if len(props) != 5:
            raise SevenZipError("bad LZMA properties")
        d = props[0]
        lc, rem = d % 9, d // 9
        lp, pb = rem % 5, rem // 5
        filters = [
            {"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack("<I", props[1:5])[0], "lc": lc, "lp": lp, "pb": pb}
        ]
    else:
        raise SevenZipError("unsupported 7z coder (only LZMA2/LZMA)", coder=coder_id.hex())
    try:
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        out = dec.decompress(packed, max_length=size + 1)
    except lzma.LZMAError as e:
        raise SevenZipError("corrupt 7z LZMA stream") from e
    if len(out) != size:
        raise SevenZipError("7z content size differs from its header", size=len(out), expected=size)
    return out


def read_7z_single(data: bytes, *, limit: int = 1_000_000_000) -> tuple[str, bytes]:
    """``(file name, content)`` of a one-file 7z archive (CRCs checked)."""
    if len(data) < 32 or data[:6] != _7Z_SIG:
        raise SevenZipError("not a 7z archive")
    (start_crc,) = struct.unpack("<I", data[8:12])
    if zlib.crc32(data[12:32]) != start_crc:
        raise SevenZipError("7z start header CRC mismatch")
    off, size, crc = struct.unpack("<QQI", data[12:32])
    if 32 + off + size > len(data):
        raise SevenZipError("7z archive truncated")
    header = data[32 + off : 32 + off + size]
    if zlib.crc32(header) != crc:
        raise SevenZipError("7z header CRC mismatch")
    c = _Cursor(header)
    t = c.byte()
    if t == 0x17:  # encoded header: decode it, then parse the real header
        s = _streams_info(c)
        start = 32 + s.pack_pos
        packed = data[start : start + sum(s.pack_sizes)]
        header = _decode(packed, s.coder_id, s.coder_props, s.unpack_size, 50_000_000)
        if s.folder_crc is not None and zlib.crc32(header) != s.folder_crc:
            raise SevenZipError("7z encoded header CRC mismatch")
        c = _Cursor(header)
        t = c.byte()
    if t != 0x01:
        raise SevenZipError("unexpected 7z header type", type=t)
    s, names = _Streams(), []
    while True:
        k = c.byte()
        if k == 0x00:
            break
        if k == 0x04:
            s = _streams_info(c)
        elif k == 0x05:
            names = _files(c)
        else:
            raise SevenZipError("unsupported 7z header property", prop=k)
    if len(names) != 1 or len(s.pack_sizes) != 1:
        raise SevenZipError("only single-file 7z archives are supported", files=len(names))
    start = 32 + s.pack_pos
    content = _decode(data[start : start + s.pack_sizes[0]], s.coder_id, s.coder_props, s.unpack_size, limit)
    want = s.stream_crc if s.stream_crc is not None else s.folder_crc
    if want is None or zlib.crc32(content) != want:
        raise SevenZipError("7z content CRC missing or mismatched", file=names[0])
    return names[0], content


def write_7z_single(name: str, content: bytes) -> bytes:
    """A one-file LZMA2 7z archive in the layout UNCTADstat serves (used to build test fixtures)."""
    p = 0x18  # 16 MiB dictionary property, as in the provider files
    comp = lzma.LZMACompressor(
        format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": (2 | (p & 1)) << (p // 2 + 11)}]
    )
    packed = comp.compress(content) + comp.flush()

    def num(v: int) -> bytes:
        if v < 0x80:
            return bytes([v])
        for n in range(1, 9):
            if v < (1 << (7 * (n + 1))) or n == 8:
                if n == 8:
                    return b"\xff" + v.to_bytes(8, "little")
                high = v >> (8 * n)
                mask = (0xFF << (8 - n)) & 0xFF
                return bytes([mask | high]) + (v & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
        raise AssertionError  # pragma: no cover

    names = b"\x00" + (name + "\x00").encode("utf-16-le")
    h = b"\x01\x04"
    h += b"\x06" + num(0) + num(1) + b"\x09" + num(len(packed)) + b"\x00"
    h += b"\x07\x0b" + num(1) + b"\x00" + num(1) + b"\x21\x21\x01" + bytes([p]) + b"\x0c" + num(len(content)) + b"\x00"
    h += b"\x08\x0a\x01" + struct.pack("<I", zlib.crc32(content)) + b"\x00"
    h += b"\x00"
    h += b"\x05" + num(1) + b"\x11" + num(len(names)) + names + b"\x00"
    h += b"\x00"
    nxt = struct.pack("<QQI", len(packed), len(h), zlib.crc32(h))
    return _7Z_SIG + b"\x00\x04" + struct.pack("<I", zlib.crc32(nxt)) + nxt + packed + h


def read_csv_all_text(data: bytes, *, what: str) -> pl.DataFrame:
    """CSV bytes -> all-string polars frame (UTF-8, BOM stripped, nothing inferred)."""
    import io

    try:
        txt = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SourceResponseError(f"{what}: not UTF-8 CSV", retryable=False) from e
    if not txt.strip():
        raise SourceResponseError(f"{what}: empty CSV", retryable=False)
    try:
        return pl.read_csv(io.StringIO(txt), infer_schema_length=0, quote_char='"')
    except Exception as e:  # polars raises several error types for malformed CSV
        raise SourceResponseError(f"{what}: malformed CSV ({type(e).__name__})", retryable=False) from e


__all__ = [
    "SevenZipError",
    "ZipError",
    "ZipMember",
    "canon",
    "central_directory",
    "code_digest",
    "iter_csv_blocks",
    "iter_member",
    "parse_content_range",
    "quarter_period",
    "read_7z_single",
    "read_csv_all_text",
    "scaled_int_expr",
    "scaled_to_text",
    "scaled_to_text_expr",
    "strict_decimal",
    "write_7z_single",
]
