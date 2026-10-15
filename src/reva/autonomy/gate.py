"""PromotionGate V1: versioned policy evaluator with explicit, recorded decisions.

* The gate verifies the policy bytes against the digest it was configured with
  (pinned by maintainers). A changed policy file makes the gate refuse
  (``blocked``): the agent cannot edit its own gates.
* The tier is *derived* from what the proposal touches (paths normalised first);
  a proposer cannot lower it by declaring T0. T0 promotes automatically; T1
  promotes automatically **only** inside the signed policy scope; T2/T3 always go
  to named review (``needs_review``).
* T1 scope is re-derived independently of the manifest (``binding.py``): the
  template code digest must equal the digest pinned in the policy; semantics must
  equal the registry concept profile of the approved exact mapping; source key and
  lineage family must be the approved template's registered source; the name,
  subject, logical-key layout (recomputed by template code), resolution
  dimensions and totals must be what reviewed code derives; the name may not
  collide with a built-in connector or a connector registered under another
  source/template.
* Reports are content-addressed: the gate re-reads each report's evidence
  artifact (digest-verified), uses the *stored* outcome/evaluator/findings, and
  ignores a report whose object disagrees with its stored evidence, or whose
  evaluator is the proposer or a gate. The validator's data contract is
  recomputed from the registry and must match the one the validator used.
* Honest limit (V1): the gate runs inside the worker that ran discovery, with the
  same credentials; independence is enforced by re-derivation and by DB checks
  (``ops.record_gate_result``: evaluator ``gate-evaluator:<name>`` that is not the
  proposal author, verified evidence artifact), not by process separation.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.adapters.connectors import eurostat_template as et
from reva.adapters.connectors.eurostat_template import TEMPLATE_HOSTS, validate_manifest
from reva.autonomy import binding
from reva.autonomy.common import ArtifactIO, AutonomyBlocked, ref_from, report_json
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
DB_DECISION = {"promote": "pass", "reject": "fail", "needs_review": "needs_review", "blocked": "blocked"}
CONNECTOR_CHANGE_SCHEMA = "reva-connector-change/1"


def record_gate_result(conn: Connection, proposal_id: uuid.UUID, gate_name: str, policy_version: str,
                       decision: str, evidence_sha256: str, evaluator: str) -> uuid.UUID:
    """Checked DB path (``ops.record_gate_result``, SECURITY DEFINER): evaluator must be
    ``gate-evaluator:<gate name>`` and not the author; evidence must be a verified artifact."""
    return uuid.UUID(str(conn.execute(
        text("SELECT ops.record_gate_result(:p, :g, :v, :d, :e, :ev)"),
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
        reports, verify_reasons, verified = self._verified_reports(proposal, independent_reports)
        reasons += verify_reasons
        trace["reports_verified"] = verified
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

    # ------------------------------------------------------------------ report verification
    def _verified_reports(self, proposal: PlatformChangeProposal, reports: Sequence[ValidationReport]
                          ) -> tuple[list[ValidationReport], list[str], list[dict[str, Any]]]:
        """Keep only reports whose object matches their stored, digest-verified evidence."""
        kept: list[ValidationReport] = []
        reasons: list[str] = []
        trace: list[dict[str, Any]] = []
        for r in reports:
            entry: dict[str, Any] = {"report_id": str(r.report_id), "evidence_sha256": r.evidence_manifest.sha256}
            trace.append(entry)
            if proposal.author in r.evaluator_version or r.evaluator_version.startswith(GATE_PREFIX):
                reasons.append(f"ignored report {r.report_id} from a non-independent evaluator")
                entry["verdict"] = "non_independent"
                continue
            try:
                ev = self.io.get_json(r.evidence_manifest)  # re-hashed against its digest
            except Exception as e:
                reasons.append(f"ignored report {r.report_id}: evidence unreadable ({type(e).__name__})")
                entry["verdict"] = "unreadable"
                continue
            if not isinstance(ev, dict):
                reasons.append(f"ignored report {r.report_id}: evidence is not a report document")
                entry["verdict"] = "malformed"
                continue
            stored_evaluator = str(ev.get("evaluator") or "")
            stored_blockers = sorted(str(f.get("code")) for f in ev.get("findings") or []
                                     if isinstance(f, dict) and f.get("severity") == "blocker")
            obj_blockers = sorted(f.code for f in r.findings if f.severity == "blocker")
            mismatch = [k for k, ok in (("outcome", ev.get("outcome") == r.outcome),
                                        ("evaluator", stored_evaluator == r.evaluator_version),
                                        ("blockers", stored_blockers == obj_blockers)) if not ok]
            if mismatch:
                reasons.append(f"ignored report {r.report_id}: report object disagrees with its stored evidence "
                               f"({', '.join(mismatch)})")
                entry["verdict"] = "mismatch"
                continue
            if proposal.author in stored_evaluator or stored_evaluator.startswith(GATE_PREFIX):
                reasons.append(f"ignored report {r.report_id}: evidence authored by a non-independent evaluator")
                entry["verdict"] = "non_independent"
                continue
            entry.update(verdict="verified", outcome=ev.get("outcome"), evaluator=stored_evaluator)
            kept.append(r)
        return kept, reasons, trace

    # ------------------------------------------------------------------ T1 scope
    def _t1_scope(self, proposal: PlatformChangeProposal, reports: Sequence[ValidationReport],
                  policy: PinnedPolicy) -> tuple[bool, list[str], dict[str, Any]]:
        from reva.ontology.registry import load_registry

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
        try:
            if self.io.get_json(ref_from(change["manifest_ref"])) != manifest:
                problems.append("embedded manifest differs from the referenced manifest artifact")
        except Exception:
            problems.append("manifest reference is unreadable")
        template = manifest["template"]
        tpl = binding.template_entry(policy, template)
        current_digest = et.template_digest()
        pinned_digest = binding.approved_template_digest(policy, template)
        if tpl is None:
            problems.append(f"template {template} not approved by policy")
        else:
            hosts = set(manifest["allowed_hosts"])
            if not hosts <= set(tpl.get("allowed_hosts") or []) or not hosts <= set(TEMPLATE_HOSTS):
                problems.append(f"hosts {sorted(hosts)} outside approved template hosts")
            if pinned_digest is None or current_digest != pinned_digest:
                problems.append("template code digest differs from the digest pinned in the signed policy "
                                "(template = reviewed code; a maintainer must re-pin it)")
        if change.get("template_digest") != current_digest:
            problems.append("proposal was generated against a different template revision")
        registry = load_registry()
        problems += binding.binding_problems(manifest, registry, policy)
        with self.engine.connect() as conn:
            problems += binding.name_conflicts(conn, manifest["name"], str(manifest["source_key"]), template)
        mapping = change.get("mapping") or {}
        sem = manifest["request_parameters"]["semantics"]
        target = {"concept_id": sem["concept_id"], "version": sem["concept_version"]}
        if mapping.get("relation") != "exact" or mapping.get("review_required"):
            problems.append(f"mapping relation {mapping.get('relation')!r} is not an approved exact mapping")
        if (mapping.get("target") or {}) != target:
            problems.append("mapping target differs from the manifest concept")
        rp = manifest["request_parameters"]
        geo = set(rp["filters"].get("geo", []))
        entry = binding.approved_entry(policy, template, manifest["dataset"], target["concept_id"],
                                       target["version"])
        if entry is None:
            problems.append("no approved exact mapping for this template/dataset/concept")
        else:
            if not geo or not geo <= set((entry.get("scope") or {}).get("geo") or []):
                problems.append(f"geography {sorted(geo)} outside approved scope {entry.get('scope')}")
            if entry.get("rights") != "verified" or (change.get("candidate") or {}).get("rights_status") != "verified":
                problems.append("rights are not verified")
        manifest_sha = (change.get("manifest_ref") or {}).get("sha256")
        expected_contract: str | None = None
        try:
            from reva.autonomy.quality import contract_for

            expected_contract = self.io.put_json(contract_for(manifest, registry), "models").sha256
        except RevaError as e:
            problems.append(f"no registry-derived data contract: {e.message}")
        kinds = set()
        for r in reports:
            if r.outcome != "pass":
                continue
            kind = r.evaluator_version.split(":", 1)[0]
            ev = self.io.get_json(r.evidence_manifest)  # already verified in _verified_reports
            if kind == "connector-validator":
                covers = ev.get("manifest") == manifest_sha
                if covers and ev.get("data_contract") != expected_contract:
                    problems.append("validator did not use the registry-derived data contract")
                    covers = False
                if covers and ev.get("template_digest") != current_digest:
                    problems.append("validator ran a different template revision")
                    covers = False
            elif kind == "sandbox":
                covers = ev.get("proposal_id") == str(proposal.proposal_id) and ev.get("manifest") == manifest_sha
            else:
                covers = False
            if covers:
                kinds.add(kind)
            elif kind in ("connector-validator", "sandbox"):
                problems.append(f"{kind} report does not cover this proposal/manifest")
        for needed in ("connector-validator", "sandbox"):
            if needed not in kinds:
                problems.append(f"missing passing independent {needed} report")
        scope = {"template": template, "template_digest": current_digest, "pinned_template_digest": pinned_digest,
                 "dataset": manifest["dataset"], "target": target, "geo": sorted(geo),
                 "hosts": manifest["allowed_hosts"], "approved_entry": entry, "connector": manifest["name"],
                 "version": manifest["version"], "source_key": manifest["source_key"],
                 "data_contract_sha256": expected_contract}
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
