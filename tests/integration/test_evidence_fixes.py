"""Regression tests for the evidence/belief review findings fixed by FX2.

R-03 publication snapshot consistency (evidence lock protocol), R-04 per-origin
lineages, R-05 flow-specific Comtrade net weights, R-06 dirty-node scoping,
R-10 Comtrade partner aliases, R-14 atomic post-publish steps, R-20 current
release definition, R-23 flow-aware valuation, R-27 retraction semantics and
prior-estimate origin, R-28 parser_version in the acquisition idempotency key.

The session database is shared with other test modules, so synthetic
observations use a distinct support (``resolution={"fx2_test": <tag>}``): they
form their own selection groups and never become alternatives of other
modules' estimates. Dirty-node markers are restored after each DB test.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors.comtrade import (
    EXCHANGE_RATE_TRANSFORM,
    VALUATION_UNDETERMINED,
    WEIGHT_CONCEPT,
    ComtradeConnector,
)
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.postgres import repositories as repo
from reva.belief.publication import add_prior_estimate_input, publish_release
from reva.belief.selection import eligible_latest, select, select_report
from reva.belief.snapshots import release_as_of
from reva.cli.seed import entity_id, register_connector_version
from reva.evidence.ingest import acquisition_idempotency_key, publish_acquisition
from reva.evidence.retractions import retract_observation
from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import ValidationProblem
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import Precision, TimeExtent
from reva.kernel.uncertainty import UncertaintySpec
from reva.ontology.registry import load_registry
from reva.ports.connectors import (
    Cursor,
    FetchBudget,
    FetchItem,
    FetchPlan,
    ObservationBatch,
    ParsedObservation,
    QualityReport,
    RawAcquisition,
)

pytestmark = pytest.mark.db

POP = "demography.population.wdi_midyear_total"
GDP = "economics.gdp.nominal_current_usd"
WB = WorldBankConnector().describe()


# --------------------------------------------------------------------------- helpers
@pytest.fixture(autouse=True)
def _restore_dirty_nodes(request: pytest.FixtureRequest) -> Iterator[None]:
    if "db" not in request.fixturenames:
        yield
        return
    db = request.getfixturevalue("db")
    with db.worker.connect() as c:
        before = [dict(r) for r in c.execute(text(
            "SELECT node_key, since_sequence, accumulated_error_bound, reason FROM ops.dirty_node")).mappings()]
    yield
    with db.worker.begin() as c:
        c.execute(text("DELETE FROM ops.dirty_node"))
        for r in before:
            c.execute(text(
                """INSERT INTO ops.dirty_node(node_key, since_sequence, accumulated_error_bound, reason)
                   VALUES (:node_key, :since_sequence, CAST(:b AS jsonb), :reason)"""),
                {**r, "b": json.dumps(r["accumulated_error_bound"])})


def _now(engine: Any) -> datetime:
    with engine.connect() as c:
        return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def _support(tag: str, year: int = 2021) -> Support:
    return Support(
        temporal=TimeExtent(kind="instant", precision=Precision.day, start=date(year, 7, 1), label=f"{year} midyear"),
        spatial=SpatialRef(scheme="provider_reporter_territory", key="WB:FRA", authority="World Bank"),
        population=PopulationRef(universe="de_facto_residents_wdi", definition_source="fx2 synthetic test"),
        aggregation="point_in_time_stock",
        resolution={"fx2_test": tag},
    )


def _synthetic(tag: str, values: dict[str, str], *, origin: str = "test_fixture", year: int = 2021,
               body_salt: str = "") -> tuple[RawAcquisition, list[ObservationBatch], list[QualityReport]]:
    """A synthetic WDI-shaped acquisition: one observation per logical-key suffix in ``values``."""
    body = json.dumps({"fx2": tag, "values": values, "salt": body_salt}, sort_keys=True).encode()
    item = FetchItem(key=f"fx2:{tag}", url=f"https://api.worldbank.org/v2/fx2-test/{tag}", max_bytes=10_000)
    raw = RawAcquisition(item=item, body=body, media_type="application/json", retrieved_at=datetime.now(UTC),
                         response_metadata={"status": 200}, request_fingerprint=f"fx2:{tag}", origin=origin)
    obs = tuple(
        ParsedObservation(
            subject_provider="worldbank", subject_key="FRA", concept_id=POP, concept_version="1.0.0",
            support=_support(tag, year), logical_key=f"fx2:{tag}:{suffix}",
            value={"kind": "decimal", "value": v, "unit_ref": "unit.persons"},
            uncertainty=UncertaintySpec.unknown("synthetic test value"),
            source_flags={"fx2": tag}, raw_selector={"match": {"suffix": suffix}},
        )
        for suffix, v in sorted(values.items())
    )
    batch = ObservationBatch(connector=WB.name, connector_version=WB.version, item_key=item.key, observations=obs)
    return raw, [batch], [QualityReport(outcome="pass")]


def _ingest(db: Any, tag: str, values: dict[str, str], **kw: Any) -> Any:
    spec = kw.pop("spec", WB)
    post_publish = kw.pop("post_publish", None)
    raw, batches, reports = _synthetic(tag, values, **kw)
    return publish_acquisition(db.worker, db.store, spec, raw, batches, reports, post_publish=post_publish)


def _obs(db: Any, logical_key: str) -> list[dict[str, Any]]:
    with db.worker.connect() as c:
        return [dict(r) for r in c.execute(text(
            """SELECT o.id, o.origin, o.revision_no, o.supersedes_id, o.value->>'value' AS v, o.recorded_at
               FROM evidence.observation o WHERE o.logical_key = :k ORDER BY o.origin, o.revision_no"""),
            {"k": logical_key}).mappings()]


def _estimate_for_support(db: Any, release_id: Any, tag: str) -> list[dict[str, Any]]:
    with db.worker.connect() as c:
        return [dict(r) for r in c.execute(text(
            """SELECT e.id, e.value, e.alternatives, e.discrepancy_flags, i.observation_id
               FROM belief.estimate e JOIN world.support s ON s.id = e.support_id
               LEFT JOIN belief.input i ON i.estimate_id = e.id
               WHERE e.release_id = :r AND s.resolution->>'fx2_test' = :t"""),
            {"r": release_id, "t": tag}).mappings()]


# --------------------------------------------------------------------------- R-03
def test_r03_release_waits_for_inflight_ingest(db: Any) -> None:
    tag = f"r03-{uuid.uuid4().hex[:8]}"
    inside, release_ingest = threading.Event(), threading.Event()
    result: dict[str, Any] = {}

    def hold(conn: Any, summary: Any) -> None:  # runs inside the acquisition transaction
        inside.set()
        assert release_ingest.wait(30)

    def ingest() -> None:
        result["ingest"] = _ingest(db, tag, {"a": "123"}, post_publish=hold)

    def publish() -> None:
        try:
            result["release"] = publish_release(db.worker, db.store, cutoff=None, origin=Origin.test_fixture,
                                                environment="test", concepts=[POP])
        except Exception as e:
            result["error"] = e

    t_ingest = threading.Thread(target=ingest)
    t_ingest.start()
    assert inside.wait(30), "ingest did not reach its publication transaction"
    in_flight = _obs(db, f"fx2:{tag}:a")
    assert in_flight == []  # inserted but uncommitted: invisible to others
    t_pub = threading.Thread(target=publish)
    t_pub.start()
    time.sleep(0.8)
    assert t_pub.is_alive(), "publication must wait for the in-flight evidence writer"
    release_ingest.set()
    t_ingest.join(30)
    t_pub.join(60)
    assert "error" not in result, result.get("error")
    rel = result["release"]
    (row,) = _obs(db, f"fx2:{tag}:a")
    assert row["recorded_at"] <= rel.knowledge_cutoff
    ests = _estimate_for_support(db, rel.release_id, tag)
    assert [e["observation_id"] for e in ests] == [row["id"]]
    # As-of reconstruction at the release cutoff agrees with the release.
    with db.worker.connect() as c:
        asof = {r["id"] for r in eligible_latest(c, rel.knowledge_cutoff, ["test_fixture"], [POP])}
    assert row["id"] in asof


def test_r03_future_cutoff_rejected_and_none_means_db_now(db: Any) -> None:
    _ingest(db, f"r03b-{uuid.uuid4().hex[:8]}", {"a": "5"})
    with pytest.raises(ValidationProblem, match="future"):
        publish_release(db.worker, db.store, cutoff=datetime.now(UTC) + timedelta(hours=1),
                        origin=Origin.test_fixture, environment="test", concepts=[POP])
    before = _now(db.worker)
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])
    assert before <= rel.knowledge_cutoff <= _now(db.worker)


# --------------------------------------------------------------------------- R-04
TEST_SOURCE = "fx2.test_source"


def _isolated_spec(db: Any) -> Any:
    """A test-only source + connector version (eligible for POP, never in a preference policy), so
    live-origin test rows never appear under a real source of the shared session database."""
    spec = WB.model_copy(update={"name": "fx2.test.connector", "source_key": TEST_SOURCE})
    with db.worker.begin() as c:
        act = repo.simple_activity("registry.seed", "review", "maintainer:fx2-test", {})
        repo.record_activity(c, act)
        if repo.source_by_key(c, TEST_SOURCE) is None:
            sid = c.execute(text(
                """INSERT INTO evidence.source(source_key, title, provider_url, lineage_family, rights, metadata,
                     provenance_activity_id)
                   VALUES (:k, 'FX2 test source', 'https://example.invalid/', 'fx2_test',
                           '{"licence":"test"}', '{}', :a) RETURNING id"""), {"k": TEST_SOURCE, "a": act.id}
            ).scalar_one()
            c.execute(text(
                """INSERT INTO evidence.source_metadata_version(source_id, version, metadata, rights,
                     provenance_activity_id) VALUES (:s, 1, '{}', '{"licence":"test"}', :a)"""),
                {"s": sid, "a": act.id})
        cv = register_connector_version(c, db.store, spec, act.id)
        concept = repo.concept_version_id(c, POP, "1.0.0")
        if not c.execute(text("SELECT 1 FROM evidence.eligibility WHERE connector_version_id=:cv"),
                         {"cv": cv}).scalar_one_or_none():
            c.execute(text(
                """INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability,
                     decision, policy_version, review_activity_id)
                   VALUES (:cv, :c, CAST(:ap AS jsonb), 'eligible', 'fx2-test', :a)"""),
                {"cv": cv, "c": concept, "a": act.id,
                 "ap": json.dumps({"subject_ids": [str(entity_id("FRA"))]})})
    return spec


def test_r04_live_ingest_identical_to_fixture_creates_live_lineage(db: Any) -> None:
    tag = f"r04-{uuid.uuid4().hex[:8]}"
    spec = _isolated_spec(db)
    fx = _ingest(db, tag, {"a": "42"}, origin="test_fixture", spec=spec)
    live = _ingest(db, tag, {"a": "42"}, origin="live", spec=spec)
    assert fx.inserted == 1 and live.inserted == 1 and live.unchanged == 0
    rows = _obs(db, f"fx2:{tag}:a")
    assert [(r["origin"], r["revision_no"], r["supersedes_id"]) for r in rows] == [
        ("live", 1, None), ("test_fixture", 1, None)]
    # Re-fetching the same live state is unchanged within the live lineage only.
    again = _ingest(db, tag, {"a": "42"}, origin="live", body_salt="refetch", spec=spec)
    assert again.inserted == 0 and again.unchanged == 1
    with db.worker.connect() as c:
        live_rows = eligible_latest(c, _now(db.worker), ["live"], [POP])
    assert any(r["logical_key"] == f"fx2:{tag}:a" and r["origin"] == "live" for r in live_rows)


# --------------------------------------------------------------------------- R-05
def _ct_body(iso_code: int, flow: str, year: int) -> dict[str, Any]:
    d = FIXTURE_ROOT / "un.comtrade.public_exact"
    for e in json.loads((d / "manifest.json").read_text()):
        url = e["url"]
        if (f"reporterCode={iso_code}&" in url and f"flowCode={flow}&" in url and f"period={year}&" in url
                and "partnerCode=0&" in url):
            return json.loads((d / e["file"]).read_bytes())
    raise KeyError((iso_code, flow, year))


def _ct_parse(ct: ComtradeConnector, key: str, body: dict[str, Any]) -> tuple[list[ObservationBatch], QualityReport]:
    item = next(i for i in ct.plan(None, FetchBudget()).items if i.key == key)
    raw = RawAcquisition(item=item, body=json.dumps(body).encode(), media_type="application/json",
                         retrieved_at=datetime.now(UTC), response_metadata={"status": 200},
                         request_fingerprint="t", origin="test_fixture")
    batches = list(ct.parse(raw))
    return batches, ct.validate(batches[0])


def test_r05_connector_emits_flow_specific_weight_concepts() -> None:
    ct = ComtradeConnector(reporters=("CHN",), total_years=(2023,), bilateral_years=())
    spec = ct.describe()
    assert spec.name == "un.comtrade.public_exact" and spec.version == "1.1.0"
    assert spec.parser_version == "comtrade-parse-2"
    concepts = {}
    for flow in ("M", "X"):
        batches, _ = _ct_parse(ct, f"total:CHN:{flow}:2023", _ct_body(156, flow, 2023))
        w = next(o for o in batches[0].observations if o.logical_key.endswith("netWgt"))
        concepts[flow] = w.concept_id
        assert batches[0].connector_version == "1.1.0"
    assert concepts == WEIGHT_CONCEPT
    ids = {c.concept_id for c in load_registry().concepts}
    assert set(WEIGHT_CONCEPT.values()) <= ids and "trade.hs2709.net_weight.world_total" in ids  # old stays
    prefs = load_registry().policies["selection"]["preferences"]
    assert "trade.hs2709.net_weight.world_total" not in prefs
    assert all(c in prefs for c in WEIGHT_CONCEPT.values())


async def test_r05_import_and_export_weights_are_separate_estimates(db: Any) -> None:
    with db.worker.connect() as c:
        cv = repo.connector_version_row(c, "un.comtrade.public_exact", "1.1.0")
        assert cv is not None, "seed must register Comtrade 1.1.0"
        n_elig = c.execute(text(
            """SELECT count(*) FROM evidence.eligibility e
               JOIN ontology.concept_version k ON k.id = e.concept_version_id
               WHERE e.connector_version_id = :cv AND e.decision = 'eligible' AND k.concept_id = ANY(:c)"""),
            {"cv": cv["id"], "c": list(WEIGHT_CONCEPT.values())}).scalar_one()
        assert n_elig == 2
    ct = builtin_connectors()["un.comtrade.public_exact"]
    rep = await run_connector(ct, FixtureHttp(ct.describe().name), db.store, db.worker, persist_cursor=False)
    assert not rep.errors, rep.errors[:3]
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test",
                          concepts=[*WEIGHT_CONCEPT.values(), "trade.hs2709.net_weight.world_total"])
    with db.worker.connect() as c:
        rows = c.execute(text(
            """SELECT k.concept_id, e.discrepancy_flags FROM belief.estimate e
               JOIN ontology.concept_version k ON k.id = e.concept_version_id WHERE e.release_id = :r"""),
            {"r": rel.release_id}).mappings().all()
    by_concept = {r["concept_id"] for r in rows}
    assert by_concept == set(WEIGHT_CONCEPT.values())  # the collapsed 1.0.0 concept is no longer produced
    assert not any(f.startswith("source_disagreement") for r in rows for f in r["discrepancy_flags"])


_BASE = {"subject_id": 1, "concept_version_id": 2, "support_id": 3, "concept_id": "c", "source_key": "s",
         "uncertainty": {"status": "unquantified"}, "selection_status": "current", "origin": "test_fixture"}


def test_r05_r3_08_incompatible_lineages_withhold_the_estimand() -> None:
    rows = [{**_BASE, "id": 10, "logical_key": "a", "value": {"kind": "decimal", "value": "100"},
             "source_flags": {"flowCode": "M"}},
            {**_BASE, "id": 11, "logical_key": "b", "value": {"kind": "decimal", "value": "40"},
             "source_flags": {"flowCode": "X"}}]
    assert select(rows, {"c": ["s"]}) == []  # no arbitrary pick among different quantities
    res = select_report(rows, {"c": ["s"]})
    (w,) = res.withheld
    assert w.reason == "incompatible_lineages" and w.discrepancy_flags == ["incompatible_lineages:flowCode"]
    assert {a["flowCode"] for a in w.alternatives} == {"M", "X"}


def test_r3_08_decimal_values_compare_numerically() -> None:
    rows = [{**_BASE, "id": 10, "logical_key": "a", "value": {"kind": "decimal", "value": "100"}, "source_flags": {}},
            {**_BASE, "id": 11, "logical_key": "b", "value": {"kind": "decimal", "value": "100.0"},
             "source_flags": {}}]
    (sel,) = select(rows, {"c": ["s"]})
    assert not any(f.startswith("source_disagreement") for f in sel.discrepancy_flags)


# --------------------------------------------------------------------------- R-23
@pytest.mark.parametrize(
    ("flow", "pv", "cif", "fob", "label", "warning"),
    [
        ("M", "100", "100", None, "CIF", None),
        ("M", "100", None, "100", "FOB", None),
        ("M", "100", "100", "100", VALUATION_UNDETERMINED, "valuation_ambiguous"),
        ("M", "100", "90", "80", VALUATION_UNDETERMINED, None),
        ("X", "100", "100", "100", "FOB", None),
        ("X", "100", None, "100", "FOB", None),
        ("X", "100", "100", "90", VALUATION_UNDETERMINED, "export_cif_label_impossible"),
        # 0.0 cif/fob are flags only and never "match" (live CHN/USA 2020-21 rows)
        ("M", "0", "0", None, VALUATION_UNDETERMINED, None),
        ("M", "100", "100", "0", "CIF", None),
        ("X", "0", None, "0", VALUATION_UNDETERMINED, None),
    ],
)
def test_r23_valuation_is_flow_aware(flow: str, pv: str, cif: str | None, fob: str | None, label: str,
                                     warning: str | None) -> None:
    ct = ComtradeConnector(reporters=("CHN",), total_years=(2023,), bilateral_years=())
    body = copy.deepcopy(_ct_body(156, flow, 2023))
    row = body["data"][0]
    row["primaryValue"] = Decimal(pv)
    row["cifvalue"] = None if cif is None else Decimal(cif)
    row["fobvalue"] = None if fob is None else Decimal(fob)
    body = json.loads(json.dumps(body, default=str))
    batches, report = _ct_parse(ct, f"total:CHN:{flow}:2023", body)
    v = next(o for o in batches[0].observations if o.logical_key.endswith("primaryValue"))
    assert v.value["basis"]["valuation"] == label
    assert v.value["basis"]["exchange_rate_transform"] == EXCHANGE_RATE_TRANSFORM
    assert v.source_flags.get("valuation_warning") == warning
    codes = {f.code for f in report.findings}
    assert ("valuation_basis_undetermined" in codes) == (warning is not None)
    assert all(f.severity != "blocker" for f in report.findings if f.code == "valuation_basis_undetermined")


def test_comtrade_estimation_flag_replaces_uninformative_derived_flag() -> None:
    ct = ComtradeConnector(reporters=("CHN",), total_years=(2023,), bilateral_years=())
    body = copy.deepcopy(_ct_body(156, "M", 2023))
    body["data"][0].update({"isReported": False, "isAggregate": True, "legacyEstimationFlag": 0})
    batches, report = _ct_parse(ct, "total:CHN:M:2023", body)
    v = next(o for o in batches[0].observations if o.logical_key.endswith("primaryValue"))
    assert v.source_flags["isReported"] is False and v.source_flags["isAggregate"] is True  # raw flags kept
    assert v.source_flags["provider_estimation_flagged"] is False
    assert "provider_derived_aggregate" not in v.source_flags
    body["data"][0]["legacyEstimationFlag"] = 6
    batches, report = _ct_parse(ct, "total:CHN:M:2023", body)
    v = next(o for o in batches[0].observations if o.logical_key.endswith("primaryValue"))
    assert v.source_flags["provider_estimation_flagged"] is True and str(v.source_flags["legacyEstimationFlag"]) == "6"
    assert "provider_estimation_flagged" in {f.code for f in report.findings}


def test_display_geometry_disputed_only_by_type() -> None:
    from reva.world.space import display_match_status

    assert display_match_status({"TYPE": "Sovereign country", "NOTE_BRK": "x"}, matched=False) == "unmatched"
    assert display_match_status({"TYPE": "Indeterminate"}, matched=False) == "disputed"
    assert display_match_status({"TYPE": "Disputed"}, matched=True) == "disputed"
    assert display_match_status({"TYPE": "Country", "NOTE_BRK": "y"}, matched=True) == "reviewed_match"


# --------------------------------------------------------------------------- R-10
def test_r10_partner_codes_get_distinct_entities(db: Any) -> None:
    with db.worker.connect() as c:
        def resolve(code: str) -> Any:
            return repo.resolve_alias(c, "comtrade_partner", code)

        assert resolve("251") == entity_id("FRA")
        assert resolve("12") == entity_id("DZA")
        assert resolve("156") == entity_id("CHN")
        assert resolve("842") == entity_id("USA")
        assert resolve("0") == entity_id("WORLD")
        for code, country in (("250", "FRA"), ("840", "USA"), ("841", "USA"), ("344", "CHN")):
            assert resolve(code) not in (None, entity_id(country)), code
        assert len({resolve(c) for c in ("250", "251")}) == 2
        assert len({resolve(c) for c in ("840", "841", "842")}) == 3
        conf = c.execute(text(
            """SELECT confidence_kind FROM world.alias WHERE provider='comtrade_partner' AND external_key='251'
               ORDER BY recorded_at DESC LIMIT 1""")).scalar_one()
        assert conf == "reviewed_exact"
        upper = c.execute(text(
            """SELECT upper(valid_range) FROM world.alias WHERE provider='comtrade_partner' AND external_key='841'
               ORDER BY recorded_at DESC LIMIT 1""")).scalar_one()
        assert upper == date(1981, 1, 1)  # entryExpiredDate 1980-12-31 honoured


# --------------------------------------------------------------------------- R-14
class _StubConnector:
    """One synthetic item through the real runner (plan/fetch/parse/validate/publish)."""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def describe(self) -> Any:
        return WB

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        raw, _, _ = _synthetic(self.tag, {"a": "7"})
        return FetchPlan(connector=WB.name, items=(raw.item,), pagination="none")

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return _synthetic(self.tag, {"a": "7"})[0]

    def parse(self, raw: RawAcquisition) -> Any:
        return iter(_synthetic(self.tag, {"a": "7"})[1])

    def validate(self, batch: ObservationBatch) -> QualityReport:
        return QualityReport(outcome="pass")

    def next_cursor(self, plan: FetchPlan, outcomes: Any) -> Cursor:
        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes)})


async def test_r14_post_publish_failure_rolls_back_and_is_retried(db: Any) -> None:
    tag = f"r14-{uuid.uuid4().hex[:8]}"
    calls = {"n": 0}

    def flaky(conn: Any, store: Any, raw: Any, batches: Any, summary: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker crashed inside post-publish")
        repo.emit_outbox(conn, "test.fx2_post_publish", {"tag": tag, "acquisition_id": str(summary.acquisition_id)})

    stub = _StubConnector(tag)
    with pytest.raises(RuntimeError):
        await run_connector(stub, None, db.store, db.worker, persist_cursor=False, post_publish=flaky)

    def counts() -> tuple[int, int]:
        with db.worker.connect() as c:
            acq = c.execute(text("SELECT count(*) FROM evidence.acquisition WHERE request_fingerprint=:f"),
                            {"f": f"fx2:{tag}"}).scalar_one()
            ev = c.execute(text("SELECT count(*) FROM ops.outbox WHERE event_type='test.fx2_post_publish' "
                                "AND payload->>'tag' = :t"), {"t": tag}).scalar_one()
        return acq, ev

    assert counts() == (0, 0) and _obs(db, f"fx2:{tag}:a") == []  # the acquisition rolled back too
    rep = await run_connector(stub, None, db.store, db.worker, persist_cursor=False, post_publish=flaky)
    assert [o.status for o in rep.outcomes] == ["published"] and calls["n"] == 2
    assert counts() == (1, 1) and len(_obs(db, f"fx2:{tag}:a")) == 1
    # A third run is a duplicate acquisition: post-publish already committed with it.
    rep = await run_connector(stub, None, db.store, db.worker, persist_cursor=False, post_publish=flaky)
    assert [o.status for o in rep.outcomes] == ["unchanged"] and calls["n"] == 2


# --------------------------------------------------------------------------- R-06
def test_r06_partial_publication_clears_only_published_concepts_of_its_origin(db: Any) -> None:
    _ingest(db, f"r06-{uuid.uuid4().hex[:8]}", {"a": "9"})
    with db.worker.begin() as c:
        c.execute(text("DELETE FROM ops.dirty_node WHERE node_key IN (:g, :p)"),
                  {"g": f"belief:{GDP}", "p": f"belief:{POP}"})
        c.execute(text(
            """INSERT INTO ops.dirty_node(node_key, since_sequence, accumulated_error_bound, reason)
               VALUES (:g, 1, '{}'::jsonb, 'upstream_changed:test_fixture'),
                      (:p, 1, '{}'::jsonb, 'upstream_changed:mixed')"""),
            {"g": f"belief:{GDP}", "p": f"belief:{POP}"})
    publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])
    with db.worker.connect() as c:
        left = dict(c.execute(text("SELECT node_key, reason FROM ops.dirty_node WHERE node_key IN (:g, :p)"),
                              {"g": f"belief:{GDP}", "p": f"belief:{POP}"}).all())
    assert left[f"belief:{GDP}"] == "upstream_changed:test_fixture"  # not published: still dirty
    assert left[f"belief:{POP}"] == "upstream_changed:live"  # mixed marker narrowed to the other origin


# --------------------------------------------------------------------------- R-20
def test_r20_release_as_of_never_returns_release_with_later_cutoff(db: Any) -> None:
    _ingest(db, f"r20-{uuid.uuid4().hex[:8]}", {"a": "11"})
    r1 = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])
    with db.worker.connect() as c:
        assert release_as_of(c, None, "test_fixture")["id"] == r1.release_id
        pub_at = c.execute(text("SELECT published_at FROM ops.publication WHERE release_id=:r"),
                           {"r": r1.release_id}).scalar_one()
    # Simulate a historical row whose cutoff lies after its publication time (possible before
    # future cutoffs were rejected): an as-of between the two must not return it.
    with db.owner.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("UPDATE ops.publication SET published_at = :t WHERE release_id = :r"),
                  {"t": r1.knowledge_cutoff - timedelta(seconds=10), "r": r1.release_id})
    try:
        with db.worker.connect() as c:
            got = release_as_of(c, r1.knowledge_cutoff - timedelta(seconds=5), "test_fixture")
            assert got is None or (got["id"] != r1.release_id
                                   and got["knowledge_cutoff"] <= r1.knowledge_cutoff - timedelta(seconds=5))
            assert release_as_of(c, r1.knowledge_cutoff, "test_fixture")["id"] == r1.release_id
    finally:
        with db.owner.begin() as c:
            c.execute(text("SET LOCAL session_replication_role = replica"))
            c.execute(text("UPDATE ops.publication SET published_at = :t WHERE release_id = :r"),
                      {"t": pub_at, "r": r1.release_id})


# --------------------------------------------------------------------------- R-27
def test_r27_retracted_latest_revision_is_not_replaced_by_superseded_value(db: Any) -> None:
    tag = f"r27-{uuid.uuid4().hex[:8]}"
    _ingest(db, tag, {"a": "100", "b": "500"})
    _ingest(db, tag, {"a": "200", "b": "500"}, body_salt="v2")
    rev1, rev2 = _obs(db, f"fx2:{tag}:a")
    assert (rev1["v"], rev2["v"], rev2["revision_no"]) == ("100", "200", 2)
    with db.worker.begin() as c:
        retract_observation(c, rev2["id"], "provider withdrew the value", actor="human:fx2-test")
    with db.worker.connect() as c:
        rows = eligible_latest(c, _now(db.worker), ["test_fixture"], [POP], include_retracted=True)
        mine = {r["logical_key"]: r for r in rows if r["logical_key"].startswith(f"fx2:{tag}:")}
        plain = {r["id"] for r in eligible_latest(c, _now(db.worker), ["test_fixture"], [POP])}
    assert mine[f"fx2:{tag}:a"]["selection_status"] == "retracted"
    assert mine[f"fx2:{tag}:a"]["id"] == rev2["id"]
    assert rev1["id"] not in plain and rev2["id"] not in plain  # no silent fallback to revision 1
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])
    (est,) = _estimate_for_support(db, rel.release_id, tag)
    assert est["value"]["value"] == "500"
    assert f"retracted_lineage_excluded:{WB.source_key}" in est["discrepancy_flags"]
    alt = [a for a in est["alternatives"] if a.get("selection_status") == "retracted"]
    assert alt == [{"observation_id": str(rev2["id"]), "source_key": WB.source_key,
                    "logical_key": f"fx2:{tag}:a", "selection_status": "retracted"}]
    with db.worker.connect() as c:
        manifest_sha = c.execute(text("SELECT manifest_sha256 FROM belief.release WHERE id=:r"),
                                 {"r": rel.release_id}).scalar_one()
    manifest = json.loads(db.store.get("belief", manifest_sha))
    assert str(rev2["id"]) in manifest["excluded_retracted_lineage_heads"]


def test_r27_ineligible_latest_revision_falls_back_with_flag(db: Any) -> None:
    tag = f"r27b-{uuid.uuid4().hex[:8]}"
    _ingest(db, tag, {"a": "100"})
    spec2 = WB.model_copy(update={"version": f"0.0.0-fx2-{tag}", "parser_version": "fx2-parse"})
    with db.worker.begin() as c:
        act = repo.simple_activity("registry.seed", "review", "maintainer:fx2-test", {})
        repo.record_activity(c, act)
        register_connector_version(c, db.store, spec2, act.id)  # no eligibility decision: ineligible
    _ingest(db, tag, {"a": "300"}, spec=spec2, body_salt="v2")
    with db.worker.connect() as c:
        (row,) = [r for r in eligible_latest(c, _now(db.worker), ["test_fixture"], [POP])
                  if r["logical_key"] == f"fx2:{tag}:a"]
    assert row["revision_no"] == 1 and row["selection_status"] == "superseded_revision_selected"
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])
    (est,) = _estimate_for_support(db, rel.release_id, tag)
    assert est["value"]["value"] == "100" and "superseded_revision_selected" in est["discrepancy_flags"]


def test_r27_prior_estimate_input_respects_origin_and_cutoff(db: Any) -> None:
    _ingest(db, f"r27c-{uuid.uuid4().hex[:8]}", {"a": "1"})
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])
    with db.worker.connect() as c:
        prior = c.execute(text("SELECT id, subject_id, concept_version_id, support_id, method_version_id "
                               "FROM belief.estimate WHERE release_id=:r LIMIT 1"),
                          {"r": rel.release_id}).mappings().one()
        meta = c.execute(text("SELECT manifest_sha256, ontology_manifest_sha256, model_manifest_sha256 "
                              "FROM belief.release WHERE id=:r"), {"r": rel.release_id}).mappings().one()

    def draft(c: Any, origin: str, cutoff: datetime) -> Any:
        act = repo.simple_activity("test.belief", "belief", "test:fx2", {})
        repo.record_activity(c, act)
        ctx = repo.create_context(c, mode="belief", origin=origin, manifest_sha256=meta["manifest_sha256"],
                                  knowledge_cutoff=cutoff)
        rid = c.execute(text(
            """INSERT INTO belief.release(context_id, origin, manifest_sha256, ontology_manifest_sha256,
                 model_manifest_sha256, knowledge_cutoff, method, provenance_activity_id)
               VALUES (:c, :o, :m, :om, :mm, :cut, 'test', :a) RETURNING id"""),
            {"c": ctx, "o": origin, "m": meta["manifest_sha256"], "om": meta["ontology_manifest_sha256"],
             "mm": meta["model_manifest_sha256"], "cut": cutoff, "a": act.id}).scalar_one()
        return c.execute(text(
            """INSERT INTO belief.estimate(release_id, subject_id, concept_version_id, support_id,
                 method_version_id, modality, value, uncertainty, provenance_activity_id)
               VALUES (:r, :s, :c, :sp, :m, 'selected_report', '{"kind":"missing","reason":"unavailable"}',
                       '{"status":"unquantified"}', :a) RETURNING id"""),
            {"r": rid, "s": prior["subject_id"], "c": prior["concept_version_id"], "sp": prior["support_id"],
             "m": prior["method_version_id"], "a": act.id}).scalar_one()

    with db.worker.connect() as c, c.begin():
        live_est = draft(c, "live", _now(db.worker))
        with pytest.raises(ValidationProblem, match="non-live prior"):
            add_prior_estimate_input(c, live_est, prior["id"])
        early = draft(c, "test_fixture", rel.knowledge_cutoff - timedelta(seconds=1))
        with pytest.raises(ValidationProblem, match="later knowledge cutoff"):
            add_prior_estimate_input(c, early, prior["id"])
        c.rollback()


# --------------------------------------------------------------------------- R-28
def test_r28_parser_version_change_reprocesses_same_bytes(db: Any) -> None:
    tag = f"r28-{uuid.uuid4().hex[:8]}"
    raw, _, _ = _synthetic(tag, {"a": "3"})
    k1 = acquisition_idempotency_key(WB, raw, "0" * 64)
    k2 = acquisition_idempotency_key(WB.model_copy(update={"parser_version": "wb-parse-9"}), raw, "0" * 64)
    assert k1 != k2
    first = _ingest(db, tag, {"a": "3"})
    dup = _ingest(db, tag, {"a": "3"})
    assert not first.duplicate_acquisition and dup.duplicate_acquisition
    reparsed = _ingest(db, tag, {"a": "3"}, spec=WB.model_copy(update={"parser_version": "wb-parse-fx2"}))
    assert not reparsed.duplicate_acquisition and reparsed.acquisition_id != first.acquisition_id
    assert reparsed.unchanged == 1  # same semantics: no new revision, but the bytes were reprocessed



# --------------------------------------------------------------------------- population from Eurostat
def _ensure_reapproved(db: Any, name: str) -> None:
    """If the promotion was retired (rollback drill elsewhere in the session), append a named-human
    re-approval with the original applicability (append-only; latest decision wins)."""
    from reva.evidence.locks import take_evidence_write_lock

    with db.worker.begin() as c:
        take_evidence_write_lock(c)
        cv = c.execute(text("SELECT id FROM evidence.connector_version WHERE name=:n ORDER BY recorded_at DESC "
                            "LIMIT 1"), {"n": name}).scalar_one()
        concept = repo.concept_version_id(c, "demography.population.jan1_age_sex", "1.0.0")
        latest = c.execute(text(
            """SELECT decision, applicability, policy_version FROM evidence.eligibility
               WHERE connector_version_id=:cv AND concept_version_id=:c ORDER BY recorded_at DESC, id DESC LIMIT 1"""),
            {"cv": cv, "c": concept}).mappings().one()
        if latest["decision"] == "eligible":
            return
        act = repo.simple_activity("evidence.eligibility_review", "review", "human:fx2-test", {"connector": name})
        repo.record_activity(c, act)
        c.execute(text(
            """INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                 policy_version, review_activity_id)
               VALUES (:cv, :c, CAST(:ap AS jsonb), 'eligible', :p, :a)"""),
            {"cv": cv, "c": concept, "ap": json.dumps(dict(latest["applicability"])),
             "p": latest["policy_version"], "a": act.id})


async def test_population_from_promoted_eurostat_connector(db: Any) -> None:
    from reva.autonomy.jobs import handle_discovery, promoted_connectors
    from reva.operations.context import JobContext
    from reva.population.service import build_population, load_cells

    jid = uuid.uuid4()
    with db.owner.begin() as conn:
        conn.execute(text("""INSERT INTO ops.job(id, job_type, payload, idempotency_key, status, worker_id,
                                 lease_until, fencing_token, attempts)
                             VALUES (:i, 'discovery', '{}'::jsonb, :k, 'running', 'fx2-worker',
                                     clock_timestamp() + interval '10 minutes', 7, 1)"""),
                     {"i": jid, "k": f"fx2-discovery-{jid}"})
    ctx = JobContext(job_id=jid, job_type="discovery", worker_id="fx2-worker", fencing_token=7, attempt=1,
                     settings=db.settings, worker_engine=db.worker, sim_engine=None, store=db.store)
    out = handle_discovery({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}, ctx)
    assert out["status"] in ("promoted", "no_gap"), out
    name = "eurostat.demo_pjan.fr"
    _ensure_reapproved(db, name)  # an earlier module may have rolled the promotion back (shared session DB)
    connector = promoted_connectors(db.worker, db.store)[name]
    rep = await run_connector(connector, FixtureHttp(name), db.store, db.worker, persist_cursor=False)
    assert not rep.errors, rep.errors[:3]
    rel = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test",
                          concepts=["demography.population.jan1_age_sex"])
    assert rel.estimates >= 309

    cells = load_cells(db.worker, geo_entity="FRA", year=2023, cutoff=_now(db.worker), origin="test_fixture")
    codes = {(c.sex_code, c.age_code) for c in cells}
    assert {("T", "TOTAL"), ("F", "Y_LT1"), ("M", "Y99"), ("F", "Y_OPEN"), ("F", "UNK")} <= codes
    assert {c.sex_code for c in cells} == {"T", "M", "F"}
    assert all(c.flags == ("p",) for c in cells)  # Eurostat 'p' (provisional) flows from source_flags.status

    res = build_population(db.worker, db.store, n_records=100_000, origin="test_fixture")
    assert res["records"] == 100_000
    with db.worker.connect() as c:
        row = c.execute(text("SELECT recipe_sha256, record_count, privacy_class FROM model.population_dataset "
                             "WHERE id = :i"), {"i": res["population_dataset_id"]}).mappings().one()
    assert row["record_count"] == 100_000 and row["privacy_class"] == "synthetic_public_aggregate_derived"
    recipe = json.loads(db.store.get("models", row["recipe_sha256"]))
    assert sum(int(leaf["count"]) for leaf in recipe["leaf_cells"]) == 68_277_210
