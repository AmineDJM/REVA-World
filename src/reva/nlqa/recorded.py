"""Planner fixtures: offline replay for tests, live re-capture for maintainers.

Two fixture kinds (one JSON file each):

* ``recorded_live_response`` — a real provider response body captured with
  :func:`reva.nl.recorded.capture` (redacted, never credentials), replayed through the same
  provider ``parse`` code as live calls;
* ``synthetic_plan`` — a **hand-written** planner output in the shape the response schema
  requires, used while no live capture is available (2026-10-15: the OpenAI account answered
  HTTP 429 ``insufficient_quota`` and the Gemini fallback 402; see
  ``tests/fixtures/nlqa/recorded/failures``). Replayed with ``model = "synthetic-fixture"`` and
  zero usage so it can never pass for a model answer.

Fixtures are matched by (sha256 of the text sent, prompt version); a fixture recorded with
another prompt version is refused (``fixture_stale``). ``python -m reva.nlqa.recorded`` re-captures
every fixture question live (gpt-4.1 through the environment proxy; maintainers only, per-run
cap, outside CI) and replaces a synthetic fixture only by a successful (HTTP 200) recording.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reva.nl.ports import LlmProviderError, LlmRequest, LlmResponse, LlmUsage
from reva.nl.providers import OpenAIDrafter

KINDS = ("recorded_live_response", "synthetic_plan")
SYNTHETIC_MODEL = "synthetic-fixture"


def load(directory: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(directory.glob("*.json")):
        doc = json.loads(p.read_text())
        if doc.get("fixture_kind") not in KINDS:
            raise LlmProviderError(f"{p.name}: unknown fixture_kind", reason="fixture_invalid")
        doc["_file"] = p.name
        out.append(doc)
    return out


class PlanReplay:
    """Offline drafter replaying planner/rephrase fixtures; never touches the network."""

    provider = "openai"

    def __init__(self, directory: Path, *, model: str = "gpt-4.1") -> None:
        self.fixtures = load(directory)
        self.model = model
        self.calls: list[str] = []

    def find(self, text_sha256: str) -> dict[str, Any] | None:
        hits = [
            f
            for f in self.fixtures
            if f.get("text_sha256") == text_sha256 and f.get("provider", self.provider) == self.provider
        ]
        hits.sort(key=lambda f: (f["fixture_kind"] != "recorded_live_response", f["_file"]))
        return hits[0] if hits else None

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request.text_sha256)
        fx = self.find(request.text_sha256)
        if fx is None:
            raise LlmProviderError("no planner fixture for this text (offline replay)", reason="fixture_missing")
        # a recorded provider refusal (HTTP 402/429 quota) does not depend on the prompt: it replays under a later
        # prompt version; a recorded or synthetic *plan* must match the version it was written for
        refusal = fx["fixture_kind"] == "recorded_live_response" and int(fx.get("http_status", 200)) != 200
        if fx.get("prompt_version") != request.prompt_version and not refusal:
            raise LlmProviderError(
                f"fixture {fx['_file']} has prompt {fx.get('prompt_version')}, not {request.prompt_version}: re-capture",
                reason="fixture_stale",
            )
        if fx["fixture_kind"] == "synthetic_plan":
            return LlmResponse(
                provider=self.provider,
                model_requested=self.model,
                model=SYNTHETIC_MODEL,
                output=fx["output"],
                usage=LlmUsage(0, 0, 0),
                latency_ms=0.0,
                finish_reason="stop",
                recorded=True,
                extra={"fixture": fx["_file"], "fixture_kind": "synthetic_plan"},
            )
        status = int(fx.get("http_status", 200))
        if status != 200:
            from datetime import UTC, datetime

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
        return OpenAIDrafter(fx.get("model_requested") or self.model).parse(
            json.dumps(fx["response_body"]).encode(), latency_ms=float(fx.get("latency_ms", 0)), recorded=True
        )


def synthetic_fixture(*, label: str, text: str, request: LlmRequest, output: dict[str, Any], note: str) -> dict:
    return {
        "fixture_kind": "synthetic_plan",
        "label": label,
        "note": note,
        "prompt_version": request.prompt_version,
        "text": text,
        "text_sha256": request.text_sha256,
        "output": output,
    }


async def recapture(directory: Path, *, catalog: Any, max_requests: int = 10, model: str = "gpt-4.1") -> list[dict]:
    """Re-capture every planner fixture question live (maintainers; per-run cap). Returns a summary."""
    from reva.nl.budget import load_llm_budget
    from reva.nl.recorded import capture
    from reva.nlqa.prompt import PROMPT_VERSION, prompt_digest
    from reva.nlqa.service import plan_request

    budget = load_llm_budget()
    out = []
    for fx in load(directory):
        if fx.get("prompt_version") != PROMPT_VERSION or not fx.get("text") or fx["label"].startswith("rephrase_"):
            continue  # rephrase fixtures depend on the composed answer: re-capture them by hand
        if len(out) >= max_requests:
            break
        q = fx["text"]
        req = plan_request(q, catalog, budget)
        tmp = directory / f".capture_{fx['label']}.json"
        doc = await capture(
            OpenAIDrafter(model),
            req,
            text=q,
            catalog_digest=catalog.digest(),
            prompt_sha256=prompt_digest(catalog, q),
            out=tmp,
            label=fx["label"],
        )
        if doc["http_status"] == 200:
            tmp.replace(directory / fx["_file"])
        else:
            tmp.unlink(missing_ok=True)
        out.append(
            {
                "label": fx["label"],
                "http_status": doc["http_status"],
                "model": doc.get("_model"),
                "usage": doc.get("_usage"),
                "latency_ms": doc.get("latency_ms"),
            }
        )
    return out


def main() -> None:  # pragma: no cover - maintainer entry point (live calls)
    import argparse
    import asyncio
    import sys

    ap = argparse.ArgumentParser(description="Re-capture NLQA planner fixtures live (gpt-4.1 via the proxy)")
    ap.add_argument("--fixtures", default="tests/fixtures/nlqa/recorded")
    ap.add_argument("--catalog", default="tests/fixtures/nlqa/catalog")
    ap.add_argument("--max-requests", type=int, default=10)
    a = ap.parse_args()
    from reva.nlqa.catalog import build_catalog

    cat = build_catalog(catalog_dir=Path(a.catalog).resolve())
    for row in asyncio.run(recapture(Path(a.fixtures), catalog=cat, max_requests=a.max_requests)):
        print(json.dumps(row), file=sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    main()
