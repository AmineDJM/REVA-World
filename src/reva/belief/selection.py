"""source_select_v1: a registered belief-publication method.

1. Filter to eligible, semantically compatible observations at the cutoff
   (recorded at/before cutoff, same origin class as the release).
2. Select the latest pre-cutoff revision in each source lineage (source, origin,
   logical key). A lineage whose latest revision is retracted is excluded and
   flagged (never silently replaced by the superseded value); an ineligible
   latest revision falls back to the latest eligible one, flagged
   ``superseded_revision_selected``.
3. Apply the published source preference policy for the exact estimand.
4. Publish selected_report with the original uncertainty status.
5. Retain alternatives and discrepancy flags; never average incomparable definitions,
   and never pick one of them: candidates that differ on a distinguishing dimension
   (flow, partner) withhold the estimand, recorded in the release manifest.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
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


# Revision selection happens over the lineage's *whole* pre-cutoff history
# (lineage = source, origin, logical key), and only then are retraction and
# eligibility applied (R-27):
# * a lineage whose latest pre-cutoff revision is retracted is 'retracted': it is
#   excluded and never silently falls back to the superseded value;
# * when the latest revision is ineligible (e.g. its connector version is
#   quarantined) the latest eligible, non-retracted earlier revision is used and
#   flagged 'superseded_revision_selected'.
# The concept filter is applied to the selected row, not before revision
# selection, so a lineage re-mapped to a new concept is not resurrected under the
# old one.
# Eligibility is evidence.observation_is_eligible, the same function the
# belief.input trigger applies (R5-03): subject, support reference year
# (world.support_ref_year; unknown never satisfies a years scope) and
# classification. A row that is out of scope is therefore not selected (its
# lineage falls back or is skipped) instead of aborting the whole release.
ELIGIBLE_SQL = """
WITH visible AS (
  SELECT o.id, o.source_id, o.origin, o.logical_key, o.revision_no, o.connector_version_id,
         o.concept_version_id, o.subject_id, o.support_id, o.value, o.uncertainty, o.source_flags,
         row_number() OVER (PARTITION BY o.source_id, o.origin, o.logical_key ORDER BY o.revision_no DESC) AS rn
  FROM evidence.observation o
  WHERE o.recorded_at <= :cutoff
    AND o.origin = ANY(:origins)
),
annotated AS (
  SELECT v.*, s.source_key, s.lineage_family, c.concept_id,
         EXISTS (SELECT 1 FROM evidence.observation_retraction r
                 WHERE r.observation_id = v.id AND r.recorded_at <= :cutoff) AS retracted,
         evidence.observation_is_eligible(v.id, :cutoff) AS eligible
  FROM visible v
  JOIN evidence.source s ON s.id = v.source_id
  JOIN ontology.concept_version c ON c.id = v.concept_version_id
),
heads AS (
  SELECT source_id, origin, logical_key, revision_no AS head_revision_no, retracted AS head_retracted, id AS head_id
  FROM annotated WHERE rn = 1
),
chosen AS (
  SELECT DISTINCT ON (a.source_id, a.origin, a.logical_key) a.*, h.head_revision_no
  FROM annotated a
  JOIN heads h USING (source_id, origin, logical_key)
  WHERE NOT h.head_retracted AND NOT a.retracted AND a.eligible
  ORDER BY a.source_id, a.origin, a.logical_key, a.revision_no DESC
),
result AS (
  SELECT id, subject_id, concept_version_id, concept_id, support_id, source_key, lineage_family, origin, value,
         uncertainty, logical_key, source_flags, revision_no,
         CASE WHEN revision_no = head_revision_no THEN 'current' ELSE 'superseded_revision_selected' END
           AS selection_status
  FROM chosen
  UNION ALL
  SELECT a.id, a.subject_id, a.concept_version_id, a.concept_id, a.support_id, a.source_key, a.lineage_family,
         a.origin, a.value, a.uncertainty, a.logical_key, a.source_flags, a.revision_no,
         'retracted' AS selection_status
  FROM annotated a JOIN heads h ON h.head_id = a.id
  WHERE h.head_retracted AND CAST(:include_retracted AS boolean)
)
SELECT * FROM result
WHERE (CAST(:concepts AS text[]) IS NULL OR concept_id = ANY(CAST(:concepts AS text[])))
ORDER BY concept_id, logical_key, origin
"""


def eligible_latest(
    conn: Connection,
    cutoff: datetime,
    origins: list[str],
    concepts: list[str] | None = None,
    *,
    include_retracted: bool = False,
) -> list[dict[str, Any]]:
    """Selected pre-cutoff revision per lineage (source, origin, logical key).

    Only rows recorded at/before the cutoff are considered, so a later correction
    can never alter an earlier as-of answer. Each row carries ``selection_status``:
    ``current`` (the lineage's latest pre-cutoff revision), ``superseded_revision_selected``
    (the latest revision is ineligible, an earlier eligible one is used) or, only
    with ``include_retracted=True``, ``retracted`` (the latest revision is retracted;
    the lineage is excluded from selection and must not be used as a value).
    """
    rows = conn.execute(
        text(ELIGIBLE_SQL),
        {"cutoff": cutoff, "origins": origins, "concepts": concepts, "include_retracted": include_retracted},
    ).mappings()
    return [dict(r) for r in rows]


# Dimensions that make two candidate lineages of one estimand different
# quantities, not disagreeing reports of the same quantity (R-05, R3-08): when
# candidates differ on one of them, no value is published for the estimand.
DISTINGUISHING_FLAGS = ("flowCode", "partnerCode")


@dataclass
class Withheld:
    """An estimand for which no value is published (R3-08): its candidate lineages
    differ on a distinguishing dimension, so they are different quantities and
    picking any one would be arbitrary."""

    subject_id: UUID
    concept_version_id: UUID
    concept_id: str
    support_id: UUID
    reason: str
    discrepancy_flags: list[str]
    alternatives: list[dict[str, Any]]
    details: dict[str, Any] | None = None  # reason-specific context (e.g. the publication pause)

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"subject_id": str(self.subject_id), "concept_id": self.concept_id,
                               "support_id": str(self.support_id), "reason": self.reason,
                               "discrepancy_flags": self.discrepancy_flags, "alternatives": self.alternatives}
        if self.details is not None:
            out["details"] = self.details
        return out


@dataclass
class SelectionResult:
    selections: list[Selection]
    withheld: list[Withheld]


def _same_value(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Value equality; decimals compare numerically ("100" == "100.0")."""
    if a.get("kind") == b.get("kind") == "decimal":
        try:
            rest_a = {k: v for k, v in a.items() if k != "value"}
            rest_b = {k: v for k, v in b.items() if k != "value"}
            return bool(Decimal(str(a.get("value"))) == Decimal(str(b.get("value"))) and rest_a == rest_b)
        except InvalidOperation:
            return a == b
    return a == b


def _independence(alt: dict[str, Any], chosen: dict[str, Any]) -> dict[str, Any]:
    """Invariant 13: an alternative from the same source lineage family as the selected
    value is never independent corroboration (it shares upstream data/processing)."""
    fam_alt, fam_chosen = alt.get("lineage_family"), chosen.get("lineage_family")
    same = fam_alt is None or fam_chosen is None or fam_alt == fam_chosen
    return {"lineage_family": fam_alt, "independence": "same_family" if same else "independent_family"}


def select(rows: list[dict[str, Any]], preferences: dict[str, list[str]]) -> list[Selection]:
    return select_report(rows, preferences).selections


def select_report(rows: list[dict[str, Any]], preferences: dict[str, list[str]]) -> SelectionResult:
    """source_select_v1 over ``eligible_latest`` rows: selections plus withheld estimands."""
    groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["subject_id"], r["concept_version_id"], r["support_id"])].append(r)
    out: list[Selection] = []
    withheld: list[Withheld] = []
    for (subject, concept, support), group in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        concept_id = group[0]["concept_id"]
        prefs = preferences.get(concept_id)
        if not prefs:
            continue  # no published preference policy for this estimand: not selected
        # (R7-B1) deterministic order independent of input row order: stored estimates and release
        # manifests are byte-identical for any caller's row order.
        retracted = sorted(
            (c for c in group if c.get("selection_status") == "retracted" and c["source_key"] in prefs),
            key=lambda c: (prefs.index(c["source_key"]), c["logical_key"], str(c.get("origin", ""))),
        )
        cands = [c for c in group if c.get("selection_status") != "retracted"]
        ranked = sorted(
            (c for c in cands if c["source_key"] in prefs),
            key=lambda c: (prefs.index(c["source_key"]), c["logical_key"], str(c.get("origin", ""))),
        )
        if not ranked:
            continue  # nothing usable (e.g. every lineage retracted): no estimate, never a stale fallback
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
        if chosen.get("selection_status") == "superseded_revision_selected":
            sel.discrepancy_flags.append("superseded_revision_selected")
        for alt in ranked[1:]:
            entry = {"observation_id": str(alt["id"]), "source_key": alt["source_key"], "value": alt["value"],
                     **_independence(alt, chosen)}
            if alt.get("selection_status") == "superseded_revision_selected":
                entry["selection_status"] = "superseded_revision_selected"
            sel.alternatives.append(entry)
            if not _same_value(alt["value"], chosen["value"]):
                sel.discrepancy_flags.append(f"source_disagreement:{alt['source_key']}")
        same_source = [c for c in ranked if c["source_key"] == chosen["source_key"]]
        if len(same_source) > 1:
            sel.discrepancy_flags.append("multiple_lineages_same_source")
        incompatible = [
            f"incompatible_lineages:{dim}" for dim in DISTINGUISHING_FLAGS
            if len({str((c.get("source_flags") or {}).get(dim)) for c in ranked}) > 1
        ]
        if incompatible:
            # Different quantities under one estimand (R-05/R3-08): never pick one arbitrarily.
            withheld.append(Withheld(
                subject_id=subject, concept_version_id=concept, concept_id=concept_id, support_id=support,
                reason="incompatible_lineages", discrepancy_flags=incompatible,
                alternatives=[{"observation_id": str(c["id"]), "source_key": c["source_key"],
                               "logical_key": c["logical_key"], "value": c["value"],
                               **{d: (c.get("source_flags") or {}).get(d) for d in DISTINGUISHING_FLAGS}}
                              for c in ranked],
            ))
            continue
        for r in retracted:
            # The retracted lineage is excluded; its value is not carried forward.
            sel.alternatives.append({"observation_id": str(r["id"]), "source_key": r["source_key"],
                                     "logical_key": r["logical_key"], "selection_status": "retracted",
                                     **_independence(r, chosen)})
            sel.discrepancy_flags.append(f"retracted_lineage_excluded:{r['source_key']}")
        sel.discrepancy_flags = list(dict.fromkeys(sel.discrepancy_flags))
        out.append(sel)
    return SelectionResult(out, withheld)
