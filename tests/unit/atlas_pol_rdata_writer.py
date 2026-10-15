"""Minimal R serialization (RDX3/XDR) *writer* for tests of ``atlas_rdata`` (agent A-POL).

Writes an ``.RData`` holding data frames of character / double / integer columns, the way R's
``save()`` does (gzip, ``RDX3``, XDR version 3, a pairlist of named objects). Used to (a) round-trip
test the reader on known values, NA patterns and an ALTREP ``compact_intseq`` column, and (b) derive
the small V-Dem fixture ``tests/fixtures/source/atlas.vdem`` from the real v16 file (subset of
rows/columns, values copied bit-for-bit). Not a general R writer.
"""

from __future__ import annotations

import gzip
import math
import struct
from collections.abc import Mapping, Sequence
from typing import Any

NA_REAL_BYTES = bytes.fromhex("7ff00000000007a2")  # R's NA_real_ (NaN with low word 1954)
NA_INTEGER = -(2**31)


class Alt:
    """Marker: write an integer column as ALTREP compact_intseq(start, n)."""

    def __init__(self, start: int, n: int) -> None:
        self.start, self.n = start, n


def _i(v: int) -> bytes:
    return struct.pack(">i", v)


def _charsxp(s: str | None) -> bytes:
    if s is None:
        return _i(9) + _i(-1)
    b = s.encode("utf-8")
    levels = (1 << 3) if any(ord(c) > 127 for c in s) else 64  # UTF8 or ASCII mask
    return _i(9 | (levels << 12)) + _i(len(b)) + b


def _sym(name: str) -> bytes:
    return _i(1) + _charsxp(name)


def _strsxp(vals: Sequence[str | None], attrs: bytes = b"") -> bytes:
    flags = 16 | ((1 << 9) if attrs else 0)
    return _i(flags) + _i(len(vals)) + b"".join(_charsxp(v) for v in vals) + attrs


def _realsxp(vals: Sequence[float | None]) -> bytes:
    out = bytearray(_i(14) + _i(len(vals)))
    for v in vals:
        if v is None:
            out += NA_REAL_BYTES
        elif isinstance(v, float) and math.isnan(v):
            out += struct.pack(">d", float("nan"))
        else:
            out += struct.pack(">d", float(v))
    return bytes(out)


def _intsxp(vals: Sequence[int | None]) -> bytes:
    return _i(13) + _i(len(vals)) + b"".join(_i(NA_INTEGER if v is None else int(v)) for v in vals)


def _altrep_intseq(start: int, n: int) -> bytes:
    # info = pairlist(compact_intseq, base, 13L)
    info = _i(2) + _sym("compact_intseq") + _i(2) + _sym("base") + _i(2) + _intsxp([13]) + _i(254)
    state = _realsxp([float(n), float(start), 1.0])
    return _i(238) + info + state + _i(254)


def _pairlist(items: Sequence[tuple[str, bytes]]) -> bytes:
    out = b""
    for tag, val in items:
        out += _i(2 | (1 << 10)) + _sym(tag) + val
    return out + _i(254)


def _column(v: Any) -> bytes:
    if isinstance(v, Alt):
        return _altrep_intseq(v.start, v.n)
    kind, vals = v
    if kind == "str":
        return _strsxp(vals)
    if kind == "real":
        return _realsxp(vals)
    if kind == "int":
        return _intsxp(vals)
    raise ValueError(kind)


def data_frame(columns: Mapping[str, Any], nrows: int) -> bytes:
    """``columns``: name -> ("str"|"real"|"int", values) or :class:`Alt`."""
    body = _i(19 | (1 << 8) | (1 << 9)) + _i(len(columns)) + b"".join(_column(v) for v in columns.values())
    attrs = _pairlist([
        ("names", _strsxp(list(columns))),
        ("class", _strsxp(["data.frame"])),
        ("row.names", _intsxp([None, -nrows])),
    ])
    return body + attrs


def rdata(objects: Mapping[str, bytes]) -> bytes:
    """gzip-compressed ``RDX3`` file holding the given serialized objects."""
    head = b"RDX3\nX\n" + _i(3) + _i(0x040402) + _i(0x030500) + _i(5) + b"UTF-8"
    return gzip.compress(head + _pairlist(list(objects.items())), mtime=0)
