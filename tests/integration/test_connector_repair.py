"""Connector self-healing (architecture §10.3): drift classification, repair proposals, replay, pause.

Offline: drifted payloads are synthetic derivations of the captured fixture bank
(tests/fixtures/repair, re-derived and byte-checked here); request failures are
served by a stub that raises exactly like SafeHttpClient. Gate results are only
ever written by PolicyPromotionGate.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text

from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.http.safe_client import request_fingerprint
from reva.adapters.postgres import repositories as repo
from reva.autonomy import repair as R
from reva.autonomy.common import ArtifactIO, AutonomyBlocked, stable_id
from reva.autonomy.gate import PolicyPromotionGate
from reva.autonomy.policy import promotion_policy
from reva.cli.seed import register_connector_version
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.errors import SourceResponseError
from reva.operations.jobs import dirty_belief_nodes

pytestmark = pytest.mark.db

REPAIR = Path(__file__).parents[1] / "fixtures" / "repair"
SOURCE = Path(__file__).parents[1] / "fixtures" / "source"
CASES = {c["case"]: c for c in json.loads((REPAIR / "manifest.json").read_text())}
POP = "demography.population.wdi_midyear_total"


def load_derive() -> Any:
    spec = importlib.util.spec_from_file_location("repair_derive", REPAIR / "derive.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _connector(name: str) -> Any:
    return WorldBankConnector() if name == "worldbank.wdi.indicators" else ComtradeConnector()


def _body(case: str) -> bytes:
    return (REPAIR / CASES[case]["file"]).read_bytes()


@pytest.fixture()
def io(db) -> ArtifactIO:
    return ArtifactIO(db.store)


@pytest.fixture()
def advisor(db) -> R.ConnectorRepairAdvisor:
    return R.ConnectorRepairAdvisor(db.worker, db.store)


@pytest.fixture()
def gate(db, io) -> PolicyPromotionGate:
    return PolicyPromotionGate(db.worker, io)


@pytest.fixture(scope="module", autouse=True)
def _clear_pause_markers(db) -> Iterator[None]:
    yield
    with db.owner.begin() as c:  # leave the shared session database without repair markers
        c.execute(text("DELETE FROM ops.dirty_node WHERE node_key LIKE :p"), {"p": R.PAUSE_PREFIX + "%"})


def _gate_rows(db, proposal_id) -> list[dict[str, Any]]:
    with db.owner.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT decision, evaluator, policy_version FROM ops.gate_result WHERE proposal_id = :p"),
            {"p": proposal_id}).mappings()]


def _proposal_row(db, proposal_id) -> dict[str, Any]:
    with db.owner.connect() as c:
        return dict(c.execute(text(
            "SELECT proposal_type, permission_tier, author, manifest_sha256, summary FROM ops.proposal WHERE id = :p"),
            {"p": proposal_id}).mappings().one())


def _registered(db, sha: str) -> bool:
    with db.owner.connect() as c:
        return c.execute(text("SELECT 1 FROM meta.artifact WHERE sha256 = :s"), {"s": sha}).scalar() == 1


def _paused(db, concept: str) -> bool:
    with db.owner.connect() as c:
        return R.is_publication_paused(c, concept)


def _isolated(base: type, version: str) -> Any:
    """The reviewed connector under a test-only version, so health/acquisition history is isolated."""

    class Isolated(base):  # type: ignore[misc,valid-type]
        def describe(self):  # type: ignore[no-untyped-def]
            return super().describe().model_copy(update={"version": version})

    return Isolated()


def _register(db, connector: Any) -> None:
    with db.worker.begin() as c:
        act = repo.simple_activity("test.connector_registration", "review", "human:test-maintainer", {})
        repo.record_activity(c, act)
        register_connector_version(c, db.store, connector.describe(), act.id)


class StubHttp:
    """Captured fixtures, drifted bodies, and provider failures raised as SafeHttpClient raises them."""

    origin = "test_fixture"

    def __init__(self, connector: str, *, overrides: dict[str, bytes] | None = None,
                 status_for: Any = None) -> None:
        self.base = FixtureHttp(connector, overrides={request_fingerprint("GET", u): b
                                                      for u, b in (overrides or {}).items()})
        self.status_for = status_for or (lambda url: None)

    async def get(self, url: str, **kw: Any) -> Any:
        st = self.status_for(url)
        if st is not None:
            raise SourceResponseError(f"HTTP {st} from {urlsplit(url).hostname}", retryable=False, status=st,
                                      attempts=1)
        return await self.base.get(url, **kw)

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- fixtures provenance
def test_drifted_fixtures_derive_from_the_captured_bank() -> None:
    derive = load_derive()
    assert {c["case"] for c in derive.manifest()} == set(CASES)
    for entry in derive.manifest():
        assert entry == CASES[entry["case"]], f"manifest out of date for {entry['case']}"
        body = derive.derive(next(c for c in derive.CASES if c["case"] == entry["case"]))
        if entry["file"] is None:
            assert body is None and entry["status"] in (401, 404)
            continue
        assert (REPAIR / entry["file"]).read_bytes() == body
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
        src = SOURCE / entry["source_fixture"]["file"]
        assert hashlib.sha256(src.read_bytes()).hexdigest() == entry["source_fixture"]["sha256"]
        assert body != src.read_bytes() and entry["synthetic"] is True


def test_semantic_fingerprint_ignores_order_and_values_but_not_types_or_units() -> None:
    sem = R.SEMANTICS["worldbank.wdi.indicators"]
    base = [{"page": 1, "pages": 1}, [{"countryiso3code": "FRA", "date": "2020", "value": 1, "unit": ""}]]
    reordered = [{"pages": 1, "page": 1}, [{"unit": "", "value": 1, "date": "2020", "countryiso3code": "FRA"}]]
    other_values = [{"page": 2, "pages": 9}, [{"countryiso3code": "USA", "date": "2021", "value": 7, "unit": ""}]]
    as_text = [{"page": 1, "pages": 1}, [{"countryiso3code": "FRA", "date": "2020", "value": "1", "unit": ""}]]
    new_unit = [{"page": 1, "pages": 1}, [{"countryiso3code": "FRA", "date": "2020", "value": 1, "unit": "%"}]]
    fp = R.semantic_schema_fingerprint
    assert fp(base, sem) == fp(reordered, sem) == fp(other_values, sem)
    assert fp(base, sem) != fp(as_text, sem)  # type preserved
    assert fp(base, sem) != fp(new_unit, sem)  # unit marker preserved
    assert R.schema_profile(base)["$[1][].value"] == ["number"]


# --------------------------------------------------------------------------- payload drift
PAYLOAD_EXPECTATIONS = {
    # case: (classification, tier, independent reports, gate outcome)
    "wb_added_field": (R.ADDITIVE, 1, {R.REVALIDATION_EVALUATOR: "pass"}, "reject"),
    "ct_added_field": (R.ADDITIVE, 1, {R.REVALIDATION_EVALUATOR: "pass"}, "reject"),
    "wb_renamed_field": (R.RENAME, 2, {R.REPLAY_EVALUATOR: "pass"}, "needs_review"),
    "ct_renamed_field": (R.RENAME, 2, {R.REPLAY_EVALUATOR: "pass"}, "needs_review"),
    "wb_unit_text_and_scale": (R.UNIT, 2, {R.REPLAY_EVALUATOR: "pass"}, "needs_review"),
    "ct_silent_scale": (R.SCALE, 2, {}, "needs_review"),
    "ct_value_type_change": (R.TYPE_CHANGE, 2, {R.REPLAY_EVALUATOR: "pass"}, "needs_review"),
    "ct_classification_change": (R.CLASSIFICATION, 2, {}, "needs_review"),
}


@pytest.mark.parametrize("case", sorted(PAYLOAD_EXPECTATIONS))
def test_payload_drift_is_classified_proposed_and_gated(db, io, advisor, gate, case) -> None:
    cls, tier, expected_reports, outcome = PAYLOAD_EXPECTATIONS[case]
    c = CASES[case]
    assert c["expected"] == cls
    conn_ = _connector(c["connector"])
    a = advisor.assess(conn_, advisor.case_from_response(conn_, c["url"], _body(case)))
    assert (a.classification, a.tier) == (cls, tier), a.changes
    assert a.semantic_diff is not None
    # A silent rescaling keeps types/units/dimensions: only the vintage value comparison sees it.
    assert (a.accepted_fingerprint == a.new_fingerprint) == (cls == R.SCALE)
    diff = io.get_json(a.semantic_diff)
    assert diff["schema"] == R.DIFF_SCHEMA and diff["classification"] == cls
    assert diff["fixture_bank"] == advisor.bank(c["connector"]).sha256

    rp = advisor.propose(conn_, a)
    assert rp is not None
    pid = rp.proposal.proposal_id
    row = _proposal_row(db, pid)
    assert row["permission_tier"] == tier and row["author"] == R.AUTHOR
    assert row["proposal_type"] == f"connector_repair:{a.action}"
    assert rp.proposal_record is not None and row["manifest_sha256"] == rp.proposal_record.sha256
    record = io.get_json(rp.proposal_record)
    assert record["proposal"]["patch_or_manifest"]["sha256"] == rp.manifest.sha256
    assert record["scope"]["concepts"] == list(a.affected_concepts)
    assert _registered(db, a.semantic_diff.sha256) and _registered(db, rp.proposal_record.sha256)
    assert {r.evaluator_version: r.outcome for r in rp.reports} == expected_reports
    manifest = io.get_json(rp.manifest)
    assert manifest["schema"] == R.REPAIR_SCHEMA and manifest["semantic_diff"] == a.semantic_diff.sha256
    if tier == 1:
        assert manifest["mapping"] == {"version": conn_.describe().parser_version, "change": "none",
                                       "accept_schema_fingerprint": a.new_fingerprint}
    else:
        assert manifest["mapping"]["requires_new_version"] is True
        assert (manifest["proposed_mapping_version"] is not None) == bool(expected_reports)
        assert "ontology_mapping" in rp.proposal.requested_capabilities
    # Publication of the affected concepts is paused; the advisor wrote no gate result.
    assert rp.pause is not None and set(rp.pause.concepts) == set(a.affected_concepts)
    assert all(_paused(db, x) for x in a.affected_concepts)
    assert _gate_rows(db, pid) == []

    d = gate.decide(rp.proposal, list(rp.reports), promotion_policy().ref())
    assert d.outcome == outcome, d.reasons
    rows = _gate_rows(db, pid)
    assert len(rows) == 1 and rows[0]["evaluator"] == "gate-evaluator:promotion"
    if tier == 1:
        # T1-eligible, but the signed promotion policy only scopes T1 auto-promotion to
        # declarative template manifests: the gate (not the advisor) keeps it out of scope.
        assert "T1 out of signed policy scope" in d.reasons
        assert any("declarative connector manifests" in r for r in d.reasons)
    else:
        assert any("named review" in r for r in d.reasons)


def test_unit_change_suggests_conditional_mapping_that_leaves_history_intact(db, io, advisor) -> None:
    c = CASES["wb_unit_text_and_scale"]
    wb = WorldBankConnector()
    a = advisor.assess(wb, advisor.case_from_response(wb, c["url"], _body("wb_unit_text_and_scale")))
    scale = next(x for x in a.changes if x["kind"] == R.SCALE)
    assert scale["rescaled_concepts"] == {f"{POP}@1.0.0": "1E-3"}
    m = a.suggested_mapping
    assert m is not None and m.scales == (R.ScaleRule("$[1][].value", "1000", "$[1][].unit", "thousands"),)
    rp = advisor.propose(wb, a)
    assert rp is not None
    ev = io.get_json(rp.reports[0].evidence_manifest)
    assert ev["vintages_replayed"] >= 5 and all(v.get("semantic_changes", 0) == 0 for v in ev["vintages"])
    # the mapped drifted payload reproduces the accepted vintage exactly
    assert ev["new_payload"]["validation_blockers"] == []
    assert ev["new_payload"]["vs_same_request_vintage"]["identical"] == 100


# --------------------------------------------------------------------------- history replay
def test_replay_blocks_a_mapping_that_rescales_history(db, io, advisor, gate) -> None:
    c = CASES["wb_unit_text_and_scale"]
    wb = WorldBankConnector()
    a = advisor.assess(wb, advisor.case_from_response(wb, c["url"], _body("wb_unit_text_and_scale")))
    unconditional = R.MappingRevision(wb.describe().parser_version,
                                      scales=(R.ScaleRule("$[1][].value", "1000"),), note="test: forgets the unit")
    rp = advisor.propose(wb, a, mapping=unconditional)
    assert rp is not None
    (rep,) = rp.reports
    assert rep.evaluator_version == R.REPLAY_EVALUATOR and rep.outcome == "blocked"
    assert any(f.code == "historical_reinterpretation" for f in rep.findings)
    ev = io.get_json(rep.evidence_manifest)
    pop_vintage = next(v for v in ev["vintages"] if "SP.POP.TOTL" in v.get("url", "") and "country/" in v["url"])
    assert pop_vintage["semantic_changes"] == 100
    d = gate.decide(rp.proposal, list(rp.reports), promotion_policy().ref())
    assert d.outcome == "blocked" and any("historical semantic outputs differ" in r for r in d.reasons)
    assert _gate_rows(db, rp.proposal.proposal_id)[0]["decision"] == "blocked"


class RoundingWorldBank(WorldBankConnector):
    """A 'repaired' parser that also rounds every population to thousands (reinterprets history)."""

    def _observation(self, indicator, r):  # type: ignore[no-untyped-def]
        o = super()._observation(indicator, r)
        if o.value.get("kind") == "decimal":
            v = round(float(o.value["value"]) / 1000) * 1000
            o = o.model_copy(update={"value": {**o.value, "value": str(int(v))}})
        return o


def test_replay_blocks_a_code_parser_that_changes_old_values(db, io, advisor, gate) -> None:
    c = CASES["wb_renamed_field"]
    wb = WorldBankConnector()
    a = advisor.assess(wb, advisor.case_from_response(wb, c["url"], _body("wb_renamed_field")))
    rp = advisor.propose(wb, a, parser=RoundingWorldBank())
    assert rp is not None
    assert "execute_code" in rp.proposal.requested_capabilities and rp.proposal.permission_tier == 2
    (rep,) = rp.reports
    assert rep.outcome == "blocked"
    manifest = io.get_json(rp.manifest)
    assert manifest["proposed_parser"]["kind"] == "code"
    assert manifest["proposed_parser"]["class"].endswith("RoundingWorldBank")
    d = gate.decide(rp.proposal, list(rp.reports), promotion_policy().ref())
    assert d.outcome == "blocked"


def test_replay_accepts_identity_and_rejects_unparseable_fixes(io, advisor) -> None:
    wb = WorldBankConnector()
    bank = advisor.bank("worldbank.wdi.indicators")
    checker = R.FixtureReplayChecker(io)
    ok = checker.check(wb, wb, bank, proposal_key="identity")
    assert ok.outcome == "pass" and ok.resource_usage["vintages"] == 5
    c = CASES["wb_renamed_field"]
    case = advisor.case_from_response(wb, c["url"], _body("wb_renamed_field"))
    new_raw = advisor._new_raw(wb, advisor.assess(wb, case))
    still_broken = checker.check(wb, wb, bank, proposal_key="no-fix", new_raw=new_raw)
    assert still_broken.outcome == "fail"  # history intact, but the drifted payload still fails validation
    assert any(f.code.startswith("new_payload_") for f in still_broken.findings)


# --------------------------------------------------------------------------- through the real ingest path
async def test_quarantined_acquisition_is_found_and_repaired(db, io, gate) -> None:
    wb = _isolated(WorldBankConnector, "1.0.0+repair-quarantine")
    _register(db, wb)
    c = CASES["wb_renamed_field"]
    rep = await run_connector(wb, StubHttp(c["connector"], overrides={c["url"]: _body("wb_renamed_field")}),
                              db.store, db.worker, persist_cursor=False)
    by_item = {o.item_key: o.status for o in rep.outcomes}
    assert by_item["series:SP.POP.TOTL:p1"] == "quarantined"
    adv = R.ConnectorRepairAdvisor(db.worker, db.store)
    case = adv.find_case(wb, version="1.0.0+repair-quarantine", origin="test_fixture")
    assert case is not None and case.quarantined and case.acquisition_id and case.raw_ref.namespace == "quarantine"
    assert case.item_key == "series:SP.POP.TOTL:p1"
    assert {f["code"] for f in case.quarantine_findings} >= {"unexpected_subject"}
    a = adv.assess(wb, case)
    assert a.classification == R.RENAME and a.raw_ref == case.raw_ref
    rp = adv.propose(wb, a)
    assert rp is not None and rp.reports[0].outcome == "pass"
    ev = io.get_json(rp.reports[0].evidence_manifest)
    assert any(v["source"] == "accepted_acquisition" for v in ev["vintages"])  # DB history replayed too
    assert io.get_json(rp.manifest)["acquisition_id"] == case.acquisition_id
    assert gate.decide(rp.proposal, list(rp.reports), promotion_policy().ref()).outcome == "needs_review"
    # the pause marker is outside the belief namespace: it neither triggers nor is cleared by a release
    with db.owner.connect() as conn:
        pauses = {p["concept"]: p for p in R.active_publication_pauses(conn)}
        assert pauses[POP]["proposal_id"] is not None
        before = dirty_belief_nodes(conn)
        ev_row = conn.execute(text(
            "SELECT payload FROM ops.outbox WHERE event_type = 'connector.publication_paused' "
            "AND payload->>'proposal_id' = :p"), {"p": str(rp.proposal.proposal_id)}).scalar_one()
    assert ev_row["concepts"] == [POP] and "stale" in ev_row["visibility"]
    with db.owner.connect() as conn:
        assert dirty_belief_nodes(conn) == before


async def test_persistent_404_triggers_documentation_discovery_not_url_guessing(db, io, gate) -> None:
    wb = _isolated(WorldBankConnector, "1.0.0+repair-404")
    _register(db, wb)
    url = CASES["wb_endpoint_404"]["url"]
    http = StubHttp("worldbank.wdi.indicators", status_for=lambda u: 404 if u == url else None)
    adv = R.ConnectorRepairAdvisor(db.worker, db.store)
    for run in range(1, 4):
        await run_connector(wb, http, db.store, db.worker, persist_cursor=False)
        case = adv.find_case(wb, version="1.0.0+repair-404", origin="test_fixture")
        assert case is not None and case.failures
        a = adv.assess(wb, case)
        if run < 3:
            assert a.classification == R.TRANSIENT and adv.propose(wb, a) is None
    assert a.classification == R.ENDPOINT and a.tier == 0
    assert a.item_key == "series:SP.POP.TOTL:p1" and a.affected_concepts == (POP,)
    assert a.evidence["consecutive_gone_runs"]["series:SP.POP.TOTL:p1"] == 3
    rp = adv.propose(wb, a)
    assert rp is not None and rp.reports == ()
    task = io.get_json(rp.manifest)["task"]
    assert task["kind"] == "documentation_discovery"
    assert task["allowed_documentation"] == ["https://api.worldbank.org/v2/",
                                             "https://datahelpdesk.worldbank.org/knowledgebase/articles/889392"]
    assert "random_url_guessing" in task["forbidden"]
    assert rp.proposal.changed_paths == () and set(rp.proposal.requested_capabilities) <= {"discover", "report"}
    d = gate.decide(rp.proposal, [], promotion_policy().ref())
    assert d.outcome == "promote" and any(r.startswith("T0") for r in d.reasons)  # the read-only task may run
    assert _paused(db, POP)


async def test_auth_change_requires_credential_provisioning(db, io, gate) -> None:
    ct = _isolated(ComtradeConnector, "1.1.0+repair-401")
    _register(db, ct)
    http = StubHttp("un.comtrade.public_exact", status_for=lambda u: 401)
    adv = R.ConnectorRepairAdvisor(db.worker, db.store)
    for _ in range(3):
        rep = await run_connector(ct, http, db.store, db.worker, persist_cursor=False)
        assert all(o.status == "failed" for o in rep.outcomes)
    case = adv.find_case(ct, version="1.1.0+repair-401", origin="test_fixture")
    assert case is not None
    a = adv.assess(ct, case)
    assert (a.classification, a.tier, a.item_key) == (R.AUTH, 3, None)  # connector-wide
    assert "trade.hs2709.import_value.world_total" in a.affected_concepts
    rp = adv.propose(ct, a)
    assert rp is not None and rp.proposal.requested_capabilities == ("secret_access",)
    task = io.get_json(rp.manifest)["task"]
    assert task["kind"] == "credential_provisioning" and task["statuses"] == [401]
    assert task["bypass"].startswith("forbidden")
    d = gate.decide(rp.proposal, [], promotion_policy().ref())
    assert d.outcome == "needs_review" and any("maintainer approval" in r for r in d.reasons)
    assert all(_paused(db, x) for x in a.affected_concepts)


# --------------------------------------------------------------------------- pause / resume, handler
def test_only_gate_promotion_or_named_human_resumes_publication(db) -> None:
    probe = "test.repair.pause_probe"
    with db.worker.begin() as conn:
        pause = R.pause_belief_publication(conn, [probe], connector="worldbank.wdi.indicators",
                                           classification=R.RENAME, proposal_id=stable_id("probe"),
                                           origin="test_fixture", item_key=None, acquisition_id=None,
                                           actor=R.AUTHOR)
    assert pause.node_keys == (R.PAUSE_PREFIX + probe,) and _paused(db, probe)
    with pytest.raises(AutonomyBlocked):
        R.resume_belief_publication(db.worker, probe, actor=R.AUTHOR, reason="agent may not resume")
    with pytest.raises(AutonomyBlocked):
        R.resume_belief_publication(db.worker, probe, actor="gate-evaluator:promotion", reason="no decision")
    assert R.resume_belief_publication(db.worker, probe, actor="human:maintainer", reason="reviewed mapping v2")
    assert not _paused(db, probe)


def test_job_handler_refuses_when_blocked_or_unknown(db) -> None:
    ctx = SimpleNamespace(settings=db.settings.model_copy(update={"reva_autonomy_kill_switch": True}),
                          worker_engine=db.worker, store=db.store)
    assert R.handle_connector_repair({"connector": "worldbank.wdi.indicators"}, ctx)["status"] == "blocked"
    ctx.settings = db.settings
    assert R.handle_connector_repair({"connector": "https://example.org/x"}, ctx)["status"] == "rejected"
    assert R.handle_connector_repair({"connector": "crossref.works.doi", "origin": "test_fixture"},
                                     ctx)["status"] in ("unsupported", "rejected")


def test_assessment_requires_matching_connector(advisor) -> None:
    wb = WorldBankConnector()
    case = R.DriftCase(connector="un.comtrade.public_exact", observed_at=datetime.now(UTC), origin="live",
                       item_key="x", body=b"{}")
    with pytest.raises(Exception, match="different connector"):
        advisor.assess(wb, case)
