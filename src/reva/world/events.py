"""Occurrences (real-world events) with uncertain time, and history views.

An Occurrence is a referent (``world.occurrence`` keyed by a ``world.entity``)
with an event type, a support carrying a possibly uncertain temporal extent,
and an event-specific component. Its participants are ordinary hyperedges.

Observed sequence is not causation. Temporal order is derived only from the
recorded time bounds and is labelled as such (``causal_claim: false``).
Causal links are *separate assertions* (relations whose predicate is declared
causal) and are returned in their own list with the modality
``modelled_causal_hypothesis``. When none exist the list is empty -- nothing is
inferred from "A preceded B".

History views keep three sections apart: ``observed_sequence`` (orderings the
time bounds settle), ``disputed_sequence`` (orderings the bounds do not settle,
unknown times, or occurrences whose component records disputed dates) and
``causal_hypotheses``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import ValidationProblem
from reva.world.entities import DEFAULT_LIMIT, check_limit, cursor_uuid, decode_cursor, encode_cursor
from reva.world.graph import Hyperedge, require_aware
from reva.world.queries import load_hyperedges

MAX_SEQUENCE_ITEMS = 200
CAUSAL_ASSERTION_CLASS = "causal_hypothesis"
SEQUENCE_NOTE = "temporal order only: preceded is not caused; causal links are separate assertions"

Relation = Literal["before", "after", "overlaps", "indeterminate", "unknown"]


@dataclass(frozen=True)
class TimeBounds:
    """Earliest/latest admissible start and (exclusive) end of an uncertain temporal extent.

    Ends are exclusive (half-open intervals); an instant occupies its day.
    """

    kind: str | None
    earliest_start: date | None
    latest_start: date | None
    earliest_end: date | None
    latest_end: date | None
    status: Literal["exact", "uncertain", "unknown"]

    @classmethod
    def from_extent(cls, te: dict[str, Any] | None) -> TimeBounds:
        te = te or {}
        kind = te.get("kind")
        su, eu = te.get("start_uncertainty") or {}, te.get("end_uncertainty") or {}
        start, end = _date(te.get("start")), _date(te.get("end"))
        es, ls = _date(su.get("earliest")) or start, _date(su.get("latest")) or start
        if kind == "instant":
            # An instant at day precision occupies one day: [d, d + 1 day).
            ee = None if es is None else es + timedelta(days=1)
            le = None if ls is None else ls + timedelta(days=1)
        else:
            ee, le = _date(eu.get("earliest")) or end, _date(eu.get("latest")) or end
        if es is None and ls is None:
            status: Literal["exact", "uncertain", "unknown"] = "unknown"
        elif su or eu or (kind != "instant" and (ee is None or le is None)) or es != ls or ee != le:
            status = "uncertain"
        else:
            status = "exact"
        return cls(kind, es, ls, ee, le, status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "earliest_start": _iso(self.earliest_start),
            "latest_start": _iso(self.latest_start),
            "earliest_end": _iso(self.earliest_end),
            "latest_end": _iso(self.latest_end),
            "status": self.status,
        }


@dataclass(frozen=True)
class Occurrence:
    entity_id: UUID
    label: str
    event_type: str
    event_type_version: str
    referent_mode: str
    support_id: UUID
    temporal_extent: dict[str, Any]
    time: TimeBounds
    spatial_spec: dict[str, Any]
    component: dict[str, Any]
    recorded_at: datetime
    provenance_activity_id: UUID

    @property
    def time_disputed(self) -> bool:
        c = self.component or {}
        claims = c.get("date_claims")
        return bool(c.get("time_disputed")) or (isinstance(claims, list) and len({str(x) for x in claims}) > 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": str(self.entity_id),
            "label": self.label,
            "event_type": self.event_type,
            "event_type_version": self.event_type_version,
            "referent_mode": self.referent_mode,
            "support_id": str(self.support_id),
            "temporal_extent": self.temporal_extent,
            "time": self.time.as_dict(),
            "time_disputed": self.time_disputed,
            "spatial_spec": self.spatial_spec,
            "component": self.component,
            "recorded_at": self.recorded_at.isoformat(),
            "provenance_activity_id": str(self.provenance_activity_id),
            "modality": "reported_observation",
        }


@dataclass(frozen=True)
class OccurrencePage:
    items: tuple[Occurrence, ...]
    next_cursor: str | None
    limit: int
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [o.as_dict() for o in self.items],
            "next_cursor": self.next_cursor,
            "limit": self.limit,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SequenceRelation:
    a: UUID
    b: UUID
    relation: Relation
    basis: str = "recorded temporal bounds"
    causal_claim: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "a": str(self.a),
            "b": str(self.b),
            "relation": self.relation,
            "basis": self.basis,
            "causal_claim": False,
            "note": SEQUENCE_NOTE,
        }


@dataclass(frozen=True)
class CausalHypothesis:
    """A causal link asserted separately (never inferred from temporal order)."""

    edge: Hyperedge
    modality: str = "modelled_causal_hypothesis"

    def as_dict(self) -> dict[str, Any]:
        return {"modality": self.modality, "assertion": self.edge.as_dict()}


@dataclass(frozen=True)
class HistoryView:
    occurrences: tuple[Occurrence, ...]
    observed_sequence: tuple[SequenceRelation, ...]
    disputed_sequence: tuple[dict[str, Any], ...]
    causal_hypotheses: tuple[CausalHypothesis, ...]
    truncated: bool
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "occurrences": [o.as_dict() for o in self.occurrences],
            "observed_sequence": [s.as_dict() for s in self.observed_sequence],
            "disputed_sequence": list(self.disputed_sequence),
            "causal_hypotheses": [c.as_dict() for c in self.causal_hypotheses],
            "truncated": self.truncated,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Pure temporal reasoning
# ---------------------------------------------------------------------------


def _certainly_before(a: TimeBounds, b: TimeBounds) -> bool | None:
    if a.latest_end is None or b.earliest_start is None:
        return None
    return a.latest_end <= b.earliest_start  # half-open: ending on day d precedes starting on d


def temporal_relation(a: TimeBounds, b: TimeBounds) -> Relation:
    """Relation of ``a`` to ``b`` that the bounds *settle*; otherwise ``indeterminate`` or ``unknown``.

    ``overlaps`` means the two extents certainly share time; ``indeterminate``
    means the uncertain bounds admit more than one ordering.
    """
    if a.status == "unknown" or b.status == "unknown":
        return "unknown"
    if _certainly_before(a, b):
        return "before"
    if _certainly_before(b, a):
        return "after"
    if (
        a.latest_start is not None
        and b.latest_start is not None
        and a.earliest_end is not None
        and b.earliest_end is not None
        and a.latest_start < b.earliest_end
        and b.latest_start < a.earliest_end
    ):
        return "overlaps"
    return "indeterminate"


def sequence_relations(occurrences: Sequence[Occurrence]) -> list[SequenceRelation]:
    """Pairwise temporal relations (bounded to ``MAX_SEQUENCE_ITEMS`` occurrences)."""
    if len(occurrences) > MAX_SEQUENCE_ITEMS:
        raise ValidationProblem(f"at most {MAX_SEQUENCE_ITEMS} occurrences per sequence computation")
    ordered = sort_occurrences(occurrences)
    out = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            out.append(SequenceRelation(a.entity_id, b.entity_id, temporal_relation(a.time, b.time)))
    return out


def sort_occurrences(occurrences: Sequence[Occurrence]) -> list[Occurrence]:
    """Order by earliest start (unknown times last), then latest start, then id. A display order only."""
    return sorted(
        occurrences,
        key=lambda o: (
            o.time.earliest_start is None,
            o.time.earliest_start or date.min,
            o.time.latest_start or date.min,
            str(o.entity_id),
        ),
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_ES = (
    "COALESCE(CAST(sp.temporal_extent->'start_uncertainty'->>'earliest' AS date), "
    "CAST(sp.temporal_extent->>'start' AS date))"
)
_LE = (
    "CASE WHEN sp.temporal_extent->>'kind' = 'instant' THEN "
    "COALESCE(CAST(sp.temporal_extent->'start_uncertainty'->>'latest' AS date), "
    "CAST(sp.temporal_extent->>'start' AS date)) ELSE "
    "COALESCE(CAST(sp.temporal_extent->'end_uncertainty'->>'latest' AS date), "
    "CAST(sp.temporal_extent->>'end' AS date)) END"
)

_OCC_SQL = f"""
SELECT oc.entity_id, e.label, e.referent_mode, e.recorded_at, c.concept_id AS event_type,
       c.version AS event_type_version, oc.component, oc.provenance_activity_id, sp.id AS support_id,
       sp.temporal_extent, sp.spatial_spec
FROM world.occurrence oc
JOIN world.entity e ON e.id = oc.entity_id
JOIN ontology.concept_version c ON c.id = oc.event_type_id
JOIN world.support sp ON sp.id = oc.support_id
WHERE (CAST(:as_of AS timestamptz) IS NULL OR e.recorded_at <= :as_of)
  AND (CAST(:et AS text) IS NULL OR c.concept_id = :et)
  AND (CAST(:ids AS uuid[]) IS NULL OR oc.entity_id = ANY(CAST(:ids AS uuid[])))
  AND (CAST(:participant AS uuid) IS NULL OR EXISTS (
        SELECT 1 FROM world.relation_participant p1
        JOIN world.relation_participant p2 ON p2.relation_id = p1.relation_id
        JOIN world.relation r ON r.id = p1.relation_id
        WHERE p1.entity_id = oc.entity_id AND p2.entity_id = CAST(:participant AS uuid)
          AND (CAST(:as_of AS timestamptz) IS NULL OR r.recorded_at <= :as_of)))
  -- possible overlap with [ws, we): unknown bounds are kept (they may overlap)
  AND (CAST(:ws AS date) IS NULL OR (
        ({_ES} IS NULL OR {_ES} < CAST(:we AS date))
        AND ({_LE} IS NULL OR {_LE} >= CAST(:ws AS date))))
  AND (CAST(:c0 AS uuid) IS NULL OR oc.entity_id > CAST(:c0 AS uuid))
ORDER BY oc.entity_id
LIMIT :lim
"""

_CAUSAL_SQL = """
SELECT DISTINCT r.id FROM world.relation r
JOIN ontology.concept_version pc ON pc.id = r.predicate_id
JOIN world.relation_participant p ON p.relation_id = r.id
WHERE p.entity_id = ANY(:ids)
  AND (pc.semantic_profile->>'assertion_class' = :cls OR pc.concept_id LIKE 'predicate.causal%')
  AND (CAST(:as_of AS timestamptz) IS NULL OR r.recorded_at <= :as_of)
LIMIT :lim
"""


def _occurrence(r: dict[str, Any]) -> Occurrence:
    te = dict(r["temporal_extent"] or {})
    return Occurrence(
        entity_id=UUID(str(r["entity_id"])),
        label=r["label"],
        event_type=r["event_type"],
        event_type_version=r["event_type_version"],
        referent_mode=r["referent_mode"],
        support_id=UUID(str(r["support_id"])),
        temporal_extent=te,
        time=TimeBounds.from_extent(te),
        spatial_spec=dict(r["spatial_spec"] or {}),
        component=dict(r["component"] or {}),
        recorded_at=r["recorded_at"],
        provenance_activity_id=UUID(str(r["provenance_activity_id"])),
    )


def list_occurrences(
    conn: Connection,
    *,
    event_type: str | None = None,
    participant_id: UUID | None = None,
    during: tuple[date, date] | None = None,
    occurrence_ids: Sequence[UUID] | None = None,
    as_of: datetime | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> OccurrencePage:
    """Occurrences with keyset pagination on id (default 100, max 1000).

    ``during`` keeps occurrences that *may* overlap the half-open date window,
    including those with unknown bounds (check ``time.status``).
    ``participant_id`` keeps occurrences sharing a hyperedge with that entity.
    ``as_of`` keeps occurrences recorded at/before that instant. An empty
    result is returned as an empty page with a note.
    """
    check_limit(limit)
    require_aware(as_of, "as_of")
    if during is not None and during[1] <= during[0]:
        raise ValidationProblem("during must satisfy start < end (half-open)")
    if occurrence_ids is not None and len(occurrence_ids) > 1000:
        raise ValidationProblem("at most 1000 occurrence ids")
    cur = decode_cursor(cursor, 1)
    rows = [
        dict(r)
        for r in conn.execute(
            text(_OCC_SQL),
            {
                "as_of": as_of,
                "et": event_type,
                "ids": None if occurrence_ids is None else list(occurrence_ids),
                "participant": participant_id,
                "ws": during[0] if during else None,
                "we": during[1] if during else None,
                "c0": cursor_uuid(cur[0]) if cur else None,
                "lim": limit + 1,
            },
        ).mappings()
    ]
    more = len(rows) > limit
    items = tuple(_occurrence(r) for r in rows[:limit])
    notes = () if items else ("no occurrences are recorded for this query",)
    return OccurrencePage(items, encode_cursor([items[-1].entity_id]) if more and items else None, limit, notes)


def occurrence_participants(
    conn: Connection, occurrence_id: UUID, *, as_of: datetime | None = None, max_edges: int = 500
) -> tuple[list[Hyperedge], bool]:
    """Participant assertions (hyperedges) in which an occurrence takes part; ``(edges, truncated)``."""
    require_aware(as_of, "as_of")
    ids = list(
        conn.execute(
            text("""SELECT DISTINCT p.relation_id FROM world.relation_participant p
                JOIN world.relation r ON r.id = p.relation_id
                WHERE p.entity_id = :e AND (CAST(:as_of AS timestamptz) IS NULL OR r.recorded_at <= :as_of)
                ORDER BY p.relation_id LIMIT :lim"""),
            {"e": occurrence_id, "as_of": as_of, "lim": max_edges + 1},
        ).scalars()
    )
    return load_hyperedges(conn, [UUID(str(i)) for i in ids[:max_edges]]), len(ids) > max_edges


def causal_hypotheses(
    conn: Connection, occurrence_ids: Sequence[UUID], *, as_of: datetime | None = None, max_edges: int = 500
) -> tuple[list[CausalHypothesis], bool]:
    """Separately asserted causal links involving the occurrences; ``(hypotheses, truncated)``.

    A relation counts as a causal assertion only when its predicate concept
    declares ``semantic_profile.assertion_class = "causal_hypothesis"`` (or is
    in the ``predicate.causal*`` family). Returns ``[]`` when none exist.
    """
    require_aware(as_of, "as_of")
    if not occurrence_ids:
        return [], False
    ids = list(
        conn.execute(
            text(_CAUSAL_SQL),
            {"ids": list(occurrence_ids), "cls": CAUSAL_ASSERTION_CLASS, "as_of": as_of, "lim": max_edges + 1},
        ).scalars()
    )
    edges = load_hyperedges(conn, [UUID(str(i)) for i in ids[:max_edges]])
    return [CausalHypothesis(e) for e in edges], len(ids) > max_edges


def history_view(
    conn: Connection,
    *,
    participant_id: UUID | None = None,
    occurrence_ids: Sequence[UUID] | None = None,
    as_of: datetime | None = None,
    limit: int = MAX_SEQUENCE_ITEMS,
) -> HistoryView:
    """Observed sequence, disputed/indeterminate sequence and causal hypotheses, kept apart.

    Select occurrences either by a participating entity or by explicit ids.
    """
    if participant_id is None and occurrence_ids is None:
        raise ValidationProblem("history_view needs participant_id or occurrence_ids")
    if not 1 <= limit <= MAX_SEQUENCE_ITEMS:
        raise ValidationProblem(f"limit must be within 1..{MAX_SEQUENCE_ITEMS}")
    page = list_occurrences(
        conn, participant_id=participant_id, occurrence_ids=occurrence_ids, as_of=as_of, limit=limit
    )
    occ = sort_occurrences(page.items)
    truncated = page.next_cursor is not None
    rels = sequence_relations(occ)
    observed = tuple(r for r in rels if r.relation in ("before", "after", "overlaps"))
    disputed: list[dict[str, Any]] = [r.as_dict() for r in rels if r.relation in ("indeterminate", "unknown")]
    for o in occ:
        if o.time_disputed:
            disputed.append(
                {
                    "occurrence": str(o.entity_id),
                    "relation": "time_disputed",
                    "date_claims": o.component.get("date_claims"),
                    "note": "sources disagree on the date of this occurrence",
                }
            )
    hyps, hyp_trunc = causal_hypotheses(conn, [o.entity_id for o in occ], as_of=as_of)
    notes = [SEQUENCE_NOTE]
    if not occ:
        notes.append("no occurrences are recorded for this query")
    if not hyps:
        notes.append("no causal hypothesis has been asserted for these occurrences")
    return HistoryView(tuple(occ), observed, tuple(disputed), tuple(hyps), truncated or hyp_trunc, tuple(notes))


def _date(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _iso(v: date | None) -> str | None:
    return None if v is None else v.isoformat()
