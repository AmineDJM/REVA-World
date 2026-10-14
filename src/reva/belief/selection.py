"""source_select_v1: a registered belief-publication method.

1. Filter to eligible, semantically compatible observations at the cutoff
   (recorded at/before cutoff, not retracted, eligible for the concept/scope,
   same origin class as the release). Filtering precedes revision selection.
2. Select the latest pre-cutoff revision in each source lineage.
3. Apply the published source preference policy for the exact estimand.
4. Publish selected_report with the original uncertainty status.
5. Retain alternatives and discrepancy flags; never average incomparable definitions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

METHOD_KEY = "belief.source_select"
METHOD_VERSION = "1.0.0"


@dataclass
class Selection:
    subject_id: UUID
    concept_version_id: UUID
    concept_id: str
    support_id: UUID
    observation_id: UUID
    source_key: str
    value: dict[str, Any]
    uncertainty: dict[str, Any]
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    discrepancy_flags: list[str] = field(default_factory=list)


ELIGIBLE_SQL = """
WITH eligible AS (
  SELECT o.*, s.source_key, c.concept_id,
         row_number() OVER (PARTITION BY o.source_id, o.logical_key ORDER BY o.revision_no DESC) AS rn
  FROM evidence.observation o
  JOIN evidence.source s ON s.id = o.source_id
  JOIN evidence.acquisition a ON a.id = o.acquisition_id
  JOIN ontology.concept_version c ON c.id = o.concept_version_id
  WHERE o.recorded_at <= :cutoff
    AND a.origin = ANY(:origins)
    AND evidence.is_eligible(o.connector_version_id, o.concept_version_id, o.subject_id, :cutoff)
    AND NOT EXISTS (SELECT 1 FROM evidence.observation_retraction r
                    WHERE r.observation_id = o.id AND r.recorded_at <= :cutoff)
    AND (CAST(:concepts AS text[]) IS NULL OR c.concept_id = ANY(CAST(:concepts AS text[])))
)
SELECT id, subject_id, concept_version_id, concept_id, support_id, source_key, value, uncertainty, logical_key, source_flags,
       revision_no
FROM eligible WHERE rn = 1
"""


def eligible_latest(
    conn: Connection, cutoff: datetime, origins: list[str], concepts: list[str] | None = None
) -> list[dict[str, Any]]:
    """Latest pre-cutoff revision per lineage among eligible observations.

    Note the revision filter is applied *after* restricting to recorded_at <= cutoff,
    so a later correction can never alter an earlier as-of answer.
    """
    rows = conn.execute(
        text(ELIGIBLE_SQL), {"cutoff": cutoff, "origins": origins, "concepts": concepts}
    ).mappings()
    return [dict(r) for r in rows]


def select(rows: list[dict[str, Any]], preferences: dict[str, list[str]]) -> list[Selection]:
    groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["subject_id"], r["concept_version_id"], r["support_id"])].append(r)
    out: list[Selection] = []
    for (subject, concept, support), cands in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        concept_id = cands[0]["concept_id"]
        prefs = preferences.get(concept_id)
        if not prefs:
            continue  # no published preference policy for this estimand: not selected
        ranked = sorted(
            (c for c in cands if c["source_key"] in prefs),
            key=lambda c: (prefs.index(c["source_key"]), c["logical_key"]),
        )
        if not ranked:
            continue
        chosen = ranked[0]
        sel = Selection(
            subject_id=subject,
            concept_version_id=concept,
            concept_id=concept_id,
            support_id=support,
            observation_id=chosen["id"],
            source_key=chosen["source_key"],
            value=dict(chosen["value"]),
            uncertainty=dict(chosen["uncertainty"]),
        )
        for alt in ranked[1:]:
            sel.alternatives.append(
                {"observation_id": str(alt["id"]), "source_key": alt["source_key"], "value": alt["value"]}
            )
            if alt["value"] != chosen["value"]:
                sel.discrepancy_flags.append(f"source_disagreement:{alt['source_key']}")
        if len(ranked) > 1 and len({c["source_key"] for c in ranked}) == 1:
            sel.discrepancy_flags.append("multiple_lineages_same_source")
        out.append(sel)
    return out
