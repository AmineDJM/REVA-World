"""Dossier document model (V8DOSSIER): what the generator returns and every export renders.

A document is sections of blocks. Prose blocks hold sentences; a sentence is the rendering of ONE template sentence
of ``registry/dossier/<kind>.yaml`` (``template_id``) whose placeholders were filled with sourced values. Its
``parts`` are text runs (from the template, verbatim) and values; every value part names its footnote (source,
licence, period, reference) and its honesty kind. A value that is not available is a ``missing`` part with its
reason, never a zero or an invented number. Tables and lists are rows of the same parts; charts are point series
whose every point carries a footnote. Nothing in a document is free text written by the generator.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ValueKind = Literal[
    "observed",  # a value as its source reports it
    "estimate",  # a modelled estimate published by its source (WHO GHE, UCDP best estimate...)
    "projection",  # a source's projection / forecast cell
    "forecast",  # a sealed forecast of REVA's registry (judged or pending)
    "scenario",  # an unvalidated scenario result of the decision engine
    "derived",  # computed by REVA from sourced values (method in the footnote)
    "claim",  # an attributed causal or relational claim (never established by REVA)
    "label",  # a name, identifier, date or category reported by a source
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Footnote(_M):
    n: int = Field(description="Footnote number (order of first use in the document)")
    source: str = Field(description="Source key (atlas source, KG source key, registry, publication)")
    title: str = Field(description="Human label of the source / dataset (from the source's own metadata)")
    dataset: str | None = None
    indicator: str | None = Field(default=None, description="Series / property / estimate key inside the source")
    period: str | None = Field(default=None, description="Period the value refers to (as published)")
    licence: str | None = Field(
        default=None, description="Licence or terms as recorded with the artifact; null = unknown"
    )
    licence_url: str | None = None
    restricted: bool = Field(False, description="Non-commercial, share-alike, no-derivatives or not-open terms")
    non_commercial: bool = False
    required_notice: str | None = None
    ref: str | None = Field(default=None, description="Raw artifact sha256, statement key, estimate key or question id")
    url: str | None = None
    retrieved: str | None = None
    verification: str | None = Field(
        default=None, description="e.g. community_edited_not_verified, derived, unvalidated"
    )
    method: str | None = Field(default=None, description="For derived values: how REVA computed them")


class Part(_M):
    """One run of a rendered sentence / cell."""

    t: Literal["text", "value", "missing"]
    text: str = Field(description="Display text (for values: formatted in French)")
    exact: str | None = Field(default=None, description="Exact value as reported (decimal string), for values")
    unit: str | None = None
    kind: ValueKind | None = None
    fn: int | None = Field(default=None, description="Footnote number (every value part has one)")
    reason: str | None = Field(default=None, description="Why the value is missing (missing parts)")
    href: str | None = Field(default=None, description="V8 deep link of the entity this value names, if any")


class Sentence(_M):
    template_id: str = Field(description="<section>.<block>.<sentence> id in the template file")
    parts: list[Part]
    text: str = Field(description="Plain text: concatenation of the parts")


class Cell(_M):
    parts: list[Part]


class TableBlock(_M):
    type: Literal["table"] = "table"
    id: str
    template_id: str
    caption: str
    columns: list[str]
    numeric: list[bool]
    rows: list[list[Cell]]
    note: str | None = None


class ParagraphBlock(_M):
    type: Literal["paragraph"] = "paragraph"
    id: str
    sentences: list[Sentence]


class ListBlock(_M):
    type: Literal["list"] = "list"
    id: str
    template_id: str
    title: str | None = None
    items: list[Sentence]
    more: int = Field(0, description="Items the source holds beyond those shown")


class ChartPoint(_M):
    x: str
    y: float | None
    exact: str | None = None
    lo: float | None = None
    hi: float | None = None


class ChartSeries(_M):
    label: str
    kind: ValueKind
    fn: int
    points: list[ChartPoint]


class ChartBlock(_M):
    type: Literal["chart"] = "chart"
    id: str
    template_id: str
    chart: Literal["line", "bar", "fan"]
    title: str
    unit: str
    x_kind: Literal["year", "month", "quarter", "category"]
    series: list[ChartSeries]
    note: str | None = None


class MissingBlock(_M):
    type: Literal["missing"] = "missing"
    id: str
    template_id: str
    what: str
    reason: str


Block = ParagraphBlock | TableBlock | ListBlock | ChartBlock | MissingBlock


class Section(_M):
    id: str
    title: str
    lede: str | None = None
    blocks: list[Block]


class SubjectRef(_M):
    kind: str
    id: str
    label: str
    sublabel: str | None = None
    entity_href: str | None = Field(
        default=None, description="V8 fiche link of the subject, when it is a KG entity/country"
    )


class NotCovered(_M):
    what: str
    reason: str
    section: str | None = None


class DocumentMeta(_M):
    template: str = Field(description="registry/dossier/<kind>.yaml")
    template_version: str = Field(description="sha256 (12) of the template file")
    snapshot_id: str = Field(description="Data snapshot id the document was computed from (cache key)")
    atlas_snapshot: str | None = None
    generated_at: str
    compute_ms: float
    cached: bool = False
    origin: str = "live"
    sentences: int
    values: int
    missing: int
    timings_ms: dict[str, float] = Field(default_factory=dict, description="Compute time per block query (uncached)")


class Document(_M):
    schema_: str = Field("reva-dossier/1", alias="schema")
    kind: str
    subject: SubjectRef
    title: str
    subtitle: str
    summary: list[Sentence] = Field(description="Answer first: the key sentences of the brief")
    sections: list[Section]
    not_covered: list[NotCovered] = Field(description="Ce que ce dossier ne couvre pas (with reasons)")
    footnotes: list[Footnote]
    licences: list[dict[str, Any]] = Field(description="Distinct licences of the cited sources, restricted first")
    honesty: list[str] = Field(description="Reading rules of the brief (templated, per kind)")
    meta: DocumentMeta

    model_config = ConfigDict(extra="forbid", populate_by_name=True, json_schema_serialization_defaults_required=True)


class SubjectHit(_M):
    kind: str
    id: str
    label: str
    sublabel: str | None = None
    featured: bool = False


class SubjectList(_M):
    query: str | None
    hits: list[SubjectHit]
    kinds: list[dict[str, str]]
