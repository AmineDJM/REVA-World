"""Effect estimates (V5, PRED-EFFECT): vocabularies vs migration 0019, validation rules, curated literature entries,
quote re-check, uncertainty helpers, connector spec. No database."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from reva.effects import vocab
from reva.effects.curation import (
    LITERATURE_DIR,
    load_entries,
    parse_entry,
    pdftotext_available,
    text_check,
    to_estimates,
)
from reva.effects.model import EffectEstimateIn, Horizon, IrfPoint, Reject, RevaRun, Uncertainty, validate_estimate
from reva.effects.uncertainty import normal_approx, sample
from reva.effects.variables import Resolver, load_variables

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ENTRY = ROOT / "tests" / "fixtures" / "effects" / "literature" / "synthetic_okun_fixture.yaml"
FIXTURE_DOC = ROOT / "tests" / "fixtures" / "effects" / "synthetic_paper.txt"
SHA = "a" * 64


def _migration() -> ModuleType:
    p = ROOT / "migrations" / "versions" / "0019_effect_estimates.py"
    spec = importlib.util.spec_from_file_location("m0019", p)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_migration_restates_the_vocabularies() -> None:
    m = _migration()
    assert (m.revision, m.down_revision) == ("0019_effect_estimates", "0018_retraction_reasons")
    assert m.TRANSFORMS == vocab.TRANSFORMS
    assert m.MEASURE_UNITS == vocab.MEASURE_UNITS
    assert m.UNITS == vocab.UNITS
    assert m.METHODS == vocab.METHODS
    assert m.RETRACTION_REASONS == vocab.RETRACTION_REASONS
    for allowed in vocab.MEASURE_UNITS.values():
        assert allowed is None or set(allowed) <= set(vocab.UNITS)


def test_variables_registry_links_only_existing_atlas_concepts() -> None:
    from reva.atlas.catalog import cached_catalog

    vs = load_variables()
    known = set(cached_catalog().concepts)
    assert len(vs.items) >= 15
    for v in vs.items.values():
        assert v.labels["en"] and v.labels["fr"] and v.definition
        assert set(v.atlas_concepts) <= known, v.id
    assert vs.by_atlas_concept("imf.cpi_index") == ["prices.consumer_level"]


RES = Resolver(load_variables(), atlas=None, event_types=None)


def _est(**kw) -> EffectEstimateIn:  # type: ignore[no-untyped-def]
    base = dict(
        estimate_id="e1",
        entry_id="paper_2020",
        cause_kind="variable",
        cause_id="output.gap",
        cause_definition="output gap",
        cause_transform="gap",
        effect_kind="variable",
        effect_id="labour.unemployment_gap",
        effect_definition="unemployment gap",
        effect_transform="gap",
        measure="slope",
        unit="pp_per_percent",
        sign="negative",
        method="time_series_regression",
        as_of="2013-01-31",
        point=-0.411,
        point_kind="point",
        as_printed="-0.411",
        uncertainty=Uncertainty(se=0.024, se_as_printed="0.024", status="reported"),
        citation={"text": "Someone (2013)."},
        locator="Table 1",
        quote="β -0.411*** (0.024)",
        transcription="transcribed",
        raw_sha256=SHA,
    )
    base.update(kw)
    return EffectEstimateIn(**base)  # type: ignore[arg-type]


def _ok(e: EffectEstimateIn, kind: str = "publication") -> dict[str, Any]:
    return validate_estimate(e, source_kind=kind, source_key="effects.literature", raws={SHA}, resolver=RES)


def _reason(e: EffectEstimateIn, kind: str = "publication") -> str:
    with pytest.raises(Reject) as ex:
        _ok(e, kind)
    return ex.value.reason


def test_valid_publication_row_and_key() -> None:
    row = _ok(_est())
    assert row["estimate_key"] == "pub:effects.literature:paper_2020:e1"
    assert row["transcription"] == "transcribed" and row["uncertainty_status"] == "reported"
    assert len(row["content_sha256"]) == 64
    assert _ok(_est())["content_sha256"] == row["content_sha256"]  # deterministic
    assert _ok(_est(applicability="other note"))["content_sha256"] != row["content_sha256"]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"unit": "currency_per_currency"}, "unit_not_allowed_for_measure"),
        ({"sign": "positive"}, "sign_mismatch"),
        ({"point": -0.4, "as_printed": "-0.411"}, "point_differs_from_as_printed"),
        ({"quote": "β -0.41 (0.024)"}, "quote_missing_number"),
        ({"uncertainty": Uncertainty(se=-1.0, status="reported")}, "se_not_positive"),
        ({"uncertainty": Uncertainty(se=0.03, se_as_printed="0.024", status="reported")}, "se_differs_from_as_printed"),
        (
            {
                "uncertainty": Uncertainty(
                    ci_low=-0.3, ci_high=-0.1, ci_level=0.95, interval_kind="confidence", status="reported"
                )
            },
            "point_outside_interval",
        ),
        (
            {
                "uncertainty": Uncertainty(
                    ci_low=-0.5, ci_high=-0.3, ci_level=95, interval_kind="confidence", status="reported"
                )
            },
            "ci_level_out_of_range",
        ),
        (
            {"uncertainty": Uncertainty(ci_low=-0.5, ci_high=-0.3, interval_kind="confidence", status="reported")},
            "ci_level_required",
        ),
        ({"uncertainty": Uncertainty(status="reported")}, "reported_uncertainty_missing"),
        ({"uncertainty": Uncertainty(se=0.02, status="not_reported")}, "uncertainty_given_but_not_reported"),
        ({"cause_id": "no.such_variable"}, "unknown_cause"),
        ({"effect_kind": "event"}, "invalid_effect_kind"),
        ({"cause_kind": "event_type", "cause_id": "coup"}, "event_cause_needs_occurrence"),
        ({"raw_sha256": "b" * 64}, "raw_not_in_load"),
        ({"raw_sha256": None}, "raw_sha256_required"),
        ({"locator": None}, "locator_required"),
        ({"citation": None}, "citation_required"),
        ({"measure": "response", "unit": "percent"}, "response_needs_shock"),
        ({"horizon": Horizon("long_run", 4, "year")}, "horizon_value_not_allowed"),
        ({"horizon": Horizon("at_horizon", 4, None)}, "horizon_value_and_unit_go_together"),
        ({"irf": (IrfPoint(1, "quarter", -0.2), IrfPoint(0, "quarter", -0.1))}, "irf_not_ascending"),
        ({"scope": {"countries": ["France"]}}, "invalid_scope_countries"),
        ({"scope": {"region": "x"}}, "invalid_scope_key"),
        ({"as_of": "2013-13-01"}, "invalid_as_of"),
        ({"transcription": "computed"}, "publication_cannot_be_computed"),
        ({"transcription": "machine_readable"}, "selector_required"),
        ({"shock_profile": "forever"}, "invalid_shock_profile"),
    ],
)
def test_rejections(kw, reason) -> None:  # type: ignore[no-untyped-def]
    assert _reason(_est(**kw)) == reason


def test_shock_profile_is_stored_in_meta() -> None:
    row = _ok(_est(shock_profile="one_time_impulse", meta={"n_obs": 3}))
    assert row["meta"] == {"n_obs": 3, "shock_profile": "one_time_impulse"}
    assert _ok(_est())["meta"] is None
    for entry in load_entries():
        for est in entry.estimates:
            assert est.get("shock_profile") in vocab.SHOCK_PROFILES, (entry.id, est["id"])


def test_negated_printed_transform_and_sign_only() -> None:
    e = _est(point=-0.112, as_printed="0.112", quote="ψ 0.112 (0.057)",
             uncertainty=Uncertainty(se=0.057, status="reported"),
             curation={"printed_transform": "negated"})  # fmt: skip
    assert _ok(e)["point"] == -0.112
    assert _reason(dataclasses.replace(e, curation={})) == "point_differs_from_as_printed"
    sign_only = _est(point=None, point_kind=None, as_printed="negative", quote="a negative relation",
                     uncertainty=Uncertainty(status="not_reported"))  # fmt: skip
    assert _ok(sign_only)["point"] is None


def test_reva_run_and_identity_rows() -> None:
    run = RevaRun("es-2026-10-17-001", "abc123", "atlas:snap-1", "2026-10-17", replicates=("pub:x:y:z",))
    e = _est(
        estimate_id="lp_h4",
        entry_id=None,
        cause_kind="event_type",
        cause_id="financial_crisis",
        cause_transform="occurrence",
        effect_kind="atlas_concept",
        effect_id="imf.gdp_real_growth",
        effect_transform="growth_rate",
        measure="response",
        unit="percent",
        shock_size=1,
        shock_unit="occurrence",
        point=-4.1,
        as_printed=None,
        uncertainty=Uncertainty(se=0.9, status="reported"),
        horizon=Horizon("at_horizon", 4, "year"),
        method="local_projection",
        citation=None,
        locator=None,
        quote=None,
        transcription=None,
        raw_sha256=None,
        run=run,
    )
    row = _ok(e, "reva_run")
    assert row["estimate_key"] == "run:es-2026-10-17-001:lp_h4" and row["transcription"] == "computed"
    assert row["run"]["replicates"] == ["pub:x:y:z"]
    assert _reason(dataclasses.replace(e, run=None), "reva_run") == "run_required"
    ident = dataclasses.replace(e, run=None, entry_id="cpi_weights", identity=None)
    assert _reason(ident, "identity") == "identity_required"


def test_every_curated_entry_validates() -> None:
    from reva.effects.variables import default_resolver

    entries = load_entries()
    assert len(entries) >= 7
    res = default_resolver()
    n = 0
    keys: set[str] = set()
    for entry in entries:
        assert entry.document["sha256"] and len(entry.document["sha256"]) == 64, entry.id
        assert entry.curation["review_status"] == "maintainer_review_pending"
        ests, refused = to_estimates(entry, raw_sha256=entry.document["sha256"])
        assert not refused
        for e in ests:
            row = validate_estimate(
                e, source_kind="publication", source_key=entry.source_key, raws={entry.document["sha256"]},
                resolver=res,
            )  # fmt: skip
            assert row["estimate_key"] not in keys
            keys.add(row["estimate_key"])
            assert row["quote"] and row["locator"] and row["as_printed"]
            n += 1
        for est in entry.estimates:
            assert est.get("pdf_page"), (entry.id, est["id"])
    assert n >= 50


def test_text_check_finds_quotes_and_refuses_mismatches() -> None:
    body = FIXTURE_DOC.read_bytes()
    entry = parse_entry(FIXTURE_ENTRY.read_bytes(), FIXTURE_ENTRY)
    assert entry.document["sha256"] == hashlib.sha256(body).hexdigest()
    tc = text_check(entry, body)
    assert tc.tool == "utf-8 text" and not tc.problems and set(tc.found.values()) == {1}
    ests, refused = to_estimates(entry, raw_sha256=entry.document["sha256"], check=tc)
    assert not refused and {e.transcription for e in ests} == {"transcribed_double_checked"}
    entry.estimates[0]["quote"] = "Atlantis -0.421*** (0.050) 31"  # a typo in a transcription
    entry.estimates[1]["pdf_page"] = 2
    tc = text_check(entry, body)
    assert tc.problems == {"atlantis": "quote_not_in_source", "lemuria": "locator_page_mismatch"}
    ests, refused = to_estimates(entry, raw_sha256=entry.document["sha256"], check=tc)
    assert set(refused) == {"atlantis", "lemuria"} and [e.estimate_id for e in ests] == ["oil_impact"]


@pytest.mark.skipif(not pdftotext_available(), reason="pdftotext not installed")
def test_pdf_text_extraction_runs_on_stdin() -> None:
    from reva.effects.curation import pdf_pages_text

    # a minimal one-page PDF with the text "0.36"
    pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
        b"endobj\n4 0 obj<</Length 36>>stream\nBT /F1 12 Tf 20 100 Td (Total 0.36) Tj ET\nendstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    pages = pdf_pages_text(pdf)
    assert len(pages) == 1 and "Total 0.36" in pages[0]


def test_uncertainty_bases() -> None:
    se = {"point": -0.4, "uncertainty": {"se": 0.1}}
    assert normal_approx(se) == {"mean": -0.4, "sd": 0.1, "basis": "se", "note": "sd = reported standard error"}
    ci = normal_approx({"point": 1.0, "uncertainty": {"ci_low": 0.0, "ci_high": 2.0, "ci_level": 0.95,
                                                      "interval_kind": "confidence"}})  # fmt: skip
    assert ci["basis"] == "ci" and abs(ci["sd"] - 1 / 1.959964) < 1e-4
    rng = range_ = normal_approx({"point": 0.03, "uncertainty": {"ci_low": -0.06, "ci_high": 0.17,
                                                                "interval_kind": "range_across_estimates"}})  # fmt: skip
    assert range_["basis"] == "range" and rng["sd"] == pytest.approx(0.0575)
    disp = normal_approx({"point": -5.03, "uncertainty": {}, "meta": {"sd_across_estimates": 9.3}})
    assert disp["basis"] == "dispersion_across_estimates"
    none = normal_approx({"point": 0.4, "uncertainty": {"status": "not_reported"}})
    assert none["basis"] is None
    g = np.random.default_rng(0)
    assert sample(se, 1000, g).std() == pytest.approx(0.1, rel=0.1)
    with pytest.raises(ValueError):
        sample({"point": 0.4, "uncertainty": {}}, 10, g)


def test_connector_spec_and_entries() -> None:
    from reva.adapters.connectors import atlas_effects_lit as C

    spec = C.atlas_source()
    assert spec.name == "atlas.effects_lit" and spec.source_key == "effects.literature"
    assert set(spec.dataset_ids) == {e.id for e in load_entries()}
    hosts = {e.document["url"].split("/")[2] for e in load_entries()}
    assert hosts <= set(spec.allowed_hosts)
    assert [e.id for e in C.entries_for("test_fixture")] == ["synthetic_okun_fixture", "synthetic_unreachable_fixture"]
    assert LITERATURE_DIR.is_dir()
