"""Entity register reads: identities with their provider-scoped aliases.

An entity record is an identity, not proof of existence. Aliases are
provider-scoped mappings (``provider``, ``external_key``) with a confidence
kind, a mapping version, an optional validity range and the instant REVA
recorded them. All functions are read-only and take an explicit SQLAlchemy
``Connection``; they work under the ``reva_api`` role.

Pagination is keyset-based on ``(label, id)``: default 100, maximum 1000 rows.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import ValidationProblem
from reva.world.graph import require_aware

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
REFERENT_MODES = ("real", "abstract", "synthetic", "hypothetical")


@dataclass(frozen=True)
class Alias:
    """A provider-scoped identifier for an entity. Never a universal id."""

    id: UUID
    entity_id: UUID
    provider: str
    external_key: str
    confidence_kind: str
    mapping_version: str
    valid_from: date | None
    valid_to: date | None
    valid_range_specified: bool
    recorded_at: datetime
    provenance_activity_id: UUID

    def valid_on(self, d: date) -> bool:
        """True when ``d`` lies in the half-open validity range (unspecified range = no restriction)."""
        if not self.valid_range_specified:
            return True
        return (self.valid_from is None or self.valid_from <= d) and (self.valid_to is None or d < self.valid_to)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "entity_id": str(self.entity_id),
            "provider": self.provider,
            "external_key": self.external_key,
            "confidence_kind": self.confidence_kind,
            "mapping_version": self.mapping_version,
            "valid_range": (
                {"start": _d(self.valid_from), "end": _d(self.valid_to), "bounds": "[)"}
                if self.valid_range_specified
                else None
            ),
            "recorded_at": self.recorded_at.isoformat(),
            "provenance_activity_id": str(self.provenance_activity_id),
        }


@dataclass(frozen=True)
class Entity:
    """A referent: identity, kind concept and referent mode, with aliases."""

    id: UUID
    label: str
    kind: str
    kind_version: str
    kind_concept_id: UUID
    namespace: str
    referent_mode: str
    recorded_at: datetime
    created_activity_id: UUID
    aliases: tuple[Alias, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "label": self.label,
            "kind": self.kind,
            "kind_version": self.kind_version,
            "kind_concept_id": str(self.kind_concept_id),
            "namespace": self.namespace,
            "referent_mode": self.referent_mode,
            "recorded_at": self.recorded_at.isoformat(),
            "created_activity_id": str(self.created_activity_id),
            "aliases": [a.as_dict() for a in self.aliases],
            "identity_note": "an identity record does not prove existence; aliases are provider-scoped",
        }


@dataclass(frozen=True)
class EntityPage:
    items: tuple[Entity, ...]
    next_cursor: str | None
    limit: int

    def as_dict(self) -> dict[str, Any]:
        return {"items": [e.as_dict() for e in self.items], "next_cursor": self.next_cursor, "limit": self.limit}


# ---------------------------------------------------------------------------
# Cursor / limit helpers (shared with events.py)
# ---------------------------------------------------------------------------


def check_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValidationProblem(f"limit must be an integer within 1..{MAX_LIMIT}", limit=str(limit)[:32])
    return limit


def encode_cursor(values: Sequence[Any]) -> str:
    raw = json.dumps([str(v) for v in values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, arity: int) -> list[str] | None:
    """Decode an opaque keyset cursor; malformed cursors raise ``ValidationProblem``."""
    if cursor is None:
        return None
    if len(cursor) > 2048:
        raise ValidationProblem("cursor too long")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationProblem("malformed cursor") from exc
    if not isinstance(values, list) or len(values) != arity or not all(isinstance(v, str) for v in values):
        raise ValidationProblem("malformed cursor")
    return values


def cursor_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationProblem("malformed cursor") from exc


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_ENTITY_COLUMNS = """
SELECT e.id, e.label, c.concept_id AS kind, c.version AS kind_version, e.kind_concept_id, e.namespace,
       e.referent_mode, e.recorded_at, e.created_activity_id
FROM world.entity e JOIN ontology.concept_version c ON c.id = e.kind_concept_id
"""

_LIST_SQL = (
    _ENTITY_COLUMNS
    + r"""
WHERE (CAST(:as_of AS timestamptz) IS NULL OR e.recorded_at <= :as_of)
  AND (CAST(:kind AS text) IS NULL OR c.concept_id = :kind)
  AND (CAST(:mode AS text) IS NULL OR e.referent_mode = :mode)
  AND (CAST(:ns AS text) IS NULL OR e.namespace = :ns)
  AND (CAST(:pat AS text) IS NULL OR e.label ILIKE :pat ESCAPE '\')
  AND (CAST(:ap AS text) IS NULL OR EXISTS (
        SELECT 1 FROM world.alias a WHERE a.entity_id = e.id AND a.provider = :ap
          AND (CAST(:as_of AS timestamptz) IS NULL OR a.recorded_at <= :as_of)))
  AND (CAST(:c_label AS text) IS NULL OR (e.label, e.id) > (CAST(:c_label AS text), CAST(:c_id AS uuid)))
ORDER BY e.label, e.id
LIMIT :lim
"""
)

_ALIASES_SQL = """
SELECT a.id, a.entity_id, a.provider, a.external_key, a.confidence_kind, a.mapping_version,
       lower(a.valid_range) AS valid_from, upper(a.valid_range) AS valid_to,
       (a.valid_range IS NOT NULL) AS valid_range_specified, a.recorded_at, a.provenance_activity_id
FROM world.alias a
WHERE a.entity_id = ANY(:ids)
  AND (CAST(:as_of AS timestamptz) IS NULL OR a.recorded_at <= :as_of)
ORDER BY a.entity_id, a.provider, a.external_key, a.mapping_version, a.id
"""


def alias_from_row(r: Any) -> Alias:
    return Alias(
        id=UUID(str(r["id"])),
        entity_id=UUID(str(r["entity_id"])),
        provider=r["provider"],
        external_key=r["external_key"],
        confidence_kind=r["confidence_kind"],
        mapping_version=r["mapping_version"],
        valid_from=r["valid_from"],
        valid_to=r["valid_to"],
        valid_range_specified=bool(r["valid_range_specified"]),
        recorded_at=r["recorded_at"],
        provenance_activity_id=UUID(str(r["provenance_activity_id"])),
    )


def load_aliases(conn: Connection, entity_ids: Sequence[UUID], *, as_of: datetime | None = None) -> dict[UUID, list[Alias]]:
    """Aliases recorded at/before ``as_of`` for the given entities, grouped by entity."""
    require_aware(as_of, "as_of")
    out: dict[UUID, list[Alias]] = {eid: [] for eid in entity_ids}
    if not entity_ids:
        return out
    for r in conn.execute(text(_ALIASES_SQL), {"ids": list(entity_ids), "as_of": as_of}).mappings():
        a = alias_from_row(r)
        out.setdefault(a.entity_id, []).append(a)
    return out


def _entity(r: Any, aliases: list[Alias]) -> Entity:
    return Entity(
        id=UUID(str(r["id"])),
        label=r["label"],
        kind=r["kind"],
        kind_version=r["kind_version"],
        kind_concept_id=UUID(str(r["kind_concept_id"])),
        namespace=r["namespace"],
        referent_mode=r["referent_mode"],
        recorded_at=r["recorded_at"],
        created_activity_id=UUID(str(r["created_activity_id"])),
        aliases=tuple(aliases),
    )


def list_entities(
    conn: Connection,
    *,
    kind: str | None = None,
    referent_mode: str | None = None,
    namespace: str | None = None,
    label_contains: str | None = None,
    alias_provider: str | None = None,
    as_of: datetime | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    include_aliases: bool = True,
) -> EntityPage:
    """List entities ordered by ``(label, id)`` with keyset pagination.

    ``as_of`` restricts to entities and aliases recorded at/before that
    instant. ``alias_provider`` keeps entities that have an alias in that
    provider's scope. Pass ``next_cursor`` back as ``cursor`` for the next page.
    """
    check_limit(limit)
    require_aware(as_of, "as_of")
    if referent_mode is not None and referent_mode not in REFERENT_MODES:
        raise ValidationProblem(f"referent_mode must be one of {REFERENT_MODES}", referent_mode=referent_mode)
    if label_contains is not None and not 1 <= len(label_contains) <= 200:
        raise ValidationProblem("label_contains must have 1..200 characters")
    cur = decode_cursor(cursor, 2)
    pattern = None
    if label_contains:
        esc = label_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{esc}%"
    rows = [
        dict(r)
        for r in conn.execute(
            text(_LIST_SQL),
            {
                "as_of": as_of,
                "kind": kind,
                "mode": referent_mode,
                "ns": namespace,
                "pat": pattern,
                "ap": alias_provider,
                "c_label": cur[0] if cur else None,
                "c_id": cursor_uuid(cur[1]) if cur else None,
                "lim": limit + 1,
            },
        ).mappings()
    ]
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [UUID(str(r["id"])) for r in rows]
    aliases = load_aliases(conn, ids, as_of=as_of) if include_aliases else {}
    items = tuple(_entity(r, aliases.get(eid, [])) for r, eid in zip(rows, ids, strict=True))
    nxt = encode_cursor([rows[-1]["label"], rows[-1]["id"]]) if more and rows else None
    return EntityPage(items=items, next_cursor=nxt, limit=limit)


def get_entity(conn: Connection, entity_id: UUID, *, as_of: datetime | None = None) -> Entity | None:
    """One entity with its aliases, or ``None`` if absent (or not yet recorded at ``as_of``)."""
    require_aware(as_of, "as_of")
    row = conn.execute(
        text(_ENTITY_COLUMNS + " WHERE e.id = :i AND (CAST(:as_of AS timestamptz) IS NULL OR e.recorded_at <= :as_of)"),
        {"i": entity_id, "as_of": as_of},
    ).mappings().one_or_none()
    if row is None:
        return None
    return _entity(row, load_aliases(conn, [entity_id], as_of=as_of).get(entity_id, []))


def get_entities(conn: Connection, entity_ids: Sequence[UUID], *, as_of: datetime | None = None) -> dict[UUID, Entity]:
    """Batch variant of :func:`get_entity` (at most ``MAX_LIMIT`` ids)."""
    require_aware(as_of, "as_of")
    if len(entity_ids) > MAX_LIMIT:
        raise ValidationProblem(f"at most {MAX_LIMIT} entity ids per call")
    if not entity_ids:
        return {}
    rows = list(
        conn.execute(
            text(
                _ENTITY_COLUMNS
                + " WHERE e.id = ANY(:ids) AND (CAST(:as_of AS timestamptz) IS NULL OR e.recorded_at <= :as_of)"
            ),
            {"ids": list(entity_ids), "as_of": as_of},
        ).mappings()
    )
    aliases = load_aliases(conn, [UUID(str(r["id"])) for r in rows], as_of=as_of)
    return {UUID(str(r["id"])): _entity(r, aliases.get(UUID(str(r["id"])), [])) for r in rows}


def _d(v: date | None) -> str | None:
    return None if v is None else v.isoformat()
