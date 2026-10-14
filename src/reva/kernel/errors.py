"""Typed errors. API layers convert them to RFC 9457-style problem details."""

from __future__ import annotations

from typing import Any


class RevaError(Exception):
    """Base class. ``code`` is a stable machine-readable identifier."""

    code: str = "reva_error"
    http_status: int = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def problem(self) -> dict[str, Any]:
        return {
            "type": f"https://reva.invalid/problems/{self.code}",
            "title": self.code.replace("_", " "),
            "status": self.http_status,
            "detail": self.message,
            "code": self.code,
            **({"details": self.details} if self.details else {}),
        }


class ValidationProblem(RevaError):
    code = "validation_error"
    http_status = 422


class CapabilityGap(RevaError):
    """A request outside the declared, implemented capability envelope."""

    code = "capability_gap"
    http_status = 422


class MissingInput(RevaError):
    """A required input is missing; it is never replaced with zero."""

    code = "missing_input"
    http_status = 422


class IncompatibleSemantics(RevaError):
    code = "incompatible_semantics"
    http_status = 422


class LeakageBlocked(RevaError):
    """Strict replay rejected information unavailable at the cutoff."""

    code = "future_information_blocked"
    http_status = 422


class ImmutableRecordViolation(RevaError):
    code = "immutable_record"
    http_status = 409


class IntegrityFailure(RevaError):
    """Checksum, size or manifest verification failed."""

    code = "integrity_failure"
    http_status = 500


class StaleLease(RevaError):
    """A worker lost its lease or fencing token and may not publish."""

    code = "stale_lease"
    http_status = 409


class PolicyDenied(RevaError):
    code = "policy_denied"
    http_status = 403


class SourceResponseError(RevaError):
    """Provider returned malformed, truncated, HTML, or API-error content."""

    code = "source_response_error"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool, **details: Any) -> None:
        super().__init__(message, **details)
        self.retryable = retryable


class UnsafeRequest(RevaError):
    """SSRF, disallowed host, oversized or decompression-bomb response."""

    code = "unsafe_request"
    http_status = 400


class ConfigurationError(RevaError):
    code = "configuration_error"
    http_status = 500
