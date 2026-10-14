"""Public HTTP contract: request and response models for every /v1 endpoint.

Stable names: the UI (agent F) generates its TypeScript client from
``contracts/openapi.json``, which is exported from these models. Change fields
additively; never rename or repurpose an existing field.

Conventions
-----------
* Data endpoints return an ``Envelope`` subclass: ``data`` plus the explanation
  fields required by the architecture (context, modality, units, support,
  baseline release, cutoff, uncertainty, provenance links, warnings, cost...).
* Missing values are typed ``{"kind": "missing", "reason": ...}``; they are never
  0 and never a bare ``null`` standing in for a number.
* Assessments that do not apply (e.g. numerical error of a source report) are
  ``{"status": "not_applicable", "reason": ...}``, not omitted and not zero.
* Timestamps are UTC ISO-8601. IDs are opaque strings/UUIDs.
* Errors are RFC 9457-style problem details (``Problem``), media type
  ``application/problem+json``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


class ApiModel(BaseModel):
    # Response schemas are exported in serialization mode: a field with a
    # default is always present in a response, so it is *required* there
    # (the generated TypeScript types then never mark it optional).
    model_config = ConfigDict(extra="forbid", populate_by_name=True, json_schema_serialization_defaults_required=True)


class OpenModel(BaseModel):
    """Response sub-objects that carry registered but open-ended JSON (e.g. a
    connector's declared capabilities) keep unknown keys."""

    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=True)


# ---------------------------------------------------------------------------
# Typed values (mirror reva.kernel.values; never null-as-zero)
# ---------------------------------------------------------------------------


class DecimalValue(OpenModel):
    kind: Literal["decimal"] = "decimal"
    value: str = Field(description="Exact decimal string; never a binary float")
    unit_ref: str
    basis: dict[str, Any] = Field(default_factory=dict, description="Explicit measurement basis")


class FloatValue(OpenModel):
    kind: Literal["float"] = "float"
    value: float
    unit_ref: str
    basis: dict[str, Any] = Field(default_factory=dict)


class MissingValue(OpenModel):
    kind: Literal["missing"] = "missing"
    reason: str = Field(description="Why the value is missing, e.g. unavailable, not_collected")
    detail: str | None = None


class SensitivityRangeValue(OpenModel):
    """An assumption-sensitivity range over a scenario grid (L2): never a point, never a confidence interval."""

    kind: Literal["sensitivity_range"] = "sensitivity_range"
    lower: str = Field(description="Exact decimal string")
    upper: str = Field(description="Exact decimal string")
    unit_ref: str
    basis: dict[str, Any] = Field(default_factory=dict)
    evaluated_points: int | None = None
    includes_fixed_quantity_point: bool | None = None
    statement: str = Field(description="Assumption sensitivity, not a confidence interval")


class OtherValue(OpenModel):
    """Category, text, entity reference, array, distribution or geometry values."""

    kind: Literal["category", "text", "entity", "array", "distribution", "geometry"]


TypedValue = Annotated[DecimalValue | FloatValue | MissingValue | SensitivityRangeValue | OtherValue,
                       Field(discriminator="kind")]


class Uncertainty(OpenModel):
    """Full uncertainty record as stored (reva.kernel.uncertainty.UncertaintySpec)."""

    status: Literal["unquantified", "exact_by_definition", "quantified"]
    kind: str = "unknown"
    description: str | None = None
    level: float | None = Field(None, description="Nominal coverage; only for probabilistic interval kinds")
    lower: str | None = None
    upper: str | None = None
    dependence_group: str | None = None
    method_version: str | None = None
    conditioning: list[str] = Field(default_factory=list)


class UncertaintySummary(ApiModel):
    """Envelope-level uncertainty. ``status`` may be ``mixed`` when rows differ."""

    status: Literal["unquantified", "exact_by_definition", "quantified", "mixed", "not_applicable"]
    kind: str | None = None
    level: float | None = None
    dependence: str | None = None
    note: str | None = None


class Assessment(ApiModel):
    """An explanation field that may not apply to this result.

    ``not_applicable``: the concept does not apply (e.g. compression error of a
    source report). ``not_assessed``: it applies but no assessment exists yet.
    ``reported``: ``detail`` carries the assessment.
    """

    status: Literal["not_applicable", "not_assessed", "reported"]
    reason: str | None = None
    detail: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class BaselineReleaseRef(ApiModel):
    id: UUID
    sequence: int | None = Field(None, description="Publication sequence (commit order)")
    knowledge_cutoff: datetime
    published_at: datetime | None = None
    origin: Literal["live", "test_fixture"]
    method: str | None = None


class ModelVersionRef(ApiModel):
    id: UUID | None = None
    model_key: str
    version: str
    approval_status: str | None = None


class ProvenanceLink(ApiModel):
    kind: Literal["estimate", "observation", "acquisition", "artifact", "sim_result", "release", "run", "scenario"]
    id: str
    href: str


class ApiWarning(ApiModel):
    code: str
    message: str


class CostReport(ApiModel):
    wall_time_ms: float
    rows: int = 0
    detail: dict[str, Any] | None = None


class Freshness(ApiModel):
    latest_retrieved_at: datetime | None = None
    latest_recorded_at: datetime | None = None
    note: str | None = None


ContextMode = Literal["evidence", "belief", "simulation", "evaluation", "population", "registry", "operations"]
OriginLabel = Literal["live", "test_fixture", "mixed", "none"]
ModalityLabel = Literal[
    "reported_observation",
    "selected_estimate",
    "forecast",
    "identified_causal_estimate",
    "assumption_conditioned_scenario",
    "unsupported",
    "metadata",
    "evaluation",
]


def _na(reason: str) -> Assessment:
    return Assessment(status="not_applicable", reason=reason)


class Envelope(ApiModel):
    """Common response envelope for data endpoints (architecture §19)."""

    data: Any
    context_id: UUID | None = Field(None, description="Context the data belongs to (null for registry metadata)")
    context_mode: ContextMode
    origin: OriginLabel = Field(description="live, test_fixture (labelled fixture data), mixed or none")
    modality: ModalityLabel
    estimand: dict[str, Any] | None = None
    units: list[str] = Field(default_factory=list)
    support: dict[str, Any] | None = None
    baseline_release: BaselineReleaseRef | None = None
    knowledge_cutoff: datetime | None = None
    valid_time: dict[str, Any] | None = Field(None, description="Valid-time window of the returned data")
    model_versions: list[ModelVersionRef] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainty: UncertaintySummary = Field(
        default_factory=lambda: UncertaintySummary(status="not_applicable")
    )
    data_freshness: Freshness | None = None
    evidence_profile: Assessment = Field(default_factory=lambda: _na("no evidence aggregation in this response"))
    applicability: Assessment = Field(default_factory=lambda: _na("no model applied"))
    numerical_error: Assessment = Field(default_factory=lambda: _na("no numerical computation"))
    compression_error: Assessment = Field(default_factory=lambda: _na("no compression applied"))
    model_disagreement: Assessment = Field(default_factory=lambda: _na("single source/method"))
    historical_validation: Assessment = Field(default_factory=lambda: _na("no forecast or model output"))
    provenance_links: list[ProvenanceLink] = Field(default_factory=list)
    warnings: list[ApiWarning] = Field(default_factory=list)
    cost: CostReport | None = None
    next_cursor: str | None = Field(None, description="Opaque keyset cursor; null when no further page")


# ---------------------------------------------------------------------------
# Problems, health, session, jobs
# ---------------------------------------------------------------------------


class Problem(OpenModel):
    """RFC 9457 problem details (application/problem+json).

    Built directly from ``RevaError.problem()`` (not serialized through this
    model), so optional members are genuinely optional on the wire.
    """

    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=False)

    type: str
    title: str
    status: int
    detail: str
    code: str
    instance: str | None = None
    request_id: str | None = None
    details: dict[str, Any] | None = None


class HealthLive(ApiModel):
    status: Literal["ok"]


class HealthCheck(ApiModel):
    name: str
    ok: bool
    detail: str | None = None


class HealthReady(ApiModel):
    status: Literal["ready", "not_ready"]
    checks: list[HealthCheck]


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SessionInfo(ApiModel):
    username: str
    principal: str = Field(description="Audit actor, e.g. human:admin")
    auth_method: Literal["session", "token"]
    scopes: list[str]
    expires_at: datetime | None = None
    csrf_token: str | None = Field(
        None, description="Echo in X-CSRF-Token on unsafe cookie-authenticated requests (also in cookie reva_csrf)"
    )


class JobAccepted(ApiModel):
    job_id: UUID
    job_type: str
    status: str
    status_url: str


class Job(ApiModel):
    id: UUID
    job_type: str
    status: Literal["queued", "running", "succeeded", "failed", "cancel_requested", "cancelled", "dead"]
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    progress: dict[str, Any]
    has_checkpoint: bool
    result_ref: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    finished_at: datetime | None = None
    cancellable: bool
    links: dict[str, str] = Field(default_factory=dict)


class JobListResponse(Envelope):
    data: list[Job]


class JobCancelResult(ApiModel):
    job_id: UUID
    status: str


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class EntityRef(ApiModel):
    id: UUID
    label: str
    kind: str | None = None


class Alias(ApiModel):
    provider: str
    external_key: str
    confidence_kind: str
    mapping_version: str
    valid_range: str | None = None


class EntitySummary(ApiModel):
    id: UUID
    label: str
    kind: str
    namespace: str
    referent_mode: Literal["real", "abstract", "synthetic", "hypothetical"]
    alias_count: int


class EntityComponent(ApiModel):
    """A data facet available for the entity (a concept with source records or
    pinned estimates, display geometry, or relation layer)."""

    component: Literal["observations", "estimates", "display_geometry", "relations"]
    concept_id: str | None = None
    layer: str | None = None
    count: int
    first_period: str | None = None
    last_period: str | None = None


class SupportedContext(ApiModel):
    mode: Literal["evidence", "belief", "simulation"]
    context_id: UUID | None = None
    release_id: UUID | None = None
    origin: str | None = None
    knowledge_cutoff: datetime | None = None
    description: str


class EntityDetail(ApiModel):
    id: UUID
    label: str
    kind: str
    namespace: str
    referent_mode: str
    recorded_at: datetime
    aliases: list[Alias]
    components: list[EntityComponent]
    supported_contexts: list[SupportedContext]


class EntityListResponse(Envelope):
    data: list[EntitySummary]


class EntityDetailResponse(Envelope):
    data: EntityDetail


# ---------------------------------------------------------------------------
# Supports and time
# ---------------------------------------------------------------------------


class SupportSummary(ApiModel):
    id: UUID
    temporal: dict[str, Any] = Field(description="TimeExtent: kind, precision, start, end, label")
    spatial: dict[str, Any]
    population: dict[str, Any]
    aggregation: str
    period_label: str | None = None


# ---------------------------------------------------------------------------
# Relations / events
# ---------------------------------------------------------------------------


class Participant(ApiModel):
    ordinal: int
    role: str
    entity_id: UUID
    entity_label: str
    entity_kind: str
    direction: Literal["in", "out", "undirected"]


class Relation(ApiModel):
    id: UUID
    predicate: str
    layer: str
    support: SupportSummary
    component: dict[str, Any]
    existence_uncertainty: dict[str, Any]
    strength: dict[str, Any] | None = None
    value: TypedValue | None = Field(None, description="Reported value of the evidence record, when any")
    concept_id: str | None = None
    evidence_observation_id: UUID | None = None
    release_id: UUID | None = None
    origin: Literal["live", "test_fixture"] | None = None
    recorded_at: datetime
    participants: list[Participant]


class GraphNode(ApiModel):
    id: UUID
    label: str
    kind: str


class RelationGraph(ApiModel):
    relations: list[Relation]
    nodes: list[GraphNode]
    truncated: bool = Field(description="True when bounds cut the result; never silent")
    max_edges: int
    depth: int
    matched_lower_bound: int = Field(description="Relations matched (exact when not truncated)")


class RelationListResponse(Envelope):
    data: RelationGraph


class Event(ApiModel):
    entity_id: UUID
    label: str
    event_type: str
    support: SupportSummary
    component: dict[str, Any]


class EventListResponse(Envelope):
    data: list[Event]


# ---------------------------------------------------------------------------
# Observations / timeseries
# ---------------------------------------------------------------------------


class Observation(ApiModel):
    id: UUID
    source_key: str
    connector: str
    connector_version: str
    acquisition_id: UUID
    origin: Literal["live", "test_fixture"]
    logical_key: str
    revision_no: int
    supersedes_id: UUID | None = None
    superseded_by_id: UUID | None = Field(None, description="Later revision visible at the as_of time")
    source_revision_id: str | None = None
    subject: EntityRef
    concept_id: str
    concept_version: str
    value: TypedValue
    uncertainty: Uncertainty
    source_flags: dict[str, Any]
    support: SupportSummary
    observed_at: datetime | None = None
    source_published_at: datetime | None = None
    source_available_at: datetime | None = None
    availability_basis: dict[str, Any] | None = None
    retrieved_at: datetime
    recorded_at: datetime
    raw_selector: dict[str, Any]
    parser_version: str
    retracted: bool = False


class ObservationListResponse(Envelope):
    data: list[Observation]


class ConceptInfo(ApiModel):
    concept_id: str
    version: str
    kind: str
    definition: str
    unit_ref: str | None = None
    aggregation: str | None = None
    semantic_profile: dict[str, Any]


class RevisionEntry(ApiModel):
    observation_id: UUID
    revision_no: int
    value: TypedValue
    source_revision_id: str | None = None
    retrieved_at: datetime
    recorded_at: datetime
    source_published_at: datetime | None = None


class TimeseriesPoint(ApiModel):
    period_label: str | None
    valid_start: str | None
    valid_end: str | None
    temporal_kind: str
    aggregation: str
    support_id: UUID
    source_key: str
    logical_key: str
    observation_id: UUID
    revision_no: int
    value: TypedValue
    uncertainty: Uncertainty
    recorded_at: datetime
    retrieved_at: datetime
    source_published_at: datetime | None = None
    origin: Literal["live", "test_fixture"]
    revisions: list[RevisionEntry] | None = Field(
        None, description="Full lineage visible at as_of (revisions=all only)"
    )


class Timeseries(ApiModel):
    subject: EntityRef
    concept: ConceptInfo
    revisions_mode: Literal["latest", "all"]
    as_of: datetime | None = None
    points: list[TimeseriesPoint]


class TimeseriesResponse(Envelope):
    data: Timeseries


# ---------------------------------------------------------------------------
# Beliefs
# ---------------------------------------------------------------------------


class BeliefEstimate(ApiModel):
    id: UUID
    release_id: UUID
    subject: EntityRef
    concept_id: str
    concept_version: str
    support: SupportSummary
    modality: Literal["selected_report", "estimated_state", "forecast"]
    value: TypedValue
    uncertainty: Uncertainty
    alternatives: list[dict[str, Any]]
    discrepancy_flags: list[str]
    source_key: str | None = None
    observation_id: UUID | None = None
    observation_revision_no: int | None = None
    retrieved_at: datetime | None = None
    method: ModelVersionRef


class BeliefListResponse(Envelope):
    data: list[BeliefEstimate]


class ReleaseConceptCount(ApiModel):
    concept_id: str
    estimates: int
    missing: int


class ReleaseArtifact(ApiModel):
    sha256: str
    role: str


class BeliefRelease(ApiModel):
    id: UUID
    sequence: int | None
    published_at: datetime | None
    knowledge_cutoff: datetime
    origin: Literal["live", "test_fixture"]
    method: str
    context_id: UUID
    manifest_sha256: str
    ontology_manifest_sha256: str
    model_manifest_sha256: str
    recorded_at: datetime
    estimate_count: int
    concepts: list[ReleaseConceptCount]
    artifacts: list[ReleaseArtifact]


class BeliefReleaseResponse(Envelope):
    data: BeliefRelease


class BeliefReleaseListResponse(Envelope):
    data: list[BeliefRelease]


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


EvidenceKindLabel = Literal["scientific_card", "study", "source_disagreement"]


class EvidenceItem(ApiModel):
    id: UUID
    kind: EvidenceKindLabel
    title: str
    status: str | None = None
    evidence_kind: str | None = None
    summary: str | None = None
    recorded_at: datetime | None = None


class SourceDisagreement(ApiModel):
    estimate_id: UUID
    release_id: UUID
    subject: EntityRef
    concept_id: str
    support: SupportSummary
    chosen: dict[str, Any]
    alternatives: list[dict[str, Any]]
    discrepancy_flags: list[str]
    note: str


class EvidenceDetail(ApiModel):
    id: UUID
    kind: EvidenceKindLabel
    title: str
    status: str | None = None
    evidence_kind: str | None = None
    card: dict[str, Any] | None = None
    claim_key: str | None = None
    claim_version: int | None = None
    study: dict[str, Any] | None = None
    effects: list[dict[str, Any]] = Field(default_factory=list)
    disagreement: SourceDisagreement | None = None
    recorded_at: datetime | None = None


class EvidenceListResponse(Envelope):
    data: list[EvidenceItem]


class EvidenceDetailResponse(Envelope):
    data: EvidenceDetail


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


ProvenanceKind = Literal["estimate", "observation", "acquisition", "artifact", "sim_result", "release", "run",
                         "scenario"]


class ProvenanceNode(ApiModel):
    id: str
    kind: Literal[
        "estimate",
        "release",
        "observation",
        "activity",
        "acquisition",
        "artifact",
        "storage_location",
        "connector_version",
        "source",
        "sim_result",
        "run",
        "scenario",
        "context",
        "model_version",
        "claim",
    ]
    label: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class ProvenanceEdge(ApiModel):
    source: str
    target: str
    relation: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class ProvenanceGraph(ApiModel):
    root: str
    nodes: list[ProvenanceNode]
    edges: list[ProvenanceEdge]
    truncated: bool
    max_nodes: int


class ProvenanceResponse(Envelope):
    data: ProvenanceGraph


# ---------------------------------------------------------------------------
# Locations (GeoJSON)
# ---------------------------------------------------------------------------


class GeoFeatureProperties(ApiModel):
    feature_key: str
    entity_id: UUID | None = None
    entity_label: str | None = None
    match_status: Literal["reviewed_match", "unmatched", "disputed", "overseas_part"]
    name: str | None = None
    dataset_sha256: str
    display_only: Literal[True] = True


class GeoFeature(ApiModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: dict[str, Any]
    properties: GeoFeatureProperties


class FeatureCollection(ApiModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoFeature]
    display_note: str
    truncated: bool


class LocationResponse(Envelope):
    data: FeatureCollection


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelVersionOut(ApiModel):
    id: UUID
    model_key: str
    version: str
    kind: str
    approval_status: str
    applicability: Any
    assumptions: Any
    input_ports: Any
    output_ports: Any
    training_cutoff: datetime | None = None
    spec_sha256: str
    recorded_at: datetime
    evaluation_count: int


class ModelEvaluation(ApiModel):
    id: UUID
    model_version_id: UUID
    model_key: str
    model_version: str
    context_id: UUID
    origin: Literal["live", "test_fixture"]
    replay_mode: str
    replay_mode_label: str = Field(
        description="Human label of the replay mode, e.g. 'revised-data retrospective — not a real-time backtest'"
    )
    task_scope: dict[str, Any]
    metrics: dict[str, Any]
    evaluation_manifest_sha256: str
    recorded_at: datetime
    promotions: list[dict[str, Any]] = Field(default_factory=list)
    is_naive_baseline: bool = Field(description="Row of the naive last-value baseline every challenger must beat")
    included_as: Literal["requested_model", "naive_baseline_comparator"] = Field(
        description="naive_baseline_comparator rows belong to the naive baseline model and are included because "
        "they were scored in the same evaluation manifest and period as a requested row"
    )


class EvaluationRequest(ApiModel):
    """Start a model evaluation (rolling-origin tournament, revised-data retrospective) as a job."""

    concepts: list[str] | None = Field(None, min_length=1, max_length=10,
                                       description="Concept ids (default: every supported positive annual series)")
    horizons: list[int] | None = Field(None, min_length=1, max_length=5, description="Forecast horizons in years")
    seed: int | None = Field(None, ge=0, le=2**31 - 1, description="Deterministic seed (default: service seed)")


class ModelListResponse(Envelope):
    data: list[ModelVersionOut]


class ModelEvaluationListResponse(Envelope):
    data: list[ModelEvaluation]


# ---------------------------------------------------------------------------
# Scenarios and simulations
# ---------------------------------------------------------------------------


class ScenarioCreated(ApiModel):
    scenario_id: UUID
    context_id: UUID
    spec_digest: str
    spec_sha256: str
    baseline_release_id: UUID
    knowledge_cutoff: datetime | None
    created_by: str
    recorded_at: datetime


class Scenario(ApiModel):
    id: UUID
    context_id: UUID
    origin: Literal["live", "test_fixture"]
    title: str
    baseline_release: BaselineReleaseRef
    knowledge_cutoff: datetime | None
    spec: dict[str, Any]
    spec_digest: str
    spec_sha256: str
    created_by: str
    recorded_at: datetime


class ScenarioResponse(Envelope):
    data: Scenario


class ScenarioListResponse(Envelope):
    data: list[Scenario]


class SimulationRequest(ApiModel):
    scenario_id: UUID
    fidelity: str = Field(description="L0, L1 or L2. L3/L4 are reserved and return a 422 capability_gap")


class SimRun(ApiModel):
    id: UUID
    fidelity: str
    model_manifest_sha256: str
    ontology_manifest_sha256: str
    seed_plan: dict[str, Any]
    execution_profile: dict[str, Any]
    recorded_at: datetime


class SimOutcome(ApiModel):
    status: Literal["succeeded", "failed", "unsupported", "missing_input"]
    output_manifest_sha256: str | None = None
    output_digest: str | None = None
    applicability: dict[str, Any]
    problems: list[Any]
    cost: dict[str, Any]
    cache_hit_of: UUID | None = None
    recorded_at: datetime


class SimResult(ApiModel):
    id: UUID
    output_key: str
    subject: EntityRef | None = None
    variant: str
    value: TypedValue
    uncertainty: Uncertainty
    applicability: dict[str, Any]
    artifact_sha256: str | None = None


class SimulationStatus(ApiModel):
    job: Job | None
    scenario: Scenario | None
    run: SimRun | None
    outcome: SimOutcome | None
    results: list[SimResult]


class SimulationStatusResponse(Envelope):
    data: SimulationStatus


ReplayMode = Literal["operational_as_known", "available_then_reconstruction", "revised_data_retrospective"]


class ReplayRequest(ApiModel):
    mode: ReplayMode
    cutoff: datetime = Field(description="Knowledge cutoff (timezone-aware)")
    concepts: list[str] = Field(default_factory=list, max_length=20)
    subjects: list[str] = Field(default_factory=list, max_length=20, description="Entity keys or UUIDs")
    model_keys: list[str] = Field(default_factory=list, max_length=10)
    horizon_years: int = Field(1, ge=1, le=5)


class DiscoveryRequest(ApiModel):
    catalogue: Literal["eurostat", "worldbank", "comtrade"] = "eurostat"
    query: str = Field(min_length=2, max_length=200)
    concept_id: str | None = Field(None, max_length=200)
    max_candidates: int = Field(10, ge=1, le=50)


class DiscoveryCandidate(ApiModel):
    """A source candidate surfaced by a completed discovery job, or a connector proposal.

    Search ranking is lexical evidence only: it never grants semantic equivalence.
    Nothing here is promoted; promotion needs passing gates and a human review.
    """

    candidate_kind: Literal["search_result", "connector_proposal"]
    job_id: UUID | None = None
    proposal_id: UUID | None = None
    origin: Literal["live", "test_fixture"] | None = Field(
        None, description="test_fixture when the discovery ran against captured catalogue responses")
    provider: str | None = None
    dataset_code: str | None = None
    requirement_id: str | None = None
    rank: int | None = Field(None, description="1-based lexical rank in the job's catalogue search")
    score: float | None = None
    jaccard: float | None = None
    inspected: bool | None = Field(None, description="Whether the dataset's metadata was inspected (null: not tried)")
    inspection_error: str | None = None
    selected: bool = Field(description="The job turned this candidate into a connector proposal")
    discovery_status: str | None = Field(None, description="Outcome of the discovery job (e.g. gate_reject)")
    proposal_type: str | None = None
    permission_tier: int | None = None
    gate_outcome: str | None = None
    reviews: list[str] = Field(default_factory=list, description="Human review decisions recorded so far")
    recorded_at: datetime


class DiscoveryCandidateListResponse(Envelope):
    data: list[DiscoveryCandidate]


# ---------------------------------------------------------------------------
# Sources, connectors, ontology
# ---------------------------------------------------------------------------


class ConnectorHealth(ApiModel):
    status: Literal["healthy", "degraded", "quarantined", "unknown"]
    metrics: dict[str, Any]
    recorded_at: datetime


class AcquisitionOriginCounts(ApiModel):
    live: int
    test_fixture: int


class ConnectorOut(ApiModel):
    id: UUID
    name: str
    version: str
    source_key: str
    licence: str | None = None
    declared_capabilities: dict[str, Any]
    spec_sha256: str
    code_digest: str
    recorded_at: datetime
    latest_health: ConnectorHealth | None = None
    cursor: dict[str, Any] | None = None
    acquisitions: int
    last_retrieved_at: datetime | None = None
    origin: OriginLabel = Field(description="Origins of this connector's acquisitions: live, test_fixture, mixed "
                                "(both) or none (never acquired)")
    latest_acquisition_origin: Literal["live", "test_fixture"] | None = Field(
        description="Origin of the most recent acquisition (null when none)")
    acquisitions_by_origin: AcquisitionOriginCounts


class SourceOut(ApiModel):
    id: UUID
    source_key: str
    title: str
    provider_url: str
    lineage_family: str
    rights: dict[str, Any]
    metadata: dict[str, Any]
    metadata_version: int | None = None
    connectors: list[str]
    observations: int
    last_retrieved_at: datetime | None = None
    origin: OriginLabel = Field(description="Origins of this source's acquisitions: live, test_fixture, mixed or none")
    latest_acquisition_origin: Literal["live", "test_fixture"] | None = None
    acquisitions_by_origin: AcquisitionOriginCounts


class SourceListResponse(Envelope):
    data: list[SourceOut]


class ConnectorListResponse(Envelope):
    data: list[ConnectorOut]


class ConceptOut(ApiModel):
    id: UUID
    concept_id: str
    version: str
    kind: str
    labels: dict[str, Any]
    definition: str
    owner: str
    semantic_profile: dict[str, Any]
    registry_sha256: str
    recorded_at: datetime


class MappingOut(ApiModel):
    id: UUID
    mapping_key: str
    version: str
    source_ref: dict[str, Any]
    target_concept_id: str
    target_concept_version: str
    mapping_kind: str
    transform_spec: dict[str, Any]
    applicability: dict[str, Any]
    approval: str
    recorded_at: datetime


class Ontology(ApiModel):
    concepts: list[ConceptOut]
    mappings: list[MappingOut]


class OntologyResponse(Envelope):
    data: Ontology


# ---------------------------------------------------------------------------
# Coverage and gaps
# ---------------------------------------------------------------------------


class RequirementCoverage(ApiModel):
    requirement_id: str
    task: str
    concept: str
    geography: list[str]
    period: list[int]
    weight: float
    acceptable_freshness_days: int
    denominator: int = Field(description="Required (entity, period) cells in the catalogue")
    available: int = Field(description="Cells with a non-missing value in the pinned release")
    typed_missing: int = Field(description="Cells the release holds as typed Missing")
    absent: int = Field(description="Cells with no estimate in the release")
    semantically_compatible: int = Field(description="Available cells whose concept/support match the requirement")
    fresh: int = Field(description="Available cells retrieved within the acceptable freshness")
    availability_ratio: str = Field(description="available/denominator as an exact fraction string 'n/d'")
    semantic_ratio: str
    freshness_ratio: str


class CoverageTotals(ApiModel):
    denominator: int
    available: int
    semantically_compatible: int
    fresh: int
    weighted_denominator: float
    weighted_available: float


class Coverage(ApiModel):
    catalogue_version: str
    statement: str
    release_id: UUID | None
    requirements: list[RequirementCoverage]
    totals: CoverageTotals


class CoverageResponse(Envelope):
    data: Coverage


class Gap(ApiModel):
    requirement_id: str
    concept: str
    entity_key: str
    entity_id: UUID | None
    period: int
    gap_kind: Literal["typed_missing", "absent", "stale", "semantic_mismatch", "no_release"]
    reason: str
    source_flags: dict[str, Any] | None = None
    action: str


class GapListResponse(Envelope):
    data: list[Gap]


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


class ProposalReviewOut(ApiModel):
    id: UUID
    decision: str
    reviewer: str
    comment: str
    recorded_at: datetime


class GateResultOut(ApiModel):
    id: UUID
    gate_name: str
    policy_version: str
    decision: str
    evidence_sha256: str
    evaluator: str
    recorded_at: datetime


class Proposal(ApiModel):
    id: UUID
    proposal_type: str
    manifest_sha256: str
    permission_tier: int
    summary: dict[str, Any]
    author: str
    recorded_at: datetime
    reviews: list[ProposalReviewOut]
    gate_results: list[GateResultOut]


class ProposalListResponse(Envelope):
    data: list[Proposal]


class ReviewRequest(ApiModel):
    decision: Literal["approve", "reject", "request_changes"]
    comment: str = Field("", max_length=4000)


class ReviewRecorded(ApiModel):
    review_id: UUID
    proposal_id: UUID
    decision: str
    reviewer: str


# ---------------------------------------------------------------------------
# QuerySpec (typed subset; no SQL/GraphQL/DSL)
# ---------------------------------------------------------------------------


class TimeseriesQuery(ApiModel):
    kind: Literal["timeseries"]
    subject_id: UUID
    concept: str = Field(max_length=200)
    as_of: datetime | None = None
    revisions: Literal["latest", "all"] = "latest"


class BeliefsQuery(ApiModel):
    kind: Literal["beliefs"]
    release_id: UUID | None = None
    as_of: datetime | None = None
    subject_ids: list[UUID] = Field(default_factory=list, max_length=50)
    concepts: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(100, ge=1, le=1000)


class RelationsQuery(ApiModel):
    kind: Literal["relations"]
    entity_id: UUID
    layer: str | None = Field(None, max_length=200)
    period: int | None = Field(None, ge=1900, le=2100)
    as_of: datetime | None = None
    release_id: UUID | None = Field(None, description="Pin to a published belief release (excludes as_of)")
    depth: int = Field(1, ge=1, le=2)
    max_edges: int = Field(500, ge=1, le=500)


QuerySpec = Annotated[TimeseriesQuery | BeliefsQuery | RelationsQuery, Field(discriminator="kind")]


