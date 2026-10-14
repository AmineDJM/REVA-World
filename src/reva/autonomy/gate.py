"""PromotionGate V1: versioned policy evaluator with explicit, recorded decisions.

* The gate verifies the policy bytes against the digest it was configured with
  (pinned by maintainers). A changed policy file makes the gate refuse
  (``blocked``): the agent cannot edit its own gates.
* The tier is *derived* from what the proposal touches; a proposer cannot lower
  it by declaring T0. T0 promotes automatically; T1 promotes only inside the
  signed policy scope (approved template at its reviewed digest, approved exact
  mapping, verified rights, known hosts, passing independent validator and
  sandbox reports); T2/T3 always go to named review.
* Reports authored by the proposer are ignored. Gate results are written to
  ``ops.gate_result`` with evaluator ``gate-evaluator:<name>``; the database
  refuses an evaluator equal to the proposal author.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.adapters.connectors.eurostat_template import TEMPLATE_HOSTS, template_digest, validate_manifest
from reva.autonomy.common import ArtifactIO, AutonomyBlocked, report_json
from reva.autonomy.policy import (
    PINNED_POLICY_DIGESTS,
    POLICY_DIR,
    AutonomyGuard,
    PinnedPolicy,
    derive_tier,
    load_pinned,
)
from reva.kernel.errors import PolicyDenied, RevaError, ValidationProblem
from reva.ports.autonomy import ArtifactRef, GateDecision, PlatformChangeProposal, ValidationReport

GATE_PREFIX = "gate-evaluator:"
DB_DECISION = {"promote": "pass", "reject": "fail", "needs_review": "blocked", "blocked": "blocked"}
CONNECTOR_CHANGE_SCHEMA = "reva-connector-change/1"


def record_gate_result(conn: Connection, proposal_id: uuid.UUID, gate_name: str, policy_version: str,
                       decision: str, evidence_sha256: str, evaluator: str) -> uuid.UUID:
    return uuid.UUID(str(conn.execute(
        text("""INSERT INTO ops.gate_result(proposal_id, gate_name, policy_version, decision, evidence_sha256,
                     evaluator) VALUES (:p, :g, :v, :d, :e, :ev) RETURNING id"""),
        {"p": proposal_id, "g": gate_name, "v": policy_version, "d": decision, "e": evidence_sha256,
         "ev": evaluator},
    ).scalar_one()))


class PolicyPromotionGate:
    def __init__(self, engine: Engine, io_: ArtifactIO, *, name: str = "promotion", policy_path: Path | None = None,
                 pinned_sha256: str | None = None, guard: AutonomyGuard | None = None) -> None:
        if not name or ":" in name:
            raise ValueError("gate name must be a plain identifier")
        self.engine = engine
        self.io = io_
        self.name = name
        self.evaluator = f"{GATE_PREFIX}{name}"
        self.policy_path = policy_path or POLICY_DIR / "promotion.yaml"
        self.pinned_sha256 = pinned_sha256 or PINNED_POLICY_DIGESTS["promotion.yaml"]
        self.guard = guard

    # ------------------------------------------------------------------ protocol
    def decide(self, proposal: PlatformChangeProposal, independent_reports: Sequence[ValidationReport],
               signed_policy: ArtifactRef) -> GateDecision:
        if proposal.author in (self.evaluator, self.name) or self.evaluator == GATE_PREFIX + proposal.author:
            raise PolicyDenied("the proposer cannot evaluate its own proposal", author=proposal.author)
        if self.guard is not None and self.guard.blocked_reason():
            return GateDecision(uuid.uuid4(), "blocked", proposal.proposal_id, "n/a", (),
                                (self.guard.blocked_reason() or "",), self.evaluator, None)
        reasons: list[str] = []
        policy: PinnedPolicy | None = None
        try:
            policy = load_pinned(self.policy_path, self.pinned_sha256)
        except AutonomyBlocked as e:
            reasons.append(e.message)
        if policy is not None and signed_policy.sha256 != policy.sha256:
            reasons.append("signed policy reference does not match the pinned policy digest")
        if reasons or policy is None:
            return self._record(proposal, "blocked", reasons, [], None, {"stage": "policy_integrity"})
        derived, why = derive_tier(proposal.changed_paths, proposal.requested_capabilities,
                                   proposal.scientific_change, proposal.migration_required)
        tier = max(int(proposal.permission_tier), derived)
        trace: dict[str, Any] = {"declared_tier": proposal.permission_tier, "derived_tier": derived,
                                 "derivation": why, "effective_tier": tier}
        if derived > proposal.permission_tier:
            reasons.append(f"declared tier T{proposal.permission_tier} raised to T{derived}: {'; '.join(why)}")
        if proposal.policy_version != policy.version:
            reasons.append(f"proposal targets policy {proposal.policy_version}, gate runs {policy.version}")
            return self._record(proposal, "reject", reasons, list(independent_reports), policy, trace)
        reports = []
        for r in independent_reports:
            if proposal.author in r.evaluator_version or r.evaluator_version.startswith(GATE_PREFIX):
                reasons.append(f"ignored report {r.report_id} from a non-independent evaluator")
                continue
            reports.append(r)
        if any(r.outcome == "blocked" for r in reports):
            reasons += [f"{r.evaluator_version}: {f.message}" for r in reports if r.outcome == "blocked"
                        for f in r.findings if f.severity == "blocker"]
            return self._record(proposal, "blocked", reasons, reports, policy, trace)
        if any(r.outcome == "fail" for r in reports):
            reasons += [f"{r.evaluator_version}: {f.code}" for r in reports if r.outcome == "fail"
                        for f in r.findings if f.severity == "blocker"]
            return self._record(proposal, "reject", reasons, reports, policy, trace)
        if tier == 0:
            reasons.append("T0: read/discover/propose within budget is automatic")
            return self._record(proposal, "promote", reasons, reports, policy, trace)
        if tier >= 2:
            need = "maintainer approval" if tier == 3 else "isolated evaluation and named review"
            reasons.append(f"T{tier}: requires {need}")
            return self._record(proposal, "needs_review", reasons, reports, policy, trace)
        ok, scope_reasons, scope = self._t1_scope(proposal, reports, policy)
        trace["t1_scope"] = scope
        if ok:
            reasons.append("T1 within signed policy scope: approved template, exact mapping, verified rights, "
                           "known hosts, independent validation and sandbox passed")
            return self._record(proposal, "promote", reasons, reports, policy, trace)
        reasons += ["T1 out of signed policy scope", *scope_reasons]
        return self._record(proposal, "reject", reasons, reports, policy, trace)

    # ------------------------------------------------------------------ T1 scope
    def _t1_scope(self, proposal: PlatformChangeProposal, reports: Sequence[ValidationReport],
                  policy: PinnedPolicy) -> tuple[bool, list[str], dict[str, Any]]:
        problems: list[str] = []
        try:
            change = self.io.get_json(proposal.patch_or_manifest)
        except Exception as e:
            return False, [f"unreadable manifest: {type(e).__name__}"], {}
        if not isinstance(change, dict) or change.get("schema") != CONNECTOR_CHANGE_SCHEMA:
            return False, ["T1 auto-promotion only covers declarative connector manifests"], {}
        manifest = change.get("manifest") or {}
        try:
            manifest = validate_manifest(manifest)
        except RevaError as e:
            return False, [f"manifest invalid: {e.message}"], {}
        template = manifest["template"]
        tpl = (policy.content.get("approved_templates") or {}).get(template)
        if tpl is None:
            problems.append(f"template {template} not approved by policy")
        else:
            hosts = set(manifest["allowed_hosts"])
            if not hosts <= set(tpl.get("allowed_hosts") or []) or not hosts <= set(TEMPLATE_HOSTS):
                problems.append(f"hosts {sorted(hosts)} outside approved template hosts")
        if change.get("template_digest") != template_digest():
            problems.append("template code differs from the reviewed template digest")
        mapping = change.get("mapping") or {}
        sem = manifest["request_parameters"]["semantics"]
        target = {"concept_id": sem["concept_id"], "version": sem["concept_version"]}
        if mapping.get("relation") != "exact" or mapping.get("review_required"):
            problems.append(f"mapping relation {mapping.get('relation')!r} is not an approved exact mapping")
        rp = manifest["request_parameters"]
        geo = set(rp["filters"].get(rp["subject"]["dimension"], []))
        entry = next((m for m in policy.content.get("approved_exact_mappings") or []
                      if m.get("template") == template and m.get("dataset") == manifest["dataset"]
                      and m.get("target", {}).get("concept_id") == target["concept_id"]
                      and str(m.get("target", {}).get("version")) == target["version"]), None)
        if entry is None:
            problems.append("no approved exact mapping for this template/dataset/concept")
        else:
            if not geo or not geo <= set((entry.get("scope") or {}).get("geo") or []):
                problems.append(f"geography {sorted(geo)} outside approved scope {entry.get('scope')}")
            if entry.get("rights") != "verified" or (change.get("candidate") or {}).get("rights_status") != "verified":
                problems.append("rights are not verified")
        concept_key = f"{target['concept_id']}@{target['version']}"
        if any(not s.startswith(concept_key + ":") for s in manifest["eligibility_scope"]):
            problems.append("eligibility scope names concepts outside the approved mapping")
        manifest_sha = (change.get("manifest_ref") or {}).get("sha256")
        kinds = set()
        for r in reports:
            if r.outcome != "pass":
                continue
            kind = r.evaluator_version.split(":", 1)[0]
            try:
                ev = self.io.get_json(r.evidence_manifest)
            except Exception:  # noqa: BLE001 - unverifiable evidence does not count
                problems.append(f"{kind} report evidence is unreadable")
                continue
            covers = (ev.get("manifest") == manifest_sha if kind == "connector-validator"
                      else ev.get("proposal_id") == str(proposal.proposal_id) if kind == "sandbox" else False)
            if covers:
                kinds.add(kind)
            elif kind in ("connector-validator", "sandbox"):
                problems.append(f"{kind} report does not cover this proposal/manifest")
        for needed in ("connector-validator", "sandbox"):
            if needed not in kinds:
                problems.append(f"missing passing independent {needed} report")
        scope = {"template": template, "dataset": manifest["dataset"], "target": target, "geo": sorted(geo),
                 "hosts": manifest["allowed_hosts"], "approved_entry": entry}
        return not problems, problems, scope

    # ------------------------------------------------------------------ recording
    def _record(self, proposal: PlatformChangeProposal, outcome: str, reasons: list[str],
                reports: Sequence[ValidationReport], policy: PinnedPolicy | None,
                trace: dict[str, Any]) -> GateDecision:
        version = policy.version if policy is not None else "policy-integrity-failure"
        evidence = {
            "schema": "reva-gate-decision/1", "gate": self.name, "evaluator": self.evaluator,
            "proposal_id": str(proposal.proposal_id), "proposal_author": proposal.author,
            "outcome": outcome, "db_decision": DB_DECISION[outcome], "reasons": reasons,
            "policy": {"version": version, "sha256": policy.sha256 if policy else None,
                       "pinned_sha256": self.pinned_sha256, "path": self.policy_path.name},
            "tier": trace, "reports": [report_json(r) for r in reports],
        }
        ref = self.io.put_json(evidence, "evaluation")
        with self.engine.begin() as conn:
            exists = conn.execute(text("SELECT 1 FROM ops.proposal WHERE id = :i"),
                                  {"i": proposal.proposal_id}).scalar_one_or_none()
            if exists is None:
                raise ValidationProblem("gate evaluates only persisted proposals (ops.proposal)")
            self.io.register(conn, ref, "gate-decision/1")
            gid = record_gate_result(conn, proposal.proposal_id, self.name, version, DB_DECISION[outcome],
                                     ref.sha256, self.evaluator)
        return GateDecision(
            decision_id=gid, outcome=outcome,  # type: ignore[arg-type]
            proposal_id=proposal.proposal_id, policy_version=version,
            reports=tuple(r.evidence_manifest for r in reports), reasons=tuple(reasons),
            reviewer_identity=self.evaluator, signed_receipt=ref,
        )
