"""Connector contracts. A connector never receives a database writer: parse is a
deterministic function of raw bytes and pinned schemas; publication is done by
the evidence ingest service."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from reva.kernel.support import Support
from reva.kernel.uncertainty import UncertaintySpec


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConnectorSpec(_M):
    name: str
    version: str
    template: Literal[
        "worldbank_v2", "comtrade_public_exact", "eurostat_jsonstat", "crossref_doi", "naturalearth_asset"
    ]
    source_key: str
    provider: str
    licence: str
    allowed_hosts: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    field_mappings: dict[str, Any]
    units: dict[str, str]
    geography: tuple[str, ...]
    time_semantics: str
    lineage_family: str
    cadence_seconds: int = Field(ge=60)
    release_aware: bool = True
    authentication_secret_ref: str | None = None
    revision_strategy: str
    max_response_bytes: int = Field(ge=1, le=52_428_800)
    max_pages: int = Field(ge=1, le=1000)
    parser_version: str
    code_digest: str = ""


class Cursor(_M):
    state: dict[str, Any] = {}


class FetchBudget(_M):
    max_requests: int = 100
    max_bytes: int = 52_428_800


class FetchItem(_M):
    key: str
    url: str
    expected_media_types: tuple[str, ...] = ("application/json",)
    max_bytes: int
    params: dict[str, Any] = {}
    required: bool = True


class FetchPlan(_M):
    connector: str
    items: tuple[FetchItem, ...]
    pagination: str
    retry: str = "bounded_backoff_retry_after"
    availability_expectation: str = ""
    budget_exhausted: bool = False


class RawAcquisition(_M):
    item: FetchItem
    body: bytes
    media_type: str
    retrieved_at: datetime
    response_metadata: dict[str, Any]
    request_fingerprint: str
    origin: Literal["live", "test_fixture"]
    raw_sha256: str | None = None
    replay_of_acquisition_id: str | None = None


class RelationSpec(_M):
    predicate: str
    layer: str
    participants: tuple[dict[str, Any], ...]  # {role, provider, key, direction}
    component: dict[str, Any] = {}


class ParsedObservation(_M):
    subject_provider: str
    subject_key: str
    concept_id: str
    concept_version: str
    support: Support
    logical_key: str
    value: dict[str, Any]
    uncertainty: UncertaintySpec
    source_flags: dict[str, Any] = {}
    source_revision_id: str | None = None
    observed_at: datetime | None = None
    source_published_at: datetime | None = None
    source_available_at: datetime | None = None
    availability_basis: dict[str, Any] | None = None
    raw_selector: dict[str, Any]
    relation: RelationSpec | None = None


class ObservationBatch(_M):
    connector: str
    connector_version: str
    item_key: str
    observations: tuple[ParsedObservation, ...]
    expected_keys: tuple[str, ...] = ()
    metadata: dict[str, Any] = {}
    schema_fingerprint: str = ""
    follow_up: tuple[FetchItem, ...] = ()


class Finding(_M):
    code: str
    severity: Literal["info", "warning", "blocker"]
    message: str


class QualityReport(_M):
    outcome: Literal["pass", "quarantine", "blocked"]
    findings: tuple[Finding, ...] = ()
    expected_count: int = 0
    received_count: int = 0
    missing_keys: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.outcome != "pass"


class FetchOutcome(_M):
    item_key: str
    status: Literal["published", "unchanged", "quarantined", "failed"]
    raw_sha256: str | None = None
    detail: str = ""


class SafeHttp(Protocol):
    async def get(self, url: str, **kw: Any) -> Any: ...


class Connector(Protocol):
    def describe(self) -> ConnectorSpec: ...

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan: ...

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition: ...

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]: ...

    def validate(self, batch: ObservationBatch) -> QualityReport: ...

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor: ...
