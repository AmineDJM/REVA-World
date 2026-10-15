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
* (R6-03) The gate evaluates the *persisted* proposal: it loads the ``ops.proposal``
  row and its digest-verified proposal document by id, derives the tier from that
  (plus a minimum tier per proposal type: ``model_registration`` and
  ``mapping_equivalence`` are at least T2), and refuses a caller object that differs
  from the stored document. ``decide`` also accepts a bare proposal id.
* (R6-06) For T1 the gate does not take the validator/sandbox outcomes on trust: it
  re-runs the validator (captured replay of the reported fixture bank, no network)
  and the trusted-template sandbox itself on the stored proposal; both must pass,
  and additivity mismatches block T1.
* (R6-05) The manifest version must be the content-derived version
  (``promotion.resolve_connector_version``), so a proposer cannot choose it.
* (R6-02) The gate evidence records the digests it evaluated (proposal document,
  manifest, mapping manifest, derived eligibility scope); promotion re-loads exactly
  those from storage and the database binds the approval to them.
* (GATE, process separation) This class runs in the separate gate process
  (``reva.gate.service``, login ``reva_gate``); the worker only files an
  ``ops.gate_request`` and waits (``reva.gate.client.GateClient``). Only ``reva_gate``
  may execute ``ops.record_gate_result``, and the approval functions accept only gate
  results recorded by it. Constructed with the worker's engine, ``_record`` fails with
  a permission error. See ``docs/dev/gate_separation.md``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.adapters.connectors import eurostat_template as et
from reva.adapters.connectors.eurostat_template import TEMPLATE_HOSTS, validate_manifest
from reva.autonomy import binding
from reva.autonomy.common import ArtifactIO, AutonomyBlocked, plain_json_bytes, ref_from, report_json, run_sync
from reva.autonomy.policy import (
    PINNED_POLICY_DIGESTS,
    POLICY_DIR,
    AutonomyGuard,
    PinnedPolicy,
    derive_tier,
    load_pinned,
)
from reva.autonomy.proposals import PROPOSAL_SCHEMA, proposal_from, proposal_json
from reva.kernel.errors import PolicyDenied, RevaError, ValidationProblem
from reva.ports.autonomy import (
    ArtifactRef,
    ConnectorProposal,
    GateDecision,
    MappingProposal,
    PlatformChangeProposal,
    SourceCandidate,
    ValidationReport,
)

GATE_PREFIX = "gate-evaluator:"
DB_DECISION = {"promote": "pass", "reject": "fail", "needs_review": "needs_review", "blocked": "blocked"}
CONNECTOR_CHANGE_SCHEMA = "reva-connector-change/1"
# Minimum tier by persisted proposal type (the type is recorded in ops.proposal, not chosen per call).
MIN_TIER_BY_TYPE = {"model_registration": 2, "mapping_equivalence": 2}
# Validator findings that block T1 auto-promotion even though they are warnings for ingestion.
T1_BLOCKING_FINDINGS = frozenset({"additivity_mismatch"})


@dataclass(frozen=True)
class StoredProposal:
    proposal: PlatformChangeProposal
    proposal_type: str
    row_tier: int
    document_sha256: str
    summary: dict[str, Any]


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

    # ------------------------------------------------------------------ persisted proposal
    def _check_author(self, author: str) -> None:
        if author in (self.evaluator, self.name) or self.evaluator == GATE_PREFIX + author:
            raise PolicyDenied("the proposer cannot evaluate its own proposal", author=author)

    def load_stored(self, proposal_id: uuid.UUID, obj: PlatformChangeProposal | None = None) -> StoredProposal:
        """The ``ops.proposal`` row + its digest-verified document; ``obj`` must equal it (R6-03)."""
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT proposal_type, manifest_sha256, permission_tier, author, summary FROM ops.proposal WHERE id = :i"),
                {"i": proposal_id}).mappings().one_or_none()
        if row is None:
            raise ValidationProblem("gate evaluates only persisted proposals (ops.proposal)")
        doc = self.io.get_json(ArtifactRef(str(row["manifest_sha256"]), "models", "application/json"))
        if not isinstance(doc, dict) or doc.get("schema") != PROPOSAL_SCHEMA \
                or doc.get("proposal_type") != row["proposal_type"]:
            raise PolicyDenied("persisted proposal document is not a proposal of the recorded type",
                               proposal_id=str(proposal_id))
        stored = proposal_from(doc["proposal"])
        if stored.proposal_id != proposal_id or stored.author != row["author"]:
            raise PolicyDenied("persisted proposal document does not match its ops.proposal row",
                               proposal_id=str(proposal_id))
        if obj is not None and json.loads(plain_json_bytes(proposal_json(obj))) != doc["proposal"]:
            raise PolicyDenied("the proposal object differs from the persisted proposal; the gate evaluates "
                               "only the stored proposal", proposal_id=str(proposal_id))
        return StoredProposal(stored, str(row["proposal_type"]), int(row["permission_tier"]),
                              str(row["manifest_sha256"]), dict(row["summary"] or {}))

    # ------------------------------------------------------------------ protocol
    def decide(self, proposal: PlatformChangeProposal | uuid.UUID, independent_reports: Sequence[ValidationReport],
               signed_policy: ArtifactRef) -> GateDecision:
        """Evaluate the persisted proposal ``proposal`` (an id, or an object that must equal the stored one)."""
        obj = proposal if isinstance(proposal, PlatformChangeProposal) else None
        pid = obj.proposal_id if obj is not None else uuid.UUID(str(proposal))
        if obj is not None:
            self._check_author(obj.author)
        if self.guard is not None and self.guard.blocked_reason():
            return GateDecision(uuid.uuid4(), "blocked", pid, "n/a", (),
                                (self.guard.blocked_reason() or "",), self.evaluator, None)
        stored = self.load_stored(pid, obj)
        self._check_author(stored.proposal.author)
        return self._decide(stored, independent_reports, signed_policy)

    def _decide(self, stored: StoredProposal, independent_reports: Sequence[ValidationReport],
                signed_policy: ArtifactRef) -> GateDecision:
        proposal = stored.proposal
        reasons: list[str] = []
        policy: PinnedPolicy | None = None
        try:
            policy = load_pinned(self.policy_path, self.pinned_sha256)
        except AutonomyBlocked as e:
            reasons.append(e.message)
        if policy is not None and signed_policy.sha256 != policy.sha256:
            reasons.append("signed policy reference does not match the pinned policy digest")
        if reasons or policy is None:
            return self._record(stored, "blocked", reasons, [], None, {"stage": "policy_integrity"})
        derived, why = derive_tier(proposal.changed_paths, proposal.requested_capabilities,
                                   proposal.scientific_change, proposal.migration_required)
        type_min = MIN_TIER_BY_TYPE.get(stored.proposal_type, 0)
        if type_min > derived:
            derived = type_min
            why = [*why, f"proposal type {stored.proposal_type} is at least T{type_min}"]
        declared = max(int(proposal.permission_tier), stored.row_tier)
        tier = max(declared, derived)
        trace: dict[str, Any] = {"declared_tier": declared, "derived_tier": derived,
                                 "derivation": why, "effective_tier": tier,
                                 "proposal_type": stored.proposal_type,
                                 "proposal_sha256": stored.document_sha256}
        if derived > declared:
            reasons.append(f"declared tier T{declared} raised to T{derived}: {'; '.join(why)}")
        if proposal.policy_version != policy.version:
            reasons.append(f"proposal targets policy {proposal.policy_version}, gate runs {policy.version}")
            return self._record(stored, "reject", reasons, list(independent_reports), policy, trace)
        reports, verify_reasons, verified = self._verified_reports(proposal, independent_reports)
        reasons += verify_reasons
        trace["reports_verified"] = verified
        if any(r.outcome == "blocked" for r in reports):
            reasons += [f"{r.evaluator_version}: {f.message}" for r in reports if r.outcome == "blocked"
                        for f in r.findings if f.severity == "blocker"]
            return self._record(stored, "blocked", reasons, reports, policy, trace)
        if any(r.outcome == "fail" for r in reports):
            reasons += [f"{r.evaluator_version}: {f.code}" for r in reports if r.outcome == "fail"
                        for f in r.findings if f.severity == "blocker"]
            return self._record(stored, "reject", reasons, reports, policy, trace)
        if tier == 0:
            reasons.append("T0: read/discover/propose within budget is automatic")
            return self._record(stored, "promote", reasons, reports, policy, trace)
        if tier >= 2:
            need = "maintainer approval" if tier == 3 else "isolated evaluation and named review"
            reasons.append(f"T{tier}: requires {need}")
            return self._record(stored, "needs_review", reasons, reports, policy, trace)
        ok, scope_reasons, scope = self._t1_scope(stored, reports, policy)
        trace["t1_scope"] = scope
        if ok:
            reasons.append("T1 within signed policy scope: promoted automatically without human review "
                           "(pinned template digest, registry-bound semantics and source, approved exact mapping, "
                           "verified rights, known hosts, verified validator and sandbox reports)")
            return self._record(stored, "promote", reasons, reports, policy, trace)
        reasons += ["T1 out of signed policy scope", *scope_reasons]
        return self._record(stored, "reject", reasons, reports, policy, trace)

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
    def _t1_scope(self, stored: StoredProposal, reports: Sequence[ValidationReport],
                  policy: PinnedPolicy) -> tuple[bool, list[str], dict[str, Any]]:
        from reva.autonomy import promotion as prom
        from reva.ontology.registry import load_registry

        proposal = stored.proposal
        problems: list[str] = []
        if stored.proposal_type != "connector_promotion":
            return False, [f"T1 auto-promotion only covers declarative connector manifests (connector_promotion "
                           f"proposals), not {stored.proposal_type!r}"], {}
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
        try:
            mapping_ref = ref_from((change.get("mapping") or {})["mapping_manifest"])
            mm = self.io.get_json(mapping_ref)
        except Exception:
            return False, ["mapping manifest is unreadable"], {}
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
        # binding: semantics, source, name, structure pins, approved entry, geography, rights, free fields
        problems += binding.binding_problems(manifest, registry, policy, mapping_manifest=mm)
        with self.engine.connect() as conn:
            problems += binding.name_conflicts(conn, manifest["name"], str(manifest["source_key"]), template)
            expected_version, _ = prom.resolve_connector_version(conn, self.io, manifest, current_digest)
        if manifest["version"] != expected_version:
            problems.append(f"version {manifest['version']!r} is not the content-derived version "
                            f"{expected_version!r} (versions are never proposer-chosen)")
        mapping = change.get("mapping") or {}
        sem = manifest["request_parameters"]["semantics"]
        target = {"concept_id": sem["concept_id"], "version": sem["concept_version"]}
        if mapping.get("relation") != "exact" or mapping.get("review_required"):
            problems.append(f"mapping relation {mapping.get('relation')!r} is not an approved exact mapping")
        if (mapping.get("target") or {}) != target:
            problems.append("mapping target differs from the manifest concept")
        rp = manifest["request_parameters"]
        geo = sorted(rp["filters"].get("geo", []))
        entry = binding.approved_entry(policy, template, manifest["dataset"], target["concept_id"],
                                       target["version"])
        if (change.get("candidate") or {}).get("rights_status") != "verified":
            problems.append("candidate rights are not verified")
        manifest_sha = (change.get("manifest_ref") or {}).get("sha256")
        expected_contract: str | None = None
        try:
            from reva.autonomy.quality import contract_for

            expected_contract = self.io.put_json(contract_for(manifest, registry), "models").sha256
        except RevaError as e:
            problems.append(f"no registry-derived data contract: {e.message}")
        kinds: set[str] = set()
        bank_sha: str | None = None
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
                if covers:
                    bank_sha = str(ev.get("fixture_bank") or ev.get("fixtures_in") or "") or None
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
        recomputed: dict[str, Any] = {}
        if not problems:  # (R6-06) recompute both reports from the stored proposal; never trust outcomes
            recomputed = self._recompute(proposal, change, manifest_sha, mapping_ref, bank_sha, problems)
        subjects = binding.iso3_for_geo(registry, geo)
        # (R6-02) the database binds the approval to ops.proposal.summary.scope, which the proposer wrote:
        # it must be exactly the scope derived here (concept, dataset, subjects, years, manifest digest).
        from reva.cli.seed import entity_id

        derived_db_scope = {"concept": f"{target['concept_id']}@{target['version']}", "dataset": manifest["dataset"],
                            "subject_ids": [str(entity_id(x)) for x in subjects],
                            "years": {"in": sorted({int(y) for y in rp["time"]})},
                            "manifest_sha256": et.manifest_digest(manifest),
                            "mapping_key": mm.get("mapping_key")}
        recorded = dict(stored.summary.get("scope") or {})
        off = sorted(k for k, v in derived_db_scope.items() if recorded.get(k) != v)
        if off:
            problems.append(f"recorded proposal scope differs from the derived scope in {off}")
        scope = {"template": template, "template_digest": current_digest, "pinned_template_digest": pinned_digest,
                 "dataset": manifest["dataset"], "target": target, "geo": geo,
                 "hosts": manifest["allowed_hosts"], "approved_entry": entry, "connector": manifest["name"],
                 "version": manifest["version"], "expected_version": expected_version,
                 "source_key": manifest["source_key"], "data_contract_sha256": expected_contract,
                 "proposal_sha256": stored.document_sha256, "manifest_sha256": manifest_sha,
                 "mapping_manifest_sha256": mapping_ref.sha256, "mapping_key": mm.get("mapping_key"),
                 "subjects": subjects, "years": {"in": sorted(int(y) for y in rp["time"])},
                 "fixture_bank_sha256": bank_sha, "recomputed": recomputed, "db_scope": derived_db_scope}
        return not problems, problems, scope

    def _recompute(self, proposal: PlatformChangeProposal, change: dict[str, Any], manifest_sha: str | None,
                   mapping_ref: ArtifactRef, bank_sha: str | None, problems: list[str]) -> dict[str, Any]:
        """Re-run the validator (captured replay, no network) and the sandbox on the stored proposal."""
        from reva.autonomy.common import default_budget
        from reva.autonomy.sandbox import TrustedTemplateSandbox, trusted_template_policy
        from reva.autonomy.validator import JsonStatConnectorValidator

        if not bank_sha or not manifest_sha:
            problems.append("no fixture bank recorded by the validator: the gate cannot recompute the reports")
            return {}
        bank = ArtifactRef(bank_sha, "raw", "application/json")
        cand = change.get("candidate") or {}
        try:
            cp = ConnectorProposal(
                proposal_id=proposal.proposal_id,
                candidate=SourceCandidate(proposal.proposal_id, str(cand.get("provider", "")), "",
                                          str(cand.get("dataset_id", "")), ref_from(cand["metadata"]),
                                          cand.get("rights_status", "unknown"), tuple(cand.get("allowed_hosts") or ())),
                template_id=str(change.get("template_id")), template_digest=str(change.get("template_digest")),
                manifest=ArtifactRef(manifest_sha, "models", "application/json"),
                mapping=MappingProposal(proposal.proposal_id, ref_from(cand["metadata"]), "", "exact", mapping_ref,
                                        False))
            budget = default_budget()
            v = run_sync(JsonStatConnectorValidator(self.io, live_client_factory=None).validate(cp, bank, budget))
            s = run_sync(TrustedTemplateSandbox(self.io, fixtures=bank).run(proposal,
                                                                           trusted_template_policy(budget)))
        except (RevaError, KeyError, TypeError, ValueError) as e:
            problems.append(f"gate could not recompute the reports: {type(e).__name__}")
            return {}
        out: dict[str, Any] = {}
        for label, r in (("connector-validator", v), ("sandbox", s)):
            blockers = sorted(f.code for f in r.findings if f.severity == "blocker")
            t1_block = sorted(f.code for f in r.findings if f.code in T1_BLOCKING_FINDINGS)
            out[label] = {"outcome": r.outcome, "evidence_sha256": r.evidence_manifest.sha256,
                          "blockers": blockers, "t1_blocking": t1_block}
            if r.outcome != "pass":
                problems.append(f"gate-recomputed {label} report is {r.outcome}: {blockers[:5]}")
            if t1_block:
                problems.append(f"gate-recomputed {label} found {t1_block}: blocks T1 auto-promotion")
        return out

    # ------------------------------------------------------------------ recording
    def _record(self, stored: StoredProposal, outcome: str, reasons: list[str],
                reports: Sequence[ValidationReport], policy: PinnedPolicy | None,
                trace: dict[str, Any]) -> GateDecision:
        proposal = stored.proposal
        version = policy.version if policy is not None else "policy-integrity-failure"
        evidence = {
            "schema": "reva-gate-decision/1", "gate": self.name, "evaluator": self.evaluator,
            "proposal_id": str(proposal.proposal_id), "proposal_author": proposal.author,
            "proposal_type": stored.proposal_type, "proposal_sha256": stored.document_sha256,
            "outcome": outcome, "db_decision": DB_DECISION[outcome], "reasons": reasons,
            "policy": {"version": version, "sha256": policy.sha256 if policy else None,
                       "pinned_sha256": self.pinned_sha256, "path": self.policy_path.name},
            "tier": trace, "reports": [report_json(r) for r in reports],
        }
        ref = self.io.put_json(evidence, "evaluation")
        with self.engine.begin() as conn:
            sha = conn.execute(text("SELECT manifest_sha256 FROM ops.proposal WHERE id = :i"),
                               {"i": proposal.proposal_id}).scalar_one_or_none()
            if sha is None or str(sha) != stored.document_sha256:
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
