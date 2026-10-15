"""Normative standard-library-only autonomy ports.

Copy into src/reva/ports/autonomy.py, adapting imports to kernel DTOs.
Protocols specify capabilities; the V1 implementations described in the
implementation prompt must supply real behaviour and contract tests.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, TypeAlias
from uuid import UUID

JSON: TypeAlias = (  # noqa: UP040 - normative port text kept verbatim
    dict[str, "JSON"] | list["JSON"] | str | int | float | bool | None
)


@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    namespace: str
    media_type: str


@dataclass(frozen=True)
class ScopedContext:
    context_id: UUID
    mode: Literal["evidence", "belief", "simulation", "evaluation", "population"]
    knowledge_cutoff: datetime
    ontology_manifest: ArtifactRef


@dataclass(frozen=True)
class ResourceBudget:
    max_requests: int
    max_bytes: int
    max_wall_seconds: int
    max_cpu_seconds: int
    max_memory_bytes: int
    max_cost_usd: str


@dataclass(frozen=True)
class KnowledgeGap:
    gap_id: UUID
    requirement_id: str
    concept_version: str
    geography: tuple[str, ...]
    temporal_support: Mapping[str, JSON]
    reason: str
    priority: float
    priority_method: str
    context: ScopedContext


@dataclass(frozen=True)
class SourceCandidate:
    candidate_id: UUID
    provider: str
    catalogue_url: str
    dataset_id: str
    metadata: ArtifactRef
    rights_status: Literal["verified", "unknown", "restricted"]
    allowed_hosts: tuple[str, ...]


@dataclass(frozen=True)
class MappingProposal:
    proposal_id: UUID
    source_metadata: ArtifactRef
    target_concept_version: str
    relation: Literal[
        "exact", "narrower", "broader", "overlap",
        "weighted_crosswalk", "approximate_transform", "incompatible"
    ]
    mapping_manifest: ArtifactRef
    review_required: bool


@dataclass(frozen=True)
class ConnectorProposal:
    proposal_id: UUID
    candidate: SourceCandidate
    template_id: str
    template_digest: str
    manifest: ArtifactRef
    mapping: MappingProposal


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Literal["info", "warning", "blocker"]
    message: str
    evidence: tuple[ArtifactRef, ...]


@dataclass(frozen=True)
class ValidationReport:
    report_id: UUID
    outcome: Literal["pass", "fail", "blocked"]
    findings: tuple[Finding, ...]
    evidence_manifest: ArtifactRef
    evaluator_version: str
    resource_usage: Mapping[str, JSON]


@dataclass(frozen=True)
class HealthReport:
    connector_version_id: UUID
    observed_at: datetime
    state: Literal["healthy", "degraded", "quarantined", "unknown"]
    dimensions: Mapping[str, JSON]
    evidence: ArtifactRef


@dataclass(frozen=True)
class EvidenceCandidate:
    candidate_id: UUID
    card: ArtifactRef
    source_spans: tuple[Mapping[str, JSON], ...]
    status: Literal["candidate"]
    unsupported_fields: tuple[str, ...]


@dataclass(frozen=True)
class ModelDescriptor:
    model_version_id: UUID
    model_key: str
    version: str
    manifest: ArtifactRef
    applicability: Mapping[str, JSON]


@dataclass(frozen=True)
class EvaluationSpec:
    spec_id: UUID
    manifest: ArtifactRef
    mode: Literal[
        "operational_as_known", "available_then_reconstruction",
        "revised_data_retrospective", "analytic_verification"
    ]
    context: ScopedContext


@dataclass(frozen=True)
class EvaluationReport:
    evaluation_id: UUID
    model: ModelDescriptor
    metrics: Mapping[str, JSON]
    predictions: ArtifactRef
    targets: ArtifactRef
    provenance: ArtifactRef
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class ValueEstimate:
    kind: Literal["decision_evsi", "uncertainty_reduction_proxy", "unsupported"]
    value: str | None
    unit: str | None
    uncertainty: Mapping[str, JSON]
    assumptions: ArtifactRef
    method_version: str


@dataclass(frozen=True)
class PlatformChangeProposal:
    proposal_id: UUID
    author: str
    permission_tier: Literal[0, 1, 2, 3]
    changed_paths: tuple[str, ...]
    base_commit: str
    patch_or_manifest: ArtifactRef
    requested_capabilities: tuple[str, ...]
    scientific_change: bool
    migration_required: bool
    expected_benefit: ValueEstimate
    policy_version: str


@dataclass(frozen=True)
class SandboxPolicy:
    policy_version: str
    isolation: Literal["trusted_template", "external_disposable_vm"]
    network_hosts: tuple[str, ...]
    writable_paths: tuple[str, ...]
    secret_access: Literal[False]
    budget: ResourceBudget


@dataclass(frozen=True)
class GateDecision:
    decision_id: UUID
    outcome: Literal["promote", "reject", "needs_review", "blocked"]
    proposal_id: UUID
    policy_version: str
    reports: tuple[ArtifactRef, ...]
    reasons: tuple[str, ...]
    reviewer_identity: str
    signed_receipt: ArtifactRef | None


class SourceDiscoverer(Protocol):
    async def discover(
        self, gap: KnowledgeGap, budget: ResourceBudget
    ) -> Sequence[SourceCandidate]: ...


class OntologyMapper(Protocol):
    def propose_mapping(
        self, candidate: SourceCandidate, context: ScopedContext
    ) -> MappingProposal: ...


class ConnectorGenerator(Protocol):
    def generate(
        self, candidate: SourceCandidate, mapping: MappingProposal,
        approved_template_ids: tuple[str, ...]
    ) -> ConnectorProposal: ...


class ConnectorValidator(Protocol):
    async def validate(
        self, proposal: ConnectorProposal, fixtures: ArtifactRef,
        budget: ResourceBudget
    ) -> ValidationReport: ...


class ConnectorHealthMonitor(Protocol):
    def assess(
        self, connector_version_id: UUID, run_history: ArtifactRef,
        expected_schedule: Mapping[str, JSON], now: datetime
    ) -> HealthReport: ...


class EvidenceExtractor(Protocol):
    def extract(
        self, document: ArtifactRef, context: ScopedContext
    ) -> Sequence[EvidenceCandidate]: ...


class DataQualityEvaluator(Protocol):
    def evaluate(
        self, observation_batch: ArtifactRef, data_contract: ArtifactRef
    ) -> ValidationReport: ...


class ModelRegistry(Protocol):
    def register(
        self, descriptor: ModelDescriptor, approval: GateDecision
    ) -> ModelDescriptor: ...

    def eligible(
        self, task: Mapping[str, JSON], context: ScopedContext
    ) -> Sequence[ModelDescriptor]: ...


class ModelEvaluator(Protocol):
    def evaluate(
        self, model: ModelDescriptor, predictions: ArtifactRef,
        targets: ArtifactRef, specification: EvaluationSpec
    ) -> EvaluationReport: ...


class BacktestRunner(Protocol):
    async def run(
        self, model: ModelDescriptor, specification: EvaluationSpec,
        budget: ResourceBudget
    ) -> EvaluationReport: ...


class KnowledgeGapDetector(Protocol):
    def detect(
        self, requirements: ArtifactRef, coverage_snapshot: ArtifactRef,
        context: ScopedContext
    ) -> Sequence[KnowledgeGap]: ...


class ValueOfInformationEstimator(Protocol):
    def estimate(
        self, gap: KnowledgeGap, acquisition_plan: ArtifactRef,
        decision_spec: ArtifactRef | None, budget: ResourceBudget
    ) -> ValueEstimate: ...


class ModelChallenger(Protocol):
    def propose(
        self, task: Mapping[str, JSON], incumbent: ModelDescriptor,
        context: ScopedContext, budget: ResourceBudget
    ) -> Sequence[PlatformChangeProposal]: ...


class SandboxRunner(Protocol):
    async def run(
        self, proposal: PlatformChangeProposal, policy: SandboxPolicy
    ) -> ValidationReport: ...


class PromotionGate(Protocol):
    def decide(
        self, proposal: PlatformChangeProposal,
        independent_reports: Sequence[ValidationReport],
        signed_policy: ArtifactRef
    ) -> GateDecision: ...

