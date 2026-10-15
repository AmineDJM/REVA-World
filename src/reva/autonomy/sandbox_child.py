"""Child process of the trusted-template sandbox. Reviewed code only.

First action: apply the OS resource limits given as ``--limits`` (no
``preexec_fn`` in the parent). Then read one JSON request on stdin: a
declarative manifest for the approved ``eurostat_jsonstat`` template and
captured fixture bodies (read-only inputs).

Before installing the Python-level network guard the child *probes* real
reachability with the raw ``_socket`` module (a listener the parent opened on
127.0.0.1, and a public IP). ``network_mode`` is ``blocked_verified`` only if
both attempts fail (e.g. under a network namespace); otherwise it is
``python_guard_only``, which is not a security boundary. The guard then refuses
connect/sendto/name resolution on both ``socket`` and ``_socket`` (V1 replays
captured bytes; the allowlist is reported, never widened). It never imports
code named by a proposal: the only executed connector code is the reviewed
template module.
"""

from __future__ import annotations

import _socket
import asyncio
import base64
import json
import os
import socket
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


PUBLIC_PROBE = ("1.1.1.1", 443)
_RAW_SOCKET = _socket.socket


def _apply_limits(argv: list[str]) -> dict[str, Any]:
    """Lower this process's own rlimits before doing anything else."""
    import resource

    if "--limits" not in argv:
        return {"applied": False, "reason": "no limits given"}
    lim = json.loads(argv[argv.index("--limits") + 1])
    pairs = {"cpu": resource.RLIMIT_CPU, "as": resource.RLIMIT_AS, "nofile": resource.RLIMIT_NOFILE,
             "fsize": resource.RLIMIT_FSIZE}
    for k, r in pairs.items():
        v = int(lim.get(k, 0))
        if v > 0:
            resource.setrlimit(r, (v, v))
    return {"applied": True, **{k: resource.getrlimit(r) for k, r in pairs.items()}}


def _raw_connect(addr: tuple[str, int], timeout: float = 0.5) -> str:
    s = _RAW_SOCKET(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(addr)
        return "connected"
    except OSError as e:
        return f"failed: {type(e).__name__}: {getattr(e, 'errno', '')}"
    finally:
        s.close()


def _probe_isolation(req: dict[str, Any]) -> dict[str, Any]:
    """Measured (not nominal) isolation facts, gathered before the Python guard is installed."""
    port = req.get("probe_port")
    loop = _raw_connect(("127.0.0.1", int(port))) if port else "not_probed"
    public = _raw_connect(PUBLIC_PROBE)
    verified = loop.startswith("failed") and public.startswith("failed")
    try:
        with open(f"/proc/{os.getppid()}/environ", "rb") as f:
            environ_readable = len(f.read(1)) >= 0
    except OSError:
        environ_readable = False
    same_uid = os.getuid() == req.get("parent_uid")
    if req.get("namespaces"):
        process = "user+pid+net namespaces (unshare); parent /proc not visible"
    elif same_uid:
        process = "none (same uid)"
    else:
        process = "different uid"
    return {
        "network_probe_raw": {"loopback_listener": loop, "public_ip": public},
        "network_mode": "blocked_verified" if verified else "python_guard_only",
        "parent_environ_readable": environ_readable,
        "process_isolation": process,
    }


def _block_network(allowlist: list[str]) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise PermissionError(f"network is blocked in the trusted-template sandbox (allowlist {allowlist}, "
                              "replay only)")

    blocked_families = {_socket.AF_INET, _socket.AF_INET6, getattr(_socket, "AF_PACKET", -2), -1}

    class _RefusingSocket(_RAW_SOCKET):  # type: ignore[misc, valid-type]
        # socket.socket.__init__ calls ``_socket.socket.__init__`` through the module
        # attribute, so this also guards sockets created via the ``socket`` module.
        # Wrapping an existing fd (socketpair, used by asyncio's self-pipe) and
        # process-local AF_UNIX sockets stay possible.
        def __init__(self, family: int = -1, type: int = -1, proto: int = -1, fileno: Any = None) -> None:
            if fileno is None and family in blocked_families:
                refuse()
            _RAW_SOCKET.__init__(self, family, type, proto, fileno)

    for name in ("connect", "connect_ex", "sendto", "sendmsg", "bind"):
        setattr(socket.socket, name, refuse)
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]
    _socket.socket = _RefusingSocket  # type: ignore[misc]
    _socket.getaddrinfo = refuse  # type: ignore[attr-defined]
    # socketpair (AF_UNIX, process-local) stays available: asyncio's self-pipe needs it.


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
    rlimits = _apply_limits(sys.argv)
    req = json.loads(sys.stdin.read())
    isolation = _probe_isolation(req)
    _block_network(list(req["network_hosts"]))
    out: dict[str, Any] = {
        "environment_keys": sorted(os.environ),
        "network_allowlist": list(req["network_hosts"]),
        "network_guard": "python socket+_socket patched (replay only)",
        **isolation,
        "cwd_writable": os.access(os.getcwd(), os.W_OK),
        "modules_from_proposal": [],
        "rlimits": rlimits,
    }
    probe = req.get("network_probe")
    if probe:  # self-test: prove that outbound connections are refused
        try:
            socket.create_connection((str(probe), 443), timeout=2)
            out["network_probe"] = "connected"
        except Exception as e:
            out["network_probe"] = f"refused: {type(e).__name__}"
    try:
        out.update(asyncio.run(_run(req)))
        out["ok"] = True
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:500]}"
    sys.stdout.write(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
