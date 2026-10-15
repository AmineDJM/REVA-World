"""Bounded pure-Python inflater for zip members compressed with Deflate (8) or Deflate64 (9) (agent A-HISTECON).

Python's ``zlib``/``zipfile`` do not implement Deflate64 (PKWARE "enhanced deflate", zip method 9), which the
HYDE 3.2.1 archives on DANS use. This is a straightforward port of Mark Adler's ``puff`` reference decoder
(zlib ``contrib/puff``) with the Deflate64 differences from zlib's ``infback9``:

* 64 KiB window (distances up to 65536);
* length code 285 = base 3 + 16 extra bits (instead of the fixed length 258);
* distance codes 30 and 31 are valid (bases 32769 and 49153, 14 extra bits).

It is slow (pure Python, bit by bit) and meant for small members (summary text tables, ~1 MB): the output
size is bounded (``max_out``) and malformed input raises :class:`InflateError`. Plain Deflate (method 8)
members go through ``zlib`` instead.
"""

from __future__ import annotations

import zlib

MAXBITS = 15
MAXLCODES = 286
MAXDCODES = 32
FIXLCODES = 288

_LBASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163,
          195, 227]
_LEXT = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5]
_DBASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073,
          4097, 6145, 8193, 12289, 16385, 24577, 32769, 49153]
_DEXT = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14]


class InflateError(ValueError):
    pass


class _Huffman:
    __slots__ = ("count", "symbol")

    def __init__(self, lengths: list[int]) -> None:
        count = [0] * (MAXBITS + 1)
        for ln in lengths:
            count[ln] += 1
        left = 1
        for n in range(1, MAXBITS + 1):
            left = (left << 1) - count[n]
            if left < 0:
                raise InflateError("over-subscribed Huffman code")
        offs = [0] * (MAXBITS + 1)
        for n in range(1, MAXBITS):
            offs[n + 1] = offs[n] + count[n]
        symbol = [0] * len(lengths)
        for sym, ln in enumerate(lengths):
            if ln:
                symbol[offs[ln]] = sym
                offs[ln] += 1
        self.count = count
        self.symbol = symbol


class _State:
    __slots__ = ("data", "pos", "bitbuf", "bitcnt", "out", "max_out", "deflate64")

    def __init__(self, data: bytes, max_out: int, deflate64: bool) -> None:
        self.data = data
        self.pos = 0
        self.bitbuf = 0
        self.bitcnt = 0
        self.out = bytearray()
        self.max_out = max_out
        self.deflate64 = deflate64

    def bits(self, need: int) -> int:
        val = self.bitbuf
        while self.bitcnt < need:
            if self.pos >= len(self.data):
                raise InflateError("unexpected end of compressed data")
            val |= self.data[self.pos] << self.bitcnt
            self.pos += 1
            self.bitcnt += 8
        self.bitbuf = val >> need
        self.bitcnt -= need
        return val & ((1 << need) - 1)

    def decode(self, h: _Huffman) -> int:
        code = first = index = 0
        count = h.count
        for ln in range(1, MAXBITS + 1):
            code |= self.bits(1)
            c = count[ln]
            if code - c < first:
                return h.symbol[index + (code - first)]
            index += c
            first += c
            first <<= 1
            code <<= 1
        raise InflateError("ran out of Huffman codes")


def _stored(s: _State) -> None:
    s.bitbuf = 0
    s.bitcnt = 0
    if s.pos + 4 > len(s.data):
        raise InflateError("truncated stored block")
    ln = s.data[s.pos] | (s.data[s.pos + 1] << 8)
    nln = s.data[s.pos + 2] | (s.data[s.pos + 3] << 8)
    s.pos += 4
    if ln != (~nln & 0xFFFF):
        raise InflateError("stored block length check failed")
    if s.pos + ln > len(s.data):
        raise InflateError("truncated stored block")
    s.out += s.data[s.pos:s.pos + ln]
    s.pos += ln


def _codes(s: _State, lencode: _Huffman, distcode: _Huffman) -> None:
    out = s.out
    window = 65536 if s.deflate64 else 32768
    while True:
        sym = s.decode(lencode)
        if sym < 256:
            out.append(sym)
        elif sym == 256:
            return
        else:
            sym -= 257
            if sym >= 29:
                raise InflateError("invalid length code")
            if sym == 28 and s.deflate64:
                length = 3 + s.bits(16)
            elif sym == 28:
                length = 258
            else:
                length = _LBASE[sym] + s.bits(_LEXT[sym])
            dsym = s.decode(distcode)
            if dsym >= (32 if s.deflate64 else 30):
                raise InflateError("invalid distance code")
            dist = _DBASE[dsym] + s.bits(_DEXT[dsym])
            if dist > len(out) or dist > window:
                raise InflateError("distance too far back")
            start = len(out) - dist
            if dist >= length:
                out += out[start:start + length]
            else:
                for i in range(length):
                    out.append(out[start + i])
        if len(out) > s.max_out:
            raise InflateError("inflated output exceeds the bound")


_FIXED: tuple[_Huffman, _Huffman] | None = None


def _fixed(s: _State) -> None:
    global _FIXED
    if _FIXED is None:
        lengths = [8] * 144 + [9] * 112 + [7] * 24 + [8] * 8
        _FIXED = (_Huffman(lengths), _Huffman([5] * 32))
    _codes(s, *_FIXED)


_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]


def _dynamic(s: _State) -> None:
    nlen = s.bits(5) + 257
    ndist = s.bits(5) + 1
    ncode = s.bits(4) + 4
    if nlen > MAXLCODES or ndist > MAXDCODES:
        raise InflateError("bad dynamic block counts")
    lengths = [0] * 19
    for i in range(ncode):
        lengths[_ORDER[i]] = s.bits(3)
    lencode = _Huffman(lengths)
    lens: list[int] = []
    while len(lens) < nlen + ndist:
        sym = s.decode(lencode)
        if sym < 16:
            lens.append(sym)
            continue
        if sym == 16:
            if not lens:
                raise InflateError("repeat with no first length")
            ln, rep = lens[-1], 3 + s.bits(2)
        elif sym == 17:
            ln, rep = 0, 3 + s.bits(3)
        else:
            ln, rep = 0, 11 + s.bits(7)
        if len(lens) + rep > nlen + ndist:
            raise InflateError("too many lengths")
        lens += [ln] * rep
    if lens[256] == 0:
        raise InflateError("no end-of-block code")
    _codes(s, _Huffman(lens[:nlen]), _Huffman(lens[nlen:]))


def inflate(data: bytes, *, deflate64: bool, max_out: int = 64 * 1024 * 1024) -> bytes:
    """Raw (headerless) Deflate / Deflate64 stream -> bytes."""
    if not deflate64:
        d = zlib.decompressobj(-15)
        out = d.decompress(data, max_out + 1)
        if len(out) > max_out:
            raise InflateError("inflated output exceeds the bound")
        return out
    s = _State(data, max_out, deflate64=True)
    last = 0
    while not last:
        last = s.bits(1)
        kind = s.bits(2)
        if kind == 0:
            _stored(s)
        elif kind == 1:
            _fixed(s)
        elif kind == 2:
            _dynamic(s)
        else:
            raise InflateError("invalid block type")
    return bytes(s.out)
