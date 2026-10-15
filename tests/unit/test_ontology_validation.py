"""Semantic compatibility: universes, temporal support, valuation, classification, bases."""

from __future__ import annotations

from datetime import date

import pytest

from reva.kernel.errors import IncompatibleSemantics, MissingInput
from reva.kernel.records import Concept, SemanticProfile
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import TimeExtent
from reva.kernel.values import MeasurementBasis
from reva.ontology.registry import load_registry
from reva.ontology.validation import (
    CountryValue,
    SemanticSide,
    Verdict,
    can_aggregate,
    can_combine_countries,
    check_compatibility,
    compare_concepts,
    parse_temporal_support,
)

REG = load_registry()
JAN1 = REG.concept("demography.population.jan1_age_sex")
MIDYEAR = REG.concept("demography.population.wdi_midyear_total")
GDP = REG.concept("economics.gdp.nominal_current_usd")
IMPORTS = REG.concept("trade.hs2709.import_value.world_total")
EXPORTS = REG.concept("trade.hs2709.export_value.world_total")
IMPORTS_BILATERAL = REG.concept("trade.hs2709.import_value.bilateral")


def _support(t: TimeExtent, scheme: str, key: str, universe: str, agg: str, **res: str) -> Support:
    return Support(
        temporal=t,
        spatial=SpatialRef(scheme=scheme, key=key, authority="test"),
        population=PopulationRef(universe=universe),
        aggregation=agg,
        resolution=res,
    )  # type: ignore[arg-type]


def _usd(valuation: str | None = None, **kw: object) -> MeasurementBasis:
    data: dict = {
        "currency": "USD",
        "price_basis": "nominal",
        "exchange_rate_transform": "provider_converted_to_usd",
        "valuation": valuation,
    }
    data.update(kw)
    return MeasurementBasis.model_validate(data)


def _with_profile(c: Concept, **update: object) -> Concept:
    assert c.semantic_profile is not None
    return c.model_copy(update={"semantic_profile": c.semantic_profile.model_copy(update=update)})


# --------------------------------------------------------------------------- January vs mid-year


def test_january_and_midyear_population_are_not_identical_concepts() -> None:
    report = compare_concepts(JAN1, MIDYEAR)
    assert report.verdict is Verdict.incompatible
    assert not report
    assert "temporal_point_mismatch" in report.codes
    assert "population_universe_mismatch" in report.codes  # usually resident vs de facto
    assert any("1 January" in r and "1 July" in r for r in report.reasons)
    with pytest.raises(IncompatibleSemantics):
        report.require()


def test_same_concept_is_identical_to_itself() -> None:
    assert compare_concepts(MIDYEAR, MIDYEAR).verdict is Verdict.compatible


def test_jan1_support_does_not_satisfy_midyear_concept() -> None:
    jan_support = _support(
        TimeExtent.on_date(date(2023, 1, 1)),
        "provider_reporter_territory",
        "WB:FRA",
        "de_facto_residents_wdi",
        "point_in_time_stock",
    )
    mid_support = jan_support.model_copy(update={"temporal": TimeExtent.on_date(date(2023, 7, 1))})
    a = SemanticSide.from_concept(MIDYEAR, support=jan_support)
    b = SemanticSide.from_concept(MIDYEAR, support=mid_support)
    rep = check_compatibility(a, b)
    assert {"temporal_extent_mismatch", "support_profile_temporal_mismatch"} <= rep.codes


def test_temporal_support_parsing() -> None:
    p = parse_temporal_support("point:07-01 (midyear estimate)")
    assert (p.kind, p.anchor, p.qualifier) == ("point", "07-01", "midyear estimate")
    assert parse_temporal_support("annual_total").kind == "period"


# --------------------------------------------------------------------------- aggregation


def _imports_side(concept: Concept, iso: str, valuation: str, year: int = 2023, cls: str = "HS:H6") -> SemanticSide:
    sup = _support(
        TimeExtent.calendar_year(year),
        "provider_reporter_territory",
        f"COMTRADE:{iso}",
        "not_applicable",
        "period_flow_total",
        classification=cls,
    )
    return SemanticSide.from_concept(concept, support=sup, basis=_usd(valuation))


def test_cif_and_fob_values_cannot_be_aggregated() -> None:
    summable = _with_profile(IMPORTS, permitted_transformations=("identity", "sum_disjoint_entities"))
    ok = can_aggregate(_imports_side(summable, "251", "CIF"), _imports_side(summable, "12", "CIF"))
    assert ok.verdict is Verdict.compatible, ok.reasons
    bad = can_aggregate(_imports_side(summable, "251", "CIF"), _imports_side(summable, "12", "FOB"))
    assert bad.verdict is Verdict.incompatible
    assert bad.codes == {"valuation_cif_vs_fob"}


def test_imports_and_exports_concepts_cannot_be_aggregated() -> None:
    rep = can_aggregate(_imports_side(IMPORTS, "251", "CIF"), _imports_side(EXPORTS, "251", "FOB"))
    assert not rep
    assert {"valuation_mismatch", "valuation_cif_vs_fob"} <= rep.codes


def test_world_total_and_bilateral_cannot_be_summed() -> None:
    rep = can_aggregate(IMPORTS, IMPORTS_BILATERAL)
    assert "spatial_support_mismatch" in rep.codes
    assert not rep


def test_registry_identity_only_concept_does_not_license_summation() -> None:
    rep = can_aggregate(_imports_side(IMPORTS, "251", "CIF"), _imports_side(IMPORTS, "12", "CIF"))
    assert rep.codes == {"aggregation_not_permitted"}


def test_classification_revision_mismatch_and_missing() -> None:
    summable = _with_profile(IMPORTS, permitted_transformations=("sum",))
    rep = can_aggregate(
        _imports_side(summable, "251", "CIF", cls="HS:H5"), _imports_side(summable, "12", "CIF", cls="HS:H6")
    )
    assert rep.codes == {"classification_revision_mismatch"}
    b = _imports_side(summable, "12", "CIF")
    b = SemanticSide(
        ref=b.ref, profile=b.profile, basis=b.basis, support=b.support.model_copy(update={"resolution": {}})
    )  # type: ignore[union-attr]
    rep2 = can_aggregate(_imports_side(summable, "251", "CIF"), b)
    assert rep2.verdict is Verdict.blocked and "classification_missing" in rep2.codes


def test_eurostat_leaf_cells_at_same_date_can_be_summed() -> None:
    def cell(age: str) -> SemanticSide:
        sup = _support(
            TimeExtent.on_date(date(2023, 1, 1)),
            "eurostat_geo_code",
            "ESTAT:FR",
            "usually_resident_population_eurostat",
            "point_in_time_stock",
            age=age,
            sex="F",
        )
        return SemanticSide.from_concept(JAN1, support=sup)

    assert can_aggregate(cell("Y1"), cell("Y2")).verdict is Verdict.compatible
    later = cell("Y3")
    later = SemanticSide(
        ref=later.ref,
        profile=later.profile,
        support=later.support.model_copy(  # type: ignore[union-attr]
            update={"temporal": TimeExtent.on_date(date(2024, 1, 1))}
        ),
    )
    assert "temporal_extent_mismatch" in can_aggregate(cell("Y1"), later).codes


def test_non_additive_scale_type_refused() -> None:
    ratio = SemanticProfile(
        population_universe="u",
        temporal_support="annual_total",
        spatial_support="s",
        aggregation="period_rate",
        scale_type="ratio",
        unit_ref="unit.ratio",
        permitted_transformations=("sum",),
    )
    sup = _support(TimeExtent.calendar_year(2023), "s", "k", "u", "period_rate")
    a = SemanticSide(ref="r@1", profile=ratio, support=sup)
    assert "non_additive_scale_type" in can_aggregate(a, a).codes


# --------------------------------------------------------------------------- blockers


def test_missing_unit_and_support_are_blockers() -> None:
    no_unit = _with_profile(JAN1, unit_ref=None)
    rep = compare_concepts(no_unit, no_unit)
    assert rep.verdict is Verdict.blocked and "unit_missing" in rep.codes
    with pytest.raises(MissingInput):
        rep.require()
    rep2 = can_aggregate(JAN1, JAN1)  # no supports
    assert rep2.verdict is Verdict.blocked and "support_missing" in rep2.codes
    unknown = _with_profile(JAN1, unit_ref="unit.heads")
    assert "unit_unknown" in compare_concepts(unknown, unknown).codes


def test_missing_profile_blocks() -> None:
    entity = REG.concept("entity.country")
    assert check_compatibility(entity, JAN1).verdict is Verdict.blocked


def test_currency_amount_without_basis_blocks_aggregation() -> None:
    summable = _with_profile(IMPORTS, permitted_transformations=("sum",))
    a = _imports_side(summable, "251", "CIF")
    b = SemanticSide(ref=a.ref, profile=a.profile, support=a.support, basis=None)
    rep = can_aggregate(a, b)
    assert rep.verdict is Verdict.blocked and "currency_basis_missing" in rep.codes


# --------------------------------------------------------------------------- cross-country combination


def _gdp(iso: str, year: int, basis: MeasurementBasis | None) -> CountryValue:
    sup = _support(
        TimeExtent.calendar_year(year),
        "provider_reporter_territory",
        f"WB:{iso}",
        "resident_producers_of_economy",
        "period_flow_total",
    )
    return CountryValue(entity_key=iso, side=SemanticSide.from_concept(GDP, support=sup, basis=basis))


def test_countries_with_different_reference_years_cannot_be_combined() -> None:
    assert can_combine_countries([_gdp("FRA", 2023, _usd()), _gdp("DZA", 2023, _usd())]).compatible
    rep = can_combine_countries([_gdp("FRA", 2023, _usd()), _gdp("DZA", 2022, _usd())])
    assert rep.verdict is Verdict.incompatible
    assert "reference_year_mismatch" in rep.codes
    with pytest.raises(IncompatibleSemantics, match="reference years"):
        rep.require()


def test_countries_with_different_gdp_bases_cannot_be_combined() -> None:
    ppp = _usd(price_basis="ppp", price_reference_year=2017, exchange_rate_transform="icp_ppp_2017")
    rep = can_combine_countries([_gdp("FRA", 2023, _usd()), _gdp("CHN", 2023, ppp)])
    assert {"price_basis_mismatch", "price_reference_year_mismatch", "exchange_rate_transform_mismatch"} <= rep.codes
    fx = _usd(exchange_rate_transform="atlas_method")
    assert (
        "exchange_rate_transform_mismatch"
        in can_combine_countries([_gdp("FRA", 2023, _usd()), _gdp("USA", 2023, fx)]).codes
    )


def test_combination_blocked_without_basis_or_reference_year() -> None:
    rep = can_combine_countries([_gdp("FRA", 2023, _usd()), _gdp("DZA", 2023, None)])
    assert rep.verdict is Verdict.blocked and "currency_basis_missing" in rep.codes
    no_year = CountryValue(entity_key="USA", side=SemanticSide.from_concept(GDP, basis=_usd()))
    rep2 = can_combine_countries([_gdp("FRA", 2023, _usd()), no_year])
    assert "reference_year_missing" in rep2.codes and not rep2


def test_combination_of_different_concepts_refused() -> None:
    pop = CountryValue(entity_key="DZA", side=SemanticSide.from_concept(MIDYEAR), reference_year=2023)
    gdp = CountryValue(entity_key="FRA", side=SemanticSide.from_concept(GDP, basis=_usd()), reference_year=2023)
    assert "concept_mismatch" in can_combine_countries([gdp, pop]).codes
