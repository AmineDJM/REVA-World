"""Eurostat demo_pjan discovery → manifest → validation → gate → ingestion (offline, captured fixtures).

Captured live on 2026-10-14 with RecordingHttp through SafeHttpClient:
  * catalogue TOC  https://ec.europa.eu/eurostat/api/dissemination/catalogue/toc/txt?lang=en
    (1,968,970 bytes, single bounded request, explicit max_bytes=4,000,000)
  * sample         .../statistics/1.0/data/demo_pjan?format=JSON&lang=EN&geo=FR&time=2023
    (JSON-stat 2.0, id [freq, unit, age, sex, geo, time], size [1,1,103,3,1,1], 309 values, all flagged 'p')
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from reva.adapters.connectors.eurostat_template import EurostatJsonStatConnector
from reva.autonomy.common import ArtifactIO, default_budget, make_context
from reva.autonomy.discovery import EUROSTAT_TOC_URL, EurostatCatalogueDiscoverer
from reva.autonomy.fixtures import BankHttp, bank_from_dir
from reva.autonomy.generator import DeclarativeConnectorGenerator
from reva.autonomy.mapping import RegistryOntologyMapper
from reva.autonomy.validator import JsonStatConnectorValidator
from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp, run_connector
from reva.ports.autonomy import KnowledgeGap

pytestmark = pytest.mark.db

CONNECTOR = "eurostat.demo_pjan.fr"
SAMPLE_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/demo_pjan"
              "?format=JSON&lang=EN&geo=FR&time=2023")


def _sample() -> dict:
    d = FIXTURE_ROOT / CONNECTOR
    entry = next(e for e in json.loads((d / "manifest.json").read_text()) if e["url"] == SAMPLE_URL)
    return json.loads((d / entry["file"]).read_bytes())


def _gap(io, **over) -> KnowledgeGap:
    ctx = make_context(io, "evidence", datetime.now(UTC), {"schema": "test"})
    base = dict(gap_id=uuid.uuid4(), requirement_id="R-POP-JAN-AGESEX",
                concept_version="demography.population.jan1_age_sex@1.0.0", geography=("FRA",),
                temporal_support={"years": [2023]}, reason="unsupported: no eligible connector", priority=1.0,
                priority_method="test", context=ctx)
    base.update(over)
    return KnowledgeGap(**base)


@pytest.fixture(autouse=True)
def _offline_sandbox_probe(monkeypatch):
    """Sandbox isolation probes target a closed local port in tests (no outbound packets)."""
    from reva.autonomy import sandbox as _sb

    monkeypatch.setattr(_sb, "PUBLIC_PROBE_ADDR", ("127.0.0.1", 1))


@pytest.fixture()
def io(tmp_path: Path) -> ArtifactIO:
    from reva.adapters.objects.filesystem import FilesystemArtifactStore

    return ArtifactIO(FilesystemArtifactStore(tmp_path / "objects"))


async def _pipeline(io, overrides=None):
    bank = bank_from_dir(io, CONNECTOR, overrides=overrides)
    gap = _gap(io)
    disc = EurostatCatalogueDiscoverer(io, BankHttp(io, bank))
    cands = await disc.discover(gap, default_budget())
    mappings = RegistryOntologyMapper(io).assess(cands[0], gap.context)
    cp = DeclarativeConnectorGenerator(io).generate(cands[0], mappings[0], ("eurostat_jsonstat",))
    return bank, disc, cands, mappings, cp


async def test_discovers_demo_pjan_from_captured_catalogue(io):
    _bank, disc, cands, _, _ = await _pipeline(io)
    assert cands[0].dataset_id == "demo_pjan"
    assert disc.last_search["datasets_in_catalogue"] > 5000  # the real catalogue, not a toy list
    assert [c["code"] for c in disc.last_search["ranked"]][:2] == ["demo_pjan", "demo_pjangroup"]
    assert cands[0].allowed_hosts == ("ec.europa.eu",)
    meta = io.get_json(cands[0].metadata)
    assert meta["inspection"]["dimension_order"] == ["freq", "unit", "age", "sex", "geo", "time"]
    assert meta["catalogue"]["url"] == EUROSTAT_TOC_URL
    assert cands[0].rights_status == "verified"


async def test_manifest_validates_against_contract_schema(io):
    import jsonschema

    *_, cp = await _pipeline(io)
    manifest = io.get_json(cp.manifest)
    schema = json.loads((Path(__file__).parents[2] / "contracts" / "connector.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(manifest)
    assert manifest["generation_mode"] == "declarative_only"
    assert manifest["template"] == "eurostat_jsonstat"
    assert manifest["name"] == CONNECTOR and manifest["version"] == "1.0.0"
    rp = manifest["request_parameters"]
    assert rp["dimension_order"] == ["freq", "unit", "age", "sex", "geo", "time"]
    assert rp["semantics"]["temporal_support"] == "point:01-01"
    assert rp["semantics"]["population_universe"] == "usually_resident_population_eurostat"
    assert rp["total_categories"] == {"age": ["TOTAL"], "sex": ["T"]}
    # deterministic generation: same inputs, same manifest bytes
    *_, cp2 = await _pipeline(io)
    assert cp2.manifest.sha256 == cp.manifest.sha256


async def test_generator_refuses_unapproved_template(io):
    from reva.kernel.errors import PolicyDenied

    _, _, cands, mappings, _ = await _pipeline(io)
    with pytest.raises(PolicyDenied):
        DeclarativeConnectorGenerator(io).generate(cands[0], mappings[0], ("worldbank_v2",))


async def test_validator_passes_captured_sample(io):
    bank, *_, cp = await _pipeline(io)
    rep = await JsonStatConnectorValidator(io).validate(cp, bank, default_budget())
    assert rep.outcome == "pass", [(f.code, f.message) for f in rep.findings]
    ev = io.get_json(rep.evidence_manifest)
    b = ev["batches"][0]
    assert b["size"] == [1, 1, 103, 3, 1, 1] and b["cube_positions"] == 309 and b["value_count"] == 309
    assert b["status_codes_seen"] == ["p"] and b["status_labels"] == {"p": "provisional"}


def _mutated(mut) -> dict[str, bytes]:
    doc = _sample()
    mut(doc)
    return {SAMPLE_URL: json.dumps(doc).encode()}


def _swap_order(doc):
    doc["id"] = ["freq", "unit", "sex", "age", "geo", "time"]
    doc["size"] = [1, 1, 3, 103, 1, 1]


def _change_unit(doc):
    doc["dimension"]["unit"]["category"] = {"index": {"THS": 0}, "label": {"THS": "Thousand"}}


def _drop_status(doc):
    del doc["status"]


@pytest.mark.parametrize("mutation,code", [(_swap_order, "dimension_order_changed"),
                                           (_change_unit, "unit_changed"),
                                           (_drop_status, "status_missing")])
async def test_validator_fails_mutated_sample(io, mutation, code):
    # the manifest is generated from the genuine sample; validation replays a mutated one
    _, _, _cands, _mappings, cp = await _pipeline(io)
    bad_bank = bank_from_dir(io, CONNECTOR, overrides=_mutated(mutation))
    rep = await JsonStatConnectorValidator(io).validate(cp, bad_bank, default_budget())
    assert rep.outcome == "fail"
    assert code in {f.code for f in rep.findings if f.severity == "blocker"}


async def test_validator_blocks_without_sample(io):
    *_, cp = await _pipeline(io)
    empty = io.put_json({"schema": "reva-fixture-bank/1", "connector": CONNECTOR, "entries": []}, "raw")
    rep = await JsonStatConnectorValidator(io).validate(cp, empty, default_budget())
    assert rep.outcome == "blocked"


async def test_wdi_midyear_equivalence_is_incompatible(io):
    _, _, _cands, mappings, _ = await _pipeline(io)
    by_target = {m.target_concept_version: m for m in mappings}
    exact = by_target["demography.population.jan1_age_sex@1.0.0"]
    wdi = by_target["demography.population.wdi_midyear_total@1.0.0"]
    assert exact.relation == "exact" and not exact.review_required
    assert wdi.relation == "incompatible"
    mm = io.get_json(wdi.mapping_manifest)
    diffs = " ".join(mm["profile_differences"])
    assert "point:01-01" in diffs and "07-01" in diffs  # 1 January vs midyear
    assert "usually_resident" in diffs and "de_facto" in diffs  # universe differs
    assert mm["decision"] == "rejected_false_equivalence"


async def test_other_datasets_are_only_candidates(io):
    _, _, cands, _, _ = await _pipeline(io)
    grp = next(c for c in cands if c.dataset_id == "demo_pjangroup")
    m = RegistryOntologyMapper(io).propose_mapping(grp, _gap(io).context)
    assert m.relation != "exact" and m.review_required


def _job_ctx(db, tmp_path):
    """A real leased job row so the handler's fenced write path is exercised."""
    from reva.operations.context import JobContext

    jid = uuid.uuid4()
    with db.owner.begin() as conn:
        conn.execute(text("""INSERT INTO ops.job(id, job_type, payload, idempotency_key, status, worker_id,
                                 lease_until, fencing_token, attempts)
                             VALUES (:i, 'discovery', '{}'::jsonb, :k, 'running', 'test-worker',
                                     clock_timestamp() + interval '10 minutes', 7, 1)"""),
                     {"i": jid, "k": f"test-discovery-{jid}"})
    return JobContext(job_id=jid, job_type="discovery", worker_id="test-worker", fencing_token=7, attempt=1,
                      settings=db.settings, worker_engine=db.worker, sim_engine=None, store=db.store)


async def test_end_to_end_promotion_ingestion_and_belief_separation(db, tmp_path):
    from reva.autonomy.jobs import handle_discovery, promoted_connectors
    from reva.belief.publication import publish_release
    from reva.kernel.context import Origin

    ctx = _job_ctx(db, tmp_path)
    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, ctx)
    assert out["status"] == "promoted", out
    assert out["gate"]["outcome"] == "promote"
    assert out["value_estimate"]["kind"] == "uncertainty_reduction_proxy"
    assert out["rejected_equivalences"] and out["rejected_equivalences"][0]["outcome"] == "reject"
    with db.owner.connect() as conn:
        cv = conn.execute(text("SELECT id, code_digest, declared_capabilities FROM evidence.connector_version "
                               "WHERE name=:n AND version='1.0.0'"), {"n": CONNECTOR}).mappings().one()
        assert cv["declared_capabilities"]["template"] == "eurostat_jsonstat"
        elig = conn.execute(text(
            """SELECT e.decision, e.applicability, e.policy_version FROM evidence.eligibility e
               JOIN ontology.concept_version c ON c.id = e.concept_version_id
               WHERE e.connector_version_id = :cv AND c.concept_id = 'demography.population.jan1_age_sex'"""),
            {"cv": cv["id"]}).mappings().all()
        assert len(elig) == 1 and elig[0]["decision"] == "eligible"
        assert elig[0]["applicability"]["subjects"] == ["FRA"]
        assert elig[0]["policy_version"] == "promotion-1.1.0"
        wdi_elig = conn.execute(text(
            """SELECT count(*) FROM evidence.eligibility e JOIN ontology.concept_version c
               ON c.id = e.concept_version_id WHERE e.connector_version_id = :cv
               AND c.concept_id = 'demography.population.wdi_midyear_total'"""), {"cv": cv["id"]}).scalar_one()
        assert wdi_elig == 0
        gates = conn.execute(text(
            """SELECT p.proposal_type, g.decision, g.evaluator, p.author FROM ops.gate_result g
               JOIN ops.proposal p ON p.id = g.proposal_id
               WHERE p.id IN (:a, :b)"""),
            {"a": uuid.UUID(out["gate"]["proposal_id"]),
             "b": uuid.UUID(out["rejected_equivalences"][0]["proposal_id"])}).mappings().all()
        kinds = {(g["proposal_type"], g["decision"]) for g in gates}
        assert ("connector_promotion", "pass") in kinds and ("mapping_equivalence", "fail") in kinds
        assert all(g["evaluator"] == "gate-evaluator:promotion" != "gate-evaluator:" + g["author"] for g in gates)
        incompatible = conn.execute(text(
            "SELECT mapping_kind FROM ontology.mapping WHERE mapping_key='eurostat.demo_pjan.to_wdi_midyear'"
        )).scalar_one()
        assert incompatible == "incompatible"
        outbox = conn.execute(text("SELECT payload FROM ops.outbox WHERE event_type='autonomy.connector_promoted'"
                                   )).scalars().all()
        assert outbox and outbox[-1]["rollback_sha256"]

    # Ingest the captured sample through the existing run_connector/publish_acquisition path.
    conn_obj = promoted_connectors(db.worker, db.store)[CONNECTOR]
    assert isinstance(conn_obj, EurostatJsonStatConnector)
    rep = await run_connector(conn_obj, FixtureHttp(CONNECTOR), db.store, db.worker, persist_cursor=False)
    assert rep.inserted == 309, rep.as_dict()
    with db.owner.connect() as conn:
        rows = conn.execute(text(
            """SELECT o.logical_key, o.value, o.source_flags, s.temporal_extent, s.population_spec, s.aggregation,
                      s.resolution FROM evidence.observation o JOIN world.support s ON s.id = o.support_id
               JOIN evidence.connector_version cv ON cv.id = o.connector_version_id WHERE cv.name = :n"""),
            {"n": CONNECTOR}).mappings().all()
    by_key = {r["logical_key"]: r for r in rows}
    total = by_key["eurostat:demo_pjan:FR:T:TOTAL:2023"]
    assert total["value"]["value"] == "68277210" and total["source_flags"]["status"] == "p"
    assert total["source_flags"]["status_labels"] == {"p": "provisional"}
    assert total["source_flags"]["cell_type"] == "total"
    assert by_key["eurostat:demo_pjan:FR:M:Y5:2023"]["source_flags"]["cell_type"] == "leaf"
    assert by_key["eurostat:demo_pjan:FR:T:Y5:2023"]["source_flags"]["cell_type"] == "subtotal"
    assert by_key["eurostat:demo_pjan:FR:F:UNK:2023"]["source_flags"]["special_categories"] == {"age": "unknown"}
    assert total["temporal_extent"]["start"] == "2023-01-01" and total["temporal_extent"]["kind"] == "instant"
    assert total["population_spec"]["universe"] == "usually_resident_population_eurostat"
    assert total["aggregation"] == "point_in_time_stock"
    assert total["resolution"] == {"age": "TOTAL", "sex": "T"}
    leaves = sum(int(r["value"]["value"]) for r in rows if r["source_flags"]["cell_type"] == "leaf")
    assert leaves == 68277210  # disjoint leaf cells sum to the published total

    # Belief selection never mixes Eurostat 1 January with WDI midyear population.
    from reva.adapters.connectors.registry import builtin_connectors

    wb = builtin_connectors()["worldbank.wdi.indicators"]
    await run_connector(wb, FixtureHttp(wb.describe().name), db.store, db.worker, persist_cursor=False)
    rel = publish_release(db.worker, db.store, cutoff=None, origin=Origin.test_fixture, environment="test",
                          concepts=["demography.population.jan1_age_sex", "demography.population.wdi_midyear_total"])
    with db.owner.connect() as conn:
        est = conn.execute(text(
            """SELECT c.concept_id, o.source_id, s.source_key, e.alternatives, sp.temporal_extent
               FROM belief.estimate e JOIN belief.input i ON i.estimate_id = e.id
               JOIN evidence.observation o ON o.id = i.observation_id
               JOIN evidence.source s ON s.id = o.source_id
               JOIN ontology.concept_version c ON c.id = e.concept_version_id
               JOIN world.support sp ON sp.id = e.support_id WHERE e.release_id = :r"""),
            {"r": rel.release_id}).mappings().all()
    jan = [e for e in est if e["concept_id"] == "demography.population.jan1_age_sex"]
    mid = [e for e in est if e["concept_id"] == "demography.population.wdi_midyear_total"]
    assert len(jan) == 309 and mid
    assert {e["source_key"] for e in jan} == {"eurostat.dissemination"}
    assert {e["source_key"] for e in mid} == {"worldbank.wdi"}
    assert all(e["temporal_extent"]["start"].endswith("-01-01") for e in jan)
    assert all(e["temporal_extent"]["start"].endswith("-07-01") for e in mid)
    assert all(not e["alternatives"] for e in jan + mid)


async def test_rollback_retires_eligibility(db, tmp_path):
    from reva.autonomy.jobs import handle_discovery, rollback_promotion
    from reva.cli.seed import entity_id

    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, _job_ctx(db, tmp_path))
    assert out["status"] in ("promoted", "no_gap", "already_promoted"), out
    with pytest.raises(Exception):
        rollback_promotion(db.worker, CONNECTOR, "1.0.0", "demography.population.jan1_age_sex@1.0.0",
                           "agent:autonomy-discovery", "test")
    rollback_promotion(db.worker, CONNECTOR, "1.0.0", "demography.population.jan1_age_sex@1.0.0",
                       "human:maintainer", "rollback drill")
    with db.owner.connect() as conn:
        cv = conn.execute(text("SELECT id FROM evidence.connector_version WHERE name=:n"),
                          {"n": CONNECTOR}).scalar_one()
        c = conn.execute(text("SELECT id FROM ontology.concept_version WHERE concept_id="
                              "'demography.population.jan1_age_sex'")).scalar_one()
        ok = conn.execute(text("SELECT evidence.is_eligible(:cv, :c, :s, clock_timestamp())"),
                          {"cv": cv, "c": c, "s": entity_id("FRA")}).scalar_one()
    assert ok is False


# --------------------------------------------------------------------------- R4-09 / R4-14 (offline)
@pytest.mark.parametrize("template", ["k:{geo.__class__.__mro__}:{time}", "eurostat:{dataset}:{time:>200000}",
                                      "eurostat:{dataset}:{geo}:{nope}:{time}", "eurostat:{dataset}:{geo}",
                                      "eurostat:{dataset}:{geo[0]}:{time}"])
async def test_r4_09_logical_key_template_is_strict(io, template):
    from reva.adapters.connectors.eurostat_template import validate_manifest
    from reva.kernel.errors import ValidationProblem

    *_, cp = await _pipeline(io)
    m = io.get_json(cp.manifest)
    m["request_parameters"]["logical_key_template"] = template
    with pytest.raises(ValidationProblem):
        validate_manifest(m)


def test_r4_09_render_is_plain_substitution():
    from reva.adapters.connectors.eurostat_template import render_logical_key
    from reva.kernel.errors import SourceResponseError

    assert render_logical_key("eurostat:{dataset}:{geo}:{time}",
                              {"dataset": "d", "geo": "{time}", "time": "2023"}) == "eurostat:d:{time}:2023"
    with pytest.raises(SourceResponseError):
        render_logical_key("eurostat:{dataset}:{geo}", {"dataset": "d", "geo": "x" * 600})


async def test_r4_09_generated_key_layout_is_template_derived(io):
    from reva.adapters.connectors.eurostat_template import canonical_logical_key_template

    *_, cp = await _pipeline(io)
    m = io.get_json(cp.manifest)
    assert m["request_parameters"]["logical_key_template"] == canonical_logical_key_template(m) == \
        "eurostat:{dataset}:{geo}:{sex}:{age}:{time}"


def _bad_index(doc):
    idx = doc["dimension"]["age"]["category"]["index"]
    idx[next(iter(idx))] = "zero"


def _dimension_not_object(doc):
    doc["dimension"]["age"] = ["not", "an", "object"]


def _size_not_int(doc):
    doc["size"] = [1, 1, "x", 3, 1, 1]


@pytest.mark.parametrize("mutation", [_bad_index, _dimension_not_object, _size_not_int])
async def test_r4_14_malformed_provider_data_is_a_typed_failure(io, mutation):
    from reva.kernel.errors import SourceResponseError

    _, _, _c, _m, cp = await _pipeline(io)
    bad_bank = bank_from_dir(io, CONNECTOR, overrides=_mutated(mutation))
    rep = await JsonStatConnectorValidator(io).validate(cp, bad_bank, default_budget())
    assert rep.outcome == "fail" and "schema_drift" in {f.code for f in rep.findings}
    conn = EurostatJsonStatConnector(io.get_json(cp.manifest))
    from reva.ports.connectors import FetchBudget

    item = conn.plan(None, FetchBudget()).items[0]
    raw = await conn.fetch(item, BankHttp(io, bad_bank))
    with pytest.raises(SourceResponseError) as ei:
        list(conn.parse(raw))
    assert ei.value.details.get("kind") == "schema_drift"


async def test_r4_14_malformed_inspection_is_recorded_not_raised(io):
    bank = bank_from_dir(io, CONNECTOR, overrides=_mutated(_dimension_not_object))
    gap = _gap(io)
    disc = EurostatCatalogueDiscoverer(io, BankHttp(io, bank))
    cands = await disc.discover(gap, default_budget())
    assert cands and disc.last_search["inspections"][0]["ok"] is False
    assert "malformed" in disc.last_search["inspections"][0]["error"]


# --------------------------------------------------------------------------- R4-03 / R4-04 (promotion state)
CONCEPT = "demography.population.jan1_age_sex@1.0.0"


def _version_rows(db):
    with db.owner.connect() as conn:
        return conn.execute(text("SELECT version FROM evidence.connector_version WHERE name=:n ORDER BY recorded_at"),
                            {"n": CONNECTOR}).scalars().all()


def _count(db, sql):
    with db.owner.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


async def test_r4_04_rediscovery_after_rollback_holds(db, tmp_path):
    """After a rollback, discovery reports rolled_back_hold: no proposal, gate pass, event or eligibility."""
    from reva.autonomy.jobs import handle_discovery, promoted_connectors, reapprove_promotion, rollback_promotion

    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, _job_ctx(db, tmp_path))
    if out["status"] != "rolled_back_hold":  # make the precondition explicit (shared session DB)
        rollback_promotion(db.worker, CONNECTOR, "1.0.0", CONCEPT, "human:maintainer", "drill")
    events = "SELECT count(*) FROM ops.outbox WHERE event_type='autonomy.connector_promoted'"
    passes = ("SELECT count(*) FROM ops.gate_result g JOIN ops.proposal p ON p.id=g.proposal_id "
              "WHERE p.proposal_type='connector_promotion' AND g.decision='pass'")
    elig = "SELECT count(*) FROM evidence.eligibility"
    before = (_count(db, events), _count(db, passes), _count(db, elig))
    out2 = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, _job_ctx(db, tmp_path))
    assert out2["status"] == "rolled_back_hold", out2
    assert "gate" not in out2 and (_count(db, events), _count(db, passes), _count(db, elig)) == before
    assert CONNECTOR not in promoted_connectors(db.worker, db.store)  # retired versions are not loaded
    with pytest.raises(Exception):
        reapprove_promotion(db.worker, CONNECTOR, "1.0.0", CONCEPT, "agent:autonomy-discovery", "no")
    reapprove_promotion(db.worker, CONNECTOR, "1.0.0", CONCEPT, "human:maintainer", "drill over")
    assert promoted_connectors(db.worker, db.store)[CONNECTOR].manifest["version"] == "1.0.0"


async def test_r4_03_changed_content_gets_new_version_and_rollback_restores_previous(db, tmp_path):
    from reva.adapters.connectors.eurostat_template import template_digest
    from reva.autonomy.common import ArtifactIO
    from reva.autonomy.gate import PolicyPromotionGate
    from reva.autonomy.jobs import connector_change_proposal, promote_connector, promoted_connectors, rollback_promotion
    from reva.autonomy.policy import promotion_policy
    from reva.autonomy.promotion import resolve_connector_version
    from reva.autonomy.proposals import ProposalStore
    from reva.autonomy.sandbox import TrustedTemplateSandbox, trusted_template_policy
    from reva.kernel.errors import ImmutableRecordViolation
    from reva.ports.autonomy import MappingProposal, ValueEstimate

    io_ = ArtifactIO(db.store)
    active = promoted_connectors(db.worker, db.store)[CONNECTOR].manifest["version"]
    bank, _d, _c, mappings, cp = await _pipeline(io_)
    m = io_.get_json(cp.manifest)
    with db.worker.connect() as conn:
        same_v, same_id = resolve_connector_version(conn, io_, m, template_digest())
    assert same_v == "1.0.0" and same_id is not None  # identical content keeps its version
    m["request_parameters"]["dataset_label"] = f"relabelled {uuid.uuid4().hex[:8]}"
    with db.worker.connect() as conn:
        new_v, new_id = resolve_connector_version(conn, io_, m, template_digest())
        assert resolve_connector_version(conn, io_, m, "sha256:" + "f" * 64)[1] is None  # template change too
    assert new_id is None and new_v not in _version_rows(db)

    async def gate_pass(manifest):
        cp2 = type(cp)(cp.proposal_id, cp.candidate, cp.template_id, cp.template_digest,
                       io_.put_json(manifest, "models"), cp.mapping)
        v = await JsonStatConnectorValidator(io_).validate(cp2, bank, default_budget())
        val = ValueEstimate("unsupported", None, None, {}, io_.put_json({"t": 1}, "evaluation"), "t")
        p = connector_change_proposal(io_, cp2, manifest, mappings[0], val, promotion_policy())
        ProposalStore(db.worker, io_).persist(p, proposal_type="connector_promotion", scope={}, risk={})
        s = await TrustedTemplateSandbox(io_, fixtures=bank).run(p, trusted_template_policy(default_budget()))
        d = PolicyPromotionGate(db.worker, io_).decide(p, [v, s], promotion_policy().ref())
        assert d.outcome == "promote", d.reasons
        return d

    mp = MappingProposal(uuid.uuid4(), cp.candidate.metadata, CONCEPT, "exact", mappings[0].mapping_manifest, False)
    # same (name, version) with different content is refused, never silently mapped to the old row
    stale = dict(m, version="1.0.0")
    with pytest.raises(ImmutableRecordViolation):
        promote_connector(db.worker, io_, _job_ctx(db, tmp_path), stale, mp, await gate_pass(stale),
                          promotion_policy())
    m["version"] = new_v
    out = promote_connector(db.worker, io_, _job_ctx(db, tmp_path), m, mp, await gate_pass(m), promotion_policy())
    assert out["status"] == "promoted" and out["version"] == new_v and out["superseded"] == [active]
    assert promoted_connectors(db.worker, db.store)[CONNECTOR].manifest["version"] == new_v
    rollback_promotion(db.worker, CONNECTOR, new_v, CONCEPT, "human:maintainer", "drill")
    assert promoted_connectors(db.worker, db.store)[CONNECTOR].manifest["version"] == active  # restored
    events = "SELECT count(*) FROM ops.outbox WHERE event_type='autonomy.connector_promoted'"
    n = _count(db, events)
    again = promote_connector(db.worker, io_, _job_ctx(db, tmp_path), m, mp, await gate_pass(m),
                              promotion_policy())
    assert again["status"] == "rolled_back_hold" and _count(db, events) == n


async def test_r4_03_template_change_is_surfaced_not_silently_promoted(db, tmp_path, monkeypatch):
    import reva.adapters.connectors.eurostat_template as et
    from reva.autonomy.gaps import build_coverage_snapshot, classify
    from reva.autonomy.jobs import (
        handle_discovery,
        promoted_connector_availability,
        promoted_connector_states,
        promoted_connectors,
    )
    from reva.ontology.registry import load_registry

    assert CONNECTOR in promoted_connectors(db.worker, db.store)
    monkeypatch.setattr(et, "template_digest", lambda: "sha256:" + "f" * 64)  # a reviewed template revision
    assert CONNECTOR not in promoted_connectors(db.worker, db.store)
    states = {x["state"] for x in promoted_connector_states(db.worker) if x["name"] == CONNECTOR}
    assert "template_changed" in states and "active" not in states
    with db.worker.connect() as conn:
        snap = build_coverage_snapshot(conn, datetime.now(UTC), ["test_fixture"], load_registry())
    assert "FRA" in snap["template_changed"]["demography.population.jan1_age_sex"]
    rows = [r for r in classify(load_registry().requirements, snap, datetime.now(UTC))
            if r["requirement_id"] == "R-POP-JAN-AGESEX"]
    assert rows and all(r["status"] == "unsupported" and "template_changed" in r["detail"] for r in rows)
    avail = promoted_connector_availability(db.worker, CONNECTOR)
    assert avail is not None and avail["status"] == "not_promoted" and avail["state"] == "template_changed"
    versions = len(_version_rows(db))
    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, _job_ctx(db, tmp_path))
    # the template is reviewed code pinned in the policy: no auto-promotion until a maintainer re-pins it
    assert out["status"] == "gate_reject", out
    assert any("pinned in the signed policy" in r for r in out["gate"]["reasons"])
    assert len(_version_rows(db)) == versions


@pytest.mark.live
async def test_live_catalogue_and_sample_still_conform(io):
    """Bounded live check (2 discovery requests + 1 sample): run with ``-m live``."""
    from reva.adapters.http.safe_client import SafeHttpClient
    from reva.autonomy.fixtures import CapturingHttp

    client = SafeHttpClient(allowed_hosts=["ec.europa.eu"], max_bytes=4_000_000)
    http = CapturingHttp(client, "Eurostat (CC BY 4.0)")
    gap = _gap(io)
    cands = await EurostatCatalogueDiscoverer(io, http).discover(gap, default_budget())
    assert cands[0].dataset_id == "demo_pjan"
    mapping = RegistryOntologyMapper(io).assess(cands[0], gap.context)[0]
    cp = DeclarativeConnectorGenerator(io).generate(cands[0], mapping, ("eurostat_jsonstat",))
    empty = io.put_json({"schema": "reva-fixture-bank/1", "connector": CONNECTOR, "entries": []}, "raw")
    rep = await JsonStatConnectorValidator(io, live_client_factory=lambda hosts: client).validate(
        cp, empty, default_budget())
    assert rep.outcome == "pass", [(f.code, f.message) for f in rep.findings]
    assert io.get_json(rep.evidence_manifest)["fixture_bank"]
