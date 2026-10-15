"""Autonomy job handlers (``handle(payload, ctx) -> dict``).

``handle_discovery`` runs the V1 discovery loop (architecture §10.2):

    KnowledgeGapDetector → ValueOfInformationEstimator (proxy) → SourceDiscoverer
    → OntologyMapper → ConnectorGenerator → ConnectorValidator → SandboxRunner
    → PromotionGate → (T1 within the signed policy scope) register a
    content-versioned connector + scoped eligibility automatically (no human
    review), keeping the previous version for rollback. T2/T3 need named review.

The kill switch is re-read before every step and inside the promotion
transaction. Live runs reserve their requests/bytes in ``ops.budget_ledger``
(``budgets.yaml``) before the first request. A connector version that was
rolled back is never silently re-enabled (``rolled_back_hold``); ``promoted`` is
reported only when a new eligibility decision was actually recorded.

The false equivalence demo_pjan ≡ WDI midyear population is recorded as its own
proposal, rejected by the gate, with the explicit incompatible mapping record.

``handle_gaps`` writes a gap/coverage report artifact and an outbox event.

Payload fields are validated against fixed vocabularies; nothing in a payload
or in fetched content can choose hosts, fixture paths, policies or tiers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.adapters.connectors import eurostat_template as et
from reva.adapters.connectors.eurostat_template import TEMPLATE_HOSTS, EurostatJsonStatConnector
from reva.adapters.postgres import repositories as repo
from reva.autonomy import binding
from reva.autonomy import promotion as prom
from reva.autonomy.budget import BudgetExhausted, reserve_discovery, settle_discovery
from reva.autonomy.common import (
    ArtifactIO,
    AutonomyBlocked,
    default_budget,
    make_context,
    now_utc,
    ref_json,
    report,
    run_sync,
    stable_id,
)
from reva.autonomy.discovery import EurostatCatalogueDiscoverer
from reva.autonomy.fixtures import BankHttp, CapturingHttp, bank_from_dir
from reva.autonomy.gaps import CatalogueGapDetector, build_coverage_snapshot, coverage_report
from reva.autonomy.gate import CONNECTOR_CHANGE_SCHEMA, PolicyPromotionGate
from reva.autonomy.generator import DeclarativeConnectorGenerator
from reva.autonomy.mapping import RegistryOntologyMapper
from reva.autonomy.policy import AutonomyGuard, PinnedPolicy, live_kill_reason, promotion_policy
from reva.autonomy.proposals import ProposalStore, base_commit
from reva.autonomy.sandbox import TrustedTemplateSandbox, external_runner_from_settings, trusted_template_policy
from reva.autonomy.validator import JsonStatConnectorValidator
from reva.autonomy.voi import GapValueEstimator
from reva.cli.seed import entity_id, record_mapping_and_eligibility, register_connector_version
from reva.evidence.locks import take_evidence_write_lock
from reva.kernel.errors import ImmutableRecordViolation, PolicyDenied, RevaError
from reva.ontology.registry import load_registry
from reva.ports.autonomy import (
    ArtifactRef,
    ConnectorProposal,
    Finding,
    GateDecision,
    MappingProposal,
    PlatformChangeProposal,
    ValueEstimate,
)

log = logging.getLogger("reva.autonomy")
AUTHOR = "agent:autonomy-discovery"
FIXTURE_CONNECTORS = {"R-POP-JAN-AGESEX": "eurostat.demo_pjan.fr"}
DISCOVERABLE_REQUIREMENTS = tuple(FIXTURE_CONNECTORS)
DISCOVERY_SOURCE = "eurostat.dissemination"
PAYLOAD_KEYS = {"requirement_id", "mode"}


def _guard(ctx: Any) -> AutonomyGuard:
    return AutonomyGuard.from_settings(ctx.settings)


def _http(mode: str, io_: ArtifactIO, requirement: str, settings: Any) -> tuple[Any, Any]:
    if mode == "fixture":
        bank = bank_from_dir(io_, FIXTURE_CONNECTORS[requirement])
        return BankHttp(io_, bank, allowed_hosts=TEMPLATE_HOSTS), bank
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
    from reva.adapters.http.safe_client import SafeHttpClient

    client = SafeHttpClient(allowed_hosts=TEMPLATE_HOSTS, timeout_seconds=settings.reva_http_timeout_seconds,
                            max_bytes=min(settings.reva_http_max_bytes, 4_000_000),
                            contact_email=settings.reva_http_contact_email,
                            proxy_mode=getattr(settings, "reva_http_proxy_mode", None),
                            rate_limiter=RateLimiter({"ec.europa.eu": HostPolicy(min_interval_seconds=1.0,
                                                                                  max_concurrency=1)}))
    return CapturingHttp(client, "Eurostat (CC BY 4.0)"), None


def handle_discovery(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    guard = _guard(ctx)
    reason = guard.blocked_reason()
    if reason:
        return {"status": "blocked", "reason": reason}
    unknown = sorted(str(k)[:40] for k in payload if k not in PAYLOAD_KEYS)
    if unknown:
        return {"status": "rejected", "reason": "unsupported payload keys", "unsupported": unknown}
    requirement = str(payload.get("requirement_id", "R-POP-JAN-AGESEX"))
    mode = str(payload.get("mode", "live"))
    if requirement not in DISCOVERABLE_REQUIREMENTS or mode not in ("live", "fixture"):
        return {"status": "rejected", "reason": "unsupported requirement or mode",
                "supported": list(DISCOVERABLE_REQUIREMENTS)}
    reservation = None
    if mode == "live":
        try:
            reservation = reserve_discovery(ctx.worker_engine, DISCOVERY_SOURCE,
                                            job_key=f"{getattr(ctx, 'job_id', None)}:{getattr(ctx, 'attempt', 0)}"
                                            if getattr(ctx, "job_id", None) else None)
        except BudgetExhausted as e:
            return {"status": "blocked", "reason": e.message, "code": e.code}
    try:
        return run_sync(discovery_flow(ctx, requirement, mode, guard, reservation=reservation))
    except AutonomyBlocked as e:
        return {"status": "blocked", "reason": e.message}
    except RevaError as e:  # typed failure report (malformed provider data, schema drift, ...)
        if e.details.get("retryable"):
            raise
        return {"status": "failed", "reason": e.message[:500], "code": e.code,
                "kind": str(e.details.get("kind", ""))[:60]}


async def discovery_flow(ctx: Any, requirement: str, mode: str, guard: AutonomyGuard,
                         reservation: Any = None) -> dict[str, Any]:
    http_box: dict[str, Any] = {}
    try:
        return await _discovery_flow(ctx, requirement, mode, guard, reservation, http_box)
    finally:
        if reservation is not None:  # record what was actually consumed (reservation counts until then)
            h = http_box.get("http")
            settle_discovery(ctx.worker_engine, reservation, requests=int(getattr(h, "requests", 0) or 0),
                             nbytes=int(getattr(h, "bytes", 0) or 0))


async def _discovery_flow(ctx: Any, requirement: str, mode: str, guard: AutonomyGuard, reservation: Any,
                          http_box: dict[str, Any]) -> dict[str, Any]:
    io_ = ArtifactIO(ctx.store)
    reg = load_registry()
    policy = promotion_policy()
    engine: Engine = ctx.worker_engine
    cutoff = now_utc()
    origins = ["test_fixture"] if mode == "fixture" else ["live"]
    if reservation is not None:
        budget = default_budget(max_requests=reservation.max_requests, max_bytes=reservation.max_bytes)
    else:  # fixture replay: no network requests, not charged
        budget = default_budget()
    summary: dict[str, Any] = {"status": "started", "requirement_id": requirement, "mode": mode,
                               "policy": {"version": policy.version, "sha256": policy.sha256},
                               "promotion_rule": "T1 within the signed policy scope is promoted automatically "
                                                 "(no human review); T2/T3 changes need named review",
                               "budget": reservation.as_dict() if reservation is not None
                               else {"charged": False, "reason": "fixture replay makes no requests"}}
    # 1. gap
    with engine.connect() as conn:
        snapshot = build_coverage_snapshot(conn, cutoff, origins, reg)
    req_ref = io_.put_json(reg.requirements, "models")
    snap_ref = io_.put_json(snapshot, "evaluation")
    context = make_context(io_, "evidence", cutoff, reg.manifest())
    gaps = [g for g in CatalogueGapDetector(io_, reg).detect(req_ref, snap_ref, context)
            if g.requirement_id == requirement and g.reason.startswith(("unsupported", "missing"))]
    if not gaps:
        return {**summary, "status": "no_gap"}
    gap = gaps[0]
    summary["gap"] = {"gap_id": str(gap.gap_id), "reason": gap.reason, "geography": list(gap.geography),
                      "years": gap.temporal_support.get("years")}
    plan_ref = io_.put_json({"schema": "reva-acquisition-plan/1", "requirement_id": requirement,
                             "expected_coverage_gain": 1.0, "catalogue": "eurostat"}, "evaluation")
    voi = GapValueEstimator(io_).estimate(gap, plan_ref, None, budget)
    summary["value_estimate"] = {"kind": voi.kind, "value": voi.value, "unit": voi.unit}
    # 2. discovery
    guard.check("discover")
    http, bank = _http(mode, io_, requirement, ctx.settings)
    http_box["http"] = http
    discoverer = EurostatCatalogueDiscoverer(io_, http, registry=reg, guard=guard)
    candidates = await discoverer.discover(gap, budget)
    summary["search"] = discoverer.last_search
    if not candidates:
        return {**summary, "status": "no_candidate", "note": "search failure is a valid, recorded result"}
    # 3. mapping: prefer a candidate covered by an approved exact mapping; otherwise the best
    # inspected candidate becomes a reviewable (T2) proposal. Ranking text never grants semantics.
    mapper = RegistryOntologyMapper(io_, registry=reg, policy=policy)
    inspected = [c for c in candidates if io_.get_json(c.metadata).get("inspection")]
    if not inspected:
        return {**summary, "status": "no_inspected_candidate", "note": "search failure is a valid result"}
    assessed = [(c, mapper.assess(c, context)) for c in inspected]
    cand, mappings = next(((c, m) for c, m in assessed if m[0].relation == "exact" and not m[0].review_required),
                          assessed[0])
    primary = mappings[0]
    summary["mappings"] = [{"target": m.target_concept_version, "relation": m.relation,
                            "review_required": m.review_required} for m in mappings]
    # 4. manifest (content-derived version: same content keeps its version, changed content bumps it)
    guard.check("generate")

    def version_for(m: dict[str, Any]) -> str:
        with engine.connect() as conn:
            return prom.resolve_connector_version(conn, io_, m, et.template_digest())[0]

    generator = DeclarativeConnectorGenerator(io_, registry=reg, policy=policy, version_resolver=version_for)
    cp = generator.generate(cand, primary, tuple((policy.content.get("approved_templates") or {}).keys()))
    manifest = io_.get_json(cp.manifest)
    summary["connector"] = {"name": manifest["name"], "version": manifest["version"],
                            "manifest_sha256": cp.manifest.sha256}
    hold = _existing_state(engine, manifest, primary)
    if hold is not None:  # rolled back or already promoted: never re-enable or re-announce silently
        summary["connector"]["state"] = hold
        summary["status"] = hold
        summary["note"] = ("this exact connector version was rolled back; re-enabling it needs a named human "
                           "review" if hold == "rolled_back_hold" else
                           "this exact connector version is already eligible; nothing to promote")
        return summary
    # 5. validation (captured replay or bounded live sample)
    live_factory = None
    if mode == "live":
        def live_factory(hosts: tuple[str, ...]) -> Any:
            return http.client
        bank = bank or io_.put_json({"schema": "reva-fixture-bank/1", "connector": manifest["name"],
                                     "entries": []}, "raw")
    guard.check("validate")
    validator = JsonStatConnectorValidator(io_, live_client_factory=live_factory, guard=guard, registry=reg)
    v_report = await validator.validate(cp, bank, budget)
    if mode == "live":
        ev = io_.get_json(v_report.evidence_manifest)
        if ev.get("fixture_bank"):
            bank = ArtifactRef(ev["fixture_bank"], "raw", "application/json")
    summary["validation"] = {"outcome": v_report.outcome, "findings": [f.code for f in v_report.findings]}
    # 6. proposal + sandbox
    proposals = ProposalStore(engine, io_, guard)
    proposal = connector_change_proposal(io_, cp, manifest, primary, voi, policy)
    proposals.persist(proposal, proposal_type="connector_promotion",
                      scope=connector_promotion_scope(io_, manifest, primary, dataset=cand.dataset_id),
                      risk={"tier": "T1", "hosts": manifest["allowed_hosts"], "code": "none (declarative)",
                            "promotion": "automatic if the gate finds it within the signed policy scope"},
                      dependencies=[f"gap:{gap.gap_id}", f"candidate:{cand.metadata.sha256}",
                                    f"mapping:{primary.mapping_manifest.sha256}", f"template:{cp.template_digest}"])
    sandbox = TrustedTemplateSandbox(io_, fixtures=bank, external_runner=external_runner_from_settings(ctx.settings),
                                     guard=guard)
    s_report = await sandbox.run(proposal, trusted_template_policy(budget))
    summary["sandbox"] = {"outcome": s_report.outcome, "findings": [f.code for f in s_report.findings]}
    # 7. gate
    gate = PolicyPromotionGate(engine, io_, guard=guard)
    decision = gate.decide(proposal, [v_report, s_report], policy.ref())
    summary["gate"] = {"proposal_id": str(proposal.proposal_id), "outcome": decision.outcome,
                       "reasons": list(decision.reasons), "gate_result_id": str(decision.decision_id)}
    # 8. explicit rejection of tempting false equivalences
    summary["rejected_equivalences"] = []
    for m in mappings[1:]:
        if m.relation != "incompatible":
            continue
        d = reject_equivalence(engine, io_, proposals, gate, policy, m, cand.dataset_id)
        summary["rejected_equivalences"].append({"target": m.target_concept_version, "outcome": d.outcome,
                                                 "proposal_id": str(d.proposal_id)})
    # 9. promotion within scope (automatic for T1 inside the signed policy scope; no human review)
    if decision.outcome == "promote":
        result = promote_connector(engine, io_, ctx, manifest, primary, decision, policy, guard=guard)
        summary["promotion"] = result
        summary["status"] = result["status"]
    else:
        summary["status"] = f"gate_{decision.outcome}"
    return summary


def connector_promotion_scope(io_: ArtifactIO, manifest: dict[str, Any], mapping: MappingProposal, *,
                              dataset: str | None = None) -> dict[str, Any]:
    """The proposal scope recorded *before* the gate runs (ops.proposal.summary.scope).

    (R5-01) The database binds approvals to it: ``evidence.approve_eligibility`` and
    ``ontology.approve_mapping`` accept a passing gate result only for exactly this concept,
    mapping key, subject_ids and years.
    """
    mm = io_.get_json(mapping.mapping_manifest)
    subjects = sorted((mm.get("scope") or {}).get("subjects") or [])
    return {"concept": mapping.target_concept_version, "dataset": dataset or manifest["dataset"],
            "geo": manifest["request_parameters"]["filters"].get("geo"),
            "eligibility_scope": manifest["eligibility_scope"], "mapping_key": mm["mapping_key"],
            "subject_ids": [str(entity_id(x)) for x in subjects], "years": eligibility_years(manifest)}


def eligibility_years(manifest: dict[str, Any]) -> dict[str, list[int]]:
    """The manifest's requested years in the explicit eligibility form ``{"in": [y, ...]}`` (R5-11)."""
    return {"in": sorted({int(str(y)) for y in manifest["request_parameters"]["time"]})}


def connector_change_proposal(io_: ArtifactIO, cp: ConnectorProposal, manifest: dict[str, Any],
                              mapping: MappingProposal, voi: Any, policy: PinnedPolicy) -> PlatformChangeProposal:
    concept_id, version = mapping.target_concept_version.split("@", 1)
    change = {
        "schema": CONNECTOR_CHANGE_SCHEMA, "manifest": manifest, "manifest_ref": ref_json(cp.manifest),
        "template_id": cp.template_id, "template_digest": cp.template_digest,
        "mapping": {"relation": mapping.relation, "review_required": mapping.review_required,
                    "target": {"concept_id": concept_id, "version": version},
                    "mapping_manifest": ref_json(mapping.mapping_manifest)},
        "candidate": {"provider": cp.candidate.provider, "dataset_id": cp.candidate.dataset_id,
                      "rights_status": cp.candidate.rights_status, "metadata": ref_json(cp.candidate.metadata),
                      "allowed_hosts": list(cp.candidate.allowed_hosts)},
    }
    ref = io_.put_json(change, "models")
    return PlatformChangeProposal(
        proposal_id=stable_id("connector-change", ref.sha256), author=AUTHOR, permission_tier=1,
        changed_paths=(f"connectors/{manifest['name']}/{manifest['version']}/manifest.json",),
        base_commit=base_commit(), patch_or_manifest=ref,
        requested_capabilities=("register_connector_version", "concept_eligibility"),
        scientific_change=True, migration_required=False, expected_benefit=voi, policy_version=policy.version,
    )


def reject_equivalence(engine: Engine, io_: ArtifactIO, proposals: ProposalStore, gate: PolicyPromotionGate,
                       policy: PinnedPolicy, mapping: MappingProposal, dataset: str) -> GateDecision:
    """Record 'dataset ≡ target' as a proposal that the gate rejects, plus the incompatible mapping record."""
    mm = io_.get_json(mapping.mapping_manifest)
    change = io_.put_json({"schema": "reva-mapping-change/1", "claimed_relation": "exact",
                           "assessed_relation": mapping.relation,
                           "mapping_manifest": ref_json(mapping.mapping_manifest),
                           "dataset": dataset, "target": mapping.target_concept_version}, "models")
    p = PlatformChangeProposal(
        proposal_id=stable_id("equivalence", change.sha256), author=AUTHOR, permission_tier=2,
        changed_paths=(f"registry/mappings/eurostat.{dataset}.json",), base_commit=base_commit(),
        patch_or_manifest=change, requested_capabilities=("ontology_mapping",), scientific_change=True,
        migration_required=False,
        expected_benefit=_no_value(io_), policy_version=policy.version)
    proposals.persist(p, proposal_type="mapping_equivalence",
                      scope={"dataset": dataset, "target": mapping.target_concept_version},
                      risk={"semantic": "false equivalence would mix 1 January and midyear populations"})
    findings = [Finding(code="incompatible_semantics", severity="blocker",
                        message="; ".join(mm.get("profile_differences") or ["incompatible"]),
                        evidence=(mapping.mapping_manifest,))]
    mapper_evaluator = "ontology-mapper:registry-profile@1"
    assessment = io_.put_json({"schema": "reva-mapping-assessment/1", "evaluator": mapper_evaluator,
                               "outcome": "fail", "mapping_manifest": mapping.mapping_manifest.sha256,
                               "findings": [{"code": f.code, "severity": f.severity, "message": f.message}
                                            for f in findings]}, "evaluation")
    mapper_report = report("fail", findings, assessment, mapper_evaluator)
    decision = gate.decide(p, [mapper_report], policy.ref())
    reg_mapping = mm.get("registry_mapping")
    with engine.begin() as conn:
        take_evidence_write_lock(conn)
        act = repo.simple_activity("autonomy.mapping_assessment", "review", gate.evaluator,
                                   {"mapping": mm.get("mapping_key"), "relation": mapping.relation,
                                    "gate_result": str(decision.decision_id)})
        io_.register(conn, mapping.mapping_manifest, "mapping-proposal/1")
        repo.record_activity(conn, act, outputs=[mapping.mapping_manifest.sha256])
        if reg_mapping:
            record_mapping_and_eligibility(conn, reg_mapping, act.id, policy_version=policy.version)
        else:
            concept_id, version = mapping.target_concept_version.split("@", 1)
            record_mapping_and_eligibility(conn, {
                "mapping_key": mm["mapping_key"], "version": mm["version"],
                "source_ref": {"provider": "eurostat", "dataset": dataset}, "mapping_kind": "incompatible",
                "target": {"concept_id": concept_id, "version": version},
                "applicability": {"reason": "; ".join(mm.get("profile_differences") or [])},
                "eligibility": "none"}, act.id, policy_version=policy.version)
    return decision


def _no_value(io_: ArtifactIO) -> ValueEstimate:
    ref = io_.put_json({"reason": "a rejected equivalence has no acquisition value"}, "evaluation")
    return ValueEstimate("unsupported", None, None, {"status": "unquantified", "kind": "unknown",
                                                     "description": "not applicable"}, ref, "autonomy.jobs@1")


def _existing_state(engine: Engine, manifest: dict[str, Any], mapping: MappingProposal) -> str | None:
    """'rolled_back_hold' / 'already_promoted' when this exact (name, version) already has a decision."""
    concept_id, version = mapping.target_concept_version.split("@", 1)
    with engine.connect() as conn:
        row = repo.connector_version_row(conn, manifest["name"], manifest["version"])
        if row is None:
            return None
        try:
            c = repo.concept_version_id(conn, concept_id, version)
        except Exception:
            return None
        latest = prom.latest_decision(conn, row["id"], c)
    if latest is None:
        return None
    return "already_promoted" if latest == "eligible" else "rolled_back_hold"


def _verify_gate_row(conn: Any, decision: GateDecision) -> None:
    """The decision must be a recorded pass of a gate evaluator for this very proposal."""
    row = conn.execute(text(
        "SELECT proposal_id, decision, evaluator FROM ops.gate_result WHERE id = :i"),
        {"i": decision.decision_id}).mappings().one_or_none()
    if (row is None or str(row["proposal_id"]) != str(decision.proposal_id) or row["decision"] != "pass"
            or row["evaluator"] != decision.reviewer_identity):
        raise PolicyDenied("promotion requires a recorded passing gate result for this proposal",
                           gate_result_id=str(decision.decision_id))


def promote_connector(engine: Engine, io_: ArtifactIO, ctx: Any, manifest: dict[str, Any],
                      mapping: MappingProposal, decision: GateDecision, policy: PinnedPolicy,
                      guard: AutonomyGuard | None = None) -> dict[str, Any]:
    """Register a content-versioned connector + scoped eligibility atomically; keep prior state for rollback.

    Returns ``{"status": "promoted" | "already_promoted" | "rolled_back_hold", ...}``. Events and audit
    rows are written only when a new eligibility decision is recorded.
    """
    reason = guard.blocked_reason() if guard is not None else live_kill_reason()
    if reason:
        raise AutonomyBlocked(reason, action="promote_connector")
    if decision.outcome != "promote" or not decision.reviewer_identity.startswith("gate-evaluator:"):
        raise AutonomyBlocked("promotion requires a promote decision from a gate evaluator")
    problems = binding.binding_problems(manifest, load_registry(), policy)
    if problems:  # defence in depth: the same derivation the gate used
        raise PolicyDenied("manifest is outside the signed policy scope", problems=problems[:10])
    connector = EurostatJsonStatConnector(manifest)
    spec = connector.describe()
    mm = io_.get_json(mapping.mapping_manifest)
    concept_id, version = mapping.target_concept_version.split("@", 1)
    subjects = sorted((mm.get("scope") or {}).get("subjects") or [])
    with engine.begin() as conn:
        take_evidence_write_lock(conn)  # R-03: eligibility decisions are evidence a release selects from
        if getattr(ctx, "job_id", None) is not None and hasattr(ctx, "assert_lease"):
            ctx.assert_lease(conn)  # fenced: a worker that lost its lease cannot promote
        reason = guard.blocked_reason() if guard is not None else live_kill_reason()
        if reason:  # re-read inside the promotion transaction
            raise AutonomyBlocked(reason, action="promote_connector")
        _verify_gate_row(conn, decision)
        conflicts = binding.name_conflicts(conn, spec.name, spec.source_key, et.TEMPLATE_ID)
        if conflicts:
            raise PolicyDenied("connector name is registered under another source/template", conflicts=conflicts)
        concept_uuid = repo.concept_version_id(conn, concept_id, version)
        existing = repo.connector_version_row(conn, spec.name, spec.version)
        if existing is not None:
            if not prom.same_registered_content(io_, existing, manifest, spec.code_digest):
                raise ImmutableRecordViolation(
                    f"connector {spec.name}@{spec.version} is registered with different content; "
                    "a changed manifest or template needs a new version", connector=spec.name, version=spec.version)
            latest = prom.latest_decision(conn, existing["id"], concept_uuid)
            if latest is not None:
                return {"status": "already_promoted" if latest == "eligible" else "rolled_back_hold",
                        "connector_version_id": str(existing["id"]), "latest_decision": latest}
        others = [r for r in prom.versions_of(conn, spec.name) if r["version"] != spec.version]
        previous = {
            "connector_versions": [{k: str(v) for k, v in r.items()} for r in others],
            "eligibility": [dict(r) for r in conn.execute(text(
                """SELECT DISTINCT ON (e.connector_version_id) e.id::text, e.connector_version_id::text,
                          e.decision, e.applicability, e.policy_version, e.recorded_at::text
                   FROM evidence.eligibility e WHERE e.concept_version_id = :c
                   ORDER BY e.connector_version_id, e.recorded_at DESC, e.id DESC"""),
                {"c": concept_uuid}).mappings()],
        }
        act = repo.simple_activity("autonomy.connector_promotion", "review", decision.reviewer_identity,
                                   {"connector": spec.name, "version": spec.version,
                                    "gate_result": str(decision.decision_id), "policy_version": policy.version,
                                    "template_digest": spec.code_digest})
        repo.record_activity(conn, act)
        cv_id = register_connector_version(conn, io_.store, spec, act.id)
        superseded = [r for r in others if prom.latest_decision(conn, r["id"], concept_uuid) == "eligible"]
        rollback = {"schema": "reva-promotion-rollback/1", "connector": spec.name, "version": spec.version,
                    "connector_version_id": str(cv_id), "concept": mapping.target_concept_version,
                    "previous": previous, "gate_result_id": str(decision.decision_id),
                    "superseded": [{"connector_version_id": str(r["id"]), "version": r["version"]}
                                   for r in superseded],
                    "rollback": "rollback_promotion(): append 'retired' for this version and re-append "
                                "'eligible' for the version it superseded (if still runnable)"}
        rb_ref = io_.put_json(rollback, "models")
        io_.register(conn, rb_ref, "promotion-rollback/1")
        # The mapping record (idempotent); the eligibility decision is written below so that the
        # *latest* decision is what counts (a retired version is never silently re-enabled).
        record_mapping_and_eligibility(conn, {
            "mapping_key": mm["mapping_key"], "version": mm["version"],
            "source_ref": {"provider": "eurostat", "dataset": manifest["dataset"],
                           "template": manifest["template"], "dimensions": manifest["request_parameters"]
                           ["resolution_dimensions"]},
            "target": {"concept_id": concept_id, "version": version}, "mapping_kind": "exact",
            "applicability": {"subjects": subjects}, "eligibility": "none",
        }, act.id, policy_version=policy.version, approval=(decision.proposal_id, decision.decision_id))
        applicability = {"subjects": subjects, "subject_ids": [str(entity_id(x)) for x in subjects],
                         "years": eligibility_years(manifest),
                         "geo": manifest["request_parameters"]["filters"].get("geo"),
                         "mapping_key": mm["mapping_key"], "gate_result_id": str(decision.decision_id),
                         "rollback_sha256": rb_ref.sha256}
        # (R5-01) 'eligible' only through the DB-checked approval path (passing gate result of this
        # proposal, whose recorded scope must cover this concept/subjects/years; one gate pass, one approval).
        conn.execute(text(
            "SELECT evidence.approve_eligibility(:p, :g, :cv, :c, CAST(:ap AS jsonb), :pv, :a)"),
            {"p": decision.proposal_id, "g": decision.decision_id, "cv": cv_id, "c": concept_uuid,
             "ap": json.dumps(applicability), "pv": policy.version, "a": act.id})
        for r in superseded:
            prev_app = conn.execute(text(
                """SELECT applicability FROM evidence.eligibility WHERE connector_version_id = :cv
                   AND concept_version_id = :c ORDER BY recorded_at DESC, id DESC LIMIT 1"""),
                {"cv": r["id"], "c": concept_uuid}).scalar_one()
            _append_eligibility(conn, r["id"], concept_uuid,
                                {**dict(prev_app), "superseded_by": {"connector_version_id": str(cv_id),
                                                                     "version": spec.version}},
                                "retired", policy.version, act.id)
        repo.emit_outbox(conn, "autonomy.connector_promoted",
                         {"connector": spec.name, "version": spec.version, "connector_version_id": str(cv_id),
                          "concept": mapping.target_concept_version, "subjects": subjects,
                          "gate_result_id": str(decision.decision_id), "rollback_sha256": rb_ref.sha256,
                          "superseded": [r["version"] for r in superseded]})
        repo.audit(conn, decision.reviewer_identity, "autonomy.connector_promoted",
                   {"connector": spec.name, "version": spec.version}, {"rollback_sha256": rb_ref.sha256})
    return {"status": "promoted", "connector_version_id": str(cv_id), "version": spec.version,
            "rollback_sha256": rb_ref.sha256, "subjects": subjects,
            "superseded": [r["version"] for r in superseded]}


def _append_eligibility(conn: Any, cv: Any, concept: Any, applicability: dict[str, Any], decision: str,
                        policy_version: str, activity: Any) -> UUID:
    return UUID(str(conn.execute(text(
        """INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
             policy_version, review_activity_id)
           VALUES (:cv, :c, CAST(:ap AS jsonb), :d, :p, :a) RETURNING id"""),
        {"cv": cv, "c": concept, "ap": json.dumps(applicability), "d": decision, "p": policy_version,
         "a": activity}).scalar_one()))


def rollback_promotion(engine: Engine, connector_name: str, version: str, concept: str, actor: str,
                       reason: str) -> UUID:
    """Append 'retired' for this version (latest decision wins) and restore the version it superseded.

    History stays immutable. Re-running discovery afterwards reports ``rolled_back_hold`` for the retired
    content instead of re-enabling it. A ``human:`` actor needs the migration-owner engine (R5-01: the
    database refuses human-attributed rows from runtime roles); a gate evaluator may use the worker.
    """
    if not actor.startswith(("gate-evaluator:", "human:")):
        raise AutonomyBlocked("rollback must be performed by a gate evaluator or a named human")
    concept_id, cversion = concept.split("@", 1)
    with engine.begin() as conn:
        take_evidence_write_lock(conn)
        cv = repo.connector_version_row(conn, connector_name, version)
        if cv is None:
            raise RevaError(f"connector {connector_name}@{version} not registered")
        c = repo.concept_version_id(conn, concept_id, cversion)
        prev = conn.execute(text(
            """SELECT applicability, policy_version FROM evidence.eligibility WHERE connector_version_id=:cv
               AND concept_version_id=:c ORDER BY recorded_at DESC, id DESC LIMIT 1"""),
            {"cv": cv["id"], "c": c}).mappings().one()
        act = repo.simple_activity("autonomy.rollback", "review", actor, {"reason": reason[:500]})
        repo.record_activity(conn, act)
        rid = _append_eligibility(conn, cv["id"], c, dict(prev["applicability"]), "retired",
                                  prev["policy_version"], act.id)
        restored = _restore_superseded(conn, cv, c, act.id)
        repo.emit_outbox(conn, "autonomy.connector_rolled_back", {"connector": connector_name, "version": version,
                                                                   "concept": concept, "restored": restored})
        repo.audit(conn, actor, "autonomy.connector_rolled_back", {"connector": connector_name, "version": version},
                   {"restored": restored, "reason": reason[:500]})
    return rid


def reapprove_promotion(engine: Engine, connector_name: str, version: str, concept: str, actor: str,
                        reason: str) -> UUID:
    """Named-human re-approval of a rolled-back connector version (append-only; latest decision wins).

    Rediscovery never re-enables a retired version by itself (``rolled_back_hold``); this is the
    explicit human path. The version must still run under the current reviewed template.
    (R5-01) Human-attributed approvals are refused by the database from runtime roles: run it with
    the migration-owner engine (admin CLI), like token minting.
    """
    if not actor.startswith("human:"):
        raise AutonomyBlocked("re-enabling a rolled-back connector needs a named human reviewer")
    concept_id, cversion = concept.split("@", 1)
    with engine.begin() as conn:
        take_evidence_write_lock(conn)
        cv = repo.connector_version_row(conn, connector_name, version)
        if cv is None:
            raise RevaError(f"connector {connector_name}@{version} not registered")
        if cv["code_digest"] != et.template_digest():
            raise PolicyDenied("connector version was built for another template revision (template_changed); "
                               "it needs re-validation as a new version")
        c = repo.concept_version_id(conn, concept_id, cversion)
        prev = conn.execute(text(
            """SELECT decision, applicability, policy_version FROM evidence.eligibility WHERE connector_version_id=:cv
               AND concept_version_id=:c ORDER BY recorded_at DESC, id DESC LIMIT 1"""),
            {"cv": cv["id"], "c": c}).mappings().one()
        if prev["decision"] == "eligible":
            raise RevaError(f"connector {connector_name}@{version} is already eligible")
        act = repo.simple_activity("autonomy.reapproval", "review", actor, {"reason": reason[:500]})
        repo.record_activity(conn, act)
        app = {k: v for k, v in dict(prev["applicability"]).items() if k != "superseded_by"}
        rid = _append_eligibility(conn, cv["id"], c, {**app, "reapproved_by": actor}, "eligible",
                                  prev["policy_version"], act.id)
        repo.emit_outbox(conn, "autonomy.connector_reapproved", {"connector": connector_name, "version": version,
                                                                  "concept": concept, "reviewer": actor})
        repo.audit(conn, actor, "autonomy.connector_reapproved", {"connector": connector_name, "version": version},
                   {"reason": reason[:500]})
    return rid


def _restore_superseded(conn: Any, cv: dict[str, Any], concept: Any, activity: Any) -> str | None:
    """Re-append 'eligible' for the version that ``cv`` superseded, if it is still runnable."""
    for r in reversed(prom.versions_of(conn, cv["name"])):
        if str(r["id"]) == str(cv["id"]) or r["recorded_at"] > cv["recorded_at"]:
            continue
        last = conn.execute(text(
            """SELECT decision, applicability, policy_version FROM evidence.eligibility
               WHERE connector_version_id = :cv AND concept_version_id = :c
               ORDER BY recorded_at DESC, id DESC LIMIT 1"""), {"cv": r["id"], "c": concept}).mappings().one_or_none()
        if last is None:
            continue
        sup = (last["applicability"] or {}).get("superseded_by") or {}
        if last["decision"] != "retired" or sup.get("connector_version_id") != str(cv["id"]):
            return None  # the previous version was retired for its own reasons: do not resurrect it
        if r["code_digest"] != et.template_digest():
            return None  # cannot run under the current template: stays retired (template_changed)
        # (R5-01) re-enabling goes through the DB check: only a version retired *because* it was
        # superseded by this (now rolled back) version gets its last approved decision back.
        conn.execute(text("SELECT evidence.restore_eligibility(:cv, :c, :rb, :a)"),
                     {"cv": r["id"], "c": concept, "rb": cv["id"], "a": activity})
        return str(r["version"])
    return None


def promoted_connectors(engine: Engine, store: Any) -> dict[str, EurostatJsonStatConnector]:
    """Rebuild *active* declarative connectors from their registered spec artifacts.

    Only versions whose latest eligibility decision is ``eligible`` and whose code digest is the current
    reviewed template are loaded (newest per name); ``template_changed`` versions are surfaced by the gap
    report and health instead. Built-in connector names are never shadowed. Under the autonomy kill switch
    no autonomy-promoted connector is loaded.
    """
    if live_kill_reason():
        log.warning("autonomy kill switch: promoted connectors are not loaded")
        return {}
    io_ = ArtifactIO(store)
    out: dict[str, EurostatJsonStatConnector] = {}
    for name, r in prom.active_specs(engine).items():
        spec = io_.get_json(ArtifactRef(r["spec_sha256"], "models", "application/json"))
        out[name] = EurostatJsonStatConnector(spec["field_mappings"]["manifest"])
    return out


def promoted_connector_availability(engine: Engine, name: str) -> dict[str, Any] | None:
    """Typed ``not_promoted`` result for a template connector name that is not loaded, else None.

    Used by the ingest handler for scheduled template connectors (``eurostat.*``) before/after promotion.
    """
    if not name.startswith(f"{binding.CONNECTOR_PREFIX}"):
        return None
    if live_kill_reason():
        return {"status": "not_promoted", "connector": name, "state": "autonomy_disabled",
                "reason": "autonomy kill switch: promoted connectors are not run"}
    with engine.connect() as conn:
        rows = [x for x in prom.connector_states(conn) if x["name"] == name]
    states = sorted({x["state"] for x in rows})
    state = "not_registered" if not rows else ("template_changed" if "template_changed" in states else states[-1])
    reasons = {"not_registered": "no connector version has been promoted for this name yet",
               "template_changed": "the reviewed template changed since promotion; re-validation is required",
               "retired": "the promoted connector version was rolled back"}
    return {"status": "not_promoted", "connector": name, "state": state,
            "reason": reasons.get(state, f"connector state {state}"),
            "versions": [{"version": x["version"], "state": x["state"]} for x in rows]}


def promoted_connector_states(engine: Engine) -> list[dict[str, Any]]:
    """State of every promoted connector version: active / template_changed / retired."""
    with engine.connect() as conn:
        return prom.connector_states(conn)


def handle_gaps(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    guard = _guard(ctx)
    reason = guard.blocked_reason()
    if reason:
        return {"status": "blocked", "reason": reason}
    origin = str(payload.get("origin", "live"))
    if origin not in ("live", "test_fixture"):
        return {"status": "rejected", "reason": "origin must be live or test_fixture"}
    io_ = ArtifactIO(ctx.store)
    reg = load_registry()
    engine: Engine = ctx.worker_engine
    cutoff: datetime = now_utc()
    with engine.connect() as conn:
        snapshot = build_coverage_snapshot(conn, cutoff, [origin], reg)
    req_ref = io_.put_json(reg.requirements, "models")
    snap_ref = io_.put_json(snapshot, "evaluation")
    context = make_context(io_, "evidence", cutoff, reg.manifest())
    gaps = CatalogueGapDetector(io_, reg).detect(req_ref, snap_ref, context)
    voi = GapValueEstimator(io_)
    plan_ref = io_.put_json({"schema": "reva-acquisition-plan/1", "expected_coverage_gain": 1.0,
                             "note": "generic: acquisition would fill the affected cells"}, "evaluation")
    items = []
    for g in gaps:
        v = voi.estimate(g, plan_ref, None, default_budget())
        items.append({"gap_id": str(g.gap_id), "requirement_id": g.requirement_id, "concept": g.concept_version,
                      "geography": list(g.geography), "temporal_support": dict(g.temporal_support),
                      "reason": g.reason, "priority": g.priority, "priority_method": g.priority_method,
                      "value_estimate": {"kind": v.kind, "value": v.value, "unit": v.unit,
                                         "method_version": v.method_version,
                                         "assumptions_sha256": v.assumptions.sha256}})
    doc = {"schema": "reva-gap-report/1", "as_of": cutoff.isoformat(), "origin": origin,
           "catalogue_version": reg.requirements.get("catalogue_version"),
           "requirements_sha256": req_ref.sha256, "coverage_snapshot_sha256": snap_ref.sha256,
           "coverage": coverage_report(reg.requirements, snapshot, cutoff), "gaps": items}
    ref = io_.put_json(doc, "evaluation")
    with engine.begin() as conn:
        if getattr(ctx, "job_id", None) is not None and hasattr(ctx, "assert_lease"):
            ctx.assert_lease(conn)
        io_.register(conn, snap_ref, "coverage-snapshot/1")
        io_.register(conn, ref, "gap-report/1")
        repo.emit_outbox(conn, "autonomy.gap_report", {"artifact_sha256": ref.sha256, "gaps": len(items),
                                                       "origin": origin, "as_of": cutoff.isoformat()})
    return {"status": "ok", "artifact_sha256": ref.sha256, "gaps": len(items),
            "coverage": doc["coverage"]["unweighted"]}
