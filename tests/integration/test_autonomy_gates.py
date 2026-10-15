"""Permission tiers, gate independence, policy pinning, kill switch, LLM blocking, backlog bounds."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from reva.autonomy.common import ArtifactIO, AutonomyBlocked, default_budget, make_context
from reva.autonomy.discovery import EurostatCatalogueDiscoverer
from reva.autonomy.evidence import LlmEvidenceExtractor, StructuredCardImporter
from reva.autonomy.fixtures import BankHttp, bank_from_dir
from reva.autonomy.gate import PolicyPromotionGate
from reva.autonomy.generator import DeclarativeConnectorGenerator
from reva.autonomy.jobs import connector_change_proposal, handle_discovery, handle_gaps
from reva.autonomy.mapping import RegistryOntologyMapper
from reva.autonomy.policy import POLICY_DIR, AutonomyGuard, promotion_policy
from reva.autonomy.proposals import ProposalStore, base_commit
from reva.autonomy.sandbox import TrustedTemplateSandbox, trusted_template_policy
from reva.autonomy.validator import JsonStatConnectorValidator
from reva.kernel.errors import PolicyDenied
from reva.ports.autonomy import KnowledgeGap, PlatformChangeProposal, ValueEstimate

pytestmark = pytest.mark.db
CONNECTOR = "eurostat.demo_pjan.fr"


@pytest.fixture(autouse=True)
def _offline_sandbox_probe(monkeypatch):
    """Sandbox isolation probes target a closed local port in tests (no outbound packets)."""
    from reva.autonomy import sandbox as _sb

    monkeypatch.setattr(_sb, "PUBLIC_PROBE_ADDR", ("127.0.0.1", 1))


@pytest.fixture()
def io(db) -> ArtifactIO:
    return ArtifactIO(db.store)


def _value(io) -> ValueEstimate:
    return ValueEstimate("unsupported", None, None, {"status": "unquantified"}, io.put_json({"t": 1}, "evaluation"),
                         "test")


def _proposal(io, *, tier=0, paths=(), caps=("report",), author="agent:test-proposer", scientific=False,
              migration=False, manifest=None) -> PlatformChangeProposal:
    ref = io.put_json(manifest or {"schema": "test", "nonce": str(uuid.uuid4())}, "models")
    return PlatformChangeProposal(uuid.uuid4(), author, tier, tuple(paths), base_commit(), ref, tuple(caps),
                                  scientific, migration, _value(io), promotion_policy().version)


async def _t1(io, db, mutate_manifest=None):
    """A genuine T1 connector change with independent validator + sandbox reports."""
    bank = bank_from_dir(io, CONNECTOR)
    ctx = make_context(io, "evidence", datetime.now(UTC), {"t": 1})
    gap = KnowledgeGap(uuid.uuid4(), "R-POP-JAN-AGESEX", "demography.population.jan1_age_sex@1.0.0", ("FRA",),
                       {"years": [2023]}, "unsupported", 1.0, "test", ctx)
    cands = await EurostatCatalogueDiscoverer(io, BankHttp(io, bank)).discover(gap, default_budget())
    mapping = RegistryOntologyMapper(io).assess(cands[0], ctx)[0]
    cp = DeclarativeConnectorGenerator(io).generate(cands[0], mapping, ("eurostat_jsonstat",))
    manifest = io.get_json(cp.manifest)
    if mutate_manifest:
        mutate_manifest(manifest)
        cp = type(cp)(cp.proposal_id, cp.candidate, cp.template_id, cp.template_digest,
                      io.put_json(manifest, "models"), cp.mapping)
    v = await JsonStatConnectorValidator(io).validate(cp, bank, default_budget())
    p = connector_change_proposal(io, cp, manifest, mapping, _value(io), promotion_policy())
    from reva.autonomy.jobs import connector_promotion_scope

    ProposalStore(db.worker, io).persist(p, proposal_type="connector_promotion",
                                         scope=connector_promotion_scope(io, manifest, mapping), risk={})
    s = await TrustedTemplateSandbox(io, fixtures=bank).run(p, trusted_template_policy(default_budget()))
    return p, v, s


def _gate(db, io, **kw) -> PolicyPromotionGate:
    return PolicyPromotionGate(db.gate, io, **kw)


def _persist(db, io, p):
    ProposalStore(db.worker, io).persist(p, proposal_type="test", scope={}, risk={})


# --------------------------------------------------------------------------- tiers
async def test_t1_within_signed_scope_promotes(db, io):
    p, v, s = await _t1(io, db)
    assert v.outcome == "pass" and s.outcome == "pass", (v.findings, s.findings)
    d = _gate(db, io).decide(p, [v, s], promotion_policy().ref())
    assert d.outcome == "promote", d.reasons
    assert d.reviewer_identity == "gate-evaluator:promotion" and d.signed_receipt is not None


async def test_t1_without_independent_reports_is_not_promoted(db, io):
    p, v, _s = await _t1(io, db)
    d = _gate(db, io).decide(p, [v], promotion_policy().ref())  # no sandbox report
    assert d.outcome == "reject"
    assert any("sandbox" in r for r in d.reasons)


async def test_t1_out_of_scope_is_rejected(db, io):
    def to_germany(m):
        rp = m["request_parameters"]
        rp["filters"]["geo"] = ["DE"]
        rp["pinned_categories"]["geo"] = ["DE"]
        m["name"] = "eurostat.demo_pjan.de"

    p, v, s = await _t1(io, db, mutate_manifest=to_germany)
    d = _gate(db, io).decide(p, [r for r in (v, s) if r.outcome == "pass"], promotion_policy().ref())
    assert d.outcome == "reject"
    assert "T1 out of signed policy scope" in d.reasons
    assert any("outside approved scope" in r for r in d.reasons)


async def test_t1_with_foreign_reports_is_rejected(db, io):
    p, v, s = await _t1(io, db)
    other = _proposal(io, tier=1, caps=("register_connector_version",), manifest=io.get_json(p.patch_or_manifest))
    ProposalStore(db.worker, io).persist(other, proposal_type="connector_promotion", scope={}, risk={})
    d = _gate(db, io).decide(other, [v, s], promotion_policy().ref())  # s covers p, not `other`
    assert d.outcome == "reject" and any("does not cover" in r for r in d.reasons)


@pytest.mark.parametrize("paths,caps,expected_tier", [
    ((), ("run_backtest",), 2),
    (("src/reva/models/forecasting/new_model.py",), ("propose",), 2),
    (("src/reva/kernel/values.py",), ("propose",), 3),
    (("migrations/versions/0008_x.py",), ("propose",), 3),
    ((), ("causal_claim",), 3),
])
def test_t2_t3_need_review(db, io, paths, caps, expected_tier):
    p = _proposal(io, tier=0, paths=paths, caps=caps, scientific=True)  # declares T0: must be raised
    _persist(db, io, p)
    d = _gate(db, io).decide(p, [], promotion_policy().ref())
    assert d.outcome in ("needs_review", "blocked")
    assert d.outcome == "needs_review", d.reasons
    ev = io.get_json(d.signed_receipt)
    assert ev["tier"]["effective_tier"] == expected_tier and ev["db_decision"] == "needs_review"


def test_t0_is_automatic(db, io):
    p = _proposal(io, tier=0, caps=("report",))
    _persist(db, io, p)
    d = _gate(db, io).decide(p, [], promotion_policy().ref())
    assert d.outcome == "promote"
    with db.owner.connect() as conn:
        row = conn.execute(text("SELECT decision, evaluator FROM ops.gate_result WHERE id=:i"),
                           {"i": d.decision_id}).mappings().one()
    assert row["decision"] == "pass" and row["evaluator"] == "gate-evaluator:promotion"


def test_declared_tier_cannot_be_lowered_for_manifest(db, io):
    p = _proposal(io, tier=0, paths=("connectors/x/1.0.0/manifest.json",), caps=("register_connector_version",))
    _persist(db, io, p)
    d = _gate(db, io).decide(p, [], promotion_policy().ref())
    assert d.outcome == "reject"  # derived T1, and it is not a valid in-scope connector change
    assert any("raised to T1" in r for r in d.reasons)


# --------------------------------------------------------------------------- independence
def test_proposer_cannot_write_its_own_gate_result(db, io):
    p = _proposal(io, author="agent:sneaky")
    _persist(db, io, p)
    ev = io.put_json({"forged": True}, "evaluation")
    with db.worker.begin() as conn:
        io.register(conn, ev, "test/1")
    for evaluator in ("gate-evaluator:agent:sneaky", "agent:sneaky"):
        with pytest.raises(sqlalchemy.exc.DBAPIError) as exc, db.worker.begin() as conn:
            conn.execute(text("""INSERT INTO ops.gate_result(proposal_id, gate_name, policy_version, decision,
                                    evidence_sha256, evaluator) VALUES (:p, 'promotion', 'x', 'pass', :e, :ev)"""),
                         {"p": p.proposal_id, "e": ev.sha256, "ev": evaluator})
        # GATE: the worker (proposer's process) holds no INSERT on ops.gate_result at all
        assert "permission denied" in str(exc.value)
    # even the independent gate login cannot record a result for a proposal authored under its own name
    p2 = _proposal(io, author="sneaky")
    _persist(db, io, p2)
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="cannot evaluate its own proposal"), \
            db.gate.begin() as conn:
        conn.execute(text("SELECT ops.record_gate_result(:p, 'sneaky', 'x', 'pass', :e, 'gate-evaluator:sneaky')"),
                     {"p": p2.proposal_id, "e": ev.sha256})
    with pytest.raises(PolicyDenied):
        PolicyPromotionGate(db.gate, io, name="sneaky").decide(
            _proposal(io, author="sneaky"), [], promotion_policy().ref())


async def test_gate_ignores_reports_from_the_proposer(db, io):
    from reva.autonomy.common import report

    p, v, s = await _t1(io, db)
    forged = report("pass", [], v.evidence_manifest, f"connector-validator:{p.author}")
    d = _gate(db, io).decide(p, [forged, s], promotion_policy().ref())
    assert d.outcome == "reject" and any("non-independent" in r for r in d.reasons)


# --------------------------------------------------------------------------- policy pinning
async def test_policy_change_makes_gate_refuse(db, io, tmp_path):
    p, v, s = await _t1(io, db)
    copy = tmp_path / "promotion.yaml"
    shutil.copy(POLICY_DIR / "promotion.yaml", copy)
    pinned = promotion_policy().sha256
    ok = PolicyPromotionGate(db.gate, io, policy_path=copy, pinned_sha256=pinned)
    assert ok.decide(p, [v, s], promotion_policy().ref()).outcome == "promote"
    copy.write_text(copy.read_text().replace("allowed_hosts: [ec.europa.eu]",
                                             "allowed_hosts: [ec.europa.eu, evil.example.com]"))
    d = PolicyPromotionGate(db.gate, io, policy_path=copy, pinned_sha256=pinned).decide(
        p, [v, s], promotion_policy().ref())
    assert d.outcome == "blocked" and any("changed" in r for r in d.reasons)
    # a forged 'signed policy' reference is refused as well
    from reva.ports.autonomy import ArtifactRef

    d2 = _gate(db, io).decide(p, [v, s], ArtifactRef("0" * 64, "policy", "application/yaml"))
    assert d2.outcome == "blocked"


def test_repository_policy_matches_pin():
    pol = promotion_policy()
    assert pol.version == "promotion-1.1.0"
    from reva.adapters.connectors.eurostat_template import template_digest
    from reva.autonomy.binding import approved_template_digest

    # the reviewed template code is pinned in the (digest-pinned) policy (R4-11)
    assert approved_template_digest(pol, "eurostat_jsonstat") == template_digest()


# --------------------------------------------------------------------------- kill switch / budgets
def _ctx(db, **settings_over):
    from reva.operations.context import JobContext

    s = db.settings.model_copy(update=settings_over)
    return JobContext(job_id=None, job_type="discovery", worker_id="t", fencing_token=0, attempt=1,  # type: ignore[arg-type]
                      settings=s, worker_engine=db.worker, sim_engine=None, store=db.store)


@pytest.mark.parametrize("over", [{"reva_autonomy_kill_switch": True}, {"reva_autonomy_mode": "disabled"}])
async def test_kill_switch_blocks_everything(db, io, over):
    with db.owner.connect() as conn:
        before = conn.execute(text("SELECT (SELECT count(*) FROM ops.proposal), (SELECT count(*) FROM ops.gate_result)"
                                   )).one()
    ctx = _ctx(db, **over)
    assert handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, ctx)["status"] == "blocked"
    assert handle_gaps({}, ctx)["status"] == "blocked"
    guard = AutonomyGuard.from_settings(ctx.settings)
    with pytest.raises(AutonomyBlocked):
        guard.check("discover")
    bank = bank_from_dir(io, CONNECTOR)
    gctx = make_context(io, "evidence", datetime.now(UTC), {"t": 1})
    gap = KnowledgeGap(uuid.uuid4(), "R-POP-JAN-AGESEX", "demography.population.jan1_age_sex@1.0.0", ("FRA",),
                       {"years": [2023]}, "unsupported", 1.0, "t", gctx)
    with pytest.raises(AutonomyBlocked):
        await EurostatCatalogueDiscoverer(io, BankHttp(io, bank), guard=guard).discover(gap, default_budget())
    p = _proposal(io)
    with pytest.raises(AutonomyBlocked):
        ProposalStore(db.worker, io, guard).persist(p, proposal_type="t", scope={}, risk={})
    rep = await TrustedTemplateSandbox(io, fixtures=bank, guard=guard).run(p, trusted_template_policy(default_budget()))
    assert rep.outcome == "blocked"
    assert PolicyPromotionGate(db.gate, io, guard=guard).decide(p, [], promotion_policy().ref()).outcome == "blocked"
    with db.owner.connect() as conn:
        after = conn.execute(text("SELECT (SELECT count(*) FROM ops.proposal), (SELECT count(*) FROM ops.gate_result)"
                                  )).one()
    assert tuple(after) == tuple(before)


def test_proposal_backlog_is_bounded(db, io):
    from reva.autonomy.policy import open_proposal_count

    with db.worker.connect() as conn:
        n = open_proposal_count(conn)
    guard = AutonomyGuard(max_open_proposals=n + 1)
    store = ProposalStore(db.worker, io, guard)
    store.persist(_proposal(io, tier=2, caps=("run_backtest",)), proposal_type="t", scope={}, risk={})
    with pytest.raises(AutonomyBlocked, match="backlog"):
        store.persist(_proposal(io, tier=2, caps=("run_backtest",)), proposal_type="t", scope={}, risk={})


def test_budgets_policy_is_used():
    guard = AutonomyGuard.from_settings(type("S", (), {})())
    assert guard.max_open_proposals == 20 and guard.source_request_limit("eurostat.dissemination") == 100
    assert guard.llm_monthly_budget_usd == 0


# --------------------------------------------------------------------------- evidence / LLM
def _card(quote: str) -> dict:
    import yaml

    doc = yaml.safe_load((Path(__file__).parents[2] / "registry" / "evidence" / "kilian2009.yaml").read_text())
    card = dict(doc["card"])
    card["source_spans"] = [{"artifact": "crossref_metadata", "locator": "/message/title/0", "quote": quote}]
    return card


def test_llm_disabled_is_blocked_and_candidate_only(db, io):
    guard = AutonomyGuard.from_settings(db.settings)
    assert guard.llm_enabled is False
    ctx = make_context(io, "evidence", datetime.now(UTC), {"t": 1})
    doc = io.put_json({"cards": []}, "normalised")
    out = LlmEvidenceExtractor(guard).run(doc, ctx)
    assert out.status == "blocked" and "llm disabled" in (out.reason or "") and out.candidates == ()
    from reva.kernel.errors import CapabilityGap

    with pytest.raises(CapabilityGap):
        LlmEvidenceExtractor(guard).extract(doc, ctx)
    enabled = AutonomyGuard(llm_enabled=True, llm_monthly_budget_usd=0)
    assert LlmEvidenceExtractor(enabled).run(doc, ctx).status == "blocked"  # budget 0


def test_structured_importer_validates_spans(db, io):
    title = "Not All Oil Price Shocks Are Alike: Disentangling Demand and Supply Shocks in the Crude Oil Market"
    ctx = make_context(io, "evidence", datetime.now(UTC), {"t": 1})
    good, bad = _card("Not All Oil Price Shocks Are Alike"), _card("a quote that is not in the source")
    bad["claim_id"] = good["claim_id"] + ".bad"
    doc = io.put_json({"cards": [good, bad, {"claim_id": "x"}], "source_text": {"crossref_metadata": title}},
                      "normalised")
    out = StructuredCardImporter(io).run(doc, ctx)
    assert len(out.candidates) == 1 and out.candidates[0].status == "candidate"
    assert len(out.rejected) == 2


# --------------------------------------------------------------------------- R4 hardening (no promotion state)
def _r4_tamper(m):
    """The reviewer's malicious manifest (R4-01): approved dataset/concept/geo, lying semantics and identity."""
    sem = m["request_parameters"]["semantics"]
    sem["temporal_support"] = "point:07-01"            # midyear instead of 1 January
    sem["population_universe"] = "de_facto_population"  # different universe
    sem["unit_ref"] = "unit.thousand_persons"           # different unit/scale
    m["source_key"] = "worldbank.wdi"                   # registered under another source
    m["lineage_family"] = "worldbank"                   # source-family independence bypass
    m["name"] = "worldbank.wdi.indicators"              # squats a built-in connector name
    m["version"] = "9.9.9"
    m["cadence"]["minimum_interval_seconds"] = 60


async def test_r4_01_malicious_manifest_is_rejected(db, io):
    p, v, s = await _t1(io, db, mutate_manifest=_r4_tamper)
    blockers = {f.code for f in v.findings if f.severity == "blocker"}
    # the validator's contract comes from the registry, so lying semantics fail validation
    assert v.outcome == "fail" and {"temporal_support_mismatch", "unit_mismatch", "support_mismatch"} <= blockers
    d = _gate(db, io).decide(p, [v, s], promotion_policy().ref())
    assert d.outcome == "reject"
    # even with a forged 'pass' for the same evidence, the gate re-derives scope from policy + registry
    from reva.autonomy.common import report

    forged = report("pass", [], v.evidence_manifest, v.evaluator_version)
    d2 = _gate(db, io).decide(p, [forged, s], promotion_policy().ref())
    assert d2.outcome == "reject", d2.reasons
    text_ = " | ".join(d2.reasons)
    assert "disagrees with its stored evidence" in text_
    for needle in ("semantics.temporal_support", "semantics.population_universe", "semantics.unit_ref",
                   "source_key 'worldbank.wdi'", "lineage_family", "collides with a built-in connector",
                   "is not the derived name", "cadence"):
        assert needle in text_, needle


async def test_r4_01_scope_is_rederived_even_with_genuine_pass_reports(db, io):
    """Fields the validator cannot see (name, source, lineage) are still bound by the gate."""
    def rename(m):
        m["name"] = "eurostat.demo_pjan.fr-shadow"
        m["lineage_family"] = "worldbank_wdi"

    p, v, s = await _t1(io, db, mutate_manifest=rename)
    assert v.outcome == "pass" and s.outcome == "pass", (v.findings, s.findings)
    d = _gate(db, io).decide(p, [v, s], promotion_policy().ref())
    assert d.outcome == "reject"
    assert any("derived name" in r for r in d.reasons) and any("lineage_family" in r for r in d.reasons)
    ev = io.get_json(d.signed_receipt)
    assert all(x["verdict"] == "verified" for x in ev["tier"]["reports_verified"])


async def test_r4_01_validator_contract_must_be_registry_derived(db, io):
    """A validator report whose data contract was built from the manifest is not accepted."""
    from reva.autonomy.common import report

    p, v, s = await _t1(io, db)
    ev = io.get_json(v.evidence_manifest)
    ev["data_contract"] = io.put_json({"schema": "reva-data-contract/1", "from": "manifest"}, "models").sha256
    fake = report("pass", list(v.findings), io.put_json(ev, "evaluation"), v.evaluator_version)
    d = _gate(db, io).decide(p, [fake, s], promotion_policy().ref())
    assert d.outcome == "reject" and any("registry-derived data contract" in r for r in d.reasons)


async def test_r4_11_template_digest_is_pinned_in_policy(db, io, tmp_path):
    import hashlib

    from reva.ports.autonomy import ArtifactRef

    p, v, s = await _t1(io, db)
    copy = tmp_path / "promotion.yaml"
    text0 = (POLICY_DIR / "promotion.yaml").read_text()
    import re as _re

    copy.write_text(_re.sub(r'template_sha256: "[^"]*"', 'template_sha256: "sha256:' + "0" * 64 + '"', text0))
    sha = hashlib.sha256(copy.read_bytes()).hexdigest()
    d = PolicyPromotionGate(db.gate, io, policy_path=copy, pinned_sha256=sha).decide(
        p, [v, s], ArtifactRef(sha, "policy", "application/yaml"))
    assert d.outcome == "reject" and any("pinned in the signed policy" in r for r in d.reasons)


@pytest.mark.parametrize("path,tier", [
    ("./src/reva/kernel/values.py", 3), ("registry/./policies/promotion.yaml", 3), ("SRC/Reva/Kernel/x.py", 3),
    ("src/reva/adapters/connectors/eurostat_template.py", 3), ("../outside.txt", 3), ("/etc/passwd", 3),
    ("a\\b.json", 3), ("connectors/eurostat.demo_pjan.fr/1.0.1/manifest.json", 1),
])
def test_r4_11_paths_are_normalised_before_tiering(path, tier):
    from reva.autonomy.policy import derive_tier

    assert derive_tier((path,), ("propose",), False, False)[0] == tier


async def test_r4_01_promote_connector_refuses_fabricated_decision(db, io):
    from reva.autonomy.jobs import promote_connector
    from reva.ports.autonomy import GateDecision

    p, _v, _s = await _t1(io, db)
    change = io.get_json(p.patch_or_manifest)
    fake = GateDecision(uuid.uuid4(), "promote", p.proposal_id, promotion_policy().version, (), ("trust me",),
                        "gate-evaluator:promotion", None)
    with db.owner.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM evidence.connector_version")).scalar_one()
    with pytest.raises(PolicyDenied):
        promote_connector(db.worker, io, _ctx(db), fake, promotion_policy())
    bad = dict(change["manifest"], name="worldbank.wdi.indicators")
    with pytest.raises(PolicyDenied):
        promote_connector(db.worker, io, _ctx(db), fake, promotion_policy(), expected_manifest=bad)
    with db.owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM evidence.connector_version")).scalar_one() == before


def test_r4_13_kill_switch_is_read_fresh(db, monkeypatch, tmp_path):
    from reva.autonomy.jobs import promoted_connectors

    guard = AutonomyGuard.from_settings(db.settings)
    assert guard.blocked_reason() is None
    monkeypatch.setenv("REVA_AUTONOMY_KILL_SWITCH", "true")  # flipped after the guard was built
    assert "KILL_SWITCH" in (guard.blocked_reason() or "")
    with pytest.raises(AutonomyBlocked):
        guard.check("promote")
    assert promoted_connectors(db.worker, db.store) == {}  # autonomy-promoted connectors stop running too
    monkeypatch.delenv("REVA_AUTONOMY_KILL_SWITCH")
    kill = tmp_path / "autonomy.kill"
    monkeypatch.setenv("REVA_AUTONOMY_KILL_FILE", str(kill))
    assert guard.blocked_reason() is None
    kill.write_text("stop")
    assert "kill file" in (guard.blocked_reason() or "")


def _budget_policy(**per_source):
    from reva.autonomy.policy import budgets_policy

    base = budgets_policy()
    content = {**base.content, "per_source_daily_requests": {**base.content["per_source_daily_requests"],
                                                             **per_source}}
    return type(base)(base.path, base.sha256, content)


def test_r4_08_discovery_budget_zero_means_zero_and_daily_limit_holds(db):
    from reva.autonomy.budget import BudgetExhausted, reserve_discovery, settle_discovery

    src = f"test.autonomy.{uuid.uuid4().hex[:8]}"
    with pytest.raises(BudgetExhausted):
        reserve_discovery(db.worker, src, policy=_budget_policy(**{src: 0}))  # 0 means no request
    with pytest.raises(BudgetExhausted):
        reserve_discovery(db.worker, src + ".unlisted", policy=_budget_policy())  # no limit: fail closed
    pol = _budget_policy(**{src: 7})
    r1 = reserve_discovery(db.worker, src, policy=pol)
    assert r1.max_requests == 5
    settle_discovery(db.worker, r1, requests=4, nbytes=10)
    r2 = reserve_discovery(db.worker, src, policy=pol)
    assert r2.max_requests == 3  # 7 per day - 4 consumed
    with pytest.raises(BudgetExhausted):  # unsettled reservation keeps counting (fail closed)
        reserve_discovery(db.worker, src, policy=pol)
    with db.owner.connect() as conn:
        rows = conn.execute(text("SELECT account_key, consumed FROM ops.budget_ledger WHERE account_key LIKE :a"),
                            {"a": f"autonomy:discovery:{src}:%"}).all()
    assert {r[0] for r in rows} == {f"autonomy:discovery:{src}:requests", f"autonomy:discovery:{src}:bytes"}


def test_r4_08_live_discovery_refused_when_budget_used_up(db):
    with db.owner.begin() as conn:
        conn.execute(text("INSERT INTO ops.budget_ledger(account_key, operation_id, unit, reserved, consumed) "
                          "VALUES ('autonomy:discovery:eurostat.dissemination:requests', :o, 'requests', 0, 1000)"),
                     {"o": f"test-exhaust-{uuid.uuid4()}"})
    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "live"}, _ctx(db))
    assert out["status"] == "blocked" and out["code"] == "budget_exhausted", out


def test_discovery_rejects_unknown_payload_keys(db):
    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture", "query": "gdp"}, _ctx(db))
    assert out["status"] == "rejected" and out["unsupported"] == ["query"]
