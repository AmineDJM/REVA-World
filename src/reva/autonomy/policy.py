"""Versioned autonomy policies, kill switch, budgets and backlog bounds.

The autonomy agent can read policies but never edit them: every policy file it
relies on is verified against a digest pinned in this reviewed module (T3 code,
changed only by maintainers). If a policy file's bytes change, gates refuse to
run until a maintainer re-pins the digest. Text fetched from providers is data
and has no path into this module: allowlists, tiers and scopes come only from
the pinned policy bytes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, text

from reva.autonomy.common import AutonomyBlocked
from reva.ontology.registry import REGISTRY_ROOT
from reva.ports.autonomy import ArtifactRef

POLICY_DIR = REGISTRY_ROOT / "policies"

# Maintainer-pinned SHA-256 of policy file bytes. Changing a policy requires a
# reviewed change to these pins (T3); the agent has no write path to either.
PINNED_POLICY_DIGESTS: dict[str, str] = {
    "promotion.yaml": "46b757ea892cb571ec9503cc5be001cc97c3f589be31f6e3e75c6ea3faa6037a",
    "budgets.yaml": "048ec5bef503f29e771828d8394601572e59c133d10905a8a283cedf4bdbd08a",
}


class PolicyIntegrityError(AutonomyBlocked):
    code = "policy_integrity"


@dataclass(frozen=True)
class PinnedPolicy:
    path: Path
    sha256: str
    content: Mapping[str, Any]

    @property
    def version(self) -> str:
        return str(self.content.get("policy_version", "unversioned"))

    def ref(self) -> ArtifactRef:
        return ArtifactRef(sha256=self.sha256, namespace="policy", media_type="application/yaml")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_pinned(path: Path, expected_sha256: str) -> PinnedPolicy:
    """Load a policy only if its bytes match the pinned digest."""
    data = path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != expected_sha256:
        raise PolicyIntegrityError(
            f"policy {path.name} changed (sha256 {got[:12]}… != pinned {expected_sha256[:12]}…); "
            "gates refuse until a maintainer re-pins it",
            policy=path.name, expected=expected_sha256, actual=got,
        )
    content = yaml.safe_load(data)
    if not isinstance(content, dict):
        raise PolicyIntegrityError(f"policy {path.name} is not a mapping")
    return PinnedPolicy(path, got, content)


def promotion_policy(path: Path | None = None, expected: str | None = None) -> PinnedPolicy:
    return load_pinned(path or POLICY_DIR / "promotion.yaml", expected or PINNED_POLICY_DIGESTS["promotion.yaml"])


def budgets_policy(path: Path | None = None, expected: str | None = None) -> PinnedPolicy:
    return load_pinned(path or POLICY_DIR / "budgets.yaml", expected or PINNED_POLICY_DIGESTS["budgets.yaml"])


@dataclass
class AutonomyGuard:
    """Kill switch, mode and budget checks consulted before every autonomy action."""

    kill_switch: bool = False
    mode: str = "template_only"
    llm_enabled: bool = False
    llm_monthly_budget_usd: float = 0.0
    max_open_proposals: int = 20
    per_source_daily_requests: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any, budgets: PinnedPolicy | None = None) -> AutonomyGuard:
        b = budgets.content if budgets is not None else budgets_policy().content
        llm_budget = min(float(getattr(settings, "reva_llm_monthly_budget_usd", 0) or 0),
                         float((b.get("llm") or {}).get("monthly_usd", 0)))
        return cls(
            kill_switch=bool(getattr(settings, "reva_autonomy_kill_switch", False)),
            mode=str(getattr(settings, "reva_autonomy_mode", "template_only")),
            llm_enabled=bool(getattr(settings, "reva_llm_enabled", False)),
            llm_monthly_budget_usd=llm_budget,
            max_open_proposals=int((b.get("autonomy") or {}).get("max_open_proposals", 20)),
            per_source_daily_requests=dict(b.get("per_source_daily_requests") or {}),
        )

    def blocked_reason(self) -> str | None:
        if self.kill_switch:
            return "REVA_AUTONOMY_KILL_SWITCH is on: all autonomy actions are blocked"
        if self.mode == "disabled":
            return "REVA_AUTONOMY_MODE=disabled: all autonomy actions are blocked"
        if self.mode != "template_only":
            return f"unknown autonomy mode {self.mode!r}: blocked"
        return None

    def check(self, action: str) -> None:
        reason = self.blocked_reason()
        if reason:
            raise AutonomyBlocked(reason, action=action)

    def llm_blocked_reason(self) -> str | None:
        if self.blocked_reason():
            return self.blocked_reason()
        if not self.llm_enabled:
            return "blocked: llm disabled (REVA_LLM_ENABLED=false)"
        if self.llm_monthly_budget_usd <= 0:
            return "blocked: llm budget is 0"
        return None

    def check_backlog(self, conn: Connection) -> int:
        n = open_proposal_count(conn)
        if n >= self.max_open_proposals:
            raise AutonomyBlocked(
                f"proposal backlog is full ({n} open >= {self.max_open_proposals}); no new proposals",
                open=n, limit=self.max_open_proposals,
            )
        return n

    def source_request_limit(self, source_key: str) -> int:
        return int(self.per_source_daily_requests.get(source_key, 0))


def open_proposal_count(conn: Connection) -> int:
    """Proposals without a final gate decision (pass/fail) and without a human review."""
    return int(conn.execute(text(
        """SELECT count(*) FROM ops.proposal p
           WHERE NOT EXISTS (SELECT 1 FROM ops.gate_result g WHERE g.proposal_id = p.id
                             AND g.decision IN ('pass','fail'))
             AND NOT EXISTS (SELECT 1 FROM ops.proposal_review r WHERE r.proposal_id = p.id)"""
    )).scalar_one())


# --------------------------------------------------------------------------- tiers
T3_PATH_PREFIXES = ("src/reva/kernel", "src/reva/adapters/http", "src/reva/adapters/postgres",
                    "src/reva/autonomy", "migrations", "registry/policies", "src/reva/settings.py",
                    "src/reva/api/security", "contracts/", ".github", "Dockerfile", "pyproject.toml",
                    "uv.lock", "registry/sources")
T3_CAPABILITIES = {"paid_resources", "sensitive_data", "licence_change", "causal_claim", "destructive_migration",
                   "secret_access", "network_any", "edit_policy", "edit_allowlist", "edit_gate"}
T2_CAPABILITIES = {"execute_code", "new_model", "ontology_mapping", "new_parser", "legal_rule", "run_backtest"}
T1_CAPABILITIES = {"register_connector_version", "concept_eligibility"}
T0_CAPABILITIES = {"read", "discover", "report", "propose"}
CODE_SUFFIXES = (".py", ".pyx", ".so", ".sh", ".js", ".ts", ".sql", ".toml", ".cfg", ".ini", ".lock")


def contains_code(changed_paths: tuple[str, ...], capabilities: tuple[str, ...]) -> bool:
    return "execute_code" in capabilities or any(p.lower().endswith(CODE_SUFFIXES) for p in changed_paths)


def derive_tier(changed_paths: tuple[str, ...], capabilities: tuple[str, ...], scientific_change: bool,
                migration_required: bool) -> tuple[int, list[str]]:
    """Minimum tier implied by what a proposal touches. The proposer cannot lower it."""
    reasons: list[str] = []
    tier = 0
    caps = set(capabilities)
    if migration_required:
        tier = 3
        reasons.append("schema migration required")
    for p in changed_paths:
        if p.startswith(T3_PATH_PREFIXES):
            tier = 3
            reasons.append(f"protected path {p}")
    if caps & T3_CAPABILITIES:
        tier = 3
        reasons.append(f"T3 capabilities {sorted(caps & T3_CAPABILITIES)}")
    if tier < 3 and (contains_code(changed_paths, capabilities) or caps & T2_CAPABILITIES):
        tier = 2
        reasons.append("code, model, parser or ontology change")
    if tier < 2 and scientific_change and not (caps <= T1_CAPABILITIES | T0_CAPABILITIES):
        tier = 2
        reasons.append("scientific change outside approved template scope")
    if tier < 1 and (caps & T1_CAPABILITIES or changed_paths):
        tier = 1
        reasons.append("new series / declarative manifest")
    unknown = caps - T0_CAPABILITIES - T1_CAPABILITIES - T2_CAPABILITIES - T3_CAPABILITIES
    if unknown:
        tier = max(tier, 2)
        reasons.append(f"unreviewed capabilities {sorted(unknown)}")
    return tier, reasons
