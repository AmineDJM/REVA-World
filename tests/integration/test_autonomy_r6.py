"""Regression tests for the R6 adversarial review of autonomy (docs/dev/review_autonomy_r6.md).

Each test is the review's scratch attack (scratchpad/review6) turned into an assertion of the fixed
behaviour: it failed against the tree the review ran on and passes after the R6 fixes
(docs/dev/review_autonomy_r6_fixes.md). Tests that change promotion state of the shared session
database (promotion, rollback) live at the end of ``test_eurostat_discovery.py`` instead.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from reva.autonomy.common import ArtifactIO, default_budget, make_context, report
from reva.autonomy.discovery import EurostatCatalogueDiscoverer
from reva.autonomy.fixtures import BankHttp, CapturingHttp, bank_from_dir
from reva.autonomy.gate import PolicyPromotionGate
from reva.autonomy.generator import DeclarativeConnectorGenerator
from reva.autonomy.jobs import connector_change_proposal
from reva.autonomy.mapping import RegistryOntologyMapper
from reva.autonomy.policy import promotion_policy
from reva.autonomy.proposals import ProposalStore, base_commit
from reva.autonomy.sandbox import TrustedTemplateSandbox, trusted_template_policy
from reva.autonomy.validator import JsonStatConnectorValidator
from reva.evidence.runner import FIXTURE_ROOT
from reva.kernel.errors import PolicyDenied, ValidationProblem
from reva.ports.autonomy import KnowledgeGap, PlatformChangeProposal, ValueEstimate

CONNECTOR = "eurostat.demo_pjan.fr"
CONCEPT = "demography.population.jan1_age_sex@1.0.0"
SAMPLE_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/demo_pjan"
              "?format=JSON&lang=EN&geo=FR&time=2023")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _offline_sandbox_probe(monkeypatch):
    from reva.autonomy import sandbox as _sb

    monkeypatch.setattr(_sb, "PUBLIC_PROBE_ADDR", ("127.0.0.1", 1))


@pytest.fixture()
def io(db) -> ArtifactIO:
    return ArtifactIO(db.store)


@pytest.fixture()
def fio(tmp_path) -> ArtifactIO:
    from reva.adapters.objects.filesystem import FilesystemArtifactStore

    return ArtifactIO(FilesystemArtifactStore(tmp_path / "objects"))


def _value(io) -> ValueEstimate:
    return ValueEstimate("unsupported", None, None, {"status": "unquantified"},
                         io.put_json({"t": str(uuid.uuid4())}, "evaluation"), "test")


def _sample_doc() -> dict:
    d = FIXTURE_ROOT / CONNECTOR
    e = next(x for x in json.loads((d / "manifest.json").read_text()) if x["url"] == SAMPLE_URL)
    return json.loads((d / e["file"]).read_bytes())


async def _generate(io, overrides=None):
    bank = bank_from_dir(io, CONNECTOR, overrides=overrides)
    ctx = make_context(io, "evidence", datetime.now(UTC), {"t": 1})
    gap = KnowledgeGap(uuid.uuid4(), "R-POP-JAN-AGESEX", CONCEPT, ("FRA",), {"years": [2023]}, "unsupported",
                       1.0, "test", ctx)
    cands = await EurostatCatalogueDiscoverer(io, BankHttp(io, bank)).discover(gap, default_budget())
    mapping = RegistryOntologyMapper(io).assess(cands[0], ctx)[0]
    cp = DeclarativeConnectorGenerator(io).generate(cands[0], mapping, ("eurostat_jsonstat",))
    return bank, mapping, cp


async def _t1(io, db, mutate_manifest=None, overrides=None, scope=None):
    """A T1 connector change persisted as ``connector_promotion`` with validator + sandbox reports."""
    from reva.adapters.connectors.eurostat_template import template_digest
    from reva.autonomy.jobs import connector_promotion_scope
    from reva.autonomy.promotion import resolve_connector_version

    bank, mapping, cp = await _generate(io, overrides)
    manifest = io.get_json(cp.manifest)
    generated_version = manifest["version"]
    if mutate_manifest:
        mutate_manifest(manifest)
    if manifest["version"] == generated_version:  # content-derived, as discovery does (shared session DB)
        with db.worker.connect() as conn:
            manifest["version"] = resolve_connector_version(conn, io, manifest, template_digest())[0]
    cp = type(cp)(cp.proposal_id, cp.candidate, cp.template_id, cp.template_digest,
                  io.put_json(manifest, "models"), cp.mapping)
    v = await JsonStatConnectorValidator(io).validate(cp, bank, default_budget())
    p = connector_change_proposal(io, cp, manifest, mapping, _value(io), promotion_policy())
    ProposalStore(db.worker, io).persist(
        p, proposal_type="connector_promotion",
        scope=scope if scope is not None else connector_promotion_scope(io, manifest, mapping), risk={})
    s = await TrustedTemplateSandbox(io, fixtures=bank).run(p, trusted_template_policy(default_budget()))
    return p, v, s, manifest, mapping


def _gate(db, io) -> PolicyPromotionGate:
    # the gate records with its own login (reva_gate) once GATE's process separation is in place
    return PolicyPromotionGate(getattr(db, "gate", None) or db.worker, io)


def _gate_rows(db, pid) -> int:
    with db.owner.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM ops.gate_result WHERE proposal_id = :p"),
                            {"p": pid}).scalar_one()


def _job_ctx(db, settings=None):
    from reva.operations.context import JobContext

    jid = uuid.uuid4()
    with db.owner.begin() as conn:
        conn.execute(text("""INSERT INTO ops.job(id, job_type, payload, idempotency_key, status, worker_id,
                                 lease_until, fencing_token, attempts)
                             VALUES (:i, 'discovery', '{}'::jsonb, :k, 'running', 'test-worker',
                                     clock_timestamp() + interval '10 minutes', 7, 1)"""),
                     {"i": jid, "k": f"r6-{jid}"})
    return JobContext(job_id=jid, job_type="discovery", worker_id="test-worker", fencing_token=7, attempt=1,
                      settings=settings or db.settings, worker_engine=db.worker, sim_engine=None, store=db.store)


# =========================================================================== R6-01
@pytest.mark.db
def test_r6_01_database_refusal_is_a_typed_final_failure(db, monkeypatch):
    """A non-RevaError DB error must not escape handle_discovery (the worker would retry and re-fetch)."""
    import sqlalchemy.exc

    from reva.autonomy import jobs

    def refuse(*a, **k):
        raise sqlalchemy.exc.DataError("SELECT evidence.approve_eligibility(...)", {"secret": "x"},
                                       Exception("applicability.years must be {...}"))

    monkeypatch.setattr(jobs, "build_coverage_snapshot", refuse)
    out = jobs.handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, _job_ctx(db))
    assert out["status"] == "failed" and out["code"] == "database_refused" and out["retryable"] is False
    assert "secret" not in json.dumps(out)


@pytest.mark.db
async def test_r6_01_promotion_scope_years_use_the_eligibility_scope_format(db, io):
    from reva.autonomy.jobs import connector_promotion_scope, eligibility_years

    _bank, mapping, cp = await _generate(io)
    m = io.get_json(cp.manifest)
    assert eligibility_years(m) == {"in": [2023]}
    assert connector_promotion_scope(io, m, mapping)["years"] == {"in": [2023]}


# =========================================================================== R6-02
@pytest.mark.db
async def test_r6_02_gate_pass_cannot_be_spent_on_another_manifest(db, io):
    import copy

    from reva.autonomy import binding
    from reva.autonomy.jobs import load_promotion, promote_connector
    from reva.ontology.registry import load_registry

    p, v, s, manifest, _mapping = await _t1(io, db)
    d = _gate(db, io).decide(p.proposal_id, [v, s], promotion_policy().ref())
    assert d.outcome == "promote", d.reasons
    other = copy.deepcopy(manifest)  # life expectancy (unit YR) published as "population on 1 January"
    other["dataset"] = "demo_mlexpec"
    other["name"] = binding.connector_name("demo_mlexpec", ["FR"])
    other["request_parameters"]["semantics"]["unit_category"] = "YR"
    problems = binding.binding_problems(other, load_registry(), promotion_policy())
    assert any("no approved exact mapping" in x for x in problems)
    assert any("mapping_version" in x for x in problems)
    with db.owner.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM evidence.connector_version")).scalar_one()
    with pytest.raises(PolicyDenied, match="not the one the gate evaluated"):
        promote_connector(db.worker, io, _job_ctx(db), d, promotion_policy(), expected_manifest=other)
    # promote_connector has no manifest/mapping parameter: what it would promote is loaded from storage
    with db.worker.connect() as conn:
        row = conn.execute(text(
            """SELECT g.proposal_id, g.decision, g.evaluator, g.evidence_sha256, g.policy_version, p.proposal_type,
                      p.manifest_sha256 FROM ops.gate_result g JOIN ops.proposal p ON p.id = g.proposal_id
               WHERE g.id = :i"""), {"i": d.decision_id}).mappings().one()
    loaded = load_promotion(io, dict(row), d)
    assert loaded["manifest"] == manifest and loaded["manifest"]["dataset"] == "demo_pjan"
    assert loaded["mapping_manifest"]["mapping_key"] == "eurostat.demo_pjan.jan1_age_sex"
    # a pass of one proposal cannot be claimed for another proposal
    from reva.ports.autonomy import GateDecision

    q, *_ = await _t1(io, db, mutate_manifest=lambda m: m["cadence"].update(minimum_interval_seconds=8 * 86400))
    forged = GateDecision(d.decision_id, "promote", q.proposal_id, d.policy_version, (), (), d.reviewer_identity,
                          None)
    with pytest.raises(PolicyDenied):
        promote_connector(db.worker, io, _job_ctx(db), forged, promotion_policy())
    with db.owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM evidence.connector_version")).scalar_one() == before


# =========================================================================== R6-03
@pytest.mark.db
async def test_r6_03_gate_evaluates_the_persisted_proposal_not_the_caller_object(db, io):
    p, *_ = await _t1(io, db)
    fake = type(p)(p.proposal_id, p.author, 0, (), p.base_commit, io.put_json({"x": 1}, "models"),
                   ("report",), False, False, p.expected_benefit, p.policy_version)
    with pytest.raises(PolicyDenied, match="differs from the persisted proposal"):
        _gate(db, io).decide(fake, [], promotion_policy().ref())
    assert _gate_rows(db, p.proposal_id) == 0
    # by id, the stored T1 connector_promotion is evaluated (no reports -> not promoted)
    d = _gate(db, io).decide(p.proposal_id, [], promotion_policy().ref())
    assert d.outcome == "reject" and any("missing passing independent" in r for r in d.reasons)
    ev = io.get_json(d.signed_receipt)
    assert ev["proposal_type"] == "connector_promotion" and ev["tier"]["effective_tier"] == 1


@pytest.mark.db
def test_r6_03_model_registration_never_approved_by_a_gate_pass(db, io):
    from reva.autonomy.models import PostgresModelRegistry
    from reva.ports.autonomy import ModelDescriptor

    spec = io.put_json({"kind": "forecast", "input_ports": [], "output_ports": [], "assumptions": [],
                        "nonce": str(uuid.uuid4())}, "models")
    change = io.put_json({"schema": "reva-model-change/1", "model_spec_sha256": spec.sha256}, "models")
    # the proposer declares T0 'report' and touches nothing: the persisted type still makes it T2
    p = PlatformChangeProposal(uuid.uuid4(), "agent:r6", 0, (), base_commit(), change, ("report",), False, False,
                               _value(io), promotion_policy().version)
    ProposalStore(db.worker, io).persist(p, proposal_type="model_registration",
                                         scope={"model_spec_sha256": spec.sha256}, risk={})
    d = _gate(db, io).decide(p.proposal_id, [], promotion_policy().ref())
    assert d.outcome == "needs_review" and any("model_registration is at least T2" in r for r in d.reasons)
    # even a recorded pass (as any login holding ops.record_gate_result could write one) is refused
    ev = io.put_json({"forged": str(uuid.uuid4())}, "evaluation")
    from reva.ports.autonomy import GateDecision

    with (getattr(db, "gate", None) or db.worker).begin() as conn:
        io.register(conn, ev, "r6/1")
        gid = conn.execute(text("SELECT ops.record_gate_result(:p,'x',:v,'pass',:e,'gate-evaluator:x')"),
                           {"p": p.proposal_id, "e": ev.sha256, "v": promotion_policy().version}).scalar_one()
    with pytest.raises(PolicyDenied, match="named human review"):
        PostgresModelRegistry(db.worker, io).register(
            ModelDescriptor(None, f"forecast.r6_{uuid.uuid4().hex[:6]}", "1.0.0", spec, {}),
            GateDecision(gid, "promote", p.proposal_id, promotion_policy().version, (), (), "gate-evaluator:x", None))


# =========================================================================== R6-04
def _ths(doc):
    doc["dimension"]["unit"]["category"] = {"index": {"THS": 0}, "label": {"THS": "Thousand persons"}}
    vals = doc["value"]
    doc["value"] = ({k: round(float(v) / 1000, 3) for k, v in vals.items()} if isinstance(vals, dict)
                    else [None if x is None else round(float(x) / 1000, 3) for x in vals])


def _citizen(doc):
    ids = list(doc["id"])
    i = ids.index("geo")
    doc["id"] = [*ids[:i], "citizen", *ids[i:]]
    doc["size"] = [*doc["size"][:i], 1, *doc["size"][i:]]
    doc["dimension"]["citizen"] = {"label": "Citizenship",
                                   "category": {"index": {"NAT": 0}, "label": {"NAT": "Reporting country"}}}


@pytest.mark.db
@pytest.mark.parametrize("mutate,needle", [(_ths, "pinned unit code"), (_citizen, "pinned provider structure")])
async def test_r6_04_provider_structure_is_pinned_by_policy(db, io, mutate, needle):
    doc = _sample_doc()
    mutate(doc)
    p, v, s, _manifest, mapping = await _t1(io, db, overrides={SAMPLE_URL: json.dumps(doc).encode()})
    mm = io.get_json(mapping.mapping_manifest)
    assert mapping.relation != "exact" and mapping.review_required  # the mapper refuses exactness
    assert any(needle in x for x in mm.get("structure_problems") or []), mm.get("structure_problems")
    d = _gate(db, io).decide(p.proposal_id, [v, s], promotion_policy().ref())
    assert d.outcome != "promote"


@pytest.mark.db
async def test_r6_04_manifest_structure_must_equal_the_policy_pins(db, io):
    """Even a hand-made manifest claiming an exact mapping is out of scope with THS or an extra dimension."""
    from reva.autonomy import binding
    from reva.ontology.registry import load_registry

    _bank, _mapping, cp = await _generate(io)
    m = io.get_json(cp.manifest)
    reg, pol = load_registry(), promotion_policy()
    assert binding.binding_problems(m, reg, pol) == []
    ths = json.loads(json.dumps(m))
    ths["request_parameters"]["semantics"]["unit_category"] = "THS"
    ths["request_parameters"]["pinned_categories"]["unit"] = ["THS"]
    assert any("pinned unit code" in x for x in binding.binding_problems(ths, reg, pol))
    agg = json.loads(json.dumps(m))  # an overlapping age aggregate typed as a leaf
    agg["request_parameters"]["pinned_categories"]["age"].append("Y15-64")
    assert any("outside the pinned vocabulary" in x for x in binding.binding_problems(agg, reg, pol))


# =========================================================================== R6-05
@pytest.mark.db
async def test_r6_05_versions_are_canonical_and_content_derived(db, io):
    from reva.adapters.connectors.eurostat_template import validate_manifest

    _bank, _mapping, cp = await _generate(io)
    m = io.get_json(cp.manifest)
    for bad in ("1.0.0\n", "01.0.0", "1.0.0-rc1", " 1.0.0"):
        with pytest.raises(ValidationProblem):
            validate_manifest({**m, "version": bad})
    p, v, s, *_ = await _t1(io, db, mutate_manifest=lambda x: x.update(version="1.0.7"))
    d = _gate(db, io).decide(p.proposal_id, [v, s], promotion_policy().ref())
    assert d.outcome == "reject" and any("content-derived version" in r for r in d.reasons)


# =========================================================================== R6-06
@pytest.mark.db
async def test_r6_06_forged_reports_do_not_promote(db, io):
    from reva.adapters.connectors.eurostat_template import template_digest
    from reva.autonomy.quality import contract_for
    from reva.ontology.registry import load_registry

    p, v_real, _s, manifest, _ = await _t1(io, db, mutate_manifest=lambda m: m["request_parameters"].update(
        time=[2099]))
    assert v_real.outcome == "blocked"
    msha = io.get_json(p.patch_or_manifest)["manifest_ref"]["sha256"]
    contract = io.put_json(contract_for(manifest, load_registry()), "models").sha256
    ev_v = io.put_json({"evaluator": "connector-validator:eurostat_jsonstat@1", "outcome": "pass", "findings": [],
                        "manifest": msha, "data_contract": contract, "template_digest": template_digest()},
                       "evaluation")
    ev_s = io.put_json({"evaluator": "sandbox:trusted_template@1", "outcome": "pass", "findings": [],
                        "proposal_id": str(p.proposal_id), "manifest": msha}, "evaluation")
    fv = report("pass", [], ev_v, "connector-validator:eurostat_jsonstat@1")
    fs = report("pass", [], ev_s, "sandbox:trusted_template@1")
    d = _gate(db, io).decide(p.proposal_id, [fv, fs], promotion_policy().ref())
    assert d.outcome == "reject" and any("cannot recompute" in r for r in d.reasons), d.reasons
    # a forged report pointing at a real bank is recomputed by the gate, which blocks on the real data
    ev_v2 = io.put_json({**io.get_json(ev_v), "fixtures_in": bank_from_dir(io, CONNECTOR).sha256}, "evaluation")
    d2 = _gate(db, io).decide(p.proposal_id, [report("pass", [], ev_v2, "connector-validator:eurostat_jsonstat@1"),
                                              fs], promotion_policy().ref())
    assert d2.outcome == "reject" and any("gate-recomputed connector-validator" in r for r in d2.reasons)


@pytest.mark.db
async def test_r6_06_genuine_t1_records_the_recomputation_and_digests(db, io):
    p, v, s, *_ = await _t1(io, db)
    d = _gate(db, io).decide(p.proposal_id, [v, s], promotion_policy().ref())
    assert d.outcome == "promote", d.reasons
    scope = io.get_json(d.signed_receipt)["tier"]["t1_scope"]
    assert {k: x["outcome"] for k, x in scope["recomputed"].items()} == {"connector-validator": "pass",
                                                                         "sandbox": "pass"}
    assert scope["manifest_sha256"] and scope["mapping_manifest_sha256"] and scope["proposal_sha256"]
    assert scope["years"] == {"in": [2023]} and scope["subjects"] == ["FRA"]


# =========================================================================== R6-07
def test_r6_07_template_digest_covers_the_import_closure(monkeypatch, tmp_path):
    from reva.adapters.connectors import eurostat_template as et
    from reva.autonomy import binding

    names = {k for k, _ in et.template_closure()}
    assert {"reva.adapters.connectors.eurostat_template", "reva.adapters.connectors.base",
            "reva.adapters.http.safe_client", "reva.kernel.support", "reva.kernel.time", "reva.kernel.uncertainty",
            "reva.ports.connectors", "file:connector.schema.json"} <= names
    before = et.template_digest()
    assert before == binding.approved_template_digest(promotion_policy(), et.TEMPLATE_ID)
    # an edit of base.json_decimal (a helper the template imports) changes the digest
    edited = tmp_path / "base.py"
    edited.write_text((REPO / "src/reva/adapters/connectors/base.py").read_text() + "\n# reviewed? no\n")
    orig = et._module_file
    monkeypatch.setattr(et, "_module_file",
                        lambda n: str(edited) if n == "reva.adapters.connectors.base" else orig(n))
    et.template_closure.cache_clear()
    try:
        assert et.template_digest() != before
    finally:
        monkeypatch.undo()
        et.template_closure.cache_clear()
    assert et.template_digest() == before
    # a runtime re-binding of an imported helper is visible in the digest too
    monkeypatch.setattr(et, "json_decimal", lambda body: None)
    assert et.template_digest() != before


# =========================================================================== R6-09
@pytest.mark.db
async def test_r6_09_validator_live_sample_is_charged_and_capped(db, fio):
    from reva.autonomy.budget import metered_discovery, reserve_discovery, settle_discovery
    from reva.autonomy.policy import budgets_policy

    class FakeLive:
        origin = "live"

        def __init__(self, inner):
            self.inner, self.gets = inner, 0

        async def get(self, url, **kw):
            self.gets += 1
            return await self.inner.get(url, **kw)

        async def aclose(self):
            return None

    src = f"test.r6.{uuid.uuid4().hex[:8]}"
    base = budgets_policy()
    pol = type(base)(base.path, base.sha256, {**base.content, "per_source_daily_requests": {
        **base.content["per_source_daily_requests"], src: 2}})
    r = reserve_discovery(db.worker, src, policy=pol)
    assert r.max_requests == 2
    bank = bank_from_dir(fio, CONNECTOR)
    fake = FakeLive(BankHttp(fio, bank))
    meter = metered_discovery(CapturingHttp(fake, "Eurostat"), db.worker, r)
    ctx = make_context(fio, "evidence", datetime.now(UTC), {"t": 1})
    gap = KnowledgeGap(uuid.uuid4(), "R-POP-JAN-AGESEX", CONCEPT, ("FRA",), {"years": [2023]}, "unsupported",
                       1.0, "test", ctx)
    budget = default_budget(max_requests=r.max_requests, max_bytes=r.max_bytes)
    cands = await EurostatCatalogueDiscoverer(fio, meter).discover(gap, budget)  # catalogue + inspection
    mapping = RegistryOntologyMapper(fio).assess(cands[0], ctx)[0]
    cp = DeclarativeConnectorGenerator(fio).generate(cands[0], mapping, ("eurostat_jsonstat",))
    empty = fio.put_json({"schema": "reva-fixture-bank/1", "connector": CONNECTOR, "entries": []}, "raw")
    v = await JsonStatConnectorValidator(fio, live_client_factory=lambda hosts: meter).validate(cp, empty, budget)
    # the third GET (the validator's live sample) is refused by the shared meter, never made
    assert fake.gets == 2 and meter.requests == 2 and v.outcome == "fail"
    assert any(f.code == "budget_exhausted" for f in v.findings), [f.code for f in v.findings]
    settle_discovery(db.worker, r, requests=meter.requests, nbytes=meter.bytes)
    with db.owner.connect() as conn:
        charged = conn.execute(text("SELECT consumed FROM ops.budget_ledger WHERE operation_id = :o"),
                               {"o": r.requests_operation_id}).scalar_one()
    assert int(charged) == fake.gets


# =========================================================================== R6-10
def test_r6_10_worker_entrypoint_keeps_the_kill_file():
    sys.path.insert(0, str(REPO / "scripts"))
    import _runtime_env as r

    env = {"PATH": "/usr/bin", "REVA_ENV": "local", "REVA_AUTONOMY_KILL_FILE": "/app/var/autonomy.kill",
           "REVA_CPU_BUDGET_THREADS": "1"}
    child, removed = r.build_child_env(env, "worker")
    assert child["REVA_AUTONOMY_KILL_FILE"] == "/app/var/autonomy.kill"
    assert "REVA_AUTONOMY_KILL_FILE" not in removed


# =========================================================================== R6-11
async def test_r6_11_identifiers_are_anchored_and_names_canonical(fio):
    from reva.adapters.connectors.eurostat_template import validate_manifest
    from reva.autonomy.binding import connector_name

    _bank, _mapping, cp = await _generate(fio)
    m = fio.get_json(cp.manifest)
    for mutate in (lambda x: x.update(dataset="demo_pjan\n"), lambda x: x.update(name=x["name"] + "\n"),
                   lambda x: x["request_parameters"]["filters"].update(geo=["FR\n"])):
        bad = json.loads(json.dumps(m))
        mutate(bad)
        with pytest.raises(ValidationProblem):
            validate_manifest(bad)
    assert connector_name("demo_pjan", ["FR-DE"]) != connector_name("demo_pjan", ["FR", "DE"])
    assert connector_name("demo_pjan", ["FR"]) == "eurostat.demo_pjan.fr"


# =========================================================================== R6-12
@pytest.mark.db
async def test_r6_12_no_free_manifest_fields(db, io):
    def tamper(m):
        rp = m["request_parameters"]
        rp["licence"] = "CC0 1.0 public domain - no attribution required"
        m["rights_ref"] = "registry/sources/worldbank.yaml#rights"
        rp["special_categories"] = {"age": {"Y_OPEN": "unknown", "UNK": "open_ended_top"}, "sex": {}}
        m["mapping_version"] = "eurostat.demo_pjan.to_wdi_midyear@1.0.0"
        rp["observed_status_labels"] = {"p": "final (not provisional)"}
        rp["dataset_label"] = "Population on 1 July (midyear) - WDI compatible"

    p, v, s, *_ = await _t1(io, db, mutate_manifest=tamper)
    d = _gate(db, io).decide(p.proposal_id, [v, s], promotion_policy().ref())
    assert d.outcome == "reject"
    joined = " | ".join(d.reasons)
    for needle in ("licence differs", "rights_ref", "special_categories", "mapping_version",
                   "are not template fields"):
        assert needle in joined, needle


# =========================================================================== R6-13
async def test_r6_13_sandbox_records_filesystem_reach_and_nproc(fio):
    bank, mapping, cp = await _generate(fio)
    manifest = fio.get_json(cp.manifest)
    p = connector_change_proposal(fio, cp, manifest, mapping, _value(fio), promotion_policy())
    rep = await TrustedTemplateSandbox(fio, fixtures=bank).run(p, trusted_template_policy(default_budget()))
    assert rep.outcome == "pass", [(f.code, f.message) for f in rep.findings]
    child = fio.get_json(rep.evidence_manifest)["child"]
    assert child["rlimits"]["nproc"] == [1, 1]
    fs = child["filesystem"]
    assert fs["mount_isolation"].startswith("none") and "outside_private_dir_writable" in fs
    assert {f.code: f.severity for f in rep.findings}.get("filesystem_shared") == "warning"


# =========================================================================== R6-15
@pytest.mark.db
def test_r6_15_fixture_mode_refused_in_production(db):
    from reva.autonomy.jobs import handle_discovery

    prod = db.settings.model_copy(update={"reva_env": "production"})
    with db.owner.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM ops.proposal")).scalar_one()
    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, _job_ctx(db, prod))
    assert out["status"] == "rejected" and out["code"] == "fixture_mode"
    with db.owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ops.proposal")).scalar_one() == before


# =========================================================================== R6-16
@pytest.mark.parametrize("path,tier", [("registry/concepts/demography.yaml", 3),
                                       ("registry/entities/countries.yaml", 3),
                                       ("registry/requirements/catalogue.yaml", 3),
                                       ("src/reva/ontology/registry.py", 3),
                                       ("registry/mappings/eurostat.demo_pjan.yaml", 2)])
def test_r6_16_registry_ground_truth_is_protected(path, tier):
    from reva.autonomy.policy import derive_tier

    assert derive_tier((path,), ("propose",), False, False)[0] == tier


# =========================================================================== P6 (redaction)
async def test_p6_external_runner_response_is_redacted(fio, monkeypatch):
    key = "r6-canary-" + uuid.uuid4().hex
    monkeypatch.setenv("REVA_COMTRADE_API_KEY", key)

    class Runner:
        async def run(self, request):
            return {"outcome": "fail", "findings": [{"code": "x", "severity": "blocker",
                                                     "message": f"leak {key}"}], "log": f"Bearer {key}"}

    p = PlatformChangeProposal(uuid.uuid4(), "agent:r6", 2, ("x.py",), base_commit(),
                               fio.put_json({"patch": 1}, "models"), ("execute_code",), True, False, _value(fio),
                               promotion_policy().version)
    from dataclasses import replace

    pol = replace(trusted_template_policy(default_budget()), isolation="external_disposable_vm")
    rep = await TrustedTemplateSandbox(fio, external_runner=Runner()).run(p, pol)
    assert rep.outcome == "fail" and key not in fio.get_bytes(rep.evidence_manifest).decode()
