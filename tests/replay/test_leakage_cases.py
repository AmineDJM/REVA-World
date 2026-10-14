"""The six required future-information leakage cases are blocked in strict mode,
available-then reconstruction excludes unproven records, strict never downgrades,
and the retrospective mode is labelled."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres import repositories as repo
from reva.cli.seed import entity_id
from reva.evaluation.cutoff import CutoffRepository
from reva.evaluation.leakage import (
    RETROSPECTIVE_DISCLAIMER,
    GuardMode,
    InputRef,
    LeakageGuard,
    RefKind,
    alias_ref,
    claim_ref,
    mapping_ref,
    model_version_ref,
    observation_ref,
    population_ref,
    scaler_ref,
)
from reva.evaluation.replay import (
    available_then_reconstruction,
    operational_as_known,
    revised_data_retrospective,
    run_replay,
)
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.errors import CapabilityGap, LeakageBlocked, ValidationProblem

pytestmark = pytest.mark.db

POP = "demography.population.wdi_midyear_total"
GDP = "economics.gdp.nominal_current_usd"
VINTAGES = Path(__file__).parents[1] / "fixtures" / "vintages"


def _loader():
    if "vintage_loader" not in sys.modules:
        spec = importlib.util.spec_from_file_location("vintage_loader", VINTAGES / "vintage_loader.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["vintage_loader"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["vintage_loader"]


vl = _loader()


def _now(engine) -> datetime:
    with engine.connect() as c:
        return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def _activity(conn, kind: str, origin: str = "review"):
    act = repo.simple_activity(kind, origin, "maintainer:test-leakage", {})
    repo.record_activity(conn, act)
    return act.id


async def test_six_leakage_cases_blocked_in_strict_mode(db):
    # (1) a revised historical value: vintage 1 known at the cutoff, the revision arrives later
    name = "pop_fra_1975_1976_leakage"
    await vl.ingest_vintage(db.worker, db.store, name, "v1")
    with db.worker.begin() as conn:  # identity/ontology recorded before the cutoff
        act = _activity(conn, "test.alias")
        provider = f"test.boundaries.{uuid.uuid4().hex[:8]}"
        conn.execute(text(
            """INSERT INTO world.alias(entity_id, provider, external_key, mapping_version, confidence_kind,
                 provenance_activity_id) VALUES (:e, :p, 'XBOUND', '1.0.0', 'reviewed_exact', :a)"""),
            {"e": entity_id("FRA"), "p": provider, "a": act})
    cutoff = _now(db.worker)

    await vl.ingest_vintage(db.worker, db.store, name, "v2")
    with db.worker.begin() as conn:
        act = _activity(conn, "test.future_records")
        # (2) renamed country boundary: new alias version and a new ontology mapping recorded after cutoff
        new_alias = conn.execute(text(
            """INSERT INTO world.alias(entity_id, provider, external_key, mapping_version, confidence_kind,
                 provenance_activity_id) VALUES (:e, :p, 'XBOUND', '2.0.0-renamed', 'reviewed_exact', :a)
               RETURNING id"""), {"e": entity_id("DZA"), "p": provider, "a": act}).scalar_one()
        target = repo.concept_version_id(conn, POP, "1.0.0")
        new_mapping = conn.execute(text(
            """INSERT INTO ontology.mapping(mapping_key, source_ref, target_concept_id, mapping_kind, version,
                 transform_spec, applicability, approval, review_activity_id)
               VALUES (:k, '{"provider":"test"}', :t, 'exact', '2.0.0', '{"kind":"identity"}', '{}', 'approved', :a)
               RETURNING id"""), {"k": f"test.boundary_rename.{uuid.uuid4().hex[:8]}", "t": target, "a": act}
        ).scalar_one()
        # (3) a later-fitted scaler (fit manifest committed as a models artifact)
        scaler_manifest = {"id": f"scaler:log_standardise:{uuid.uuid4().hex[:8]}", "transform": "log_standardise",
                           "fitted_on_data_until": (cutoff + timedelta(days=30)).isoformat()}
        scaler_sha = repo.store_json_artifact(conn, db.store, scaler_manifest, "models",
                                              schema_version="fitted-scaler/1")
        scaler_recorded = conn.execute(text("SELECT recorded_at FROM meta.artifact WHERE sha256=:s"),
                                       {"s": scaler_sha}).scalar_one()
        # (4) a later-trained model
        spec_sha = repo.store_json_artifact(conn, db.store, {"model": "test.trained", "n": uuid.uuid4().hex},
                                            "models", schema_version="model-spec/1")
        train_sha = repo.store_json_artifact(conn, db.store, {"training": "window", "n": uuid.uuid4().hex},
                                             "models", schema_version="training-manifest/1")
        model_id = conn.execute(text(
            """INSERT INTO model.version(model_key, version, kind, spec_sha256, code_digest, input_ports,
                 output_ports, applicability, assumptions, training_manifest_sha256, training_cutoff,
                 approval_status, provenance_activity_id)
               VALUES ('forecast.test_trained', :v, 'forecast', :s, 'test', '[]', '[]', '{}', '[]', :tm,
                       clock_timestamp(), 'candidate', :a) RETURNING id"""),
            {"v": f"1.0.0-{uuid.uuid4().hex[:8]}", "s": spec_sha, "tm": train_sha, "a": act}).scalar_one()
        # (5) a later paper coefficient
        claim_id = conn.execute(text(
            """INSERT INTO evidence.claim(claim_key, version, card, evidence_kind, status, extraction_method,
                 provenance_activity_id)
               VALUES (:k, 1, CAST(:c AS jsonb), 'quasi_experimental', 'reviewed', 'manual_review', :a)
               RETURNING id"""),
            {"k": f"test.later_paper.{uuid.uuid4().hex[:8]}",
             "c": json.dumps({"coefficient": "-0.25", "note": "test card recorded after cutoff"}), "a": act}
        ).scalar_one()
        # (6) a subsequent population recalibration
        recipe = repo.store_json_artifact(conn, db.store, {"recipe": "recalibration", "n": uuid.uuid4().hex},
                                          "models", schema_version="population-recipe/1")
        pctx = repo.create_context(conn, mode="population", origin="test_fixture", manifest_sha256=recipe,
                                   knowledge_cutoff=_now(db.worker))
        pop_id = conn.execute(text(
            """INSERT INTO model.population_dataset(context_id, recipe_sha256, supported_attributes, diagnostics,
                 seed_plan, privacy_class, provenance_activity_id)
               VALUES (:c, :r, '{}', '{}', '{}', 'synthetic_public_aggregate_derived', :a) RETURNING id"""),
            {"c": pctx, "r": recipe, "a": act}).scalar_one()

    with db.api.connect() as conn:
        rev2 = conn.execute(text("SELECT id FROM evidence.observation WHERE logical_key = :k AND revision_no = 2"),
                            {"k": "wb:SP.POP.TOTL:FRA:1976"}).scalar_one()
        rev1 = conn.execute(text("SELECT id FROM evidence.observation WHERE logical_key = :k AND revision_no = 1"),
                            {"k": "wb:SP.POP.TOTL:FRA:1976"}).scalar_one()
        refs = {
            "revised_historical_value": observation_ref(conn, rev2),
            "renamed_boundary_alias": alias_ref(conn, new_alias),
            "renamed_boundary_mapping": mapping_ref(conn, new_mapping),
            "later_fitted_scaler": scaler_ref({**scaler_manifest, "recorded_at": scaler_recorded}),
            "later_trained_model": model_version_ref(conn, model_id),
            "later_paper_coefficient": claim_ref(conn, claim_id),
            "subsequent_population_recalibration": population_ref(conn, pop_id),
        }
        guard = LeakageGuard(cutoff)  # strict by default
        assert guard.mode is GuardMode.strict
        for case, ref in refs.items():
            with pytest.raises(LeakageBlocked) as ei:
                guard.check(ref)
            assert ei.value.details["kind"] == ref.kind.value, case
            assert ei.value.details["mode"] == "strict", case
            assert ei.value.code == "future_information_blocked"
        assert {r.kind for r in refs.values()} == set(RefKind)
        assert guard.mode is GuardMode.strict  # blocking never downgrades the guard
        assert all(not v.admitted for v in guard.verdicts)
        assert guard.check(observation_ref(conn, rev1)).admitted  # the as-known vintage is fine

        # The cutoff-scoped repository never hands these records out.
        cr = CutoffRepository(conn, cutoff, "as_known", origins=["test_fixture"])
        rows = {r["logical_key"]: r for r in cr.observations([POP]).rows}
        assert rows["wb:SP.POP.TOTL:FRA:1976"]["revision_no"] == 1
        assert rows["wb:SP.POP.TOTL:FRA:1976"]["value"]["value"] == "52909000"
        assert cr.resolve_alias(provider, "XBOUND") == entity_id("FRA")  # not the renamed mapping
        assert str(new_alias) not in {str(a["id"]) for a in cr.aliases(provider)}
        assert str(new_mapping) not in {str(m["id"]) for m in cr.mappings()}
        models = {str(m["id"]) for m in cr.model_versions()}
        assert str(model_id) not in models
        assert any(m["model_key"] == "belief.source_select" for m in cr.model_versions("belief_method"))
        assert str(claim_id) not in {str(c["id"]) for c in cr.claims()}
        assert str(pop_id) not in {str(p["id"]) for p in cr.population_datasets()}


def test_scaler_and_model_rules_need_proof_and_never_guess(db):
    cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    g = LeakageGuard(cutoff)
    early = datetime(2019, 6, 1, tzinfo=UTC)
    late = datetime(2021, 1, 1, tzinfo=UTC)
    # fitted on old data but committed after the cutoff: did not exist operationally
    with pytest.raises(LeakageBlocked):
        g.check(InputRef(RefKind.fitted_scaler, "s1", recorded_at=late, fitted_on_data_until=early))
    # unknown fit window is never assumed early
    with pytest.raises(LeakageBlocked):
        g.check(InputRef(RefKind.fitted_scaler, "s2", recorded_at=early))
    with pytest.raises(LeakageBlocked):
        g.check(InputRef(RefKind.trained_model, "m1", recorded_at=early))
    assert g.check(InputRef(RefKind.trained_model, "m2", recorded_at=early, training_cutoff=early)).admitted
    # a later reconstruction may refit on proven-old data; it still blocks later data
    a = LeakageGuard(cutoff, mode="available_then")
    assert a.check(InputRef(RefKind.fitted_scaler, "s1", recorded_at=late, fitted_on_data_until=early)).admitted
    with pytest.raises(LeakageBlocked):
        a.check(InputRef(RefKind.fitted_scaler, "s3", recorded_at=early, fitted_on_data_until=late))
    with pytest.raises(LeakageBlocked):  # observation without availability proof
        a.check(InputRef(RefKind.observation_revision, "o1", recorded_at=early, source_available_at=early))
    with pytest.raises(ValidationProblem):
        LeakageGuard(datetime(2020, 1, 1))  # naive cutoff refused


def test_strict_never_downgrades(db):
    cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    future = InputRef(RefKind.observation_revision, "o-future", recorded_at=datetime(2026, 1, 1, tzinfo=UTC))
    g = LeakageGuard(cutoff)
    for _ in range(2):
        with pytest.raises(LeakageBlocked):
            g.check(future)
    assert g.mode is GuardMode.strict and g.blocking
    with pytest.raises(AttributeError):
        g.mode = GuardMode.revised_data_retrospective  # type: ignore[misc]
    with pytest.raises(CapabilityGap):
        LeakageGuard(cutoff, mode="lenient")
    # A downgrade must be explicit and is labelled on every verdict.
    r = LeakageGuard(cutoff, mode="revised_data_retrospective")
    v = r.check(future)
    assert v.admitted and v.would_block_in_strict and v.label == "revised_data_retrospective"
    assert r.label == "revised_data_retrospective" and r.flagged() == [v]
    # Unknown replay modes are capability gaps, never a silent fallback.
    with db.api.connect() as conn:
        with pytest.raises(CapabilityGap):
            run_replay(conn, "strict", cutoff)
        with pytest.raises(ValidationProblem):  # future cutoffs are refused, not clamped
            run_replay(conn, "operational_as_known", datetime(2999, 1, 1, tzinfo=UTC))


async def test_available_then_excludes_records_without_availability_proof(db):
    name = "pop_fra_1970_1971_archive"
    await vl.ingest_vintage(db.worker, db.store, name, "current")  # today's vintage: no availability proof
    await vl.ingest_vintage(db.worker, db.store, name, "archived_2015")  # archived vintage with proof
    with db.api.connect() as conn:
        m = available_then_reconstruction(conn, datetime(2016, 1, 1, tzinfo=UTC), origins=["test_fixture"],
                                          concepts=[POP])
        assert m["mode"] == m["label"] == "available_then_reconstruction"
        assert "not what REVA actually believed" in m["honesty_statement"]
        assert m["status"] == "ok"
        keys = {i["observation_id"]: i for i in m["inputs"]}
        mine = {i["logical_key"]: i for i in keys.values() if i["logical_key"].startswith("wb:SP.POP.TOTL:FRA:197")}
        assert set(mine) == {"wb:SP.POP.TOTL:FRA:1970", "wb:SP.POP.TOTL:FRA:1971"}
        for i in mine.values():
            assert i["availability_proof"]["kind"] == "source_availability"
            assert i["availability_proof"]["source_available_at"].startswith("2015-07-01")
            assert i["source_published_at"] is None
        values = {o["observation_id"]: o["value"]["value"] for o in m["outputs"]}
        assert values[mine["wb:SP.POP.TOTL:FRA:1970"]["observation_id"]] == "50772000"  # archived, not current
        excl = {e["reason"]: e["count"] for e in m["exclusions"]}
        assert excl["no_availability_proof"] >= 2 and m["excluded_without_proof"] == excl["no_availability_proof"]
        # algorithm design is dated separately from availability
        design = m["algorithm_design"]
        assert design["method"] == "belief.source_select@1.0.0" and design["method_registered_at"] > "2026"
        assert m["availability"]["latest_source_available_at"].startswith("2015-07-01")

        early = available_then_reconstruction(conn, datetime(2014, 1, 1, tzinfo=UTC), origins=["test_fixture"],
                                              concepts=[POP])
        assert early["mode"] == "available_then_reconstruction"  # no fallback to another mode
        assert early["status"] == "no_available_inputs" and early["outputs"] == []
        assert {e["reason"]: e["count"] for e in early["exclusions"]}["available_after_cutoff"] >= 2

        # operationally, REVA had no release in 2016: a typed result, not an empty success
        op = operational_as_known(conn, datetime(2016, 1, 1, tzinfo=UTC), origin="test_fixture")
        assert op["status"] == "no_release_at_cutoff" and op["outputs"] == []


async def test_revised_data_retrospective_is_labelled_everywhere(db):
    c = builtin_connectors()["worldbank.wdi.indicators"]
    await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)
    with db.api.connect() as conn:
        m = revised_data_retrospective(conn, origins=["test_fixture"], concepts=[POP, GDP], min_train=5,
                                       horizons=(1, 2))
    assert m["mode"] == m["label"] == "revised_data_retrospective"
    assert RETROSPECTIVE_DISCLAIMER == "not a real-time backtest; forecasts were not issued at these origins"
    assert RETROSPECTIVE_DISCLAIMER in m["honesty_statement"] and m["disclaimer"] == RETROSPECTIVE_DISCLAIMER
    assert m["real_time_backtest"] is False and m["status"] == "ok"
    assert m["outputs"] and all(f["label"] == "revised_data_retrospective" for f in m["outputs"])
    assert m["scores"] and all(s["label"] == "revised_data_retrospective" for s in m["scores"].values())
    assert "forecast.last_value@1.0.0" in m["scores"]  # naive baseline always present
    # every fold used values recorded long after its origin, and says so
    assert all(f["inputs_recorded_after_origin"] > 0 for f in m["outputs"])
    assert m["design"]["inputs_recorded_after_fold_origin"] > 0
    # WDI 2000-2024 annual series for 4 countries x 2 concepts, origins from 2004
    fold = next(f for f in m["outputs"] if f["origin_year"] == 2010 and f["horizon"] == 1
                and f["series"]["concept_id"] == POP)
    assert fold["target_year"] == 2011 and fold["train_years"] == [2000, 2010]
    lv = next(p for p in fold["predictions"] if p["model"] == "forecast.last_value@1.0.0")
    assert lv["abs_error_latest_revised"] == pytest.approx(abs(lv["point"] - fold["actual_latest_revised"]))
