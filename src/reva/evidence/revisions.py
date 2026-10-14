"""Revision lineage semantics.

* logical_key = source dataset + series dimensions + valid support. It excludes
  retrieval time and the numeric value.
* Re-fetching an unchanged latest semantic state creates no revision.
* A changed state appends revision n+1 superseding n, including a return to an
  earlier value after an intervening revision (A -> B -> A gives 3 revisions).
* The semantic hash is never globally unique.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from reva.kernel.ids import logical_hash
from reva.ports.connectors import ParsedObservation


def semantic_hash(obs: ParsedObservation) -> str:
    return logical_hash(
        {
            "concept": [obs.concept_id, obs.concept_version],
            "subject": [obs.subject_provider, obs.subject_key],
            "support": obs.support.digest(),
            "value": obs.value,
            "uncertainty": obs.uncertainty.model_dump(mode="json"),
            "source_flags": obs.source_flags,
            "source_revision_id": obs.source_revision_id,
            "source_published_at": obs.source_published_at,
            "source_available_at": obs.source_available_at,
        }
    )


@dataclass(frozen=True)
class LineageHead:
    observation_id: UUID
    revision_no: int
    semantic_hash: str


@dataclass(frozen=True)
class RevisionDecision:
    action: str  # "insert" | "unchanged"
    revision_no: int
    supersedes_id: UUID | None


def decide_revision(latest: LineageHead | None, new_semantic_hash: str) -> RevisionDecision:
    if latest is None:
        return RevisionDecision("insert", 1, None)
    if latest.semantic_hash == new_semantic_hash:
        return RevisionDecision("unchanged", latest.revision_no, latest.observation_id)
    return RevisionDecision("insert", latest.revision_no + 1, latest.observation_id)
