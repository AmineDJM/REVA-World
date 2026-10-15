"""LLM provider adapters behind the :class:`~reva.nl.ports.ScenarioDrafter` port.

* :class:`OpenAIDrafter` — Chat Completions with a strict JSON-schema
  ``response_format``, ``temperature=0``, ``store=false``, no tools.
* :class:`GeminiDrafter` — ``generateContent`` with ``responseJsonSchema``,
  ``temperature=0``, thinking disabled, no tools (fallback provider).

Both POST through :class:`~reva.nl.http.LlmHttpClient` (fixed host allowlist).
Neither adapter reads, receives or stores an API key: credentials are injected
by the deployment's egress proxy. A 401/403 therefore surfaces as a typed
``llm_provider_error`` (``reason=unauthorized``), never as a guessed draft.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from reva.adapters.http.safe_client import HttpResult
from reva.kernel.errors import SourceResponseError, UnsafeRequest
from reva.nl.http import GEMINI_HOST, OPENAI_HOST, LlmHttpClient
from reva.nl.ports import LlmProviderError, LlmRequest, LlmResponse, LlmUsage

DEFAULT_OPENAI_MODEL = "gpt-4.1"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def check_model_name(model: str) -> str:
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        raise LlmProviderError("invalid LLM model name (letters, digits, '.', '_', '-'; max 64)",
                               reason="invalid_model_name")
    return model


def _status_error(provider: str, result: HttpResult) -> LlmProviderError:
    status = result.status
    reason = {401: "unauthorized", 403: "forbidden", 404: "model_not_found", 429: "rate_limited_or_quota"}.get(
        status, "server_error" if status >= 500 else "bad_request")
    # Provider error bodies are untrusted; keep only a short, typed message (never echoed prompts).
    message = ""
    try:
        err = json.loads(result.body.decode("utf-8", "replace")).get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("status") or "")[:200]
    except (ValueError, AttributeError):
        pass
    return LlmProviderError(f"{provider} returned HTTP {status} ({reason})", retryable=status == 429 or status >= 500,
                            provider=provider, http_status=status, reason=reason, provider_message=message)


def _parse_json_object(provider: str, text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as e:
        raise LlmProviderError(f"{provider} output is not JSON", provider=provider, reason="output_not_json") from e


def _http_client(client: LlmHttpClient | None, **kw: Any) -> tuple[LlmHttpClient, bool]:
    return (client, False) if client is not None else (LlmHttpClient(**kw), True)


class OpenAIDrafter:
    provider = "openai"

    def __init__(self, model: str = DEFAULT_OPENAI_MODEL, *, client: LlmHttpClient | None = None,
                 proxy_mode: str | None = None, timeout_seconds: float = 60.0) -> None:
        self.model = check_model_name(model)
        self._client = client
        self._kw = {"proxy_mode": proxy_mode, "timeout_seconds": timeout_seconds}

    def payload(self, request: LlmRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system_prompt},
                         {"role": "user", "content": request.user_content}],
            "temperature": 0,
            "n": 1,
            "max_completion_tokens": request.max_output_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": request.schema_name, "strict": True,
                                                "schema": request.output_schema}},
            "store": False,
        }

    async def complete(self, request: LlmRequest) -> LlmResponse:
        client, owned = _http_client(self._client, **self._kw)
        t0 = time.perf_counter()
        try:
            result = await client.post_json(f"https://{OPENAI_HOST}/v1/chat/completions", self.payload(request))
        except (SourceResponseError, UnsafeRequest) as e:
            raise LlmProviderError(f"openai request failed: {e.message}", retryable=True, provider=self.provider,
                                   reason="transport") from e
        finally:
            if owned:
                await client.aclose()
        latency = (time.perf_counter() - t0) * 1000
        if result.status != 200:
            raise _status_error(self.provider, result)
        return self.parse(result.body, latency_ms=latency)

    def parse(self, body: bytes, *, latency_ms: float, recorded: bool = False) -> LlmResponse:
        doc = _parse_json_object(self.provider, body.decode("utf-8", "replace"))
        try:
            choice = doc["choices"][0]
            message = choice["message"]
            usage = doc.get("usage") or {}
            u = LlmUsage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                         int(usage.get("total_tokens", 0)))
            refusal = message.get("refusal")
            finish = choice.get("finish_reason")
            content = message.get("content")
            model = str(doc.get("model") or self.model)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LlmProviderError("openai response has an unexpected shape", provider=self.provider,
                                   reason="unexpected_shape") from e
        output = None
        if refusal is None:
            if finish == "length":
                raise LlmProviderError("openai output truncated (max tokens reached)", provider=self.provider,
                                       reason="truncated", usage=u.as_json())
            if not isinstance(content, str):
                raise LlmProviderError("openai response has no content", provider=self.provider, reason="empty")
            output = _parse_json_object(self.provider, content)
        return LlmResponse(provider=self.provider, model_requested=self.model, model=model, output=output, usage=u,
                           latency_ms=round(latency_ms, 1), finish_reason=finish,
                           refusal=str(refusal)[:500] if refusal else None, recorded=recorded)


class GeminiDrafter:
    provider = "gemini"

    def __init__(self, model: str = DEFAULT_GEMINI_MODEL, *, client: LlmHttpClient | None = None,
                 proxy_mode: str | None = None, timeout_seconds: float = 60.0) -> None:
        self.model = check_model_name(model)
        self._client = client
        self._kw = {"proxy_mode": proxy_mode, "timeout_seconds": timeout_seconds}

    def payload(self, request: LlmRequest) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "temperature": 0,
            "candidateCount": 1,
            "maxOutputTokens": request.max_output_tokens,
            "responseMimeType": "application/json",
            "responseJsonSchema": request.output_schema,
        }
        if self.model.startswith("gemini-2.5-flash"):
            generation["thinkingConfig"] = {"thinkingBudget": 0}
        return {
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": request.user_content}]}],
            "generationConfig": generation,
        }

    async def complete(self, request: LlmRequest) -> LlmResponse:
        client, owned = _http_client(self._client, **self._kw)
        t0 = time.perf_counter()
        url = f"https://{GEMINI_HOST}/v1beta/models/{self.model}:generateContent"
        try:
            result = await client.post_json(url, self.payload(request))
        except (SourceResponseError, UnsafeRequest) as e:
            raise LlmProviderError(f"gemini request failed: {e.message}", retryable=True, provider=self.provider,
                                   reason="transport") from e
        finally:
            if owned:
                await client.aclose()
        latency = (time.perf_counter() - t0) * 1000
        if result.status != 200:
            raise _status_error(self.provider, result)
        return self.parse(result.body, latency_ms=latency)

    def parse(self, body: bytes, *, latency_ms: float, recorded: bool = False) -> LlmResponse:
        doc = _parse_json_object(self.provider, body.decode("utf-8", "replace"))
        try:
            meta = doc.get("usageMetadata") or {}
            prompt = int(meta.get("promptTokenCount", 0))
            total = int(meta.get("totalTokenCount", 0))
            u = LlmUsage(prompt, max(0, total - prompt), total)
            model = str(doc.get("modelVersion") or self.model)
            feedback = doc.get("promptFeedback") or {}
            cands = doc.get("candidates") or []
            if not cands:
                block = feedback.get("blockReason")
                return LlmResponse(provider=self.provider, model_requested=self.model, model=model, output=None,
                                   usage=u, latency_ms=round(latency_ms, 1), finish_reason="blocked",
                                   refusal=f"blocked: {block}" if block else "no candidate", recorded=recorded)
            cand = cands[0]
            finish = cand.get("finishReason")
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought"))
        except (TypeError, ValueError, AttributeError) as e:
            raise LlmProviderError("gemini response has an unexpected shape", provider=self.provider,
                                   reason="unexpected_shape") from e
        if finish == "MAX_TOKENS":
            raise LlmProviderError("gemini output truncated (max tokens reached)", provider=self.provider,
                                   reason="truncated", usage=u.as_json())
        if finish not in (None, "STOP"):
            return LlmResponse(provider=self.provider, model_requested=self.model, model=model, output=None, usage=u,
                               latency_ms=round(latency_ms, 1), finish_reason=str(finish),
                               refusal=f"finish reason {finish}", recorded=recorded)
        return LlmResponse(provider=self.provider, model_requested=self.model, model=model,
                           output=_parse_json_object(self.provider, text), usage=u, latency_ms=round(latency_ms, 1),
                           finish_reason=finish, recorded=recorded)
