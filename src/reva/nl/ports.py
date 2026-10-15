"""Provider port for natural-language scenario drafting.

A :class:`ScenarioDrafter` turns one fixed request (system prompt, the user's
text wrapped as data, a JSON output schema) into one parsed JSON object. It has
no tools, no conversation state and no access to the database: it only drafts.
Everything it returns is untrusted and re-validated by :mod:`reva.nl.service`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from reva.kernel.errors import RevaError


@dataclass(frozen=True)
class LlmUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def as_json(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class LlmRequest:
    """One drafting request. ``user_content`` is the JSON data envelope holding the user's text."""

    system_prompt: str
    user_content: str
    output_schema: dict[str, Any]
    schema_name: str
    max_output_tokens: int
    prompt_version: str
    text_sha256: str


@dataclass(frozen=True)
class LlmResponse:
    provider: str
    model_requested: str
    model: str  # as reported by the provider (e.g. a dated snapshot name)
    output: Any  # parsed JSON (untrusted)
    usage: LlmUsage
    latency_ms: float
    finish_reason: str | None = None
    refusal: str | None = None
    recorded: bool = False  # True when replayed from a recorded fixture (tests)
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ScenarioDrafter(Protocol):
    provider: str
    model: str

    async def complete(self, request: LlmRequest) -> LlmResponse: ...


class LlmUnavailable(RevaError):
    """Drafting is disabled or not configured (never replaced by a guess)."""

    code = "llm_unavailable"
    http_status = 503


class LlmProviderError(RevaError):
    """The provider failed or returned something that is not a usable draft."""

    code = "llm_provider_error"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool = False, **details: Any) -> None:
        super().__init__(message, **details)
        self.retryable = retryable
