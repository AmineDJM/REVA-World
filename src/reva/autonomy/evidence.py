"""EvidenceExtractor V1.

* ``StructuredCardImporter``: imports structured (JSON/YAML) evidence cards,
  validates each against the EvidenceCard schema (``reva.evidence.cards.validate_card``)
  and checks that every source span quote occurs verbatim in the supplied source
  text for that span's artifact kind. Output is always ``status='candidate'``;
  review is a separate human step.
* ``LlmEvidenceExtractor``: optional adapter, disabled by default
  (REVA_LLM_ENABLED=false, monthly budget 0). It returns a typed blocked result
  and can never produce anything but candidates. No provider integration is
  shipped in V1, so even when enabled it is blocked rather than pretending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from reva.autonomy.common import ArtifactIO, stable_id
from reva.autonomy.policy import AutonomyGuard
from reva.evidence.cards import NOT_EXTRACTED, validate_card
from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.ports.autonomy import ArtifactRef, EvidenceCandidate, ScopedContext


@dataclass(frozen=True)
class ExtractionOutcome:
    status: Literal["candidates", "blocked"]
    candidates: tuple[EvidenceCandidate, ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    extractor: str = ""
    notes: tuple[str, ...] = field(default=())


class StructuredCardImporter:
    EXTRACTOR = "evidence-import:structured@1"

    def __init__(self, io_: ArtifactIO) -> None:
        self.io = io_

    def _document(self, document: ArtifactRef) -> dict[str, Any]:
        raw = self.io.get_bytes(document)
        doc = yaml.safe_load(raw.decode("utf-8"))  # JSON is valid YAML
        if not isinstance(doc, dict) or not isinstance(doc.get("cards"), list):
            raise ValidationProblem("structured evidence document must contain a 'cards' list")
        return doc

    def run(self, document: ArtifactRef, context: ScopedContext) -> ExtractionOutcome:
        doc = self._document(document)
        sources: dict[str, str] = {str(k): str(v) for k, v in (doc.get("source_text") or {}).items()}
        out: list[EvidenceCandidate] = []
        rejected: list[dict[str, Any]] = []
        for i, raw_card in enumerate(doc["cards"]):
            try:
                card = validate_card(dict(raw_card))
            except ValidationProblem as e:
                rejected.append({"index": i, "reason": e.message[:500]})
                continue
            bad_spans = [s.locator for s in card.source_spans
                         if s.artifact not in sources or s.quote not in sources[s.artifact]]
            if bad_spans:
                rejected.append({"index": i, "claim_id": card.claim_id,
                                 "reason": f"span quotes not found verbatim in supplied source text: {bad_spans}"})
                continue
            data = card.model_dump(mode="json")
            unsupported = tuple(sorted(k for k, v in data.items() if v == NOT_EXTRACTED or v is None))
            ref = self.io.put_json({"schema": "reva-evidence-candidate/1", "status": "candidate",
                                    "card": data, "document": document.sha256,
                                    "context_id": str(context.context_id), "extractor": self.EXTRACTOR}, "models")
            out.append(EvidenceCandidate(
                candidate_id=stable_id("evidence-candidate", document.sha256, card.claim_id,
                                       str(card.claim_version)),
                card=ref,
                source_spans=tuple({"artifact": s.artifact, "locator": s.locator, "quote": s.quote}
                                   for s in card.source_spans),
                status="candidate",
                unsupported_fields=unsupported,
            ))
        return ExtractionOutcome("candidates", tuple(out), tuple(rejected), extractor=self.EXTRACTOR)

    def extract(self, document: ArtifactRef, context: ScopedContext) -> tuple[EvidenceCandidate, ...]:
        return self.run(document, context).candidates


class LlmEvidenceExtractor:
    EXTRACTOR = "evidence-extract:llm-candidate@0"

    def __init__(self, guard: AutonomyGuard, provider: Any = None) -> None:
        self.guard = guard
        self.provider = provider  # no V1 provider adapter exists

    def run(self, document: ArtifactRef, context: ScopedContext) -> ExtractionOutcome:
        reason = self.guard.llm_blocked_reason()
        if reason is None and self.provider is None:
            reason = "blocked: no LLM provider adapter is implemented in V1"
        if reason is not None:
            return ExtractionOutcome("blocked", reason=reason, extractor=self.EXTRACTOR,
                                     notes=("an LLM can only ever produce candidate cards with exact spans",))
        raise CapabilityGap("LLM extraction provider adapters are not part of V1")  # pragma: no cover

    def extract(self, document: ArtifactRef, context: ScopedContext) -> tuple[EvidenceCandidate, ...]:
        out = self.run(document, context)
        if out.status == "blocked":
            raise CapabilityGap(out.reason or "blocked", extractor=self.EXTRACTOR)
        return out.candidates  # pragma: no cover
