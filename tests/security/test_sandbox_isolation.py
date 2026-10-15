"""Generated code never runs without an isolated external runner; the trusted-template
sandbox gets no secrets and no network; the production worker never imports proposed code."""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from reva.autonomy.common import ArtifactIO, default_budget
from reva.autonomy.fixtures import bank_from_dir
from reva.autonomy.gate import CONNECTOR_CHANGE_SCHEMA
from reva.autonomy.proposals import base_commit
from reva.autonomy.sandbox import (
    HttpExternalRunner,
    TrustedTemplateSandbox,
    external_runner_from_settings,
    scrubbed_environment,
    trusted_template_policy,
)
from reva.kernel.errors import ConfigurationError
from reva.ports.autonomy import PlatformChangeProposal, SandboxPolicy, ValueEstimate

CONNECTOR = "eurostat.demo_pjan.fr"
SECRET_RE = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY|DATABASE|_URL$)", re.I)
AUTONOMY_SRC = Path(__file__).parents[2] / "src" / "reva" / "autonomy"


@pytest.fixture(autouse=True)
def _offline_sandbox_probe(monkeypatch):
    """Sandbox isolation probes target a closed local port in tests (no outbound packets)."""
    from reva.autonomy import sandbox as _sb

    monkeypatch.setattr(_sb, "PUBLIC_PROBE_ADDR", ("127.0.0.1", 1))


@pytest.fixture()
def io(tmp_path) -> ArtifactIO:
    from reva.adapters.objects.filesystem import FilesystemArtifactStore

    return ArtifactIO(FilesystemArtifactStore(tmp_path / "objects"))


def _value(io) -> ValueEstimate:
    return ValueEstimate("unsupported", None, None, {}, io.put_json({"t": 1}, "evaluation"), "t")


def _code_proposal(io, marker: Path) -> PlatformChangeProposal:
    patch = (f"open({str(marker)!r}, 'w').write('executed')\n"
             "import os; os.environ['REVA_PWNED'] = '1'\n").encode()
    ref = io.put_bytes(patch, "models", "text/x-python")
    return PlatformChangeProposal(uuid.uuid4(), "agent:codegen", 2, ("src/reva/autonomy/generated/evil_patch.py",),
                                  base_commit(), ref, ("execute_code",), True, False, _value(io), "promotion-1.1.0")


def _manifest_proposal(io) -> tuple[PlatformChangeProposal, object]:
    from datetime import UTC, datetime

    from reva.autonomy.common import make_context, run_sync
    from reva.autonomy.discovery import EurostatCatalogueDiscoverer
    from reva.autonomy.fixtures import BankHttp
    from reva.autonomy.generator import DeclarativeConnectorGenerator
    from reva.autonomy.jobs import connector_change_proposal
    from reva.autonomy.mapping import RegistryOntologyMapper
    from reva.autonomy.policy import promotion_policy
    from reva.ports.autonomy import KnowledgeGap

    bank = bank_from_dir(io, CONNECTOR)
    ctx = make_context(io, "evidence", datetime.now(UTC), {"t": 1})
    gap = KnowledgeGap(uuid.uuid4(), "R-POP-JAN-AGESEX", "demography.population.jan1_age_sex@1.0.0", ("FRA",),
                       {"years": [2023]}, "unsupported", 1.0, "t", ctx)
    cands = run_sync(EurostatCatalogueDiscoverer(io, BankHttp(io, bank)).discover(gap, default_budget()))
    mapping = RegistryOntologyMapper(io).assess(cands[0], ctx)[0]
    cp = DeclarativeConnectorGenerator(io).generate(cands[0], mapping, ("eurostat_jsonstat",))
    p = connector_change_proposal(io, cp, io.get_json(cp.manifest), mapping, _value(io), promotion_policy())
    return p, bank


async def test_generated_code_is_blocked_without_external_runner(io, tmp_path, monkeypatch):
    monkeypatch.delenv("REVA_SANDBOX_ENDPOINT", raising=False)
    from reva.settings import Settings

    assert external_runner_from_settings(Settings(_env_file=None)) is None  # type: ignore[call-arg]
    marker = tmp_path / "marker"
    p = _code_proposal(io, marker)
    for isolation in ("trusted_template", "external_disposable_vm"):
        pol = SandboxPolicy("sandbox-1.0.0", isolation, (), (), False, default_budget())  # type: ignore[arg-type]
        rep = await TrustedTemplateSandbox(io, external_runner=None).run(p, pol)
        assert rep.outcome == "blocked"
        assert "no_isolated_runner" in {f.code for f in rep.findings}
    assert not marker.exists() and "REVA_PWNED" not in os.environ


async def test_external_runner_posts_only_when_configured(io, tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"outcome": "fail", "findings": [
            {"code": "tests_failed", "severity": "blocker", "message": "unit tests failed in VM"}]})

    runner = HttpExternalRunner("https://runner.example.org/run", "tok", transport=httpx.MockTransport(handler))
    p = _code_proposal(io, tmp_path / "m")
    pol = SandboxPolicy("sandbox-1.0.0", "external_disposable_vm", (), (), False, default_budget())
    rep = await TrustedTemplateSandbox(io, external_runner=runner).run(p, pol)
    assert len(seen) == 1 and seen[0].method == "POST" and seen[0].headers["authorization"] == "Bearer tok"
    body = json.loads(seen[0].content)
    assert body["secrets"] == "none" and body["network"] == "blocked_by_default"
    assert rep.outcome == "fail" and rep.evaluator_version.startswith("sandbox:external")
    with pytest.raises(ConfigurationError):
        HttpExternalRunner("http://runner.example.org/run")
    # a trusted-template sandbox with no runner never contacts anything for code proposals
    seen.clear()
    await TrustedTemplateSandbox(io).run(p, pol)
    assert seen == []


async def test_trusted_template_env_has_no_secrets_and_network_is_template_only(io, monkeypatch):
    for k, v in {"REVA_WORKER_DB_PASSWORD": "pw", "REVA_API_DB_PASSWORD": "pw", "REVA_SESSION_SECRET": "s" * 40,
                 "REVA_S3_SECRET_ACCESS_KEY": "k", "REVA_LLM_API_KEY": "k", "REVA_SANDBOX_TOKEN": "t",
                 "DATABASE_URL": "postgresql://x:y@h/db", "REVA_MIGRATION_DATABASE_URL": "postgresql://o:p@h/db",
                 "REVA_COMTRADE_API_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    env = scrubbed_environment()
    assert not [k for k in env if SECRET_RE.search(k) or (k.startswith("REVA_") and k != "REVA_SANDBOX_ISOLATION")]
    p, bank = _manifest_proposal(io)
    sb = TrustedTemplateSandbox(io, fixtures=bank)
    sb.network_probe = "ec.europa.eu"  # even the template host is unreachable: replay only
    rep = await sb.run(p, trusted_template_policy(default_budget()))
    assert rep.outcome == "pass", [(f.code, f.message) for f in rep.findings]
    child = io.get_json(rep.evidence_manifest)["child"]
    leaked = [k for k in child["environment_keys"] if SECRET_RE.search(k) or "DATABASE" in k]
    assert leaked == []
    assert not any(k.startswith("REVA_") and k != "REVA_SANDBOX_ISOLATION" for k in child["environment_keys"])
    assert child["network_allowlist"] == ["ec.europa.eu"]
    assert child["network_probe"].startswith("refused")
    assert child["results"][0]["observations"] == 309
    # widening the sandbox network beyond the template's hosts is refused
    wide = SandboxPolicy("sandbox-1.0.0", "trusted_template", ("ec.europa.eu", "evil.example.com"), (), False,
                         default_budget())
    assert (await TrustedTemplateSandbox(io, fixtures=bank).run(p, wide)).outcome == "blocked"


async def test_manifest_proposal_cannot_smuggle_code(io):
    p, bank = _manifest_proposal(io)
    change = io.get_json(p.patch_or_manifest)
    assert change["schema"] == CONNECTOR_CHANGE_SCHEMA
    change["manifest"]["template"] = "python_inline"
    bad = PlatformChangeProposal(p.proposal_id, p.author, 1, p.changed_paths, p.base_commit,
                                 io.put_json(change, "models"), p.requested_capabilities, True, False,
                                 p.expected_benefit, p.policy_version)
    rep = await TrustedTemplateSandbox(io, fixtures=bank).run(bad, trusted_template_policy(default_budget()))
    assert rep.outcome in ("fail", "blocked") and "manifest_invalid" in {f.code for f in rep.findings}


def test_worker_never_imports_proposed_code(io, tmp_path):
    marker = tmp_path / "marker"
    _code_proposal(io, marker)
    assert not any(m.startswith("reva.autonomy.generated") or "evil_patch" in m for m in sys.modules)
    assert not marker.exists()
    # static guarantee: the autonomy package has no dynamic code loading/execution primitive
    forbidden = re.compile(r"(?<![.\w])(exec|eval|compile)\(|import_module\(|__import__\("
                           r"|runpy|spec_from_file_location")
    offenders = [f"{p.name}:{i}" for p in AUTONOMY_SRC.glob("*.py")
                 for i, line in enumerate(p.read_text().splitlines(), 1) if forbidden.search(line)]
    assert offenders == []
    # the sandbox child only ever runs the reviewed template module
    child_src = (AUTONOMY_SRC / "sandbox_child.py").read_text()
    assert "eurostat_template import EurostatJsonStatConnector" in child_src


# --------------------------------------------------------------------------- R4-10 / R4-15
async def test_isolation_evidence_is_measured_not_nominal(io, monkeypatch):
    """network_mode is 'blocked_verified' only if raw-_socket probes failed inside the child."""
    from reva.autonomy import sandbox as sb_mod

    monkeypatch.setenv("R4_PARENT_SECRET", "x")
    monkeypatch.setattr(sb_mod, "PUBLIC_PROBE_ADDR", ("127.0.0.1", 1))  # tests stay offline
    p, bank = _manifest_proposal(io)
    rep = await TrustedTemplateSandbox(io, fixtures=bank).run(p, trusted_template_policy(default_budget()))
    assert rep.outcome == "pass", [(f.code, f.message) for f in rep.findings]
    child = io.get_json(rep.evidence_manifest)["child"]
    probes = child["network_probe_raw"]
    verified = probes["loopback_listener"].startswith("failed") and probes["public_ip"].startswith("failed")
    assert child["network_mode"] == ("blocked_verified" if verified else "python_guard_only")
    assert child["rlimits"]["applied"] is True and child["rlimits"]["nofile"] == [64, 64]
    assert child["cwd_writable"] is False
    if sb_mod.namespace_isolation() is not None:  # available in this container (unprivileged userns)
        assert child["network_mode"] == "blocked_verified" and child["parent_environ_readable"] is False
        assert child["process_isolation"].startswith("user+pid+net namespaces")

    # Without namespaces the same run must say so honestly (python guard only, same uid).
    monkeypatch.setattr(sb_mod, "namespace_isolation", lambda: None)
    rep2 = await TrustedTemplateSandbox(io, fixtures=bank).run(p, trusted_template_policy(default_budget()))
    child2 = io.get_json(rep2.evidence_manifest)["child"]
    assert child2["launcher"] == "plain_subprocess"
    assert child2["network_probe_raw"]["loopback_listener"] == "connected"  # same netns as the worker
    assert child2["network_mode"] == "python_guard_only"
    assert child2["process_isolation"] == "none (same uid)"
    codes = {f.code: f.severity for f in rep2.findings}
    assert codes.get("network_not_os_isolated") == "warning"
    assert rep2.outcome == "pass"  # trusted template only: honest warning, not a boundary claim


def test_python_guard_blocks_socket_and_raw_socket(tmp_path):
    import subprocess

    code = (
        "from reva.autonomy.sandbox_child import _block_network\n"
        "_block_network(['ec.europa.eu'])\n"
        "import socket, _socket, asyncio\n"
        "out = []\n"
        "for f in (lambda: socket.create_connection(('127.0.0.1', 9)),\n"
        "          lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),\n"
        "          lambda: _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM),\n"
        "          lambda: _socket.socket(_socket.AF_INET6, _socket.SOCK_DGRAM)):\n"
        "    try:\n"
        "        f(); out.append('allowed')\n"
        "    except PermissionError:\n"
        "        out.append('refused')\n"
        "asyncio.run(asyncio.sleep(0))  # the event loop's local socketpair still works\n"
        "print(','.join(out))\n")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60, cwd=tmp_path,
                       env=scrubbed_environment())
    assert r.stdout.decode().strip() == "refused,refused,refused,refused", r.stderr.decode()[-500:]


def test_no_preexec_fn_and_dead_proxies():
    src = (AUTONOMY_SRC / "sandbox.py").read_text()
    assert "preexec_fn" not in src.replace("no ``preexec_fn``", "").replace("No ``preexec_fn``", "")
    env = scrubbed_environment({"HTTPS_PROXY": "http://real-proxy:3128", "HOME": "/root"})
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:9" and env["HOME"] == "/nonexistent"
