"""V3 causality (agent CAUSCORE): sourced causal claims, their grades, and quarantined candidates.

REVA never asserts causation: it stores what sources assert (``kg.causal_claim``), checks it against time,
counts independent corroboration, keeps dissent and grades it A–E with declared rules
(``registry/causal/grading.yaml``). Interface: ``docs/dev/v3_causal_interface.md``.
"""

from __future__ import annotations

from typing import Any

from reva.causal.grader import GradeReport, grade_claims
from reva.causal.model import (
    CandidateIn,
    CandidateReport,
    ClaimLoadReport,
    Codebook,
    DatasetClaim,
    insert_candidates,
    load_dataset_claims,
    load_kg_claims,
)

__all__ = [
    "CandidateIn",
    "CandidateReport",
    "ClaimLoadReport",
    "Codebook",
    "DatasetClaim",
    "GradeReport",
    "grade_claims",
    "insert_candidates",
    "load_dataset_claims",
    "load_kg_claims",
    "refresh",
]


def refresh(engine: Any, store: Any = None, *, origin: str | None = None) -> dict[str, Any]:
    """Load causal claims from the KG current view, then grade every current claim (worker)."""
    loaded = load_kg_claims(engine, origin=origin, store=store)  # refs/qualifiers verbatim from raw (audit L4)
    graded = grade_claims(engine, origin=origin)
    return {"loaded": loaded.as_dict(), "graded": graded.as_dict()}
