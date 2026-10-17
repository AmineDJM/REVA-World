"""Declarative brief templates: ``registry/dossier/<kind>.yaml`` (loaded, validated, versioned by content hash)."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "registry" / "dossier"
PLACEHOLDER = re.compile(r"\{([$]?[A-Za-z0-9_.]+)(\|vocab)?\}")
ID_RE = r"^[a-z][a-z0-9_]{0,40}$"


class _T(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SentenceT(_T):
    id: str = Field(pattern=ID_RE)
    text: str
    what: str | None = None
    missing: str | None = None


class ColumnT(_T):
    header: str
    cell: str
    numeric: bool = False


class TableT(_T):
    caption: str
    columns: list[ColumnT]
    note: str | None = None


class ListT(_T):
    title: str | None = None
    item: str
    item_missing: str | None = Field(None, description="Item template used when a value of `item` is missing")


class ChartT(_T):
    type: str = Field("line", pattern="^(line|bar|fan)$")
    title: str
    note: str | None = None


class EmptyT(_T):
    what: str


class BlockT(_T):
    id: str = Field(pattern=ID_RE)
    query: str
    params: dict[str, Any] = Field(default_factory=dict)
    when: str | None = Field(None, description="Run only when this subject parameter is set (non-empty)")
    sentences: list[SentenceT] = Field(default_factory=list)
    table: TableT | None = None
    list_: ListT | None = Field(None, alias="list")
    chart: ChartT | None = None
    empty: EmptyT
    missing_list: bool = Field(False, description="State the query's own Missing items (e.g. coverage gaps)")


class SectionT(_T):
    id: str = Field(pattern=ID_RE)
    title: str
    lede: str | None = None
    blocks: list[BlockT]


class NotCoveredT(_T):
    what: str
    reason: str


class SubjectT(_T):
    label: str
    sublabel: str | None = None
    entity: str | None = Field(None, description="KG id of the subject (fiche link)")
    entity_kind: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    featured: bool = True


class FeaturedT(_T):
    id: str
    label: str
    sublabel: str | None = None


class TemplateT(_T):
    kind: str = Field(pattern=ID_RE)
    label: str
    title: str
    subtitle: str
    resolver: str = Field(pattern="^(country|curated|kg|chembl_drug|ucdp_conflict|decision)$")
    id_pattern: str
    honesty: list[str]
    not_covered: list[NotCoveredT] = Field(default_factory=list)
    subjects: dict[str, SubjectT] = Field(default_factory=dict)
    generic: dict[str, Any] = Field(
        default_factory=dict, description="Parameters of a non-curated subject ('$id' = the subject id)"
    )
    featured: list[FeaturedT] = Field(default_factory=list)
    vocab: dict[str, str] = Field(
        default_factory=dict, description="French display of source codes, used by {name|vocab} (exact code kept)"
    )
    summary: list[SentenceT] = Field(default_factory=list)
    sections: list[SectionT]

    @model_validator(mode="after")
    def _ids_unique(self) -> TemplateT:
        seen: set[str] = set()
        for s in self.sections:
            if s.id in seen:
                raise ValueError(f"duplicate section id {s.id}")
            seen.add(s.id)
            bseen: set[str] = set()
            for b in s.blocks:
                if b.id in bseen:
                    raise ValueError(f"duplicate block id {s.id}.{b.id}")
                bseen.add(b.id)
                if not (b.sentences or b.table or b.list_ or b.chart):
                    raise ValueError(f"block {s.id}.{b.id} renders nothing")
        for h in self.honesty:
            if PLACEHOLDER.search(h):
                raise ValueError("honesty sentences are static (no placeholder)")
        return self


class Template:
    def __init__(self, t: TemplateT, path: Path, version: str) -> None:
        self.t = t
        self.path = path
        self.version = version

    def sentence_ids(self) -> set[str]:
        ids = {f"summary.{s.id}" for s in self.t.summary}
        ids |= {f"honesty.{i}" for i in range(len(self.t.honesty))}
        ids |= {"title", "subtitle"}
        for s in self.t.sections:
            if s.lede:
                ids.add(f"{s.id}.lede")
            for b in s.blocks:
                ids |= {f"{s.id}.{b.id}.{x.id}" for x in b.sentences}
                ids |= {f"{s.id}.{b.id}.{x.id}.missing" for x in b.sentences if x.missing}
                if b.list_:
                    ids.add(f"{s.id}.{b.id}.item")
                    ids.add(f"{s.id}.{b.id}.item_missing")
                if b.table:
                    ids.add(f"{s.id}.{b.id}.table")
                if b.chart:
                    ids.add(f"{s.id}.{b.id}.chart")
                ids.add(f"{s.id}.{b.id}.empty")
        return ids


def _load(path: Path) -> Template:
    raw = path.read_bytes()
    t = TemplateT.model_validate(yaml.safe_load(raw))
    return Template(t, path, hashlib.sha256(raw).hexdigest()[:12])


@lru_cache(maxsize=32)
def _cached(path: str, mtime: float, size: int) -> Template:
    return _load(Path(path))


def load_template(kind: str, directory: Path = TEMPLATE_DIR) -> Template | None:
    p = directory / f"{kind}.yaml"
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", kind) or not p.exists():
        return None
    st = p.stat()
    return _cached(str(p), st.st_mtime, st.st_size)


def all_templates(directory: Path = TEMPLATE_DIR) -> list[Template]:
    out = []
    for p in sorted(directory.glob("*.yaml")):
        t = load_template(p.stem, directory)
        if t is not None:
            out.append(t)
    return out
