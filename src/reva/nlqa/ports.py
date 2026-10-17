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

from reva.atlas.projection import is_projection as _is_projection
from reva.atlas.projection import kind_class
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
    #: label_exact | alias_exact | label_prefix | fuzzy | search | label_words (every content word in its labels)
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
    #: catalog sub-domain (e.g. ``causes_of_death``), when the catalog declares one
    subdomain: str | None = None

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


_ESTIMATE_TYPES = frozenset({"ilo_modelled", "ilo_estimate_in_national_series"})


def is_projection(flags: Mapping[str, Any] | None, *, period_start: Any = None, table: str | None = None) -> bool:
    """A cell that is not an observation — the shared rule of ``reva.atlas.projection`` (FIX5;
    ``registry/atlas/projection_kinds.yaml``: projection / forecast / unknown-boundary kinds, unknown kinds,
    projection flags, periods starting after a release date in the flags or after today)."""
    return _is_projection(flags, period_start=period_start, table=table)


def cell_is_projection(c: AtlasCell) -> bool:
    return _is_projection(c.flags, period_start=c.period_start, table=f"{c.source}.{c.dataset}")


def estimate_status(flags: Mapping[str, Any] | None, *, period_start: Any = None) -> str | None:
    """``projection``, ``estimate`` (provider-estimated, imputed or modelled) or ``None`` (as reported)."""
    f = flags or {}
    if is_projection(f, period_start=period_start):
        return "projection"
    if kind_class(f.get("estimate_kind")) == "estimate":
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
    #: the parties of ``supported_by`` with their ids (``{"id", "label"}``), for the evidence item (APIFIX)
    supported_by_refs: tuple[Mapping[str, str], ...] = ()
    #: P5102 nature-of-statement values (``{"id", "label"}``), verbatim
    nature_of_statement: tuple[Mapping[str, str], ...] = ()
    #: why the temporal check is what it is (CAUSCORE ``grade.temporal``): ``reason``, ``entity_dated_by_inception``
    #: (sides dated by P571: entities, not events), ``undated`` (sides with no start date in the KG)
    temporal_reason: str | None = None
    temporal_entity_sides: tuple[str, ...] = ()
    temporal_undated_sides: tuple[str, ...] = ()
    #: P1480 sourcing-circumstances values (``{"id", "label"[, "label_fr"]}``): Wikidata hedges the claim (audit2 N2)
    sourcing_circumstances: tuple[Mapping[str, str], ...] = ()
    #: P518 / P1001 / P1011 / P1012 / P1013 scope restrictions (``{"property", "id", "label"}``), worded in the note
    scope_qualifiers: tuple[Mapping[str, str], ...] = ()
    #: the asserting source's licence (CAUSCORE ``source.rights``, normalised like atlas cells; audit2 N9)
    rights: Mapping[str, Any] | None = None
    #: sides that are classes (``generic`` lane of ``why``: a statement about a kind of event, not a cause; M1)
    generic_ends: tuple[str, ...] = ()

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


# --------------------------------------------------------------------------- V6 domains (agent NLQA6)
@dataclass(frozen=True)
class FamilyNeighbour:
    """One neighbour of an entity in a relation family (``reva.kg.family_query``): the sourced statement, the
    reading of the relation from the entity (fr/en), the share the source states (``None`` = Missing) and the
    licence of the raw artifact the statement was read from."""

    family: str
    reading: Mapping[str, str]
    inverse: bool
    neighbour: Entity
    statement: KgStatement
    share: Mapping[str, Any] | None = None
    rights: Mapping[str, Any] | None = None
    subtype: str | None = None
    identity: bool = False


@dataclass(frozen=True)
class FamilyGroup:
    family: str
    label: Mapping[str, str]
    count: int
    neighbours: tuple[FamilyNeighbour, ...]
    count_is_lower_bound: bool = False


@dataclass(frozen=True)
class FamilyView:
    """Neighbours of one entity grouped by relation family; ``literals`` are the entity's literal statements
    (names as published, quantities) with the licence of their raw artifact."""

    qid: str
    labels: Mapping[str, str]
    groups: tuple[FamilyGroup, ...] = ()
    literals: tuple[tuple[KgStatement, Mapping[str, Any] | None], ...] = ()
    identities: tuple[FamilyNeighbour, ...] = ()
    kind: str | None = None


@dataclass(frozen=True)
class CrossRow:
    """An entity linked to A (edge ``a``) and to B (edge ``b``): two sourced statements."""

    entity: Entity
    a: FamilyNeighbour
    b: FamilyNeighbour


@dataclass(frozen=True)
class CrossResult:
    rows: tuple[CrossRow, ...]
    total: int
    total_is_lower_bound: bool = False


@runtime_checkable
class FamiliesPort(Protocol):
    """Relation families of the KG (V4CORE ``/v1/kg/entities/{id}/families``)."""

    name: str

    def families(self, qid: str, *, families: Sequence[str], per_family: int) -> FamilyView: ...

    def cross(
        self, a_qid: str, a_families: Sequence[str] | None, b_qid: str, b_families: Sequence[str], *, limit: int
    ) -> CrossResult: ...


@dataclass(frozen=True)
class EffectTerm:
    """A cause/effect id of the effect-estimate layer with its registry labels (variable, atlas concept, event
    type): what a question's words are matched against (registry labels, never entity names)."""

    kind: str
    id: str
    labels: Mapping[str, str]


@runtime_checkable
class EffectsPort(Protocol):
    """Attributed effect estimates (PRED-EFFECT ``/v1/effects``); each estimate is a dict as ``estimate_json``."""

    name: str

    def terms(self) -> list[EffectTerm]: ...

    def estimates(
        self, cause_ids: Sequence[str], effect_ids: Sequence[str], *, country: str | None, limit: int
    ) -> list[Mapping[str, Any]]: ...


@runtime_checkable
class ForecastPort(Protocol):
    """The prospective forecast registry (PRED-REGISTRY ``/v1/forecasts``): sealed questions and forecasts."""

    name: str

    def families(self) -> list[Mapping[str, Any]]: ...

    def questions(self, family: str, geo: str | None, *, limit: int) -> list[Mapping[str, Any]]: ...

    def question(self, question_id: str) -> Mapping[str, Any] | None: ...

    def models(self) -> Mapping[str, tuple[str, str]]: ...


@runtime_checkable
class DecisionPort(Protocol):
    """The decision engine (PRED-ENGINE ``/v1/predict/decision``): unvalidated scenarios, never probabilities."""

    name: str

    def references(self) -> list[Mapping[str, Any]]: ...

    def instruments(self) -> list[Mapping[str, Any]]: ...

    def run(self, decision: Mapping[str, Any], *, draws: int, seed: int) -> Mapping[str, Any]: ...


@dataclass
class Backends:
    """The adapters an executor runs against; ``None`` = capability unavailable on this server."""

    kg: KgPort | None = None
    paths: PathPort | None = None
    atlas: AtlasPort | None = None
    links: LinksPort | None = None
    search: EntitySearchPort | None = None
    causal: CausalPort | None = None
    families: FamiliesPort | None = None
    effects: EffectsPort | None = None
    forecasts: ForecastPort | None = None
    decisions: DecisionPort | None = None
    notes: list[str] = field(default_factory=list)
