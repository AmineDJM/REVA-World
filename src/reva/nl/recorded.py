"""Recorded provider responses: replay for offline tests, capture for maintainers.

A fixture is one JSON file holding a *real* provider response body captured
once from a live call (``capture``), redacted (response ids removed; no request
headers, no credentials — REVA never holds any), plus the request metadata
needed to match it: the sha256 of the user text, the prompt version, the
catalogue digest, the provider and the model that answered.

:class:`RecordedDrafter` replays them through the same ``parse`` code as the
live adapters, so tests exercise the real response shapes. A fixture recorded
with another ``PROMPT_VERSION`` is refused (``fixture_stale``): fixtures must be
re-captured when the prompt changes. Hand-written adversarial outputs (a
compromised or injected model) are *not* recordings and live in a separate
``synthetic/`` directory with ``fixture_kind: synthetic_adversarial``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reva.nl.ports import LlmProviderError, LlmRequest, LlmResponse, LlmUsage
from reva.nl.providers import GeminiDrafter, OpenAIDrafter

FIXTURE_KINDS = ("recorded_live_response", "synthetic_adversarial")
_REDACT_KEYS = {"id", "responseId"}


def _parser(provider: str, model: str) -> OpenAIDrafter | GeminiDrafter:
    if provider == "openai":
        return OpenAIDrafter(model)
    if provider == "gemini":
        return GeminiDrafter(model)
    raise LlmProviderError(f"unknown provider in fixture: {provider}", reason="fixture_invalid")


def load_fixtures(directory: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(directory.glob("*.json")):
        doc = json.loads(p.read_text())
        if doc.get("fixture_kind") not in FIXTURE_KINDS:
            raise LlmProviderError(f"{p.name}: unknown fixture_kind", reason="fixture_invalid")
        doc["_file"] = p.name
        out.append(doc)
    return out


class RecordedDrafter:
    """Replays fixtures by (text sha256, provider). Offline; never touches the network."""

    def __init__(self, directory: Path | list[Path], *, provider: str = "openai", model: str | None = None) -> None:
        dirs = directory if isinstance(directory, list) else [directory]
        self.fixtures = [f for d in dirs for f in load_fixtures(d)]
        self.provider = provider
        self.model = model or next(
            (f["model_requested"] for f in self.fixtures if f["provider"] == provider), "recorded"
        )
        self.calls: list[str] = []

    def find(self, text_sha256: str) -> dict[str, Any] | None:
        return next(
            (f for f in self.fixtures if f["text_sha256"] == text_sha256 and f["provider"] == self.provider), None
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request.text_sha256)
        fx = self.find(request.text_sha256)
        if fx is None:
            raise LlmProviderError(
                "no recorded response for this text (offline replay)", reason="fixture_missing", provider=self.provider
            )
        if fx["prompt_version"] != request.prompt_version:
            raise LlmProviderError(
                f"fixture {fx['_file']} was recorded with prompt {fx['prompt_version']}, "
                f"not {request.prompt_version}: re-capture it",
                reason="fixture_stale",
            )
        status = int(fx.get("http_status", 200))
        if status != 200:
            from reva.adapters.http.safe_client import HttpResult
            from reva.nl.providers import _status_error

            raise _status_error(
                self.provider,
                HttpResult(
                    url="recorded",
                    final_url="recorded",
                    status=status,
                    headers={},
                    body=json.dumps(fx["response_body"]).encode(),
                    retrieved_at=datetime.now(UTC),
                    elapsed_seconds=0.0,
                    redirects=(),
                    attempts=1,
                ),
            )
        parser = _parser(fx["provider"], fx["model_requested"])
        return parser.parse(
            json.dumps(fx["response_body"]).encode(), latency_ms=float(fx.get("latency_ms", 0)), recorded=True
        )


def redact_body(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: ("redacted" if k in _REDACT_KEYS else redact_body(v)) for k, v in body.items()}
    if isinstance(body, list):
        return [redact_body(v) for v in body]
    return body


async def capture(
    drafter: OpenAIDrafter | GeminiDrafter,
    request: LlmRequest,
    *,
    text: str,
    catalog_digest: str,
    prompt_sha256: str,
    out: Path,
    label: str,
) -> dict[str, Any]:
    """One live call (maintainers only, outside CI); writes a redacted fixture and returns it."""
    from reva.nl.http import LlmHttpClient

    client = LlmHttpClient()
    t0 = time.perf_counter()
    try:
        url = (
            "https://api.openai.com/v1/chat/completions"
            if drafter.provider == "openai"
            else f"https://generativelanguage.googleapis.com/v1beta/models/{drafter.model}:generateContent"
        )
        result = await client.post_json(url, drafter.payload(request))
    finally:
        await client.aclose()
    latency = (time.perf_counter() - t0) * 1000
    body = json.loads(result.body)
    fixture = {
        "fixture_kind": "recorded_live_response",
        "label": label,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": drafter.provider,
        "model_requested": drafter.model,
        "http_status": result.status,
        "latency_ms": round(latency, 1),
        "prompt_version": request.prompt_version,
        "prompt_sha256": prompt_sha256,
        "catalog_digest": catalog_digest,
        "text": text,
        "text_sha256": request.text_sha256,
        "response_body": redact_body(body),
    }
    out.write_text(json.dumps(fixture, ensure_ascii=False, indent=1) + "\n")
    if result.status == 200:
        parsed = drafter.parse(result.body, latency_ms=latency)
        fixture["_usage"] = parsed.usage.as_json()
        fixture["_model"] = parsed.model
    return fixture


__all__ = ["LlmUsage", "RecordedDrafter", "capture", "load_fixtures", "redact_body"]
