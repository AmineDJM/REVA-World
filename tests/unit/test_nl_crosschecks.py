"""Server cross-checks between the request text and the draft (scientific review V2: H2, H3, M6).

Each case reproduces what gpt-4.1-2025-04-14 produced live during the review (docs/dev/review_science_v2.md)
either from a recorded response (``tests/fixtures/nl/recorded``) or, labelled ``synthetic``, as a hand-written
output with the same content (the reviewer's live outputs were not stored as fixtures). Every test checks the
status the draft would have had without the cross-checks, so it fails on the code before the fix.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from reva.nl import service
from reva.nl.budget import LlmBudget
from reva.nl.catalog import current_catalog
from reva.nl.crosschecks import EXCLUSION_SCOPED, origin_mentions, stated_age_level
from reva.nl.ports import LlmResponse, LlmUsage
from reva.nl.recorded import RecordedDrafter, load_fixtures
from reva.nl.service import DraftInput, draft_scenario

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nl" / "recorded"
BASELINE = "11111111-1111-4111-8111-111111111111"
CUTOFF = "2026-10-01T00:00:00Z"
BUDGET = LlmBudget("test", "", 100, 10_000_000, 1500)
TEXTS = {f["label"]: f["text"] for f in load_fixtures(FIXTURES)}

H2_TEXT = (
    "Pour tous les pays sauf la Chine, quel est l'effet d'une hausse de 20 % du prix du pétrole brut importé "
    "en 2023, exportations inchangées ?"
)
H3_TEXT = "Quel est l'effet d'une hausse de 25% du prix du pétrole sur les prix à la consommation en France en 2024 ?"
M6_TEXT = (
    "Pour la France, si l'âge de départ passe à 65 ans, quel effet sur l'emploi des 55-69 ans et le nombre de "
    "retraités d'ici 2035 ? Année de référence 2024."
)
LLM_ONLY = "La plateforme traitera la demande et signalera l'exclusion manuellement si nécessaire"


class Ledger:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def reserve(self, operation_id: str, *, requests: int, tokens: int) -> None:
        self.rows[operation_id] = (requests, tokens)

    def settle(self, operation_id: str, *, requests: int, tokens: int) -> None:
        self.rows[operation_id] = (requests, tokens)


class Synthetic:
    """SYNTHETIC: a hand-written LLM output (not a recording)."""

    provider, model = "synthetic", "synthetic-replay-of-review-output"

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def complete(self, request: Any) -> LlmResponse:
        return LlmResponse(self.provider, self.model, self.model, self.output, LlmUsage(10, 10, 20), 1.0)


def draft(text: str, drafter: Any | None = None) -> Any:
    inp = DraftInput(
        text=text,
        baseline_release_id=BASELINE,
        knowledge_cutoff=CUTOFF,
        operation_key=hashlib.sha256(text.encode()).hexdigest(),
    )
    return asyncio.run(
        draft_scenario(inp, drafters=[drafter or RecordedDrafter(FIXTURES)], ledger=Ledger(), budget=BUDGET)
    )


def without_crosschecks(monkeypatch: pytest.MonkeyPatch, text: str, drafter: Any | None = None) -> Any:
    """The same draft with the cross-checks disabled: the behaviour before the fix."""
    with monkeypatch.context() as m:
        m.setattr(service, "crosscheck", lambda *a, **k: [])
        return draft(text, drafter)


def out(**over: Any) -> dict[str, Any]:
    base = {
        "input_language": "fr",
        "request_restatement": "paraphrase",
        "model_key": "economic.crude_exposure",
        "title": "t",
        "countries": ["ALL"],
        "reference_year": 2023,
        "fidelity": None,
        "outputs": [],
        "parameters": [
            {"path": "intervention.import_multiplier", "number": 1.2, "text": None, "quote": "hausse de 20 %"},
            {"path": "intervention.export_multiplier", "number": 1.0, "text": None, "quote": "exportations inchangées"},
        ],
        "unsupported": [],
        "assumptions_added": [],
    }
    base.update(over)
    return base


def keys(r: Any) -> set[str]:
    return {u["capability_key"] for u in r.unsupported}


def needs_crude_all() -> None:
    if "ALL" not in current_catalog().models["economic.crude_exposure"].countries:
        pytest.skip("the crude plugin does not declare supports_all_countries in this build")


# --------------------------------------------------------------------------- H2: excluded countries
def test_h2_all_countries_except_china_is_never_a_ready_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review H2 (live): countries ["ALL"] + an LLM geography gap whose text promised a manual exclusion."""
    needs_crude_all()
    llm = Synthetic(
        out(unsupported=[{"text": "sauf la Chine", "capability_key": "geography.not_covered", "reason": LLM_ONLY}])
    )
    assert without_crosschecks(monkeypatch, H2_TEXT, llm).status == "draft_ready"  # the reviewed defect
    r = draft(H2_TEXT, llm)
    assert r.status == "needs_input" and r.draft_spec is None and r.draft_spec_sha256 is None
    assert "countries" in r.missing_fields
    assert "geography.exclusion_not_supported" in keys(r)
    blocking = [u for u in r.unsupported if u.get("blocks_draft")]
    assert blocking and blocking[0]["source"] == "server_guard" and blocking[0]["text"] == "pays sauf"
    # the LLM's false promise is never part of the server restatement (it stays in its labelled item)
    assert LLM_ONLY not in r.restatement and "paraphrase" not in r.restatement
    assert any(u["reason"] == LLM_ONLY and u["source"] == "llm" for u in r.unsupported)
    assert "À préciser" in r.restatement and "exclusion" in r.restatement
    assert "aucune exclusion" in r.restatement  # ALL is spelled out, never shown as a bare code


def test_h2_exclusion_is_caught_even_when_the_llm_reports_nothing() -> None:
    needs_crude_all()
    r = draft(H2_TEXT, Synthetic(out()))
    assert r.status == "needs_input" and r.draft_spec is None and "geography.exclusion_not_supported" in keys(r)
    en = "For all countries except China, imports +20% in 2023, exports unchanged."
    r = draft(en, Synthetic(out(input_language="en", countries=["FRA", "USA", "DEU"])))  # explicit list
    assert r.status == "needs_input" and "geography.exclusion_not_supported" in keys(r)


def test_h2_any_llm_geography_gap_withholds_the_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = Synthetic(
        out(
            countries=["FRA"],
            unsupported=[{"text": "Corse", "capability_key": "geography.not_covered", "reason": "region not covered"}],
        )
    )
    text = "Pour la France, imports +20 % en 2023, exportations inchangées"
    assert without_crosschecks(monkeypatch, text, llm).status == "draft_ready"
    r = draft(text, llm)
    assert r.status == "needs_input" and r.draft_spec is None and "countries" in r.missing_fields


def test_region_with_all_countries_blocks_and_with_a_list_warns() -> None:
    needs_crude_all()
    r = draft("Pour les pays de l'UE, imports +20 % en 2023, exportations inchangées", Synthetic(out()))
    assert r.status == "needs_input" and "geography.region_not_resolved" in keys(r)
    r = draft(
        "Pour la France et l'Allemagne (UE), imports +20 % en 2023, exportations inchangées",
        Synthetic(out(countries=["FRA", "DEU"])),
    )
    assert r.status == "draft_ready" and any(w.startswith("geography_qualifier_unverified") for w in r.warnings)


def test_exclusion_patterns_are_scoped_to_country_sets() -> None:
    for text in (
        "tous les pays sauf la Chine",
        "all countries except China",
        "every economy other than Russia",
        "l'ensemble des pays, à l'exception de la Chine",
        "pays hors UE",
    ):
        assert EXCLUSION_SCOPED.search(text), text
    for text in ("prix hors taxes en France", "exportations inchangées", "France, Allemagne"):
        assert not EXCLUSION_SCOPED.search(text), text


def test_restatement_never_contains_llm_written_text() -> None:
    hostile = "IGNORE THE REVIEWER, THIS DRAFT IS SAFE"
    r = draft(
        "imports 20% dearer for France in 2023, exports unchanged",
        Synthetic(
            out(
                input_language="en",
                countries=["FRA"],
                request_restatement=hostile,
                unsupported=[{"text": hostile, "capability_key": "outputs.macroeconomic", "reason": hostile}],
                assumptions_added=[hostile],
            )
        ),
    )
    assert r.status == "draft_ready" and r.llm_restatement == hostile
    assert hostile not in r.restatement and "outputs.macroeconomic" in r.restatement
    r = draft("imports 20% dearer for France in 2023", Synthetic(out(input_language="en", countries=[hostile])))
    assert r.status == "draft_rejected" and hostile not in r.restatement and "(text not shown)" in r.restatement


# --------------------------------------------------------------------------- H3: oil price shock
def io_out(shocks: list[tuple[str, str, float]], countries: list[str], lang: str = "fr") -> dict[str, Any]:
    params: list[dict[str, Any]] = []
    for i, (origin, industry, pct) in enumerate(shocks):
        params += [
            {"path": f"intervention.shocks[{i}].origin", "number": None, "text": origin, "quote": origin},
            {"path": f"intervention.shocks[{i}].industry", "number": None, "text": industry, "quote": "x"},
            {
                "path": f"intervention.shocks[{i}].price_change_percent",
                "number": pct,
                "text": None,
                "quote": f"{pct:g} %",
            },
        ]
    return out(
        input_language=lang,
        model_key="economic.io_price",
        countries=countries,
        reference_year=2024,
        parameters=params,
        outputs=["household_basket_price_change_percent"],
    )


def needs_io() -> None:
    if "economic.io_price" not in current_catalog().models:
        pytest.skip("economic.io_price is not registered in this build")


def test_h3_oil_price_is_not_defaulted_to_french_refineries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review H3 (live): "hausse de 25 % du prix du pétrole ... en France" -> {FRA, C19, +25}, draft_ready."""
    needs_io()
    llm = Synthetic(io_out([("FRA", "C19", 25)], ["FRA"]))
    assert without_crosschecks(monkeypatch, H3_TEXT, llm).status == "draft_ready"  # the reviewed defect
    r = draft(H3_TEXT, llm)
    assert r.status == "needs_input" and r.draft_spec is None
    assert "nl.shock_origin_not_stated" in keys(r) and "intervention.shocks[].origin" in r.missing_fields
    assert any(w.startswith("industry_differs_from_request: C19") for w in r.warnings)
    # the server restatement says what C19 is (reviewed text), and asks for the origins
    assert "coke and refined petroleum products" in r.restatement and "REVA ne les remplace pas" in r.restatement


def test_h3_recorded_norway_b_shock_warns_that_b_is_broader() -> None:
    needs_io()
    r = draft(TEXTS["openai_en_io_energy_shock"])  # recorded: "...crude petroleum and natural gas from Norway..."
    assert r.status == "draft_ready"  # the origin is stated ("from Norway")
    assert any(w.startswith("industry_broader_than_request: B is all of mining and quarrying") for w in r.warnings)
    assert "Mining and quarrying (NACE B05-B09)" in r.restatement


def test_h3_unnamed_origin_and_world_wide_shocks_ask_instead_of_guessing() -> None:
    needs_io()
    r = draft(
        "Effet en Allemagne d'une hausse de 30 % du prix du gaz en 2024", Synthetic(io_out([("NOR", "B", 30)], ["DEU"]))
    )
    assert r.status == "needs_input" and "nl.shock_origin_not_stated" in keys(r)
    r = draft(
        "Hausse mondiale de 25 % du prix du pétrole brut en 2024 : effet en France ?",
        Synthetic(io_out([("SAU", "B", 25), ("NOR", "B", 25)], ["FRA"])),
    )
    assert r.status == "needs_input" and "nl.shock_origin_not_stated" in keys(r)
    ok = draft(
        "Si le prix du pétrole brut norvégien augmente de 30 % en 2024, effet sur les prix en France",
        Synthetic(io_out([("NOR", "B", 30)], ["FRA"])),
    )
    assert ok.status == "draft_ready", ok.unsupported
    ok = draft(
        "French electricity prices +10 % in 2024: effect on the French household basket",
        Synthetic(io_out([("FRA", "D35", 10)], ["FRA"], "en")),
    )
    assert ok.status == "draft_ready" and any(w.startswith("industry_broader_than_request: D35") for w in ok.warnings)


def test_origin_mentions() -> None:
    assert origin_mentions(H3_TEXT, "FRA") == (True, False)  # a location, not an origin
    assert origin_mentions("crude petroleum from Norway", "NOR") == (True, True)
    assert origin_mentions("pétrole en provenance d'Arabie saoudite", "SAU") == (True, True)
    assert origin_mentions("raffineries françaises", "FRA") == (True, True)
    assert origin_mentions("Norway's oil", "NOR") == (True, True)
    assert origin_mentions("prices for us", "USA") == (False, False)
    assert origin_mentions("the rest of the world", "FIGARO_ROW") == (True, False)


def test_io_catalogue_shows_the_reviewed_industry_contents() -> None:
    needs_io()
    from reva.models.economic.io_price import SHOCKABLE_INDUSTRIES

    view = next(v for v in current_catalog().prompt_view()["models"] if v["model_key"] == "economic.io_price")
    params = {p["path"]: p for p in view["parameters"]}
    assert params["intervention.shocks[].industry"]["choice_meanings"] == SHOCKABLE_INDUSTRIES
    assert "never only in the reported country" in params["intervention.shocks[].origin"]["meaning"]


# --------------------------------------------------------------------------- M6: an age level is not a shift
def retirement_out(text_kind: str = "retirement_age_shift") -> dict[str, Any]:
    return out(
        model_key="social.retirement_age",
        countries=["FRA"],
        reference_year=2024,
        parameters=[
            {"path": "intervention.kind", "number": None, "text": text_kind, "quote": "passe à 65 ans"},
            {"path": "intervention.reference_path", "number": None, "text": "law_in_force_2026", "quote": ""},
            {"path": "intervention.shift_months", "number": 36, "text": None, "quote": "65 ans"},
            {"path": "intervention.first_birth_year", "number": None, "text": None, "quote": ""},
            {"path": "horizon.periods", "number": 12, "text": None, "quote": "d'ici 2035"},
        ],
        outputs=["employed_persons_55_69", "pension_recipients"],
    )


def test_m6_stated_age_level_is_never_converted_into_a_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review M6 (live): "l'âge de départ passe à 65 ans" -> retirement_age_shift with shift_months 36."""
    if "social.retirement_age" not in current_catalog().models:
        pytest.skip("social.retirement_age is not registered in this build")
    o = retirement_out()
    o["parameters"][3] = {"path": "intervention.first_birth_year", "number": 1969, "text": None, "quote": ""}
    before = without_crosschecks(monkeypatch, M6_TEXT, Synthetic(o))
    assert before.status in ("draft_ready", "draft_invalid") and before.status != "needs_input"
    r = draft(M6_TEXT, Synthetic(o))
    assert r.status == "needs_input" and r.draft_spec is None
    assert "nl.age_level_not_shift" in keys(r) and "intervention.kind" in r.missing_fields
    assert "65 ans" in r.restatement and "ne convertit pas un niveau en décalage" in r.restatement
    # an explicit shift is not questioned
    shift = draft(
        "Pour la France, relever l'âge de départ de 6 mois pour les générations nées à partir de 1969, "
        "année de référence 2024, d'ici 2035",
        Synthetic(o),
    )
    assert "nl.age_level_not_shift" not in keys(shift)


def test_stated_age_level_patterns() -> None:
    assert stated_age_level(M6_TEXT) == (65, "âge de départ passe à 65 ans")
    assert (
        stated_age_level(
            "Raise the legal retirement age in France to 66 and show the effect on employment of people aged 60 to 69."
        )[0]
        == 66
    )
    assert stated_age_level("employment of people aged 60 to 69") is None
    assert stated_age_level("l'emploi des 55-69 ans") is None


def test_target_age_intervention_is_used_for_a_level_and_its_value_is_checked() -> None:
    """Once a target-age kind exists (SFX1: retirement_age_target), a shift is refused in favour of it and the
    target must equal the stated level (65 years = 780 months)."""
    from reva.nl.crosschecks import retirement_findings
    from reva.nl.guards import number_provenance, stated_numbers

    kinds = ["retirement_age_target"]
    shift = {"intervention": {"kind": "retirement_age_shift", "shift_months": 36}}
    [f] = retirement_findings(M6_TEXT, shift, kinds)
    assert f.block and "retirement_age_target" in f.reason_en and f.capability_key == "nl.age_level_not_shift"
    ok = {"intervention": {"kind": "retirement_age_target", "target_age_months": 780, "first_birth_year": 1969}}
    assert retirement_findings(M6_TEXT, ok, kinds) == []
    bad = {"intervention": {"kind": "retirement_age_target", "target_age_months": 792, "first_birth_year": 1969}}
    [f] = retirement_findings(M6_TEXT, bad, kinds)
    assert f.block and f.field == "intervention.target_age_months" and "780" in f.reason_en
    assert number_provenance(780, stated_numbers(M6_TEXT)) == "converted_from_stated"


def test_level_kinds_are_read_from_the_registered_schema() -> None:
    from reva.nl.crosschecks import level_intervention_kinds

    m = current_catalog().models.get("social.retirement_age")
    if m is None:
        pytest.skip("social.retirement_age is not registered in this build")
    kinds = level_intervention_kinds(m.branch)
    variants = {p.path for p in m.parameters}
    assert ("intervention.target_age_months" in variants) == bool(kinds)


def test_m6_level_drafted_with_the_target_age_intervention_is_accepted() -> None:
    m = current_catalog().models.get("social.retirement_age")
    if m is None or m.param("intervention.target_age_months") is None:
        pytest.skip("no target-age intervention registered in this build")
    o = retirement_out("retirement_age_target")
    o["parameters"] = [p for p in o["parameters"] if p["path"] != "intervention.shift_months"]
    o["parameters"][2] = {"path": "intervention.first_birth_year", "number": 1969, "text": None, "quote": ""}
    o["parameters"].append({"path": "intervention.target_age_months", "number": 780, "text": None, "quote": "65 ans"})
    r = draft(M6_TEXT, Synthetic(o))
    assert "nl.age_level_not_shift" not in keys(r), r.restatement
    prov = {n["path"]: n["provenance"] for n in r.numbers_to_confirm}
    assert prov["intervention.target_age_months"] == "converted_from_stated"  # 65 years -> 780 months
    assert r.status in ("draft_ready", "needs_input", "draft_invalid")  # the model's own checks decide the rest
    o["parameters"][-1] = {"path": "intervention.target_age_months", "number": 792, "text": None, "quote": "65 ans"}
    r = draft(M6_TEXT, Synthetic(o))
    assert r.status == "needs_input" and "intervention.target_age_months" in r.missing_fields


# --------------------------------------------------------------------------- recorded live responses (2026-10-15)
def test_recorded_h2_exclusion_is_withheld_even_though_the_model_wrote_all() -> None:
    needs_crude_all()
    r = draft(TEXTS["openai_fr_all_countries_except_china"])  # gpt-4.1: countries ["ALL"] + an exclusion gap
    assert r.model.startswith("gpt-4.1") and r.status == "needs_input" and r.draft_spec is None
    assert r.candidate_spec["countries"] == ["ALL"] and "countries" in r.missing_fields
    assert "geography.exclusion_not_supported" in keys(r)
    assert r.llm_restatement and r.llm_restatement not in r.restatement


def test_recorded_h3_model_still_defaults_the_origin_and_the_server_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    needs_io()
    text = TEXTS["openai_fr_oil_price_io"]  # gpt-4.1 with the industry contents shown: still {FRA, C19, +25}
    assert without_crosschecks(monkeypatch, text).status == "draft_ready"
    r = draft(text)
    assert r.status == "needs_input" and r.draft_spec is None
    assert r.candidate_spec["intervention"]["shocks"] == [
        {"origin": "FRA", "industry": "C19", "price_change_percent": 25}
    ]
    assert "nl.shock_origin_not_stated" in keys(r)
    assert any(w.startswith("industry_differs_from_request: C19") for w in r.warnings)


def test_recorded_m6_level_uses_the_target_intervention_and_asks_for_the_cohort() -> None:
    m = current_catalog().models.get("social.retirement_age")
    if m is None or m.param("intervention.target_age_months") is None:
        pytest.skip("no target-age intervention registered in this build")
    r = draft(TEXTS["openai_fr_retirement_level"])  # gpt-4.1: retirement_age_target, 780 months, no cohort
    assert r.status == "needs_input" and r.draft_spec is None
    assert r.candidate_spec["intervention"]["kind"] == "retirement_age_target"
    assert (
        r.candidate_spec["intervention"]["target_age_months"] == 780
        and "shift_months" not in r.candidate_spec["intervention"]
    )
    assert "intervention.first_birth_year" in r.missing_fields  # asked for, never defaulted
    assert "nl.age_level_not_shift" not in keys(r)
