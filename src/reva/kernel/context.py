"""Contexts separate evidence, belief releases, simulations, evaluations and
synthetic populations. Context is a first-class field, not a trailing string."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class ContextMode(StrEnum):
    evidence = "evidence"
    belief = "belief"
    simulation = "simulation"
    evaluation = "evaluation"
    population = "population"
    quarantine = "quarantine"


class Origin(StrEnum):
    live = "live"
    test_fixture = "test_fixture"


class Modality(StrEnum):
    """Output modality. Not a confidence grade."""

    reported_observation = "reported_observation"
    selected_estimate = "selected_estimate"
    forecast = "forecast"
    identified_causal_estimate = "identified_causal_estimate"
    assumption_conditioned_scenario = "assumption_conditioned_scenario"
    unsupported = "unsupported"


class Context(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    context_id: UUID
    mode: ContextMode
    origin: Origin
    parent_context_id: UUID | None = None
    manifest_sha256: str
    ontology_manifest_sha256: str | None = None
    model_manifest_sha256: str | None = None
    knowledge_cutoff: datetime | None = None

    @field_validator("knowledge_cutoff")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("knowledge_cutoff must be timezone-aware")
        return v


def assert_publishable(origin: Origin, environment: str) -> None:
    """Fixture-origin contexts can never be published in production."""
    from reva.kernel.errors import PolicyDenied

    if environment == "production" and origin != Origin.live:
        raise PolicyDenied("test_fixture context cannot be published in production")
