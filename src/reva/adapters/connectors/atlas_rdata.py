"""Minimal, bounded, read-only reader for R ``.RData`` files (agent A-POL).

The V-Dem Institute distributes the full V-Dem dataset (Country-Year Full+Others) in its own
R package ``vdemdata`` as ``data/vdem.RData`` (gzip-compressed R serialization, format
``RDX2``/``RDX3``, XDR big-endian). The project has no R dependency, so this module reads the
subset of the R serialization format that data frames use, with the standard library and
numpy only:

* atomic vectors (logical, integer, double, character, raw), generic vectors (lists),
  pairlists (attributes), symbols, references, ``NULL`` and the special environments;
* ALTREP vectors as written by R >= 3.5 (``compact_intseq``, ``compact_realseq``,
  ``wrap_*``, ``deferred_string``); any other ALTREP class is refused;
* R's ``NA`` values are kept as missing (``None`` in strings; a mask for numbers), never as
  numbers. A double NaN that is *not* R's ``NA`` payload is reported separately (``nan``).

Data frames are read **column-selectively** in two streaming passes (R writes a vector's
attributes -- including a data frame's column names -- after its contents, so pass 1 skips
every column to learn the names, pass 2 materialises only the requested ones). The
decompressed stream is never held in memory as a whole.

Safety bounds (the bytes are untrusted): decompressed size, vector lengths, string lengths,
nesting depth and item counts are capped; closures, environments with code, bytecode and
external pointers are refused (nothing is ever evaluated -- this is a pure parser).
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import numpy as np

MAX_DECOMPRESSED = 3 * 1024**3  # 3 GiB of serialized stream (V-Dem v16: ~1.05 GB)
MAX_VECTOR = 50_000_000
MAX_STRING = 10 * 1024 * 1024
MAX_DEPTH = 64
MAX_ITEMS = 50_000_000

# SEXP types
NILSXP, SYMSXP, LISTSXP, CLOSXP, ENVSXP, PROMSXP, LANGSXP = 0, 1, 2, 3, 4, 5, 6
CHARSXP, LGLSXP, INTSXP, REALSXP, CPLXSXP, STRSXP = 9, 10, 13, 14, 15, 16
DOTSXP, VECSXP, EXPRSXP, BCODESXP, EXTPTRSXP, WEAKREFSXP, RAWSXP, S4SXP = 17, 19, 20, 21, 22, 23, 24, 25
# serialization pseudo-types
REFSXP, NILVALUE_SXP, GLOBALENV_SXP, UNBOUNDVALUE_SXP, MISSINGARG_SXP = 255, 254, 253, 252, 251
BASENAMESPACE_SXP, NAMESPACESXP, PACKAGESXP, PERSISTSXP = 250, 249, 248, 247
EMPTYENV_SXP, BASEENV_SXP, ATTRLANGSXP, ATTRLISTSXP, ALTREP_SXP = 242, 241, 240, 239, 238

NA_INTEGER = -(2**31)
#: R's NA_real_ is a NaN whose low 32-bit word is 1954 (big-endian high word 0x7FF00000|...).
_NA_REAL_LOW_WORD = 1954


class RDataError(ValueError):
    """The bytes are not an R serialization this reader supports (fails closed)."""


@dataclass
class RSymbol:
    name: str


@dataclass
class RObject:
    """A decoded R value with its attributes (``attrs``: name -> value)."""

    sexptype: int
    value: Any
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RealColumn:
    values: np.ndarray  # float64; NA / NaN positions hold NaN
    na: np.ndarray  # bool mask: R NA_real_
    nan: np.ndarray  # bool mask: a NaN that is not R's NA


@dataclass(frozen=True)
class IntColumn:
    values: np.ndarray  # int64
    na: np.ndarray
    logical: bool = False
    levels: tuple[str, ...] | None = None  # factor levels when the column is a factor


@dataclass(frozen=True)
class StrColumn:
    values: list[str | None]


Column = RealColumn | IntColumn | StrColumn


def _open_stream(raw: bytes) -> BinaryIO:
    if raw[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=io.BytesIO(raw))  # type: ignore[return-value]
    if raw[:3] == b"BZh":
        return bz2.BZ2File(io.BytesIO(raw))  # type: ignore[return-value]
    if raw[:6] == b"\xfd7zXZ\x00":
        return lzma.LZMAFile(io.BytesIO(raw))  # type: ignore[return-value]
    return io.BytesIO(raw)


class _Reader:
    def __init__(self, stream: BinaryIO, max_bytes: int = MAX_DECOMPRESSED) -> None:
        self.s = stream
        self.pos = 0
        self.max_bytes = max_bytes
        self.refs: list[Any] = []
        self.items = 0

    # ------------------------------------------------------------------ primitives
    def read(self, n: int) -> bytes:
        if n < 0:
            raise RDataError("negative read length")
        self.pos += n
        if self.pos > self.max_bytes:
            raise RDataError(f"decompressed stream exceeds {self.max_bytes} bytes")
        b = self.s.read(n)
        if len(b) != n:
            raise RDataError("truncated R serialization stream")
        return b

    def skip(self, n: int) -> None:
        while n > 0:
            k = min(n, 8 * 1024 * 1024)
            self.read(k)
            n -= k

    def int(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def length(self) -> int:
        n = self.int()
        if n == -1:
            hi, lo = self.int(), self.int()
            n = (hi << 32) + (lo & 0xFFFFFFFF)
        if n < 0 or n > MAX_VECTOR:
            raise RDataError(f"vector length {n} out of bounds")
        return n

    # ------------------------------------------------------------------ header
    def header(self) -> None:
        magic = self.read(5)
        if magic not in (b"RDX2\n", b"RDX3\n"):
            raise RDataError(f"not an RData file (magic {magic!r})")
        fmt = self.read(2)
        if fmt != b"X\n":
            raise RDataError(f"only XDR serialization is supported (format {fmt!r})")
        version = self.int()
        self.int()  # writer R version
        self.int()  # minimal reader version
        if version == 3:
            n = self.int()
            if not 0 <= n <= 64:
                raise RDataError("bad native-encoding length")
            self.read(n)
        elif version != 2:
            raise RDataError(f"unsupported serialization version {version}")

    # ------------------------------------------------------------------ items
    def charsxp(self, flags: int) -> str | None:
        n = self.int()
        if n == -1:
            return None
        if n < 0 or n > MAX_STRING:
            raise RDataError(f"string length {n} out of bounds")
        b = self.read(n)
        levels = flags >> 12
        enc = "latin-1" if levels & (1 << 2) and not levels & (1 << 3) else "utf-8"
        try:
            return b.decode(enc)
        except UnicodeDecodeError as e:
            raise RDataError(f"undecodable string ({enc})") from e

    def string_vector(self, skip: bool) -> list[str | None] | None:
        n = self.length()
        out: list[str | None] | None = None if skip else []
        for _ in range(n):
            f = self.int()
            if f & 0xFF != CHARSXP:
                raise RDataError("string vector element is not a CHARSXP")
            s = self.charsxp(f)
            if out is not None:
                out.append(s)
        return out

    def item(self, depth: int = 0, skip: bool = False) -> Any:
        if depth > MAX_DEPTH:
            raise RDataError("nesting too deep")
        self.items += 1
        if self.items > MAX_ITEMS:
            raise RDataError("too many items")
        flags = self.int()
        t = flags & 0xFF
        has_attr = bool(flags & (1 << 9))
        has_tag = bool(flags & (1 << 10))

        if t == NILVALUE_SXP:
            return None
        if t in (EMPTYENV_SXP, BASEENV_SXP, GLOBALENV_SXP, BASENAMESPACE_SXP, UNBOUNDVALUE_SXP, MISSINGARG_SXP):
            return RObject(t, None)
        if t == REFSXP:
            idx = flags >> 8
            if idx == 0:
                idx = self.int()
            if not 1 <= idx <= len(self.refs):
                raise RDataError("bad reference index")
            return self.refs[idx - 1]
        if t == SYMSXP:
            f = self.int()
            name = self.charsxp(f) or ""
            sym = RSymbol(name)
            self.refs.append(sym)
            return sym
        if t in (PACKAGESXP, NAMESPACESXP, PERSISTSXP):
            self.int()  # 0
            info = self.string_vector(skip=False)
            obj = RObject(t, info)
            self.refs.append(obj)
            return obj
        if t == ENVSXP:
            locked = self.int()
            obj = RObject(t, {"locked": locked})
            self.refs.append(obj)
            enclos = self.item(depth + 1)
            frame = self.item(depth + 1)
            hashtab = self.item(depth + 1)
            attrib = self.item(depth + 1)
            obj.value.update({"enclos": enclos, "frame": frame, "hashtab": hashtab})
            obj.attrs = _pairlist_dict(attrib)
            return obj
        if t in (LISTSXP, LANGSXP, DOTSXP, ATTRLISTSXP, ATTRLANGSXP):
            return self.pairlist(flags, depth, skip)
        if t in (CLOSXP, PROMSXP, BCODESXP, EXTPTRSXP, WEAKREFSXP, S4SXP):
            raise RDataError(f"unsupported R object type {t} (code/pointers are never read)")
        if t == ALTREP_SXP:
            return self.altrep(depth, skip)

        # vector-like types: contents first, then attributes
        if t == CHARSXP:
            value: Any = self.charsxp(flags)
        elif t in (LGLSXP, INTSXP):
            n = self.length()
            if skip:
                self.skip(4 * n)
                value = None
            else:
                value = np.frombuffer(self.read(4 * n), dtype=">i4").astype(np.int64)
        elif t == REALSXP:
            n = self.length()
            if skip:
                self.skip(8 * n)
                value = None
            else:
                value = self.read(8 * n)
        elif t == CPLXSXP:
            n = self.length()
            self.skip(16 * n)
            value = None
        elif t == RAWSXP:
            n = self.length()
            value = None if skip else self.read(n)
            if skip:
                pass
        elif t == STRSXP:
            value = self.string_vector(skip)
        elif t in (VECSXP, EXPRSXP):
            n = self.length()
            value = [self.item(depth + 1, skip) for _ in range(n)]
        else:
            raise RDataError(f"unsupported R object type {t}")
        attrs = _pairlist_dict(self.item(depth + 1)) if has_attr else {}
        del has_tag
        return RObject(t, value, attrs)

    def pairlist(self, flags: int, depth: int, skip: bool) -> RObject:
        entries: list[tuple[str | None, Any]] = []
        t0 = flags & 0xFF
        attrs: dict[str, Any] = {}
        while True:
            t = flags & 0xFF
            if t not in (LISTSXP, LANGSXP, DOTSXP, ATTRLISTSXP, ATTRLANGSXP):
                raise RDataError("malformed pairlist")
            if flags & (1 << 9):
                a = _pairlist_dict(self.item(depth + 1))
                if not entries:
                    attrs = a
            tag = None
            if flags & (1 << 10):
                sym = self.item(depth + 1)
                tag = sym.name if isinstance(sym, RSymbol) else None
            car = self.item(depth + 1, skip)
            entries.append((tag, car))
            flags = self.int()
            if flags & 0xFF == NILVALUE_SXP:
                break
            if len(entries) > MAX_VECTOR:
                raise RDataError("pairlist too long")
        return RObject(t0 if t0 != ATTRLISTSXP else LISTSXP, entries, attrs)

    def altrep(self, depth: int, skip: bool) -> RObject:
        info = self.item(depth + 1)
        state = self.item(depth + 1)
        attr = self.item(depth + 1)
        cls = ""
        if isinstance(info, RObject) and isinstance(info.value, list) and info.value:
            first = info.value[0][1]
            cls = first.name if isinstance(first, RSymbol) else ""
        attrs = _pairlist_dict(attr)
        if cls == "compact_intseq":
            n, start, step = np.frombuffer(state.value, dtype=">f8")
            vals = (int(start) + int(step) * np.arange(int(n), dtype=np.int64)) if not skip else None
            return RObject(INTSXP, vals, attrs)
        if cls == "compact_realseq":
            n, start, step = np.frombuffer(state.value, dtype=">f8")
            vals = None if skip else (start + step * np.arange(int(n), dtype=np.float64)).astype(">f8").tobytes()
            return RObject(REALSXP, vals, attrs)
        if cls.startswith("wrap_"):
            inner = state.value[0] if isinstance(state, RObject) and isinstance(state.value, list) else None
            if not isinstance(inner, RObject):
                raise RDataError("malformed wrap_* ALTREP state")
            return RObject(inner.sexptype, inner.value, {**inner.attrs, **attrs})
        if cls == "deferred_string":
            # state: pairlist (arg . info); arg is the integer/double vector to format
            arg = state.value[0][1] if isinstance(state, RObject) and isinstance(state.value, list) else None
            if not isinstance(arg, RObject):
                raise RDataError("malformed deferred_string ALTREP state")
            if arg.sexptype == INTSXP:
                vals = [None if v == NA_INTEGER else str(int(v)) for v in arg.value]
            elif arg.sexptype == REALSXP:
                col = _real_column(arg.value)
                vals = [None if m else _r_double_text(v) for v, m in zip(col.values, col.na | col.nan, strict=True)]
            else:
                raise RDataError("unsupported deferred_string argument")
            return RObject(STRSXP, vals, attrs)
        raise RDataError(f"unsupported ALTREP class {cls!r}")


def _pairlist_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if not isinstance(obj, RObject) or not isinstance(obj.value, list):
        raise RDataError("attributes are not a pairlist")
    return {tag: v for tag, v in obj.value if tag is not None}


def _real_column(buf: bytes) -> RealColumn:
    words = np.frombuffer(buf, dtype=">u4").reshape(-1, 2)
    vals = np.frombuffer(buf, dtype=">f8").astype(np.float64)
    isnan = np.isnan(vals)
    na = isnan & (words[:, 1] == _NA_REAL_LOW_WORD)
    return RealColumn(vals, na, isnan & ~na)


def _r_double_text(v: float) -> str:
    s = repr(float(v))
    if s.endswith(".0"):
        s = s[:-2]
    return "0" if s in ("-0", "-0.0") else s


def to_column(obj: Any) -> Column:
    """Convert a decoded atomic vector into a typed column (NA kept as a mask/None)."""
    if not isinstance(obj, RObject):
        raise RDataError("column is not an R vector")
    if obj.sexptype == REALSXP:
        return _real_column(obj.value)
    if obj.sexptype in (INTSXP, LGLSXP):
        vals = obj.value
        levels = obj.attrs.get("levels")
        lv = tuple(levels.value) if isinstance(levels, RObject) and obj.sexptype == INTSXP else None
        return IntColumn(vals, vals == NA_INTEGER, logical=obj.sexptype == LGLSXP, levels=lv)  # type: ignore[arg-type]
    if obj.sexptype == STRSXP:
        return StrColumn(list(obj.value))
    raise RDataError(f"column of R type {obj.sexptype} is not supported")


def _names(attrs: dict[str, Any]) -> list[str]:
    nm = attrs.get("names")
    if not isinstance(nm, RObject) or nm.sexptype != STRSXP:
        raise RDataError("object has no names attribute")
    return [n or "" for n in nm.value]


def _read_frame(raw: bytes, object_name: str, wanted: Callable[[int], bool], max_bytes: int) -> RObject:
    r = _Reader(_open_stream(raw), max_bytes)
    r.header()
    flags = r.int()
    if flags & 0xFF != LISTSXP:
        raise RDataError("RData top level is not a pairlist of objects")
    while True:
        if not flags & (1 << 10):
            raise RDataError("RData object without a name")
        if flags & (1 << 9):
            r.item(1)
        sym = r.item(1)
        name = sym.name if isinstance(sym, RSymbol) else None
        if name == object_name:
            f = r.int()
            if f & 0xFF != VECSXP:
                raise RDataError(f"{object_name} is not a list/data frame")
            n = r.length()
            cols = [r.item(2, skip=not wanted(i)) for i in range(n)]
            attrs = _pairlist_dict(r.item(2)) if f & (1 << 9) else {}
            return RObject(VECSXP, cols, attrs)
        r.item(1, skip=True)
        flags = r.int()
        if flags & 0xFF == NILVALUE_SXP:
            raise RDataError(f"object {object_name!r} not found in RData file")


def rdata_object_names(raw: bytes, max_bytes: int = MAX_DECOMPRESSED) -> list[str]:
    """Names of the objects saved in an RData file (skips their contents)."""
    r = _Reader(_open_stream(raw), max_bytes)
    r.header()
    out: list[str] = []
    flags = r.int()
    while flags & 0xFF == LISTSXP:
        if flags & (1 << 9):
            r.item(1)
        sym = r.item(1)
        out.append(sym.name if isinstance(sym, RSymbol) else "")
        r.item(1, skip=True)
        flags = r.int()
    return out


@dataclass(frozen=True)
class RFrame:
    """Selected columns of an R data frame (all column names kept for reference)."""

    all_columns: tuple[str, ...]
    columns: dict[str, Column]
    nrows: int
    classes: tuple[str, ...]


def read_rdata_frame(
    raw: bytes,
    object_name: str,
    columns: Iterable[str] | Callable[[str], bool] | None = None,
    *,
    max_bytes: int = MAX_DECOMPRESSED,
) -> RFrame:
    """Read selected columns of data frame ``object_name`` from RData bytes.

    ``columns``: names to read (unknown names raise), a predicate on the name, or ``None`` for
    all. Two streaming passes; nothing beyond the requested columns is materialised.
    """
    meta = _read_frame(raw, object_name, lambda i: False, max_bytes)
    names = _names(meta.attrs)
    if columns is None:
        pick = set(range(len(names)))
    elif callable(columns):
        pick = {i for i, n in enumerate(names) if columns(n)}
    else:
        want = list(columns)
        unknown = sorted(set(want) - set(names))
        if unknown:
            raise RDataError(f"columns not in {object_name}: {unknown[:10]}")
        pick = {i for i, n in enumerate(names) if n in set(want)}
    cls_obj = meta.attrs.get("class")
    classes = tuple(cls_obj.value) if isinstance(cls_obj, RObject) and cls_obj.sexptype == STRSXP else ()
    full = _read_frame(raw, object_name, lambda i: i in pick, max_bytes) if pick else meta
    out: dict[str, Column] = {}
    nrows = -1
    for i in sorted(pick):
        col = to_column(full.value[i])
        n = len(col.values)
        if nrows == -1:
            nrows = n
        elif n != nrows:
            raise RDataError(f"column {names[i]} has {n} rows, expected {nrows}")
        out[names[i]] = col
    if nrows == -1:
        nrows = _frame_nrows(meta.attrs)
    return RFrame(tuple(names), out, nrows, classes)


def _frame_nrows(attrs: dict[str, Any]) -> int:
    rn = attrs.get("row.names")
    if isinstance(rn, RObject) and rn.sexptype == INTSXP:
        v = rn.value
        if len(v) == 2 and v[0] == NA_INTEGER:
            return abs(int(v[1]))
        return len(v)
    if isinstance(rn, RObject) and rn.sexptype == STRSXP:
        return len(rn.value)
    return 0


def column_text(col: Column, i: int) -> str | None:
    """Cell ``i`` as source text: exact shortest repr for doubles, ``None`` for NA/NaN."""
    if isinstance(col, StrColumn):
        return col.values[i]
    if isinstance(col, RealColumn):
        if col.na[i] or col.nan[i]:
            return None
        return _r_double_text(col.values[i])
    if col.na[i]:
        return None
    v = int(col.values[i])
    if col.levels is not None:
        return col.levels[v - 1] if 1 <= v <= len(col.levels) else None
    if col.logical:
        return "TRUE" if v else "FALSE"
    return str(v)


__all__ = [
    "Column",
    "IntColumn",
    "RDataError",
    "RFrame",
    "RealColumn",
    "StrColumn",
    "column_text",
    "rdata_object_names",
    "read_rdata_frame",
]
