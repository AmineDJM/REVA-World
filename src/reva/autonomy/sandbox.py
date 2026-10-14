"""SandboxRunner V1.

``trusted_template`` isolation executes a *declarative manifest* through the
reviewed template code in a child process with

* a scrubbed environment (no REVA_* secrets, no database URLs, no tokens),
* read-only inputs (captured fixture bytes on stdin; an empty, read-only cwd),
* network blocked by default (the allowlist is limited to the template's hosts
  and V1 replays captured bytes only),
* OS limits on CPU seconds, address space, open files and wall time.

A Python subprocess is **not** a security boundary for hostile code, so any
proposal that contains code (``.py``/script paths or ``execute_code``) is never
run here: it goes to an ``ExternalRunner`` (dedicated disposable VM/microVM
service at ``REVA_SANDBOX_ENDPOINT``) or stays unexecuted with a ``blocked``
report. The production worker never imports proposed code.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

from reva.adapters.connectors.eurostat_template import TEMPLATE_HOSTS, TEMPLATE_ID, validate_manifest
from reva.autonomy.common import ArtifactIO, report
from reva.autonomy.fixtures import BankHttp
from reva.autonomy.gate import CONNECTOR_CHANGE_SCHEMA
from reva.autonomy.policy import AutonomyGuard, contains_code
from reva.autonomy.proposals import proposal_json
from reva.kernel.errors import ConfigurationError, RevaError
from reva.ports.autonomy import ArtifactRef, Finding, PlatformChangeProposal, SandboxPolicy, ValidationReport
from reva.ports.connectors import FetchBudget

EVALUATOR_TEMPLATE = "sandbox:trusted_template@1"
EVALUATOR_EXTERNAL = "sandbox:external_disposable_vm@1"
SAFE_ENV_KEYS = ("LANG", "LC_ALL", "TZ")


def scrubbed_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal allowlisted environment: nothing secret survives (default deny)."""
    src = dict(os.environ if source is None else source)
    env = {k: src[k] for k in SAFE_ENV_KEYS if k in src}
    env.update({"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
                "REVA_SANDBOX_ISOLATION": "trusted_template", "HOME": "/nonexistent"})
    return env


def _f(code: str, severity: str, message: str) -> Finding:
    return Finding(code=code, severity=severity, message=message, evidence=())  # type: ignore[arg-type]


class ExternalRunner(Protocol):
    """Adapter to a dedicated isolated runner (disposable VM/microVM) for generated code."""

    async def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class HttpExternalRunner:
    """POSTs a run request to REVA_SANDBOX_ENDPOINT. Constructed only when configured."""

    def __init__(self, endpoint: str, token: str | None = None, *, transport: Any = None,
                 timeout_seconds: float = 600.0) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ConfigurationError("REVA_SANDBOX_ENDPOINT must be an https URL without credentials")
        self.endpoint = endpoint
        self._token = token
        self._transport = transport
        self.timeout = timeout_seconds

    async def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout,
                                     follow_redirects=False) as client:
            r = await client.post(self.endpoint, content=json.dumps(dict(request), default=str), headers=headers)
        if r.status_code != 200:
            return {"outcome": "blocked", "findings": [{"code": "external_runner_http", "severity": "blocker",
                                                         "message": f"runner returned HTTP {r.status_code}"}]}
        data = r.json()
        return data if isinstance(data, dict) else {"outcome": "blocked", "findings": []}


def external_runner_from_settings(settings: Any) -> HttpExternalRunner | None:
    endpoint = getattr(settings, "reva_sandbox_endpoint", None)
    if not endpoint:
        return None
    tok = getattr(settings, "reva_sandbox_token", None)
    return HttpExternalRunner(endpoint, tok.get_secret_value() if tok is not None else None)


class TrustedTemplateSandbox:
    def __init__(self, io_: ArtifactIO, *, fixtures: ArtifactRef | None = None,
                 external_runner: ExternalRunner | None = None, guard: AutonomyGuard | None = None) -> None:
        self.io = io_
        self.fixtures = fixtures
        self.external_runner = external_runner
        self.guard = guard

    network_probe: str | None = None  # self-test hook: the child tries one outbound connection

    async def run(self, proposal: PlatformChangeProposal, policy: SandboxPolicy) -> ValidationReport:
        started = time.monotonic()
        findings: list[Finding] = []
        evidence: dict[str, Any] = {"schema": "reva-sandbox-run/1", "proposal_id": str(proposal.proposal_id),
                                    "policy": {"version": policy.policy_version, "isolation": policy.isolation,
                                               "network_hosts": list(policy.network_hosts),
                                               "writable_paths": list(policy.writable_paths),
                                               "secret_access": policy.secret_access}}

        def finish(outcome: str, evaluator: str, usage: dict[str, Any] | None = None) -> ValidationReport:
            evidence["outcome"] = outcome
            evidence["findings"] = [{"code": x.code, "severity": x.severity, "message": x.message} for x in findings]
            u = {**(usage or {}), "wall_seconds": round(time.monotonic() - started, 3)}
            evidence["resource_usage"] = u
            return report(outcome, findings, self.io.put_json(evidence, "evaluation"), evaluator, u)

        if self.guard is not None and self.guard.blocked_reason():
            findings.append(_f("autonomy_blocked", "blocker", self.guard.blocked_reason() or ""))
            return finish("blocked", EVALUATOR_TEMPLATE)
        if policy.secret_access is not False:
            findings.append(_f("secret_access_requested", "blocker", "sandboxes never receive secrets"))
            return finish("blocked", EVALUATOR_TEMPLATE)
        if contains_code(proposal.changed_paths, proposal.requested_capabilities):
            return await self._code_proposal(proposal, policy, findings, evidence, finish)
        if policy.isolation != "trusted_template":
            findings.append(_f("isolation_mismatch", "blocker", "declarative manifests run in trusted_template"))
            return finish("blocked", EVALUATOR_TEMPLATE)
        if not set(policy.network_hosts) <= set(TEMPLATE_HOSTS):
            findings.append(_f("network_allowlist_too_wide", "blocker",
                               f"sandbox network hosts {sorted(policy.network_hosts)} exceed template hosts"))
            return finish("blocked", EVALUATOR_TEMPLATE)
        try:
            change = self.io.get_json(proposal.patch_or_manifest)
            if change.get("schema") != CONNECTOR_CHANGE_SCHEMA:
                raise ValueError("not a declarative connector change")
            manifest = validate_manifest(change["manifest"])
        except (RevaError, ValueError, KeyError) as e:
            findings.append(_f("manifest_invalid", "blocker", str(getattr(e, "message", e))))
            return finish("fail", EVALUATOR_TEMPLATE)
        if manifest["template"] != TEMPLATE_ID:
            findings.append(_f("template_not_trusted", "blocker", "only the approved template can run"))
            return finish("blocked", EVALUATOR_TEMPLATE)
        if not set(manifest["allowed_hosts"]) <= set(policy.network_hosts):
            findings.append(_f("manifest_hosts_outside_policy", "blocker",
                               f"manifest hosts {manifest['allowed_hosts']} not in sandbox allowlist"))
            return finish("fail", EVALUATOR_TEMPLATE)
        inputs = self._inputs(manifest, policy)
        if inputs is None:
            findings.append(_f("no_read_only_inputs", "blocker", "no captured fixture bank for the planned requests"))
            return finish("blocked", EVALUATOR_TEMPLATE)
        request = {"manifest": manifest, "fixtures": inputs, "network_hosts": list(policy.network_hosts),
                   "max_items": policy.budget.max_requests, "max_bytes": policy.budget.max_bytes,
                   "network_probe": self.network_probe}
        out, usage = self._spawn(request, policy)
        evidence["child"] = out
        if out.get("timeout"):
            findings.append(_f("wall_time_exceeded", "blocker", "sandbox run exceeded its wall-time budget"))
            return finish("fail", EVALUATOR_TEMPLATE, usage)
        if not out.get("ok"):
            findings.append(_f("sandbox_run_failed", "blocker", str(out.get("error", "child failed"))[:500]))
            return finish("fail", EVALUATOR_TEMPLATE, usage)
        leaked = [k for k in out.get("environment_keys", []) if _secretish(k)]
        if leaked:
            findings.append(_f("secret_in_environment", "blocker", f"secret-like env keys present: {leaked}"))
        for r in out.get("results", []):
            for x in r["quality"]["findings"]:
                findings.append(_f(x["code"], x["severity"], x["message"]))
        if not out.get("results"):
            findings.append(_f("no_output", "blocker", "template produced no batches"))
        outcome = "fail" if any(x.severity == "blocker" for x in findings) else "pass"
        return finish(outcome, EVALUATOR_TEMPLATE, usage)

    async def _code_proposal(self, proposal: PlatformChangeProposal, policy: SandboxPolicy,
                             findings: list[Finding], evidence: dict[str, Any], finish: Any) -> ValidationReport:
        evidence["contains_code"] = True
        if self.external_runner is None or policy.isolation != "external_disposable_vm":
            findings.append(_f("no_isolated_runner", "blocker",
                               "proposal contains code; no isolated external runner is configured "
                               "(REVA_SANDBOX_ENDPOINT unset or isolation is not external_disposable_vm); "
                               "the proposal remains unexecuted"))
            return finish("blocked", EVALUATOR_EXTERNAL)
        request = {"proposal": proposal_json(proposal),
                   "patch_b64": base64.b64encode(self.io.get_bytes(proposal.patch_or_manifest)).decode(),
                   "policy": evidence["policy"],
                   "budget": {"max_cpu_seconds": policy.budget.max_cpu_seconds,
                              "max_memory_bytes": policy.budget.max_memory_bytes,
                              "max_wall_seconds": policy.budget.max_wall_seconds},
                   "network": "blocked_by_default", "secrets": "none"}
        resp = dict(await self.external_runner.run(request))
        evidence["external_response"] = resp
        outcome = resp.get("outcome") if resp.get("outcome") in ("pass", "fail", "blocked") else "blocked"
        for x in resp.get("findings") or []:
            sev = x.get("severity") if x.get("severity") in ("info", "warning", "blocker") else "warning"
            findings.append(_f(str(x.get("code", "external")), sev, str(x.get("message", ""))[:500]))
        return finish(outcome, EVALUATOR_EXTERNAL)

    def _inputs(self, manifest: dict[str, Any], policy: SandboxPolicy) -> list[dict[str, Any]] | None:
        if self.fixtures is None:
            return None
        from reva.adapters.connectors.eurostat_template import EurostatJsonStatConnector

        plan = EurostatJsonStatConnector(manifest).plan(None, FetchBudget(max_requests=policy.budget.max_requests,
                                                                          max_bytes=policy.budget.max_bytes))
        bank = BankHttp(self.io, self.fixtures)
        out = []
        for item in plan.items:
            if not bank.has(item.url):
                return None
            from reva.adapters.http.safe_client import request_fingerprint

            e = bank.entries[request_fingerprint("GET", item.url)]
            body = self.io.get_bytes(ArtifactRef(e["body_sha256"], "raw", e["media_type"]))
            out.append({"url": item.url, "body_b64": base64.b64encode(body).decode(), "media_type": e["media_type"],
                        "status": e["status"], "retrieved_at": e["retrieved_at"]})
        return out

    @staticmethod
    def _spawn(request: dict[str, Any], policy: SandboxPolicy) -> tuple[dict[str, Any], dict[str, Any]]:
        cpu = max(1, int(policy.budget.max_cpu_seconds))
        mem = int(policy.budget.max_memory_bytes)

        def limits() -> None:  # runs in the child before exec
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            if mem > 0:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            resource.setrlimit(resource.RLIMIT_FSIZE, (1_048_576, 1_048_576))

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="reva-sandbox-") as cwd:
            os.chmod(cwd, 0o500)  # read-only working directory
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", "-m", "reva.autonomy.sandbox_child"],
                    input=json.dumps(request).encode(), capture_output=True, env=scrubbed_environment(),
                    cwd=cwd, timeout=max(1, int(policy.budget.max_wall_seconds)), preexec_fn=limits,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "timeout": True}, {"wall_seconds": round(time.monotonic() - started, 3)}
            finally:
                os.chmod(cwd, 0o700)
        usage = {"wall_seconds": round(time.monotonic() - started, 3), "exit_code": proc.returncode,
                 "stdout_bytes": len(proc.stdout), "requests": 0}
        try:
            out = json.loads(proc.stdout or b"{}")
        except ValueError:
            out = {"ok": False, "error": "unparseable child output"}
        if proc.returncode != 0:
            out.setdefault("ok", False)
            out.setdefault("error", proc.stderr.decode(errors="replace")[-500:])
        return out, usage


def _secretish(key: str) -> bool:
    k = key.upper()
    return ("DATABASE" in k or (k.endswith("_URL") and "DB" in k) or any(
        s in k for s in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "ACCESS_KEY", "PRIVATE"))
        or (k.startswith("REVA_") and k.endswith("_KEY")))


def trusted_template_policy(budget: Any, version: str = "sandbox-1.0.0") -> SandboxPolicy:
    return SandboxPolicy(policy_version=version, isolation="trusted_template", network_hosts=TEMPLATE_HOSTS,
                         writable_paths=(), secret_access=False, budget=budget)
