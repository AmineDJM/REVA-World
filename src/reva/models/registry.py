"""Pure model-registry helpers: descriptors, version immutability, promotion pointers.

Rules
-----
* A model version ``key@semver`` is immutable. Registering the same reference
  with a different specification digest is an :class:`ImmutableRecordViolation`;
  changes require a new version.
* Promotion never mutates a model artifact. It appends a decision event; the
  *pointer* (which version is the incumbent for a task scope) is derived by
  replaying those events (:func:`current_pointer`).
* Rollback selects an earlier, previously promoted, compatible and non-retired
  version (:func:`rollback_target`); it is itself an appended event.

Pure module: no database, HTTP or adapter imports.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from reva.kernel.errors import ImmutableRecordViolation, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.models.applicability import ApplicabilityDescriptor, descriptor_from_registry

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ApprovalStatus = Literal["approved", "candidate", "retired"]
Decision = Literal["promote", "reject", "rollback", "retire", "keep_incumbent"]
POINTER_DECISIONS = frozenset({"promote", "rollback", "retire"})


def parse_semver(version: str) -> tuple[int, int, int]:
    m = SEMVER.match(version)
    if not m:
        raise ValidationProblem(f"model version {version!r} is not semantic (MAJOR.MINOR.PATCH)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def parse_ref(ref: str) -> tuple[str, str]:
    """``"forecast.log_drift@1.0.0"`` -> ``("forecast.log_drift", "1.0.0")``."""
    key, sep, version = ref.partition("@")
    if not sep or not key:
        raise ValidationProblem(f"model reference {ref!r} must be key@version")
    parse_semver(version)
    return key, version


def bump(version: str, part: Literal["major", "minor", "patch"]) -> str:
    major, minor, patch = parse_semver(version)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@dataclass(frozen=True)
class ModelDescriptor:
    """Registry view of one immutable model version."""

    model_key: str
    version: str
    kind: str
    approval_status: ApprovalStatus
    description: str
    input_ports: tuple[Mapping[str, Any], ...]
    output_ports: tuple[Mapping[str, Any], ...]
    applicability: Mapping[str, Any]
    assumptions: tuple[str, ...]
    spec: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        parse_semver(self.version)
        if self.approval_status not in ("approved", "candidate", "retired"):
            raise ValidationProblem(f"invalid approval status {self.approval_status!r}")

    @property
    def ref(self) -> str:
        return f"{self.model_key}@{self.version}"

    @property
    def spec_digest(self) -> str:
        """Logical digest of the full specification (what immutability protects)."""
        return logical_hash(dict(self.spec))

    def applicability_descriptor(self) -> ApplicabilityDescriptor:
        return descriptor_from_registry(self.spec)

    @classmethod
    def from_registry_entry(cls, entry: Mapping[str, Any]) -> ModelDescriptor:
        for k in ("model_key", "version", "kind", "approval_status"):
            if k not in entry:
                raise ValidationProblem(f"model registry entry lacks {k!r}")
        return cls(
            model_key=str(entry["model_key"]),
            version=str(entry["version"]),
            kind=str(entry["kind"]),
            approval_status=entry["approval_status"],
            description=str(entry.get("description", "")).strip(),
            input_ports=tuple(dict(p) for p in entry.get("input_ports", [])),
            output_ports=tuple(dict(p) for p in entry.get("output_ports", [])),
            applicability=dict(entry.get("applicability") or {}),
            assumptions=tuple(str(a) for a in entry.get("assumptions", [])),
            spec=dict(entry),
        )


def index_descriptors(entries: Iterable[Mapping[str, Any]]) -> dict[str, ModelDescriptor]:
    """Index registry entries by ``key@version``; duplicates must be identical."""
    out: dict[str, ModelDescriptor] = {}
    for e in entries:
        d = ModelDescriptor.from_registry_entry(e)
        if d.ref in out:
            assert_immutable_update(out[d.ref], d)
            continue
        out[d.ref] = d
    return out


def assert_immutable_update(existing: ModelDescriptor, proposed: ModelDescriptor) -> None:
    """Re-registering ``key@version`` is allowed only with an identical spec."""
    if existing.ref != proposed.ref:
        return
    if existing.spec_digest != proposed.spec_digest:
        raise ImmutableRecordViolation(
            f"model version {existing.ref} is immutable; publish a new version instead of mutating it",
            ref=existing.ref,
            existing_digest=existing.spec_digest,
            proposed_digest=proposed.spec_digest,
        )


def _port_signature(ports: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        sorted(
            (
                str(p.get("name")),
                str(p.get("concept", "")),
                str(p.get("unit", "")),
                str(p.get("kind", "")),
            )
            for p in ports
        )
    )


def versions_compatible(a: ModelDescriptor, b: ModelDescriptor) -> tuple[bool, tuple[str, ...]]:
    """Can ``b`` stand in for ``a`` as the incumbent of the same task?

    Same kind and the same output-port signature (names, concepts, units); a
    retired version is never compatible as a replacement.
    """
    reasons: list[str] = []
    if a.kind != b.kind:
        reasons.append(f"kind {a.kind!r} != {b.kind!r}")
    if _port_signature(a.output_ports) != _port_signature(b.output_ports):
        reasons.append("output port signatures differ")
    if b.approval_status == "retired":
        reasons.append(f"{b.ref} is retired")
    return (not reasons, tuple(reasons))


@dataclass(frozen=True)
class PromotionEvent:
    """One appended decision (mirrors a ``model.promotion`` row)."""

    ref: str
    decision: Decision
    task_key: str
    sequence: int  # total order of recording (e.g. recorded_at rank)
    reasons: tuple[str, ...] = ()


def current_pointer(events: Iterable[PromotionEvent], task_key: str, default: str) -> str:
    """Replay pointer-changing events for ``task_key``; ``default`` if none.

    ``promote``/``rollback`` move the pointer to the event's version; ``retire``
    of the current incumbent returns the pointer to ``default`` (the permanent
    naive baseline). ``keep_incumbent``/``reject`` never move it.
    """
    pointer = default
    for ev in sorted((e for e in events if e.task_key == task_key), key=lambda e: e.sequence):
        if ev.decision in ("promote", "rollback"):
            pointer = ev.ref
        elif ev.decision == "retire" and ev.ref == pointer:
            pointer = default
    return pointer


def rollback_target(
    events: Iterable[PromotionEvent],
    task_key: str,
    descriptors: Mapping[str, ModelDescriptor],
    default: str,
) -> str | None:
    """The version to roll back to from the current pointer, or ``None``.

    Walks the pointer history backwards and returns the most recent earlier
    incumbent that differs from the current one, is known, non-retired and
    compatible with it. Never invents a version; artifacts are not touched.
    """
    evs = sorted((e for e in events if e.task_key == task_key), key=lambda e: e.sequence)
    history: list[str] = [default]
    retired: set[str] = set()
    for ev in evs:
        if ev.decision in ("promote", "rollback"):
            history.append(ev.ref)
        elif ev.decision == "retire":
            retired.add(ev.ref)
            if history and history[-1] == ev.ref:
                history.append(default)
    current = history[-1]
    cur_desc = descriptors.get(current)
    for ref in reversed(history[:-1]):
        if ref == current or ref in retired:
            continue
        cand = descriptors.get(ref)
        if cand is None:
            continue
        if cur_desc is not None:
            ok, _ = versions_compatible(cur_desc, cand)
            if not ok:
                continue
        elif cand.approval_status == "retired":
            continue
        return ref
    return None


__all__ = [
    "ModelDescriptor",
    "PromotionEvent",
    "assert_immutable_update",
    "bump",
    "current_pointer",
    "index_descriptors",
    "parse_ref",
    "parse_semver",
    "rollback_target",
    "versions_compatible",
]
