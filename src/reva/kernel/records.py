"""The seven stable kernel record types plus component manifests.

A computational Activity is not a real-world Process: an earthquake is an
Occurrence referent; the code that extracts its location is an Activity.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reva.kernel.uncertainty import UncertaintySpec


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScaleType(StrEnum):
    extensive = "extensive"
    intensive = "intensive"
    stock = "stock"
    rate = "rate"
    flow = "flow"
    ratio = "ratio"
    categorical = "categorical"


class SemanticProfile(_Frozen):
    """Everything required to know whether two numbers mean the same thing."""

    population_universe: str
    denominator: str | None = None
    temporal_support: str  # e.g. "point:01-01", "annual_average", "annual_total"
    spatial_support: str
    aggregation: str
    scale_type: ScaleType
    unit_ref: str | None = None
    currency_basis: str | None = None
    price_basis: str | None = None
    valuation: str | None = None
    permitted_transformations: tuple[str, ...] = ()
    missingness: tuple[str, ...] = ()
    measurement_method: str | None = None
    sensitivity: Literal["public_aggregate", "synthetic_public_aggregate_derived"] = "public_aggregate"
    signature: dict[str, Any] | None = None  # predicate role signature


class ConceptKind(StrEnum):
    entity_type = "entity_type"
    quantity = "quantity"
    predicate = "predicate"
    role = "role"
    category = "category"
    unit = "unit"
    component = "component"


class Concept(_Frozen):
    concept_id: str
    version: str
    kind: ConceptKind
    labels: dict[str, str]
    definition: str
    owner: str
    schema_ref: str | None = None
    semantic_profile: SemanticProfile | None = None

    @model_validator(mode="after")
    def _quantity_profile(self) -> Concept:
        if self.kind in (ConceptKind.quantity, ConceptKind.predicate) and not self.semantic_profile:
            raise ValueError(f"{self.kind} concept {self.concept_id} requires a semantic profile")
        return self


class Referent(_Frozen):
    id: UUID
    kind_ref: str
    namespace: str
    referent_mode: Literal["real", "abstract", "synthetic", "hypothetical"]
    label: str
    created_activity_id: UUID


class ParticipantBinding(_Frozen):
    role_ref: str
    entity_id: UUID
    ordinal: int = Field(ge=0)
    direction: Literal["in", "out", "undirected"] = "undirected"


class EpistemicProfile(_Frozen):
    evidence_kind: Literal[
        "reported",
        "experimental",
        "quasi_experimental",
        "observational",
        "mechanistic",
        "structural_calibration",
        "expert_judgment",
        "hypothesis",
        "computed",
    ]
    relation_type: Literal["descriptive", "statistical", "mechanistic", "identified_causal"] = "descriptive"


class Assertion(_Frozen):
    id: UUID
    predicate_ref: str
    participant_bindings: tuple[ParticipantBinding, ...]
    value: dict[str, Any]
    support_id: UUID
    context_ref: UUID
    epistemic_profile: EpistemicProfile
    uncertainty: UncertaintySpec
    provenance_activity_id: UUID


class Artifact(_Frozen):
    sha256: str
    media_type: str
    byte_size: int = Field(ge=0)
    storage_ref: str
    schema_version: str
    rights_ref: dict[str, Any]
    privacy_class: Literal["public_aggregate", "synthetic_public_aggregate_derived"]


class ActivityOrigin(StrEnum):
    source = "source"
    normalisation = "normalisation"
    belief = "belief"
    model = "model"
    simulation = "simulation"
    evaluation = "evaluation"
    review = "review"


class Activity(_Frozen):
    """Computational provenance. Never a real-world event."""

    id: UUID
    kind: str
    origin: ActivityOrigin
    code_digest: str
    parameters: dict[str, Any]
    environment_digest: str
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    actor: str
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def _times(self) -> Activity:
        if self.ended_at < self.started_at:
            raise ValueError("activity ended before it started")
        return self


class ComponentManifest(_Frozen):
    """Independently versioned component schema (e.g. demographics.v1)."""

    name: str
    version: str  # semver; meaning/unit/required-field change => major bump
    applicable_entity_kinds: tuple[str, ...]
    json_schema: dict[str, Any]
    concepts: tuple[str, ...]
    support_requirements: dict[str, Any]
    privacy_class: Literal["public_aggregate", "synthetic_public_aggregate_derived"]
    validation_rules: tuple[str, ...] = ()
