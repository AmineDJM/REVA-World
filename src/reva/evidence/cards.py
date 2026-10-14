"""Evidence cards: versioned scientific records, not LLM summaries with scores.

V1: curated cards, DOI metadata retrieval, a structured manual import path and a
deterministic schema validator. Missing values stay missing (explicit reason).
Automated/LLM extraction can only ever produce candidate cards with exact spans.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, Engine, text

from reva.adapters.postgres import repositories as repo
from reva.evidence.ingest import PublishSummary
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.ports.connectors import ObservationBatch, RawAcquisition
from reva.ports.storage import ArtifactStore

CARD_DIR = Path(__file__).resolve().parents[3] / "registry" / "evidence"
NOT_EXTRACTED = {"missing": "not_extracted"}


class SourceSpan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact: Literal["crossref_metadata", "full_text", "abstract", "table"]
    locator: str  # JSON pointer / page+table+cell / byte offsets
    quote: str = Field(min_length=1)


class EvidenceCard(BaseModel):
    """All fields required by the architecture; unknowns are explicit missing markers."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    claim_id: str
    claim_version: int = Field(ge=1)
    question: str
    estimand: Any
    treatment: Any
    comparator: Any
    outcome: Any
    population: Any
    geography: Any
    period: Any
    horizon: Any
    units: Any
    effect_scale: Any
    study_ids: list[str]
    dataset_families: Any
    sample_size: Any
    design: Any
    coefficient: Any  # coefficient / interval / artifact; never invented
    standard_error: Any
    covariance_ref: Any
    identification_assumptions: Any
    confounders: Any
    selection: Any
    interference: Any
    measurement_validity: Any
    risk_of_bias: Any
    robustness: Any
    replication_status: Any
    publication_status: Any
    retraction_status: Any
    external_validity: Any
    mechanism: Any
    competing_explanations: Any
    source_spans: list[SourceSpan] = Field(min_length=1)
    extraction_method: str
    extraction_version: str
    human_review: Any
    licence: str
    evidence_kind: Literal["experimental", "quasi_experimental", "observational", "mechanistic",
                           "structural_calibration", "expert_judgment", "hypothesis"]
    ratings: dict[str, Any]
    relevance_to_reva: str
    supersedes_id: str | None = None

    @model_validator(mode="after")
    def _no_fabrication(self) -> EvidenceCard:
        if self.extraction_method.startswith("llm") and self.human_review not in (None, NOT_EXTRACTED):
            raise ValueError("LLM extraction cannot carry a human review in the same record")
        for name in ("coefficient", "standard_error"):
            v = getattr(self, name)
            if v not in (None, NOT_EXTRACTED) and not any(s.artifact in ("full_text", "table") for s in self.source_spans):
                raise ValueError(f"{name} requires a full-text/table source span; metadata cannot support it")
        if self.evidence_kind == "hypothesis" and self.coefficient not in (None, NOT_EXTRACTED):
            raise ValueError("a hypothesis cannot carry a causal coefficient")
        return self


def validate_card(data: dict[str, Any]) -> EvidenceCard:
    try:
        return EvidenceCard.model_validate(data)
    except Exception as e:  # pydantic ValidationError
        raise ValidationProblem(f"invalid evidence card: {e}") from None


def publish_study(
    engine: Engine, store: ArtifactStore, raw: RawAcquisition, batches: list[ObservationBatch], summary: PublishSummary
) -> str | None:
    study = batches[0].metadata["study"] if batches else None
    if not study:
        return None
    with engine.begin() as conn:
        latest = conn.execute(
            text("SELECT metadata FROM evidence.study WHERE lower(doi)=:d ORDER BY recorded_at DESC LIMIT 1"),
            {"d": study["doi"]},
        ).scalar_one_or_none()
        stable = {k: v for k, v in study.items() if k not in ("crossref_indexed", "is_referenced_by_count")}
        if latest is not None and logical_hash(
            {k: v for k, v in latest.items() if k not in ("crossref_indexed", "is_referenced_by_count")}
        ) == logical_hash(stable):
            return None
        act = repo.simple_activity("study.metadata", "normalisation", "worker:crossref", {"doi": study["doi"]})
        repo.record_activity(conn, act, inputs=[(summary.raw_sha256, {"json_pointer": "/message"})])
        sid = conn.execute(
            text(
                """INSERT INTO evidence.study(doi, metadata, source_artifact_sha256, acquisition_id,
                     provenance_activity_id) VALUES (:d, CAST(:m AS jsonb), :s, :acq, :a) RETURNING id"""
            ),
            {"d": study["doi"], "m": json.dumps(study), "s": summary.raw_sha256,
             "acq": summary.acquisition_id, "a": act.id},
        ).scalar_one()
        repo.emit_outbox(conn, "evidence.study_metadata", {"doi": study["doi"], "study_id": str(sid),
                                                            "update_signal": bool(study.get("updated_by"))})
        return str(sid)


def import_card(
    conn: Connection, card: EvidenceCard, *, status: Literal["candidate", "reviewed"], actor: str
) -> str:
    if status == "reviewed" and not actor.startswith(("human:", "maintainer:")):
        raise ValidationProblem("only a named human/maintainer can record a reviewed card")
    if card.extraction_method.startswith("llm") and status != "candidate":
        raise ValidationProblem("LLM-extracted cards are candidate-only")
    existing = conn.execute(
        text("SELECT id, card FROM evidence.claim WHERE claim_key=:k AND version=:v"),
        {"k": card.claim_id, "v": card.claim_version},
    ).mappings().one_or_none()
    payload = card.model_dump(mode="json")
    if existing:
        if logical_hash(existing["card"]) != logical_hash(payload):
            raise ValidationProblem("claim version already published with different content; bump claim_version")
        return str(existing["id"])
    act = repo.simple_activity("evidence.card_import", "review", actor, {"claim": card.claim_id,
                                                                         "version": card.claim_version})
    repo.record_activity(conn, act)
    cid = conn.execute(
        text(
            """INSERT INTO evidence.claim(claim_key, version, card, evidence_kind, status, extraction_method,
                 provenance_activity_id) VALUES (:k, :v, CAST(:c AS jsonb), :ek, :st, :em, :a) RETURNING id"""
        ),
        {"k": card.claim_id, "v": card.claim_version, "c": json.dumps(payload), "ek": card.evidence_kind,
         "st": status, "em": card.extraction_method, "a": act.id},
    ).scalar_one()
    return str(cid)


def load_curated_cards() -> list[tuple[EvidenceCard, str]]:
    out = []
    for p in sorted(CARD_DIR.glob("*.yaml")):
        doc = yaml.safe_load(p.read_text())
        out.append((validate_card(doc["card"]), doc["status"]))
    return out
