"""Board documents (same shape as V8BOARDS' zod ``Board`` in ``ui/src/v8/features/tableaux/model.ts``).

Items are references (ids, concept + country, question id, decision ref, Markdown note), never values.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Str = Annotated[str, Field(min_length=1, max_length=400)]
Label = Annotated[str, Field(max_length=400)]
ID_RE = r"^[A-Za-z0-9_-]{1,64}$"
DECIMAL_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
RULE_KINDS = ("new_data", "revision", "resolution", "threshold")
RuleKind = Literal["new_data", "revision", "resolution", "threshold"]
ThresholdOp = Literal[">", ">=", "<", "<="]
MAX_ITEMS = 200
MAX_RULES = 200


class _Pin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: Str
    added_at: str = Field(max_length=64)


class EntityPin(_Pin):
    kind: Literal["entity"]
    entityKind: Str  # JSON shape of the UI contract (camelCase)
    id: Str
    label: Label
    sublabel: Label | None = None


class SeriesPin(_Pin):
    kind: Literal["series"]
    concept: Str
    geo: Str
    label: Label
    table: Str | None = None
    unit: Label | None = None
    headline: bool | None = None


class ForecastPin(_Pin):
    kind: Literal["forecast"]
    questionId: Str  # camelCase: UI contract
    label: Label


class DecisionPin(_Pin):
    kind: Literal["decision"]
    ref: Str
    label: Label
    summary: Annotated[str, Field(max_length=2000)] | None = None


class NotePin(_Pin):
    kind: Literal["note"]
    markdown: Annotated[str, Field(max_length=20000)]
    label: Label | None = None


BoardItem = Annotated[EntityPin | SeriesPin | ForecastPin | DecisionPin | NotePin, Field(discriminator="kind")]

RULES_FOR: dict[str, tuple[str, ...]] = {
    "series": ("new_data", "revision", "threshold"),
    "forecast": ("resolution",),
    "entity": ("new_data",),
    "decision": (),
    "note": (),
}


class WatchRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Str
    kind: RuleKind
    item_uid: Str
    op: ThresholdOp | None = None
    value: Annotated[str, Field(max_length=64)] | None = None
    created_at: str = Field(max_length=64)


def rule_problem(kind: str, op: str | None, value: str | None, item: BaseModel | None) -> str | None:
    """Same rules as ``ruleProblem`` in model.ts (French reasons)."""
    if item is None:
        return "élément introuvable sur ce tableau"
    ik = item.kind  # type: ignore[attr-defined]
    if kind not in RULES_FOR.get(ik, ()):
        return f"règle « {kind} » sans objet pour ce type d’élément ({ik})"
    if kind == "threshold":
        if not op:
            return "opérateur manquant"
        if not value or not DECIMAL_RE.match(value.strip()):
            return "seuil : nombre décimal attendu (ex. 2.5)"
    return None


class BoardDoc(BaseModel):
    """What the client sends (create / full update)."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    items: list[BoardItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    rules: list[WatchRule] = Field(default_factory=list, max_length=MAX_RULES)

    @model_validator(mode="after")
    def _consistent(self) -> BoardDoc:
        if not self.title.strip():
            raise ValueError("title must not be blank")
        uids = [i.uid for i in self.items]
        if len(set(uids)) != len(uids):
            raise ValueError("duplicate item uid")
        rids = [r.id for r in self.rules]
        if len(set(rids)) != len(rids):
            raise ValueError("duplicate rule id")
        by_uid = {i.uid: i for i in self.items}
        for r in self.rules:
            why = rule_problem(r.kind, r.op, r.value, by_uid.get(r.item_uid))
            if why:
                raise ValueError(f"rule {r.id}: {why}")
        return self


class BoardCreate(BoardDoc):
    id: str | None = Field(None, pattern=ID_RE)


class BoardUpdate(BoardDoc):
    version: int = Field(ge=1)


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: RuleKind
    item_uid: Str
    op: ThresholdOp | None = None
    value: Annotated[str, Field(max_length=64)] | None = None


class Board(BaseModel):
    id: str
    title: str
    items: list[BoardItem]
    rules: list[WatchRule]
    created_at: str
    updated_at: str
    version: int


class BoardSummary(BaseModel):
    id: str
    title: str
    n_items: int
    n_rules: int
    updated_at: str
    version: int
