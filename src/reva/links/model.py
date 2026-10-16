"""Records of the interconnection layer (V3, agent LINKS).

* :class:`Stmt` — the minimal view of a KG statement the link layer needs. It is built from KGCORE's
  ``reva.kg.model.KgStatement`` (duck-typed, see :func:`stmt_from_any`), from a ``kg.statement`` row or
  from the offline fixture. The link layer never writes statements.
* :class:`AtlasRowRef` — the minimal view of an atlas-table row (long schema of ``reva.atlas.tables``)
  for dyadic (country → country) datasets such as IMF DOTS or UN DESA migrant stock.
* :class:`Link` — a typed connection between two nodes, derived *only* from evidence. A link without
  evidence cannot be constructed (:class:`NoEvidence`): that is the layer's core invariant.

Node ids are Wikidata QIDs (``Q155``). An atlas country that the crosswalk cannot map to a QID keeps the
node id ``iso3:<CODE>``; nothing is dropped silently.

Honesty: a link says "connected via <label>". It is not a cause. Causal wording is reserved to KG
statements whose property is itself causal (P828 has cause / P1542 has effect, and the related
immediate-cause / contributing-factor properties) and is always attributed to the asserting source.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

QID_RE = re.compile(r"^Q[1-9][0-9]*$")
#: Node ids: a Wikidata QID, or a provider-scoped key as in ``reva.kg.model`` (``ucdp:conflict:333``,
#: ``iso3:BRA`` for an atlas country the crosswalk could not map to a QID).
NODE_RE = re.compile(r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$")
PID_RE = re.compile(r"^(P[1-9][0-9]{0,9}|[a-z][a-z0-9_]{1,31}:[a-z][a-z0-9_]{0,63})$")

#: Wikidata time precision: 9 = year, 10 = month, 11 = day (7 = century, 8 = decade).
YEAR_PRECISION = 9


class NoEvidence(ValueError):
    """Raised when something tries to create a link (or a hop) without evidence."""


class LinkError(ValueError):
    """Malformed link input (bad node id, bad weight, inconsistent dates)."""


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([+-]?)(\d{1,16})-(\d{2})-(\d{2})")


@dataclass(frozen=True, order=True)
class PartialDate:
    """A date as precise as its source: ``year`` always, month/day only when the precision says so.

    ``precision`` follows Wikidata (9 year, 10 month, 11 day; < 9 decade/century/millennium). Years are
    astronomical-style signed integers as written by Wikidata (``-0044`` = 44 BCE).
    """

    year: int
    month: int | None = None
    day: int | None = None
    precision: int = YEAR_PRECISION

    @classmethod
    def parse(cls, value: Any, precision: int | None = None) -> PartialDate | None:
        """Parse a Wikidata time string (``+1494-06-07T00:00:00Z``), an ISO date/datetime, a year int,
        or a mapping ``{"time": ..., "precision": ...}``. Returns ``None`` for ``None``/empty."""
        if value is None or value == "":
            return None
        if isinstance(value, PartialDate):
            return value
        if isinstance(value, Mapping):
            return cls.parse(
                value.get("time") or value.get("iso") or value.get("value"), value.get("precision", precision)
            )
        if hasattr(value, "iso") and hasattr(value, "precision"):  # reva.kg.model.KgTime
            return cls.parse(str(value.iso), int(value.precision))
        if isinstance(value, bool):
            raise LinkError(f"not a date: {value!r}")
        if isinstance(value, int):
            return cls(value, None, None, YEAR_PRECISION)
        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):  # date / datetime
            p = 11 if precision is None else int(precision)
            return cls._at(int(value.year), int(value.month), int(value.day), p)
        s = str(value).strip()
        if re.fullmatch(r"[+-]?\d{1,16}", s):
            return cls(int(s), None, None, YEAR_PRECISION if precision is None else min(int(precision), 9))
        m = _TIME_RE.match(s)
        if not m:
            raise LinkError(f"not a date: {value!r}")
        sign, y, mo, d = m.groups()
        year = -int(y) if sign == "-" else int(y)
        p = 11 if precision is None else int(precision)
        return cls._at(year, int(mo), int(d), p)

    @classmethod
    def _at(cls, year: int, month: int, day: int, precision: int) -> PartialDate:
        if precision >= 11 and month and day:
            return cls(year, month, day, 11)
        if precision >= 10 and month:
            return cls(year, month, None, 10)
        return cls(year, None, None, min(precision, YEAR_PRECISION))

    def iso(self) -> str:
        if self.precision >= 11 and self.month and self.day:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.precision >= 10 and self.month:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}"

    def key(self) -> tuple[int, int, int]:
        """Sort key; unknown month/day sort as the start of the year/month."""
        return (self.year, self.month or 1, self.day or 1)


def _later(a: PartialDate | None, b: PartialDate | None) -> PartialDate | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a.key() >= b.key() else b


def _earlier(a: PartialDate | None, b: PartialDate | None) -> PartialDate | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a.key() <= b.key() else b


@dataclass(frozen=True)
class Temporal:
    """Temporal semantics of a link or statement.

    * ``interval``: ``start``/``end`` (either may be open = ``None`` = not stated by the source),
    * ``point``: a dated event (``point``),
    * ``snapshot``: a value reported for one reference ``year`` (atlas rows),
    * ``undated``: the source states no date. Undated is *not* "always".
    """

    kind: Literal["interval", "point", "snapshot", "undated"] = "undated"
    start: PartialDate | None = None
    end: PartialDate | None = None
    point: PartialDate | None = None
    year: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "interval" and self.start is None and self.end is None:
            raise LinkError("interval needs a start or an end (use kind='undated')")
        if self.kind == "point" and self.point is None:
            raise LinkError("point temporal needs a point")
        if self.kind == "snapshot" and self.year is None:
            raise LinkError("snapshot temporal needs a year")
        if self.start is not None and self.end is not None and self.start.year > self.end.year:
            raise LinkError(f"interval ends before it starts: {self.start.iso()} > {self.end.iso()}")

    @classmethod
    def of(
        cls,
        start: PartialDate | None = None,
        end: PartialDate | None = None,
        point: PartialDate | None = None,
    ) -> Temporal:
        if start is not None or end is not None:
            return cls("interval", start, end)
        if point is not None:
            return cls("point", point=point)
        return cls("undated")

    @property
    def first_year(self) -> int | None:
        if self.kind == "interval":
            return self.start.year if self.start else None
        if self.kind == "point":
            return self.point.year if self.point else None
        if self.kind == "snapshot":
            return self.year
        return None

    @property
    def last_year(self) -> int | None:
        if self.kind == "interval":
            return self.end.year if self.end else None
        if self.kind == "point":
            return self.point.year if self.point else None
        if self.kind == "snapshot":
            return self.year
        return None

    def overlap(self, other: Temporal) -> Temporal | None:
        """Intersection of two intervals (open bounds stay open). ``None`` when they are disjoint.

        Undated on either side gives ``undated`` (the overlap is not established, but not refuted either:
        the caller decides; derivations record ``temporal_basis='undated'``)."""
        if self.kind == "undated" or other.kind == "undated":
            return Temporal("undated")
        a0, a1 = self._bounds()
        b0, b1 = other._bounds()
        start = _later(a0, b0)
        end = _earlier(a1, b1)
        if start is not None and end is not None and start.year > end.year:
            return None
        if start is not None and end is not None and start.year == end.year and start.key() > end.key():
            return None
        if start is not None and end is not None and start == end:
            return Temporal("point", point=start)
        return Temporal.of(start, end)

    def _bounds(self) -> tuple[PartialDate | None, PartialDate | None]:
        if self.kind == "interval":
            return self.start, self.end
        if self.kind == "point":
            return self.point, self.point
        if self.kind == "snapshot":
            assert self.year is not None
            return PartialDate(self.year), PartialDate(self.year)
        return None, None

    def label(self) -> str:
        """Human-readable dates: ``1953–1961``, ``1494-06-07``, ``depuis 1822``-style is left to renderers."""
        if self.kind == "interval":
            s = self.start.iso() if self.start else "?"
            e = self.end.iso() if self.end else ""
            return f"{s}–{e}" if e or self.start else f"?–{e}"
        if self.kind == "point" and self.point:
            return self.point.iso()
        if self.kind == "snapshot":
            return str(self.year)
        return ""

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.start:
            d["start"] = self.start.iso()
        if self.end:
            d["end"] = self.end.iso()
        if self.point:
            d["point"] = self.point.iso()
        if self.year is not None:
            d["year"] = self.year
        return d


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stmt:
    """A KG statement as the link layer sees it (entity-valued or literal-valued).

    ``statement_id`` is the Wikidata statement GUID (``Q155$...``) or the dataset statement key; it is
    what evidence cites. ``source_key`` names the source (``wikidata``, ``ucdp``, ``unga_votes``...).
    """

    statement_id: str
    subject: str
    pid: str
    object: str | None = None  # entity QID when the value is an item
    value: str | None = None  # literal (dates, quantities as decimal strings, external ids, text)
    value_date: PartialDate | None = None  # the main value when it is a time (P571 inception, P585...)
    start: PartialDate | None = None
    end: PartialDate | None = None
    point: PartialDate | None = None
    qualifiers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    rank: str = "normal"
    references: tuple[Mapping[str, Any], ...] = ()
    source_key: str = "wikidata"
    source_revision: str | None = None
    raw_sha256: str | None = None
    retrieved_at: str | None = None
    origin: str | None = None  # e.g. "test_fixture" for the offline fixture

    def __post_init__(self) -> None:
        if not self.statement_id:
            raise NoEvidence("a statement without an id cannot be cited")
        if not PID_RE.match(self.pid):
            raise LinkError(f"bad property id {self.pid!r}")
        if self.rank == "deprecated":
            raise LinkError("deprecated-rank statements are excluded from the KG and from links")

    @property
    def temporal(self) -> Temporal:
        return Temporal.of(self.start, self.end, self.point)

    def qualifier(self, pid: str) -> tuple[str, ...]:
        v = self.qualifiers.get(pid, ())
        return (v,) if isinstance(v, str) else tuple(v)


#: Folded indicator dimensions (``reva.atlas.tables``: extra dimensions are folded into ``indicator`` as
#: ``CODE|key=value``) that name the other country of a dyadic cell. ``origin`` means the row's ``iso3`` is
#: the *destination* (UN DESA migrant stock by destination and origin).
COUNTERPART_DIMS = ("counterpart", "partner", "origin", "destination")


_YEAR_RE = re.compile(r"^\s*([+-]?)(\d{1,6})-")


def period_year(value: Any) -> int:
    """Astronomical year of an atlas period start: ISO text with a signed year (``-0043-03-15``), a Python
    ``date``, or DuckDB's VARCHAR form of a BCE date (``0044-03-15 (BC)`` = astronomical -43)."""
    if hasattr(value, "year") and not isinstance(value, str):
        return int(value.year)
    s = str(value)
    m = _YEAR_RE.match(s)
    if not m:
        raise ValueError(f"not a period start: {s!r}")
    y = int(m.group(2))
    if s.rstrip().endswith("(BC)"):
        return 1 - y
    return -y if m.group(1) == "-" else y


def split_indicator(indicator: str) -> tuple[str, dict[str, str]]:
    """``"TXG_FOB_USD|counterpart=USA"`` → ``("TXG_FOB_USD", {"counterpart": "USA"})``."""
    code, *parts = indicator.split("|")
    dims: dict[str, str] = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            dims[k.strip()] = v.strip()
    return code, dims


@dataclass(frozen=True)
class AtlasRowRef:
    """One atlas-table row (``reva.atlas.tables.ATLAS_SCHEMA``) of a dyadic or categorical dataset.

    The long schema has one ``iso3``. The other country of a dyadic cell is read, in order, from a
    ``counterpart_iso3`` column (requested from ATLASCORE as an additive nullable column), from
    ``flags["counterpart_iso3"]``, or from the folded indicator dimension ``|counterpart=<ISO3>``
    (``partner=`` / ``destination=``: row iso3 is the subject; ``origin=``: row iso3 is the destination).
    ``value`` is a decimal string exactly as the source reports it.
    """

    source: str
    dataset: str
    indicator: str
    iso3: str
    counterpart_iso3: str | None
    period_start: str
    period_end: str | None
    value: str | None
    value_text: str | None
    unit: str | None
    raw_sha256: str
    selector: str
    concept_id: str | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)
    iso3_is_subject: bool = True
    #: origin of the atlas table the row comes from (``live`` / ``test_fixture``); ``None`` = not recorded
    origin: str | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> AtlasRowRef:
        import json

        flags = row.get("flags") or {}
        if isinstance(flags, str):
            flags = json.loads(flags) if flags.strip() else {}
        selector = row.get("selector") or ""
        if not isinstance(selector, str):
            selector = json.dumps(selector, sort_keys=True, separators=(",", ":"))
        _, dims = split_indicator(str(row["indicator"]))
        cp = row.get("counterpart_iso3") or flags.get("counterpart_iso3")
        subject_first = True
        if not cp:
            for k in COUNTERPART_DIMS:
                if dims.get(k):
                    cp = dims[k]
                    subject_first = k != "origin"
                    break
        return cls(
            source=str(row["source"]),
            dataset=str(row["dataset"]),
            indicator=str(row["indicator"]),
            iso3=str(row.get("iso3") or ""),
            counterpart_iso3=str(cp).upper() if cp else None,
            period_start=str(row["period_start"]),
            period_end=str(row["period_end"]) if row.get("period_end") is not None else None,
            value=None if row.get("value") is None else str(row["value"]),
            value_text=row.get("value_text"),
            unit=row.get("unit"),
            raw_sha256=str(row.get("raw_sha256") or ""),
            selector=str(selector),
            concept_id=row.get("concept_id"),
            flags=dict(flags),
            iso3_is_subject=subject_first,
            origin=str(row["origin"]) if row.get("origin") else None,
        )

    @property
    def year(self) -> int:
        """Astronomical year of ``period_start`` (``-0043-03-15`` → -43; a DuckDB ``0044-03-15 (BC)`` → -43)."""
        return period_year(self.period_start)

    @property
    def subject_object(self) -> tuple[str, str | None]:
        """(subject iso3, object iso3) in the link's direction (exporter→importer, origin→destination)."""
        if self.iso3_is_subject:
            return self.iso3, self.counterpart_iso3
        return self.counterpart_iso3 or "", self.iso3


# ---------------------------------------------------------------------------
# Evidence and links
# ---------------------------------------------------------------------------


def is_wikidata(source_key: str | None) -> bool:
    """``wikidata``, ``wikidata.wbapi``, ``wikidata.wdqs``..."""
    return bool(source_key) and (source_key == "wikidata" or str(source_key).startswith("wikidata."))


SOURCE_LABELS = {
    "wikidata": "Wikidata (community-edited, CC0)",
    "imf_dots": "IMF Direction of Trade Statistics",
    "un_desa_migrant_stock": "UN DESA International Migrant Stock",
    "ucdp": "UCDP (Uppsala Conflict Data Program)",
    "unga_votes": "UN General Assembly voting data",
    "naturalearth": "Natural Earth",
}


@dataclass(frozen=True)
class Evidence:
    """What a link (or a hop) rests on: a KG statement or an atlas row."""

    kind: Literal["kg_statement", "atlas_row"]
    source: str  # source key: wikidata, imf_dots, ...
    statement_id: str | None = None  # kg_statement
    subject: str | None = None
    pid: str | None = None
    object: str | None = None
    source_revision: str | None = None
    raw_sha256: str | None = None  # atlas_row (and dataset statements)
    selector: str | None = None
    retrieved_at: str | None = None
    origin: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "kg_statement" and not self.statement_id:
            raise NoEvidence("kg_statement evidence needs a statement id")
        if self.kind == "atlas_row" and not (self.raw_sha256 and self.selector):
            raise NoEvidence("atlas_row evidence needs raw_sha256 and selector")

    @classmethod
    def of_stmt(cls, s: Stmt) -> Evidence:
        return cls(
            kind="kg_statement",
            source=s.source_key,
            statement_id=s.statement_id,
            subject=s.subject,
            pid=s.pid,
            object=s.object if s.object is not None else s.value,
            source_revision=s.source_revision,
            raw_sha256=s.raw_sha256,
            retrieved_at=s.retrieved_at,
            origin=s.origin,
        )

    @classmethod
    def of_row(cls, r: AtlasRowRef) -> Evidence:
        return cls(
            kind="atlas_row",
            source=r.source,
            raw_sha256=r.raw_sha256,
            selector=r.selector,
            subject=r.iso3,
            object=r.counterpart_iso3,
            pid=r.indicator,
            origin=r.origin,
        )

    @property
    def source_label(self) -> str:
        if is_wikidata(self.source):
            return SOURCE_LABELS["wikidata"]
        return SOURCE_LABELS.get(self.source, self.source)

    def cite(self) -> str:
        """Compact citation: ``wikidata:Q155$abc (rev 123)`` or ``imf_dots:<sha12>#<selector>``."""
        if self.kind == "kg_statement":
            rev = f" rev {self.source_revision}" if self.source_revision else ""
            tag = " [fixture]" if self.origin == "test_fixture" else ""
            return f"{self.source}:{self.statement_id}{rev}{tag}"
        tag = " [fixture]" if self.origin == "test_fixture" else ""
        return f"{self.source}:{(self.raw_sha256 or '')[:12]}#{self.selector}{tag}"

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def check_node(node: str) -> str:
    if isinstance(node, str) and NODE_RE.match(node):
        return node
    raise LinkError(f"bad node id {node!r} (expected a QID or iso3:XXX)")


def check_decimal(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LinkError("weights are decimal strings exactly as the source reports them")
    try:
        d = Decimal(value)
    except InvalidOperation as e:
        raise LinkError(f"weight {value!r} is not a decimal string") from e
    if not d.is_finite():
        raise LinkError(f"weight {value!r} is not finite")
    return value


@dataclass(frozen=True)
class Link:
    """A typed, dated, evidenced connection.

    ``via`` is the shared node for derived co-links (the language of a shared official language, the
    organisation of a co-membership, the treaty of a co-signature, the colony between a successor state
    and its former coloniser). ``weight`` is set only when the source itself provides a quantity
    (trade value, migrant stock, agreement score) and is kept as the source's decimal string.
    """

    subject: str
    object: str
    link_type: str
    directed: bool
    temporal: Temporal
    evidence: tuple[Evidence, ...]
    via: tuple[str, ...] = ()
    weight: str | None = None
    unit: str | None = None
    basis: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence:
            raise NoEvidence(f"{self.link_type} {self.subject}->{self.object}: a link without evidence must not exist")
        check_node(self.subject)
        check_node(self.object)
        for v in self.via:
            check_node(v)
        if self.subject == self.object:
            raise LinkError(f"self link {self.subject} ({self.link_type})")
        check_decimal(self.weight)
        if self.weight is not None and not any(
            e.kind == "atlas_row" or (e.kind == "kg_statement" and not is_wikidata(e.source)) for e in self.evidence
        ):
            raise LinkError("a weight must come from a source quantity (atlas row or dataset statement)")

    @property
    def source_labels(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for e in self.evidence:
            seen.setdefault(e.source_label, None)
        return tuple(seen)

    @property
    def key(self) -> tuple[str, str, str, tuple[str, ...], str, str | None, str]:
        """Identity for de-duplication: same type, endpoints (unordered when undirected), via, dates, weight
        and facet (HS section, vote issue). Two links differing only by evidence are merged (evidence united)."""
        a, b = (self.subject, self.object)
        if not self.directed and b < a:
            a, b = b, a
        facet = str(self.basis.get("facet") or self.basis.get("issue") or "")
        return (self.link_type, a, b, self.via, self.temporal.label(), self.weight, facet)

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "subject": self.subject,
            "object": self.object,
            "link_type": self.link_type,
            "directed": self.directed,
            "temporal": self.temporal.to_json(),
            "via": list(self.via),
            "evidence": [e.to_json() for e in self.evidence],
            "sources": list(self.source_labels),
        }
        if self.weight is not None:
            d["weight"] = self.weight
            d["unit"] = self.unit
        if self.basis:
            d["basis"] = dict(self.basis)
        return d


def _literal(v: Any) -> str | None:
    """A typed KG literal (``{"type": "quantity", "amount": "12.5"}``...) as a plain string."""
    if v is None:
        return None
    if isinstance(v, str):
        import json

        try:
            v = json.loads(v) if v.startswith("{") else v
        except ValueError:
            return str(v)
        if isinstance(v, str):
            return v
    if isinstance(v, Mapping):
        for k in ("value", "amount", "time", "iso", "text", "id"):
            if v.get(k) is not None:
                return str(v[k])
        return None
    return str(v)


def stmt_from_any(obj: Any) -> Stmt:
    """Build a :class:`Stmt` from KGCORE's ``reva.kg.model.KgStatement`` (attribute access), a
    ``kg.statement_current`` row mapping, or a fixture dict (same field names as ``KgStatement``)."""

    def g(*names: str, default: Any = None) -> Any:
        for n in names:
            if isinstance(obj, Mapping):
                if obj.get(n) is not None:
                    return obj[n]
            elif getattr(obj, n, None) is not None:
                return getattr(obj, n)
        return default

    def date(*pairs: tuple[str, str | None]) -> PartialDate | None:
        for name, prec_name in pairs:
            v = g(name)
            if v is not None:
                return PartialDate.parse(v, g(prec_name) if prec_name else None)
        return None

    quals = g("qualifiers", default={}) or {}
    if isinstance(quals, str):
        import json

        quals = json.loads(quals)
    norm: dict[str, tuple[str, ...]] = {}
    for k, v in dict(quals).items():
        vals = v if isinstance(v, list | tuple) else [v]
        out = [x for x in (_literal(x) for x in vals) if x is not None]
        norm[str(k)] = tuple(out)
    refs = g("references", "refs", default=()) or ()
    if isinstance(refs, str):
        import json

        refs = json.loads(refs)
    obj_qid = g("object_qid", "object")
    raw_value = g("value")
    if isinstance(raw_value, str) and raw_value.startswith("{"):
        import json

        try:
            raw_value = json.loads(raw_value)
        except ValueError:
            pass
    value = _literal(raw_value)
    value_date = None
    if isinstance(raw_value, Mapping) and (raw_value.get("type") == "time" or "precision" in raw_value):
        if raw_value.get("time") or raw_value.get("iso"):
            try:
                value_date = PartialDate.parse(raw_value)
            except LinkError:
                value_date = None  # an unparseable date stays a plain literal
    if obj_qid is not None and not NODE_RE.match(str(obj_qid)):
        value = value if value is not None else str(obj_qid)
        obj_qid = None
    sid = g("statement_id", "statement_key", "id")
    if sid is None and not isinstance(obj, Mapping):
        try:  # dataset statements without a source id: KGCORE's content-hash key
            from reva.kg.model import statement_key

            sid = statement_key(obj, g("source_key", default="") or "")
        except Exception:  # pragma: no cover - KG model unavailable
            sid = None
    rev = g("source_revision", "revision_id")
    return Stmt(
        statement_id="" if sid is None else str(sid),
        subject=str(g("subject_qid", "subject")),
        pid=str(g("property_pid", "property")),
        object=None if obj_qid is None else str(obj_qid),
        value=value,
        value_date=value_date,
        start=date(("start", None), ("start_time", "start_precision")),
        end=date(("end", None), ("end_time", "end_precision")),
        point=date(("point_in_time", None), ("pit_time", "pit_precision"), ("point", None)),
        qualifiers=norm,
        rank=str(g("rank", default="normal")),
        references=tuple(refs),
        source_key=str(g("source_key", "source", default="wikidata")) or "wikidata",
        source_revision=None if rev is None else str(rev),
        raw_sha256=g("raw_sha256"),
        retrieved_at=None if g("retrieved_at") is None else str(g("retrieved_at")),
        origin=g("origin"),
    )
