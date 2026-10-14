"""Provider-scoped alias resolution, as of a knowledge instant.

A source-specific code is never a universal identifier: ``"12"`` is Algeria
as a Comtrade reporter *and* under UN M49, ``"251"`` is France only as a
Comtrade reporter (UN M49 France is ``"250"``). Resolution therefore always
requires a provider scope.

Rules
-----
* Only aliases recorded at/before ``as_of`` are admissible (``recorded_at <= as_of``);
  a later identity merge or mapping cannot change an earlier answer.
* By default only ``reviewed_exact`` and ``provider_declared`` aliases resolve;
  ``candidate`` aliases are reported but ignored unless ``include_candidates``.
* With ``valid_on`` an alias whose validity range excludes that date is not
  admissible; aliases with no validity range are unrestricted.
* If admissible aliases point at more than one entity the result is
  ``ambiguous`` with every candidate listed -- never a silent pick.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import ValidationProblem
from reva.world.entities import Alias, alias_from_row
from reva.world.graph import require_aware

ACCEPTED_CONFIDENCE = ("reviewed_exact", "provider_declared")
MAX_KEYS = 1000
MAX_KEY_LENGTH = 256

_SQL = """
SELECT a.id, a.entity_id, a.provider, a.external_key, a.confidence_kind, a.mapping_version,
       lower(a.valid_range) AS valid_from, upper(a.valid_range) AS valid_to,
       (a.valid_range IS NOT NULL) AS valid_range_specified, a.recorded_at, a.provenance_activity_id
FROM world.alias a
WHERE (CAST(:provider AS text) IS NULL OR a.provider = :provider)
  AND a.external_key = ANY(CAST(:keys AS text[]))
  AND (CAST(:as_of AS timestamptz) IS NULL OR a.recorded_at <= :as_of)
ORDER BY a.provider, a.external_key, a.recorded_at, a.id
"""


@dataclass(frozen=True)
class AliasResolution:
    """Outcome of resolving one provider-scoped key."""

    provider: str
    external_key: str
    status: Literal["resolved", "ambiguous", "unresolved"]
    entity_id: UUID | None
    matches: tuple[Alias, ...]
    ignored: tuple[Alias, ...] = ()
    as_of: datetime | None = None
    valid_on: date | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def candidate_entity_ids(self) -> list[UUID]:
        return sorted({a.entity_id for a in self.matches}, key=str)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "external_key": self.external_key,
            "status": self.status,
            "entity_id": None if self.entity_id is None else str(self.entity_id),
            "candidate_entity_ids": [str(e) for e in self.candidate_entity_ids],
            "matches": [a.as_dict() for a in self.matches],
            "ignored": [a.as_dict() for a in self.ignored],
            "as_of": None if self.as_of is None else self.as_of.isoformat(),
            "valid_on": None if self.valid_on is None else self.valid_on.isoformat(),
            "notes": list(self.notes),
        }


def _check_scope(provider: str, keys: Sequence[str]) -> None:
    if not isinstance(provider, str) or not provider.strip():
        raise ValidationProblem("alias resolution requires a provider scope; provider codes are not universal ids")
    if len(keys) > MAX_KEYS:
        raise ValidationProblem(f"at most {MAX_KEYS} keys per call")
    for k in keys:
        if not isinstance(k, str) or not k or len(k) > MAX_KEY_LENGTH:
            raise ValidationProblem("external keys must be non-empty strings of bounded length")


def _decide(
    provider: str,
    key: str,
    rows: list[Alias],
    *,
    as_of: datetime | None,
    valid_on: date | None,
    include_candidates: bool,
) -> AliasResolution:
    accepted = ACCEPTED_CONFIDENCE + (("candidate",) if include_candidates else ())
    matches: list[Alias] = []
    ignored: list[Alias] = []
    notes: list[str] = []
    for a in rows:
        if a.confidence_kind not in accepted:
            ignored.append(a)
        elif valid_on is not None and not a.valid_on(valid_on):
            ignored.append(a)
        else:
            matches.append(a)
    n_cand = sum(1 for a in ignored if a.confidence_kind == "candidate")
    if n_cand:
        notes.append(f"{n_cand} unreviewed candidate alias(es) ignored (include_candidates=False)")
    n_range = sum(1 for a in ignored if a.confidence_kind in accepted)
    if n_range:
        notes.append(f"{n_range} alias(es) excluded because their validity range does not contain {valid_on}")
    entities = {a.entity_id for a in matches}
    if not matches:
        notes.append(
            "no admissible alias in this provider scope"
            + ("" if as_of is None else f" recorded at/before {as_of.isoformat()}")
        )
        return AliasResolution(provider, key, "unresolved", None, (), tuple(ignored), as_of, valid_on, tuple(notes))
    if len(entities) > 1:
        notes.append(f"key maps to {len(entities)} distinct entities in provider scope '{provider}'")
        if valid_on is None and any(a.valid_range_specified for a in matches):
            notes.append("aliases carry validity ranges; pass valid_on to disambiguate by validity")
        return AliasResolution(
            provider, key, "ambiguous", None, tuple(matches), tuple(ignored), as_of, valid_on, tuple(notes)
        )
    if any(a.confidence_kind == "candidate" for a in matches):
        notes.append("resolution relies on an unreviewed candidate alias")
    return AliasResolution(
        provider, key, "resolved", next(iter(entities)), tuple(matches), tuple(ignored), as_of, valid_on, tuple(notes)
    )


def resolve_aliases(
    conn: Connection,
    provider: str,
    external_keys: Sequence[str],
    *,
    as_of: datetime | None = None,
    valid_on: date | None = None,
    include_candidates: bool = False,
) -> dict[str, AliasResolution]:
    """Resolve many keys within one provider scope (one query). Keys map to their resolution."""
    _check_scope(provider, external_keys)
    require_aware(as_of, "as_of")
    keys = list(dict.fromkeys(external_keys))
    grouped: dict[str, list[Alias]] = {k: [] for k in keys}
    if keys:
        for r in conn.execute(text(_SQL), {"provider": provider, "keys": keys, "as_of": as_of}).mappings():
            grouped[r["external_key"]].append(alias_from_row(r))
    return {
        k: _decide(provider, k, rows, as_of=as_of, valid_on=valid_on, include_candidates=include_candidates)
        for k, rows in grouped.items()
    }


def resolve_alias(
    conn: Connection,
    provider: str,
    external_key: str,
    *,
    as_of: datetime | None = None,
    valid_on: date | None = None,
    include_candidates: bool = False,
) -> AliasResolution:
    """Resolve one provider-scoped key to an entity, reporting ambiguity explicitly."""
    return resolve_aliases(
        conn, provider, [external_key], as_of=as_of, valid_on=valid_on, include_candidates=include_candidates
    )[external_key]


def key_scopes(conn: Connection, external_key: str, *, as_of: datetime | None = None) -> dict[str, AliasResolution]:
    """How each provider scope interprets the same key (shows that codes are not universal).

    Returns one :class:`AliasResolution` per provider that uses ``external_key``.
    """
    _check_scope("*", [external_key])
    require_aware(as_of, "as_of")
    grouped: dict[str, list[Alias]] = {}
    for r in conn.execute(text(_SQL), {"provider": None, "keys": [external_key], "as_of": as_of}).mappings():
        grouped.setdefault(r["provider"], []).append(alias_from_row(r))
    return {
        p: _decide(p, external_key, rows, as_of=as_of, valid_on=None, include_candidates=False)
        for p, rows in sorted(grouped.items())
    }


def aliases_for_entity(
    conn: Connection, entity_id: UUID, *, provider: str | None = None, as_of: datetime | None = None
) -> list[Alias]:
    """All aliases of one entity recorded at/before ``as_of`` (optionally one provider)."""
    require_aware(as_of, "as_of")
    rows = conn.execute(
        text(
            """SELECT a.id, a.entity_id, a.provider, a.external_key, a.confidence_kind, a.mapping_version,
                      lower(a.valid_range) AS valid_from, upper(a.valid_range) AS valid_to,
                      (a.valid_range IS NOT NULL) AS valid_range_specified, a.recorded_at, a.provenance_activity_id
               FROM world.alias a
               WHERE a.entity_id = :e
                 AND (CAST(:p AS text) IS NULL OR a.provider = :p)
                 AND (CAST(:as_of AS timestamptz) IS NULL OR a.recorded_at <= :as_of)
               ORDER BY a.provider, a.external_key, a.mapping_version, a.id"""
        ),
        {"e": entity_id, "p": provider, "as_of": as_of},
    ).mappings()
    return [alias_from_row(r) for r in rows]
