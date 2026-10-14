"""Autonomy job handlers (``handle(payload, ctx) -> dict``).

``handle_discovery`` runs the V1 discovery loop (architecture §10.2):

    KnowledgeGapDetector → ValueOfInformationEstimator (proxy) → SourceDiscoverer
    → OntologyMapper → ConnectorGenerator → ConnectorValidator → SandboxRunner
    → PromotionGate → (T1 within scope) register connector version + scoped
    eligibility, keeping the previous state for rollback.

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

from reva.adapters.connectors.eurostat_template import (
    TEMPLATE_HOSTS,
    EurostatJsonStatConnector,
    template_digest,
)
from reva.adapters.postgres import repositories as repo
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
from reva.autonomy.policy import AutonomyGuard, PinnedPolicy, promotion_policy
from reva.autonomy.proposals import ProposalStore, base_commit
from reva.autonomy.sandbox import TrustedTemplateSandbox, external_runner_from_settings, trusted_template_policy
from reva.autonomy.validator import JsonStatConnectorValidator
from reva.autonomy.voi import GapValueEstimator
from reva.cli.seed import record_mapping_and_eligibility, register_connector_version
from reva.kernel.errors import RevaError
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
    requirement = str(payload.get("requirement_id", "R-POP-JAN-AGESEX"))
    mode = str(payload.get("mode", "live"))
    if requirement not in DISCOVERABLE_REQUIREMENTS or mode not in ("live", "fixture"):
        return {"status": "rejected", "reason": "unsupported requirement or mode",
                "supported": list(DISCOVERABLE_REQUIREMENTS)}
    try:
        return run_sync(discovery_flow(ctx, requirement, mode, guard))
    except AutonomyBlocked as e:
        return {"status": "blocked", "reason": e.message}


async def discovery_flow(ctx: Any, requirement: str, mode: str, guard: AutonomyGuard) -> dict[str, Any]:
    io_ = ArtifactIO(ctx.store)
    reg = load_registry()
    policy = promotion_policy()
    engine: Engine = ctx.worker_engine
    cutoff = now_utc()
    origins = ["test_fixture"] if mode == "fixture" else ["live"]
    budget = default_budget(max_requests=min(5, guard.source_request_limit("eurostat.dissemination") or 5))
    summary: dict[str, Any] = {"status": "started", "requirement_id": requirement, "mode": mode,
                               "policy": {"version": policy.version, "sha256": policy.sha256}}
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
    http, bank = _http(mode, io_, requirement, ctx.settings)
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
    # 4. manifest
    generator = DeclarativeConnectorGenerator(io_, registry=reg, policy=policy)
    cp = generator.generate(cand, primary, tuple((policy.content.get("approved_templates") or {}).keys()))
    manifest = io_.get_json(cp.manifest)
    summary["connector"] = {"name": manifest["name"], "version": manifest["version"],
                            "manifest_sha256": cp.manifest.sha256}
    # 5. validation (captured replay or bounded live sample)
    live_factory = None
    if mode == "live":
        def live_factory(hosts: tuple[str, ...]) -> Any:
            return http.client
        bank = bank or io_.put_json({"schema": "reva-fixture-bank/1", "connector": manifest["name"],
                                     "entries": []}, "raw")
    validator = JsonStatConnectorValidator(io_, live_client_factory=live_factory, guard=guard)
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
                      scope={"concept": primary.target_concept_version, "dataset": cand.dataset_id,
                             "geo": manifest["request_parameters"]["filters"].get("geo"),
                             "eligibility_scope": manifest["eligibility_scope"]},
                      risk={"tier": "T1", "hosts": manifest["allowed_hosts"], "code": "none (declarative)"},
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
    # 9. promotion within scope
    if decision.outcome == "promote":
        summary["promotion"] = promote_connector(engine, io_, ctx, manifest, primary, decision, policy)
        summary["status"] = "promoted"
    else:
        summary["status"] = f"gate_{decision.outcome}"
    return summary


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
    mapper_report = report("fail", findings, mapping.mapping_manifest, "ontology-mapper:registry-profile@1")
    decision = gate.decide(p, [mapper_report], policy.ref())
    reg_mapping = mm.get("registry_mapping")
    with engine.begin() as conn:
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


def promote_connector(engine: Engine, io_: ArtifactIO, ctx: Any, manifest: dict[str, Any],
                      mapping: MappingProposal, decision: GateDecision, policy: PinnedPolicy) -> dict[str, Any]:
    """Register connector version + scoped eligibility atomically; keep prior state for rollback."""
    if decision.outcome != "promote" or not decision.reviewer_identity.startswith("gate-evaluator:"):
        raise AutonomyBlocked("promotion requires a promote decision from a gate evaluator")
    connector = EurostatJsonStatConnector(manifest)
    spec = connector.describe()
    mm = io_.get_json(mapping.mapping_manifest)
    concept_id, version = mapping.target_concept_version.split("@", 1)
    subjects = sorted((mm.get("scope") or {}).get("subjects") or [])
    with engine.begin() as conn:
        if getattr(ctx, "job_id", None) is not None and hasattr(ctx, "assert_lease"):
            ctx.assert_lease(conn)  # fenced: a worker that lost its lease cannot promote
        concept_uuid = repo.concept_version_id(conn, concept_id, version)
        previous = {
            "connector_versions": [dict(r) for r in conn.execute(text(
                "SELECT id::text, name, version, spec_sha256, code_digest FROM evidence.connector_version "
                "WHERE name = :n ORDER BY recorded_at"), {"n": spec.name}).mappings()],
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
                                    "template_digest": template_digest()})
        repo.record_activity(conn, act)
        cv_id = register_connector_version(conn, io_.store, spec, act.id)
        rollback = {"schema": "reva-promotion-rollback/1", "connector": spec.name, "version": spec.version,
                    "connector_version_id": str(cv_id), "concept": mapping.target_concept_version,
                    "previous": previous, "gate_result_id": str(decision.decision_id),
                    "rollback": "append eligibility decision 'retired' for this connector version; "
                                "previous connector versions remain registered and selectable"}
        rb_ref = io_.put_json(rollback, "models")
        io_.register(conn, rb_ref, "promotion-rollback/1")
        record_mapping_and_eligibility(conn, {
            "mapping_key": mm["mapping_key"], "version": mm["version"],
            "source_ref": {"provider": "eurostat", "dataset": manifest["dataset"],
                           "template": manifest["template"], "dimensions": manifest["request_parameters"]
                           ["resolution_dimensions"]},
            "target": {"concept_id": concept_id, "version": version}, "mapping_kind": "exact",
            "applicability": {"subjects": subjects, "years": manifest["request_parameters"]["time"],
                              "geo": manifest["request_parameters"]["filters"].get("geo"),
                              "gate_result_id": str(decision.decision_id), "rollback_sha256": rb_ref.sha256},
            "eligibility": "eligible", "connector": {"name": spec.name, "version": spec.version},
        }, act.id, policy_version=policy.version)
        repo.emit_outbox(conn, "autonomy.connector_promoted",
                         {"connector": spec.name, "version": spec.version, "connector_version_id": str(cv_id),
                          "concept": mapping.target_concept_version, "subjects": subjects,
                          "gate_result_id": str(decision.decision_id), "rollback_sha256": rb_ref.sha256})
        repo.audit(conn, decision.reviewer_identity, "autonomy.connector_promoted",
                   {"connector": spec.name, "version": spec.version}, {"rollback_sha256": rb_ref.sha256})
    return {"connector_version_id": str(cv_id), "rollback_sha256": rb_ref.sha256, "subjects": subjects}


def rollback_promotion(engine: Engine, connector_name: str, version: str, concept: str, actor: str,
                       reason: str) -> UUID:
    """Append a 'retired' eligibility decision (latest decision wins); history stays immutable."""
    if not actor.startswith(("gate-evaluator:", "human:")):
        raise AutonomyBlocked("rollback must be performed by a gate evaluator or a named human")
    concept_id, cversion = concept.split("@", 1)
    with engine.begin() as conn:
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
        rid = conn.execute(text(
            """INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                 policy_version, review_activity_id)
               VALUES (:cv, :c, CAST(:ap AS jsonb), 'retired', :p, :a) RETURNING id"""),
            {"cv": cv["id"], "c": c, "ap": json.dumps(dict(prev["applicability"])),
             "p": prev["policy_version"], "a": act.id}).scalar_one()
        repo.emit_outbox(conn, "autonomy.connector_rolled_back", {"connector": connector_name, "version": version,
                                                                   "concept": concept})
    return UUID(str(rid))


def promoted_connectors(engine: Engine, store: Any) -> dict[str, EurostatJsonStatConnector]:
    """Rebuild eligible declarative connectors from their registered spec artifacts.

    A spec whose code digest differs from the current reviewed template is skipped
    (the template changed: re-validation/promotion is required).
    """
    io_ = ArtifactIO(store)
    out: dict[str, EurostatJsonStatConnector] = {}
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name, version, spec_sha256, code_digest FROM evidence.connector_version "
            "WHERE declared_capabilities->>'template' = 'eurostat_jsonstat' ORDER BY recorded_at")).mappings().all()
    for r in rows:
        if r["code_digest"] != template_digest():
            log.warning("skipping %s@%s: template digest changed", r["name"], r["version"])
            continue
        spec = io_.get_json(ArtifactRef(r["spec_sha256"], "models", "application/json"))
        out[r["name"]] = EurostatJsonStatConnector(spec["field_mappings"]["manifest"])
    return out


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
