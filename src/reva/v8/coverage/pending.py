"""Queued loads and owner decisions (``registry/coverage/pending.yaml``), with a live check of queued loads.

Each item is either a ``load`` (a command the lead runs when the machine allows it) or a ``decision`` (something only
the owner can decide or obtain: a licence, an account or key, a permission, an attribution contact, a policy choice).
Items are transcribed from the agents' reports (``source_docs``); their declared ``status`` is the report's. For a
load, ``check`` names the connector runs that would complete it; :func:`observe` compares that with the database
(live acquisitions per connector version, registered atlas tables) so the console can say "constaté chargé" without
anyone editing the file. A check never marks a decision as taken.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from reva.ontology.registry import REGISTRY_ROOT

PENDING_PATH = REGISTRY_ROOT / "coverage" / "pending.yaml"
REPO_ROOT = REGISTRY_ROOT.parent
SCHEMA = "reva-v8-coverage-pending/1"

Kind = Literal[
    "licence",  # a licence / terms review or grant
    "account_key",  # an account, API key or subscription
    "permission",  # a provider's authorisation or allow-listing
    "disk",  # needs >= 5 GB free on the dev box
    "bigger_machine",  # needs more disk / RAM / bandwidth than the dev container
    "attribution_contact",  # a contact person / address the provider requires
    "policy_choice",  # a methodological or publication choice
]
KINDS: tuple[str, ...] = Kind.__args__  # type: ignore[attr-defined]
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class Check(BaseModel):
    """A connector run that would complete a queued load: a live acquisition by ``connector`` (at ``min_version`` or
    later when given) and, when ``tables`` is given, those atlas tables registered."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connector: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,99}$")
    min_version: str | None = None
    tables: tuple[str, ...] = ()

    @field_validator("min_version")
    @classmethod
    def _semver(cls, v: str | None) -> str | None:
        if v is not None and not _SEMVER.match(v):
            raise ValueError("min_version must be MAJOR.MINOR.PATCH")
        return v


class Unlocks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    domains: tuple[str, ...] = ()
    atlas_sources: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    tracks: tuple[str, ...] = ()  # forecast tracks / features it unblocks (free text ids, e.g. "beat-health:H2")
    text: str = Field(min_length=3, max_length=600)  # French: what becomes possible


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,79}$")
    type: Literal["load", "decision"]
    kind: Kind
    title: str = Field(min_length=5, max_length=200)  # French
    detail: str | None = Field(None, max_length=1200)  # French or the report's words
    unlocks: Unlocks
    action: str = Field(min_length=5, max_length=800)  # French: what must be done
    commands: tuple[str, ...] = ()  # exact shell commands (copyable)
    who: Literal["owner", "lead"]
    status: Literal["open", "queued", "blocked", "done", "dropped"]
    disk_mb: float | None = Field(None, ge=0)  # raw + stored growth estimate from the report
    source_docs: tuple[str, ...] = Field(min_length=1)
    check: tuple[Check, ...] = ()

    @model_validator(mode="after")
    def _rules(self) -> Item:
        if self.type == "decision" and self.check:
            raise ValueError("a decision has no connector check (only the owner can take it)")
        if self.type == "load" and not self.commands:
            raise ValueError("a load needs its exact commands")
        if self.kind in ("disk", "bigger_machine") and self.type != "load":
            raise ValueError("disk / bigger_machine items are loads")
        for text in (self.title, self.detail or "", self.action, *self.commands):
            if _EMAIL.search(text):
                raise ValueError("no e-mail address in the registry (personal data)")
        return self


class PendingRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_: str = Field(alias="schema")
    updated: str
    note: str | None = None
    items: tuple[Item, ...]

    @model_validator(mode="after")
    def _unique(self) -> PendingRegistry:
        ids = [i.id for i in self.items]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"duplicate item ids: {dup}")
        if self.schema_ != SCHEMA:
            raise ValueError(f"schema must be {SCHEMA}")
        return self


def problems(
    reg: PendingRegistry,
    *,
    domains: Iterable[str],
    connectors: Mapping[str, Any] | None = None,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    """Cross-file problems: unknown domains, missing source docs, connectors not declared (when ``connectors``)."""
    out: list[str] = []
    dom = set(domains)
    for it in reg.items:
        for d in it.unlocks.domains:
            if d not in dom:
                out.append(f"{it.id}: unknown domain {d!r}")
        for doc in it.source_docs:
            if doc.startswith("lead:"):  # the lead's integration todo (kept outside the repository)
                continue
            path = doc.split("#", 1)[0]
            if not (repo_root / path).exists():
                out.append(f"{it.id}: source doc {path} not found")
        if connectors is not None:
            for c in it.check:
                if c.connector.startswith("atlas.") and c.connector not in connectors:
                    out.append(f"{it.id}: connector {c.connector} is not a declared atlas source")
    return out


@lru_cache(maxsize=2)
def load_pending(path: Path = PENDING_PATH) -> PendingRegistry:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        return PendingRegistry.model_validate(doc)
    except (ValidationError, yaml.YAMLError) as e:
        raise ValueError(f"{path}: {e}") from e


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".")) if _SEMVER.match(v) else (-1,)


def observe(item: Item, acquisitions: Mapping[str, Mapping[str, Any]], tables: Iterable[str]) -> dict[str, Any] | None:
    """Observed state of a queued load: ``done`` when every check is met, ``partial`` when some are, ``not_seen``
    otherwise; ``None`` for a decision or a load without checks. ``acquisitions``: connector name -> {versions,
    last_retrieved_at}; ``tables``: registered atlas dataset keys."""
    if item.type != "load" or not item.check:
        return None
    have = set(tables)
    rows = []
    for c in item.check:
        a = acquisitions.get(c.connector)
        versions = sorted(a.get("versions") or [], key=_ver) if a else []
        ok_v = bool(versions) and (c.min_version is None or any(_ver(v) >= _ver(c.min_version) for v in versions))
        missing = [t for t in c.tables if t not in have]
        rows.append(
            {
                "connector": c.connector,
                "min_version": c.min_version,
                "versions_acquired": versions,
                "last_retrieved_at": (a or {}).get("last_retrieved_at"),
                "tables_missing": missing,
                "met": ok_v and not missing,
            }
        )
    met = sum(r["met"] for r in rows)
    state = "done" if met == len(rows) else ("partial" if met else "not_seen")
    return {"state": state, "checks": rows}
