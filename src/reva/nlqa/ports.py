"""Adapter ports of the question-answering executor (V3, agent NLQA).

The executor never talks to a database, DuckDB or the network directly: it calls these
protocols. Real adapters (:mod:`reva.nlqa.adapters`) wrap the KG (``reva.kg`` /
``kg.*`` tables), the atlas (``reva.atlas.query``), the interconnection layer
(``reva.links``) and the optional Wikidata search; tests use in-memory fakes. A backend
that is absent or fails raises :class:`CapabilityUnavailable`, which the executor turns into
a typed ``capability_unavailable`` result — never into a guess.

Every record carries its provenance: a KG statement keeps its Wikidata statement id,
revision, retrieval time and raw artifact sha256; an atlas cell keeps source, dataset,
indicator, raw sha256 and selector. The composer cites these, nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from reva.kernel.errors import RevaError

#: Wikidata properties whose statements assert causation (shown only as "asserted by <source>").
CAUSAL_PROPERTIES = frozenset({"P828", "P1542", "P1478", "P1536", "P1479", "P1537"})


class CapabilityUnavailable(RevaError):
    """A backend (KG, atlas, links, entity search) is absent or failed; nothing is guessed."""

    code = "capability_unavailable"
    http_status = 503


def pick_label(labels: Mapping[str, str] | None, lang: str, fallback: str = "") -> str:
    """The label in ``lang``, else English, else any label, else ``fallback``."""
    labels = labels or {}
    for k in (lang, "en", "fr"):
        v = labels.get(k)
        if v:
            return str(v)
    for v in labels.values():
        if v:
            return str(v)
    return fallback


@dataclass(frozen=True)
class TimeValue:
    """A Wikidata-style time: ISO date ``YYYY[-MM[-DD]]`` (``-`` prefix for BCE) and precision
    (7 century, 8 decade, 9 year, 10 month, 11 day)."""

    iso: str
    precision: int = 11

    @property
    def year(self) -> int | None:
        s = self.iso.lstrip("+")
        neg = s.startswith("-")
        digits = s.lstrip("-").split("-")[0]
        try:
            y = int(digits)
        except ValueError:
            return None
        return -y if neg else y

    def display(self) -> str:
        s = self.iso.lstrip("+")
        neg = s.startswith("-")
        parts = s.lstrip("-").split("T")[0].split("-")
        y = parts[0].lstrip("0") or "0"
        prefix = "-" if neg else ""
        if self.precision >= 11 and len(parts) >= 3:
            return f"{prefix}{y}-{parts[1]}-{parts[2]}"
        if self.precision == 10 and len(parts) >= 2:
            return f"{prefix}{y}-{parts[1]}"
        return f"{prefix}{y}"

    def sort_key(self) -> tuple[int, str]:
        y = self.year
        return (y if y is not None else 10**9, self.iso)


@dataclass(frozen=True)
class Entity:
    qid: str
    labels: Mapping[str, str] = field(default_factory=dict)
    descriptions: Mapping[str, str] = field(default_factory=dict)
    kind: str | None = None
    sitelinks: int = 0
    iso3: str | None = None
    #: where the identification came from: ``kg`` (REVA KG) or ``wikidata.wbsearchentities`` (live search)
    source: str = "kg"
    source_revision: str | None = None
    retrieved_at: str | None = None

    def label(self, lang: str) -> str:
        return pick_label(self.labels, lang, self.qid)


@dataclass(frozen=True)
class EntityMatch:
    entity: Entity
    #: label_exact | alias_exact | label_prefix | fuzzy | search
    match: str
    matched_text: str = ""


@dataclass(frozen=True)
class KgStatement:
    """One sourced KG statement (subject —property→ object|literal), as stored in ``kg.statement``."""

    statement_id: str
    subject_qid: str
    property: str
    subject_labels: Mapping[str, str] = field(default_factory=dict)
    property_labels: Mapping[str, str] = field(default_factory=dict)
    object_qid: str | None = None
    object_labels: Mapping[str, str] = field(default_factory=dict)
    literal: str | None = None
    literal_unit: str | None = None
    start: TimeValue | None = None
    end: TimeValue | None = None
    point_in_time: TimeValue | None = None
    rank: str = "normal"
    qualifiers: Mapping[str, Any] = field(default_factory=dict)
    references: Sequence[Mapping[str, Any]] = ()
    source_key: str = "wikidata"
    source_revision: str | None = None
    retrieved_at: str | None = None
    raw_sha256: str | None = None
    origin: str | None = None

    def subject_label(self, lang: str) -> str:
        return pick_label(self.subject_labels, lang, self.subject_qid)

    def object_label(self, lang: str) -> str:
        if self.object_qid is not None:
            return pick_label(self.object_labels, lang, self.object_qid)
        if self.literal is None:
            return ""
        return f"{self.literal} {self.literal_unit}".strip() if self.literal_unit else self.literal

    def property_label(self, lang: str) -> str:
        return pick_label(self.property_labels, lang, self.property)

    @property
    def causal(self) -> bool:
        return self.property in CAUSAL_PROPERTIES

    def when(self) -> TimeValue | None:
        return self.point_in_time or self.start


@dataclass(frozen=True)
class DerivedLinkHop:
    """A hop over a *derived* link of the interconnection layer (e.g. ``former_colony_of``), which rests on
    several sourced records; it is cited through all of them."""

    link_type: str
    subject_qid: str
    object_qid: str
    subject_labels: Mapping[str, str] = field(default_factory=dict)
    object_labels: Mapping[str, str] = field(default_factory=dict)
    statements: tuple[KgStatement, ...] = ()
    cells: tuple[AtlasCell, ...] = ()
    period: str | None = None
    derived_by: str = "reva.links"


@dataclass(frozen=True)
class PathHop:
    """One hop of a path: a sourced statement (``forward`` False when it is followed against its direction)
    or a derived link."""

    statement: KgStatement | None = None
    forward: bool = True
    link: DerivedLinkHop | None = None

    @property
    def causal(self) -> bool:
        return self.statement is not None and self.statement.causal

    @property
    def from_qid(self) -> str:
        if self.statement is None:
            assert self.link is not None
            return self.link.subject_qid if self.forward else self.link.object_qid
        return self.statement.subject_qid if self.forward else str(self.statement.object_qid)

    @property
    def to_qid(self) -> str:
        if self.statement is None:
            assert self.link is not None
            return self.link.object_qid if self.forward else self.link.subject_qid
        return str(self.statement.object_qid) if self.forward else self.statement.subject_qid


@dataclass(frozen=True)
class KgPath:
    hops: tuple[PathHop, ...]
    #: who computed it: ``links.paths`` (LINKS path engine) or ``nlqa.bfs`` (bounded BFS here)
    engine: str = "links.paths"


@dataclass(frozen=True)
class Concept:
    """An atlas catalog concept (``registry/atlas/catalog/<domain>.yaml``), provider_declared."""

    id: str
    labels: Mapping[str, str] = field(default_factory=dict)
    unit: str | None = None
    domain: str | None = None
    definition: str | None = None
    source_order: tuple[str, ...] = ()
    review_status: str = "provider_declared"
    #: explicit catalog declaration (``rankable: false``); ``None`` = derived from the unit
    rankable: bool | None = None

    def label(self, lang: str) -> str:
        return pick_label(self.labels, lang, self.id)

    @property
    def comparable_across_countries(self) -> bool:
        """False when the catalog says ``rankable: false`` or the concept's unit is a currency (or unit)
        named per country/cell ("local currency", "LCU", "national currency", "named per cell"): such
        values are never ranked or listed against each other (no currency conversion in REVA)."""
        if self.rankable is not None:
            return self.rankable
        return not per_country_unit(self.unit)


#: Units whose meaning changes from one country (or cell) to the next: a different currency per country.
_PER_COUNTRY_UNIT = re.compile(
    r"\b(lcu|ncu|local currenc(y|ies)|national currenc(y|ies)|monnaie (locale|nationale)|named per cell|"
    r"per cell|unit per country)\b",
    re.IGNORECASE,
)


def per_country_unit(unit: str | None) -> bool:
    """True for a unit text that names a currency per country/cell rather than one common unit."""
    return bool(unit) and bool(_PER_COUNTRY_UNIT.search(str(unit)))


_PROJECTION_KINDS = frozenset({"projection", "forecast", "projected"})
_ESTIMATE_KINDS = frozenset({"estimated", "estimate", "imputed", "modelled", "modeled", "nowcast"})
_ESTIMATE_TYPES = frozenset({"ilo_modelled", "ilo_estimate_in_national_series"})


def _truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    return str(v).strip().lower() not in ("", "false", "0", "no", "none")


def is_projection(flags: Mapping[str, Any] | None) -> bool:
    """A cell is a projection when its provider says so — the semantics of the atlas query layer's
    ``PROJECTION_SQL`` (``estimate_kind = projection``, a truthy ``projection`` flag, the WDI's
    ``period_after_vintage``) plus FAO's ``estimate_kind = forecast``."""
    f = flags or {}
    return (
        str(f.get("estimate_kind") or "").strip().lower() in _PROJECTION_KINDS
        or _truthy(f.get("projection"))
        or str(f.get("period_after_vintage") or "").strip().lower() == "true"
    )


def estimate_status(flags: Mapping[str, Any] | None) -> str | None:
    """``projection``, ``estimate`` (provider-estimated, imputed or modelled) or ``None`` (as reported)."""
    f = flags or {}
    if is_projection(f):
        return "projection"
    if str(f.get("estimate_kind") or "").strip().lower() in _ESTIMATE_KINDS:
        return "estimate"
    if str(f.get("estimate_type") or "").strip().lower() in _ESTIMATE_TYPES:
        return "estimate"
    return None


_YEAR_RE = re.compile(r"^\s*([+-]?\d+)-")


@dataclass(frozen=True)
class AtlasCell:
    """One atlas cell exactly as a source reports it (long schema of an atlas table)."""

    source: str
    dataset: str
    indicator: str
    iso3: str
    period_start: str
    period_end: str
    value: str | None = None  # decimal string
    value_text: str | None = None
    unit: str | None = None
    concept_id: str | None = None
    entity_qid: str | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)
    raw_sha256: str | None = None
    selector: str | None = None
    country_label: str | None = None
    #: further provenance from the backend (table artifact sha256, dataset key, snapshot)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def year(self) -> int | None:
        """Year of ``period_start`` (astronomical ISO text: ``-0043-01-01`` is year -43 = 44 BCE)."""
        m = _YEAR_RE.match(str(self.period_start))
        return int(m.group(1)) if m else None

    @property
    def end_year(self) -> int | None:
        m = _YEAR_RE.match(str(self.period_end))
        return int(m.group(1)) if m else None


@dataclass(frozen=True)
class AtlasResult:
    cells: tuple[AtlasCell, ...]
    #: every source the backend looked in for this request (named in "missing" sentences)
    sources_consulted: tuple[str, ...] = ()


@dataclass(frozen=True)
class CountryLink:
    """One interconnection edge between two countries (LINKS layer); its evidence is cited. ``kind`` is the
    user-level group (border, shared_language, colonial…), ``link_type`` the LINKS type id."""

    kind: str
    a_iso3: str
    b_iso3: str
    a_label: str | None = None
    b_label: str | None = None
    period: str | None = None
    weight: str | None = None
    unit: str | None = None
    statements: tuple[KgStatement, ...] = ()
    cells: tuple[AtlasCell, ...] = ()
    source: str | None = None
    derived_by: str = "links"
    link_type: str | None = None
    #: how the link was computed (vote agreement: ``n_common``, ``n_identical``, formula); empty for source facts
    basis: Mapping[str, Any] = field(default_factory=dict)
    facet: str | None = None  # ``issue=co`` (vote agreement), ``section=XVI`` (BACI)
    a_node: str | None = None  # node ids when an end is not a crosswalk country (a predecessor state)
    b_node: str | None = None
    via: tuple[str, ...] = ()  # shared node ids (the language, the organisation) of a derived co-link


@dataclass(frozen=True)
class PairLinkGroup:
    """Every link of one LINKS type between two countries (``links`` may be capped; ``count`` is the total)."""

    link_type: str
    kind: str  # user-level group (trade, migration, diplomatic…)
    count: int
    first_year: int | None
    last_year: int | None
    links: tuple[CountryLink, ...]
    labels: Mapping[str, str] = field(default_factory=dict)  # type labels {fr, en}
    directed: bool = False


@dataclass(frozen=True)
class PredecessorLink:
    """A link of ``country`` with a predecessor state of ``predecessor_of``, shown with its succession chain."""

    country: str
    predecessor_of: str
    link: CountryLink
    succession: tuple[CountryLink, ...]


@dataclass(frozen=True)
class PairLinks:
    """Every link between two countries over time (``reva.links.service.pair_view``)."""

    a_iso3: str
    b_iso3: str
    groups: tuple[PairLinkGroup, ...]
    #: link types with no link between the two (in the data consulted): "no link", never zero
    kinds_without_links: tuple[str, ...] = ()
    predecessor_links: tuple[PredecessorLink, ...] = ()
    truncated: tuple[str, ...] = ()
    node_labels: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    store: str | None = None  # "materialisation <id>" or "on demand"
    #: {link type: {fr, en}} labels (taxonomy) of every type named, incl. those without links
    type_labels: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


#: Tiers of the *claim* space (v3_causality.md): T1 expert-coded dataset, T2 cited scholarly reference, T3/T4
#: Wikidata statement with/without references. T5/T6 are candidates: never used by NLQA.
CLAIM_TIERS = frozenset({"T1", "T2", "T3", "T4"})
GRADES = ("A", "B", "C", "D", "E")


@dataclass(frozen=True)
class CausalClaim:
    """One causal claim made by a named source (``kg.causal_claim`` + its current grade): REVA asserts nothing.

    ``cause_id``/``effect_id`` are normalised (cause → effect) whatever side the source wrote it on; ``grade`` is
    ``None`` until the grader has run; ``stance`` is ``denies`` for a source denying the link."""

    claim_key: str
    cause_id: str
    effect_id: str
    relation: str
    source_key: str
    tier: str
    cause_labels: Mapping[str, str] = field(default_factory=dict)
    effect_labels: Mapping[str, str] = field(default_factory=dict)
    lane: str | None = None
    stance: str = "asserts"
    polarity: str | None = None
    dataset: str | None = None
    source_revision: str | None = None
    statement_id: str | None = None
    selector: Mapping[str, Any] | None = None
    raw_sha256: str | None = None
    citation: Mapping[str, Any] | None = None
    codebook: Mapping[str, Any] | None = None
    references: Sequence[Mapping[str, Any]] = ()
    mechanism: str | None = None
    grade: str | None = None
    grade_rules: tuple[str, ...] = ()
    rules_version: str | None = None
    temporal_check: str | None = None
    corroboration: int | None = None
    contested_by: tuple[str, ...] = ()
    review_status: str | None = None
    origin: str | None = None
    #: who Wikidata records as making the claim (P3680) — labels; the answer names them (audit H3)
    supported_by: tuple[str, ...] = ()
    #: nature-of-statement / doubtful-certainty note from CAUSCORE's ``attribution`` (``{"en": ..., "fr": ...}``)
    attribution_note: Mapping[str, str] = field(default_factory=dict)

    def cause_label(self, lang: str) -> str:
        return pick_label(self.cause_labels, lang, self.cause_id)

    def effect_label(self, lang: str) -> str:
        return pick_label(self.effect_labels, lang, self.effect_id)

    @property
    def community_edited(self) -> bool:
        return self.source_key.startswith("wikidata")


@dataclass(frozen=True)
class CausalExplanation:
    """Causes (``direction="causes"``) or effects (``"effects"``) of an event, as sourced claims, plus dissent."""

    event_id: str
    direction: str
    claims: tuple[CausalClaim, ...]
    dissent: tuple[CausalClaim, ...] = ()
    event_labels: Mapping[str, str] = field(default_factory=dict)
    truncated: bool = False


@dataclass(frozen=True)
class CausalChainHop:
    """One hop of a causal chain, asserted by one or more claims (each with its own source and grade)."""

    from_id: str
    to_id: str
    claims: tuple[CausalClaim, ...]
    from_labels: Mapping[str, str] = field(default_factory=dict)
    to_labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CausalChain:
    """REVA's *composition* of separately asserted claims (no source asserts the chain); grade = weakest hop."""

    hops: tuple[CausalChainHop, ...]
    engine: str = "reva.causal"


def best_grade(grades: Sequence[str | None]) -> str | None:
    gs = [g for g in grades if g in GRADES]
    return min(gs, key=GRADES.index) if gs else None


def hop_grade(h: CausalChainHop) -> str | None:
    """Best grade among the claims asserting the hop (CAUSCORE's rule); ``None`` if none is graded."""
    return best_grade([c.grade for c in h.claims if c.stance == "asserts"])


def chain_grade(ch: CausalChain) -> str | None:
    """Weakest hop; ``None`` (not gradable) when a hop has no graded claim."""
    gs = [hop_grade(h) for h in ch.hops]
    graded = [g for g in gs if g is not None]
    if not gs or len(graded) != len(gs):
        return None
    return max(graded, key=GRADES.index)


@runtime_checkable
class CausalPort(Protocol):
    """Causal claims service (CAUSCORE, ``reva.causal``). Candidates (T5/T6) are never returned through it."""

    name: str

    def explain(self, event_id: str, *, direction: str, limit: int) -> CausalExplanation: ...

    def chains(self, from_id: str, to_id: str, *, max_hops: int, limit: int) -> list[CausalChain]: ...


@runtime_checkable
class KgPort(Protocol):
    name: str

    def search_entities(self, text: str, *, kind: str | None, lang: str, limit: int) -> list[EntityMatch]: ...

    def entity(self, qid: str) -> Entity | None: ...

    def neighbours(
        self, qid: str, *, properties: Sequence[str] | None, as_of: str | None, limit: int
    ) -> list[KgStatement]: ...

    def timeline(self, qid: str, *, from_year: int | None, to_year: int | None, limit: int) -> list[KgStatement]: ...


@runtime_checkable
class PathPort(Protocol):
    name: str

    def paths(self, from_qid: str, to_qid: str, *, max_hops: int, as_of: str | None, limit: int) -> list[KgPath]: ...


@runtime_checkable
class AtlasPort(Protocol):
    name: str

    def concepts(self) -> list[Concept]: ...

    def values(self, concept_id: str, iso3s: Sequence[str] | None, year: int | None) -> AtlasResult:
        """Cells for ``concept_id``; ``iso3s=None`` means every country; ``year=None`` the latest per
        country and source."""
        ...

    def series(self, concept_id: str, iso3: str, from_year: int | None, to_year: int | None) -> AtlasResult: ...


@runtime_checkable
class LinksPort(Protocol):
    name: str

    def links(self, iso3: str, kinds: Sequence[str] | None, limit: int) -> list[CountryLink]: ...


@runtime_checkable
class PairLinksPort(Protocol):
    """Optional on a links backend: every link between two countries (``links_pair``)."""

    name: str

    def pair(
        self, a: str, b: str, kinds: Sequence[str] | None, from_year: int | None, to_year: int | None
    ) -> PairLinks: ...


@runtime_checkable
class EntitySearchPort(Protocol):
    """Fallback identification outside the REVA KG (e.g. Wikidata ``wbsearchentities``), labelled."""

    name: str

    def search(self, text: str, *, lang: str, limit: int) -> list[EntityMatch]: ...


@dataclass
class Backends:
    """The adapters an executor runs against; ``None`` = capability unavailable on this server."""

    kg: KgPort | None = None
    paths: PathPort | None = None
    atlas: AtlasPort | None = None
    links: LinksPort | None = None
    search: EntitySearchPort | None = None
    causal: CausalPort | None = None
    notes: list[str] = field(default_factory=list)
