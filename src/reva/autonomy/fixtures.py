"""Immutable raw fixture bank (content-addressed) for replay, validation and repairs.

A bank is a JSON manifest artifact listing captured responses whose bodies are
stored as separate ``raw`` artifacts. Banks are immutable: a repair or a mutated
test sample produces a *new* bank, so captured history cannot be reinterpreted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reva.adapters.http.safe_client import HttpResult, request_fingerprint
from reva.autonomy.common import ArtifactIO
from reva.evidence.runner import FIXTURE_ROOT
from reva.kernel.errors import IntegrityFailure, SourceResponseError, UnsafeRequest
from reva.kernel.ids import sha256_bytes
from reva.ports.autonomy import ArtifactRef

BANK_SCHEMA = "reva-fixture-bank/1"


def bank_from_dir(
    io_: ArtifactIO, connector: str, root: Path = FIXTURE_ROOT, overrides: Mapping[str, bytes] | None = None
) -> ArtifactRef:
    """Load captured fixtures (tests/fixtures/source/<connector>/manifest.json) into a bank.

    ``overrides`` maps URL -> replacement body (used to build mutated samples).
    """
    d = root / connector
    entries = json.loads((d / "manifest.json").read_text())
    out = []
    for e in entries:
        body = (d / e["file"]).read_bytes()
        if sha256_bytes(body) != e["sha256"]:
            raise IntegrityFailure("captured fixture bytes do not match their manifest hash", file=e["file"])
        mutated = False
        if overrides and e["url"] in overrides:
            body, mutated = overrides[e["url"]], True
        media = (e.get("headers") or {}).get("content-type", "application/octet-stream")
        ref = io_.put_bytes(body, "raw", media.split(";")[0].strip() or "application/octet-stream")
        out.append(
            {
                "request_fingerprint": e["request_fingerprint"],
                "url": e["url"],
                "body_sha256": ref.sha256,
                "media_type": ref.media_type,
                "status": e["status"],
                "headers": e.get("headers") or {},
                "retrieved_at": e["retrieved_at"],
                "licence": e.get("licence", ""),
                "captured_file": e["file"],
                "mutated": mutated,
            }
        )
    return io_.put_json(
        {"schema": BANK_SCHEMA, "connector": connector, "entries": sorted(out, key=lambda x: x["url"])}, "raw"
    )


class BankHttp:
    """Replays a fixture bank; never touches the network. Origin is test_fixture."""

    origin = "test_fixture"

    def __init__(self, io_: ArtifactIO, bank: ArtifactRef, allowed_hosts: tuple[str, ...] | None = None) -> None:
        self.io = io_
        doc = io_.get_json(bank)
        if doc.get("schema") != BANK_SCHEMA:
            raise SourceResponseError("not a fixture bank", retryable=False)
        self.entries = {e["request_fingerprint"]: e for e in doc["entries"]}
        self.allowed_hosts = allowed_hosts
        self.requests = 0
        self.bytes = 0

    def has(self, url: str) -> bool:
        return request_fingerprint("GET", url) in self.entries

    async def get(self, url: str, **kw: Any) -> HttpResult:
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise UnsafeRequest(f"host {host!r} is not allowlisted")
        e = self.entries.get(request_fingerprint("GET", url))
        if e is None:
            raise SourceResponseError(f"no captured fixture for {url}", retryable=False, kind="fixture_missing")
        body = self.io.get_bytes(ArtifactRef(e["body_sha256"], "raw", e["media_type"]))
        limit = kw.get("max_bytes")
        if limit is not None and len(body) > int(limit):
            raise UnsafeRequest(f"response exceeds limit {limit} bytes (decoded)")
        self.requests += 1
        self.bytes += len(body)
        headers = {**{k.lower(): v for k, v in (e.get("headers") or {}).items()}}
        headers.setdefault("content-type", e["media_type"])
        return HttpResult(
            url, url, int(e["status"]), headers, body, datetime.fromisoformat(e["retrieved_at"]), 0.0, (), 1
        )

    async def aclose(self) -> None:
        return None


class CapturingHttp:
    """Wraps a live SafeHttpClient and records every response into a new bank."""

    origin = "live"

    def __init__(self, client: Any, licence: str) -> None:
        self.client = client
        self.licence = licence
        self.captured: list[tuple[str, HttpResult]] = []
        self.requests = 0
        self.bytes = 0

    async def get(self, url: str, **kw: Any) -> HttpResult:
        r = await self.client.get(url, **kw)
        self.requests += 1
        self.bytes += len(r.body)
        self.captured.append((url, r))
        return r

    def bank(self, io_: ArtifactIO, connector: str) -> ArtifactRef:
        out = []
        for url, r in self.captured:
            ref = io_.put_bytes(r.body, "raw", r.media_type or "application/octet-stream")
            out.append(
                {
                    "request_fingerprint": request_fingerprint("GET", url),
                    "url": url,
                    "body_sha256": ref.sha256,
                    "media_type": ref.media_type,
                    "status": r.status,
                    "headers": {k: v for k, v in r.headers.items() if k in ("content-type", "etag", "last-modified")},
                    "retrieved_at": r.retrieved_at.astimezone(UTC).isoformat(),
                    "licence": self.licence,
                    "captured_file": None,
                    "mutated": False,
                }
            )
        return io_.put_json(
            {"schema": BANK_SCHEMA, "connector": connector, "entries": sorted(out, key=lambda x: x["url"])}, "raw"
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def bank_entries(io_: ArtifactIO, bank: ArtifactRef) -> list[dict[str, Any]]:
    return list(io_.get_json(bank)["entries"])
