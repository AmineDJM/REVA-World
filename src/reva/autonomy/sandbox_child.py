"""Child process of the trusted-template sandbox. Reviewed code only.

Reads one JSON request on stdin: a declarative manifest for the approved
``eurostat_jsonstat`` template and captured fixture bodies (read-only inputs).
Network is blocked: every socket connect and name resolution is refused (V1
replays captured bytes; the allowlist is reported, never widened). The process
runs with a scrubbed environment and OS resource limits set by the parent. It
never imports code named by a proposal: the only executed connector code is the
reviewed template module.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


def _block_network(allowlist: list[str]) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise PermissionError(f"network is blocked in the trusted-template sandbox (allowlist {allowlist}, "
                              "replay only)")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


class _Replay:
    origin = "test_fixture"

    def __init__(self, fixtures: list[dict[str, Any]], hosts: list[str]) -> None:
        from reva.adapters.http.safe_client import request_fingerprint

        self.fp = request_fingerprint
        self.by_fp = {request_fingerprint("GET", f["url"]): f for f in fixtures}
        self.hosts = set(hosts)

    async def get(self, url: str, **kw: Any) -> Any:
        from reva.adapters.http.safe_client import HttpResult

        host = (urlsplit(url).hostname or "").lower()
        if host not in self.hosts:
            raise PermissionError(f"host {host!r} outside sandbox network allowlist")
        f = self.by_fp.get(self.fp("GET", url))
        if f is None:
            raise LookupError(f"no read-only input for {url}")
        body = base64.b64decode(f["body_b64"])
        if kw.get("max_bytes") is not None and len(body) > int(kw["max_bytes"]):
            raise ValueError("input exceeds max_bytes")
        return HttpResult(url, url, int(f["status"]), {"content-type": f["media_type"]}, body,
                          datetime.fromisoformat(f["retrieved_at"]), 0.0, (), 1)


async def _run(req: dict[str, Any]) -> dict[str, Any]:
    from reva.adapters.connectors.eurostat_template import EurostatJsonStatConnector
    from reva.ports.connectors import FetchBudget

    conn = EurostatJsonStatConnector(req["manifest"])
    http = _Replay(req["fixtures"], req["network_hosts"])
    plan = conn.plan(None, FetchBudget(max_requests=int(req["max_items"]), max_bytes=int(req["max_bytes"])))
    results = []
    for item in plan.items:
        raw = await conn.fetch(item, http)
        for b in conn.parse(raw):
            q = conn.validate(b)
            results.append({
                "item": b.item_key, "observations": len(b.observations),
                "missing": sum(1 for o in b.observations if o.value.get("kind") == "missing"),
                "cell_types": sorted({o.source_flags.get("cell_type") for o in b.observations}),
                "structure_fingerprint": b.schema_fingerprint, "quality": q.model_dump(mode="json"),
            })
    return {"plan_items": len(plan.items), "results": results}


def main() -> int:
    req = json.loads(sys.stdin.read())
    _block_network(list(req["network_hosts"]))
    out: dict[str, Any] = {
        "environment_keys": sorted(os.environ),
        "network_allowlist": list(req["network_hosts"]),
        "network_mode": "blocked_by_default_replay_only",
        "cwd_writable": os.access(os.getcwd(), os.W_OK),
        "modules_from_proposal": [],
    }
    try:
        out.update(asyncio.run(_run(req)))
        out["ok"] = True
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:500]}"
    try:
        import resource

        out["rlimits"] = {"cpu": resource.getrlimit(resource.RLIMIT_CPU),
                          "as": resource.getrlimit(resource.RLIMIT_AS)}
    except Exception:
        out["rlimits"] = None
    sys.stdout.write(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
