"""P11 external isolated code-runner interface (architecture §18.3).

A Python subprocess is not a security sandbox, and Docker without an
appropriate isolation policy is not enough for hostile code. Generated code
may only run in a dedicated disposable VM/microVM (or equivalently isolated
external runner) with no secrets, read-only inputs, egress blocked by default,
bounded CPU/RAM/disk/time and allowlisted dependencies — never inside the V1
worker.

:class:`ExternalRunPolicy` encodes those invariants so an unsafe policy cannot
be constructed. :func:`resolve_external_runner` is the only way to obtain a
runner and, in this build, always raises ``CapabilityGap``: ``blocked`` when
``REVA_SANDBOX_ENDPOINT`` is unset, ``reserved`` when it is set (no runner
client has passed the P11 privilege/egress/resource attack gates). Proposals
therefore remain unexecuted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from reva.capabilities import require
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import is_sha256

Isolation = Literal["firecracker_microvm", "gvisor_vm", "disposable_cloud_vm"]
_REJECTED_ISOLATION = {"python_subprocess", "docker_default", "local_container", "worker_process"}


@dataclass(frozen=True)
class RunnerResourceBudget:
    vcpus: int
    cpu_seconds: int
    memory_bytes: int
    disk_bytes: int
    wall_seconds: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        for name in ("vcpus", "cpu_seconds", "memory_bytes", "disk_bytes", "wall_seconds", "max_output_bytes"):
            v = getattr(self, name)
            if not (isinstance(v, int) and not isinstance(v, bool) and v > 0):
                raise ValidationProblem(f"runner budget {name} must be a positive int (unbounded is not allowed)")


@dataclass(frozen=True)
class ExternalRunPolicy:
    policy_version: str
    isolation: str
    budget: RunnerResourceBudget
    allowlisted_dependencies: tuple[str, ...]
    """Pinned requirement strings with hashes, e.g. 'numpy==2.1.3 --hash=sha256:…'."""
    egress_allowlist: tuple[str, ...] = ()
    """Empty = network blocked (default). Hosts here require a separate approval."""
    secrets_mounted: Literal[False] = False
    inputs_read_only: Literal[True] = True
    disposable: Literal[True] = True

    def __post_init__(self) -> None:
        if self.isolation in _REJECTED_ISOLATION or self.isolation not in (
            "firecracker_microvm",
            "gvisor_vm",
            "disposable_cloud_vm",
        ):
            raise ValidationProblem(
                f"isolation {self.isolation!r} is not acceptable for generated code; a disposable VM/microVM "
                "is required"
            )
        if self.secrets_mounted is not False:
            raise ValidationProblem("external runs must not receive secrets")
        if self.inputs_read_only is not True:
            raise ValidationProblem("external run inputs must be read-only")
        if self.disposable is not True:
            raise ValidationProblem("external runners must be disposable")
        for dep in self.allowlisted_dependencies:
            if "==" not in dep or "--hash=sha256:" not in dep:
                raise ValidationProblem(f"dependency {dep!r} must be pinned with a sha256 hash")
        for host in self.egress_allowlist:
            if not host or "/" in host or "*" in host:
                raise ValidationProblem(f"egress entry {host!r} must be an exact host name")

    @property
    def network_blocked(self) -> bool:
        return not self.egress_allowlist


@dataclass(frozen=True)
class RunInput:
    name: str
    sha256: str
    media_type: str

    def __post_init__(self) -> None:
        if not is_sha256(self.sha256):
            raise ValidationProblem(f"run input {self.name!r} must be content-addressed (sha256)")


@dataclass(frozen=True)
class ExternalRunRequest:
    proposal_id: UUID
    base_commit: str
    patch_sha256: str
    command_manifest_sha256: str
    inputs: tuple[RunInput, ...]
    """Minimal, non-sensitive fixtures only; mounted read-only."""
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        for digest in (self.patch_sha256, self.command_manifest_sha256):
            if not is_sha256(digest):
                raise ValidationProblem("patch and command manifest must be content-addressed")
        lowered = {k.upper() for k in self.environment}
        suspicious = sorted(
            k
            for k in lowered
            if any(s in k for s in ("TOKEN", "SECRET", "PASSWORD", "KEY", "DATABASE_URL", "CREDENTIAL"))
        )
        if suspicious:
            raise ValidationProblem(f"environment variables look like secrets and are refused: {suspicious}")


@dataclass(frozen=True)
class ExternalRunReceipt:
    run_id: str
    proposal_id: UUID
    status: Literal["passed", "failed", "blocked", "timeout", "resource_exceeded"]
    report_sha256: str | None
    resource_usage: Mapping[str, Any]
    runner_attestation_sha256: str | None
    """Attestation of the isolation boundary; gate evidence comes from an independent evaluator."""


@runtime_checkable
class ExternalRunner(Protocol):
    """A remote, disposable, isolated runner. Implementations live outside the V1 worker."""

    runner_id: str

    async def run(self, request: ExternalRunRequest, policy: ExternalRunPolicy) -> ExternalRunReceipt: ...
    async def cancel(self, run_id: str) -> None: ...


def resolve_external_runner(settings: Any | None = None) -> ExternalRunner:
    """Return a configured runner or raise ``CapabilityGap`` (``autonomy.external_sandbox_runner``).

    ``settings`` is duck-typed (``reva_sandbox_endpoint``); the endpoint and token are
    never copied into error details.
    """
    require("autonomy.external_sandbox_runner", settings)
    raise AssertionError("unreachable: no external runner client exists in this build")  # pragma: no cover
