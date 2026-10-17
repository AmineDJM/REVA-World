"""V6 INTEG: measured_by bridge (catalog concept vs atlas table entity) and providers' required notices.

Shapes follow the statements the loaders write (dev DB, 2026-10-17): V4CORE ``atlas:measured_by`` -> ``atlas:<concept>``
with qualifiers ``concept``/``iso3``; V4ECON ``econ:measured_by`` -> ``atlas:oecd.reg_pop`` with ``table`` +
``provider_geo``; V4MIND ``ilostat:measured_by`` -> ``atlas:ilostat.<dataset>`` with ``classif1`` + ``atlas_table``;
V4ECO ``gbif_occ:measured_by`` -> ``atlas:gbif_occ.<dataset>`` with ``indicators``.
"""

from __future__ import annotations

from reva.atlas.query import registry_notices, required_notices, with_required_notice
from reva.atlas.snapshot import normalise_rights
from reva.kg.families import load_registry, measured_by_target


def test_concept_convention_unchanged() -> None:
    h = measured_by_target("atlas:energy.oil_production", {"concept": "energy.oil_production", "iso3": "sau"})
    assert h is not None and h["kind"] == "concept"
    assert (h["concept"], h["iso3"], h["table"]) == ("energy.oil_production", "SAU", None)
    # object id only (no qualifiers, kind unknown): V4CORE contract, read as a catalog concept
    assert measured_by_target("atlas:energy.oil_production", None)["concept"] == "energy.oil_production"  # type: ignore[index]


def test_econ_region_table_entity() -> None:
    h = measured_by_target("atlas:oecd.reg_pop", {"table": "oecd.reg_pop", "provider_geo": "FRL04"}, "data_series")
    assert h is not None and h["kind"] == "table" and h["concept"] is None
    assert (h["source"], h["dataset"], h["provider_geo"], h["iso3"]) == ("oecd", "reg_pop", "FRL04", None)


def test_ilostat_occupation_table_entity() -> None:
    q = {"classif1": "OCU_ISCO08_2", "atlas_table": "ilostat.emp_temp_sex_ocu_nb_a"}
    h = measured_by_target("atlas:ilostat.emp_temp_sex_ocu_nb_a", q, "atlas_table")
    assert h is not None and h["kind"] == "table"
    assert (h["table"], h["indicator_contains"], h["provider_geo"]) == (
        "ilostat.emp_temp_sex_ocu_nb_a",
        "classif1=OCU_ISCO08_2",
        None,
    )


def test_gbif_table_entity_with_indicators_and_kind_only() -> None:
    h = measured_by_target(
        "atlas:gbif_occ.taxon_country", {"indicators": ["occurrences.gbif_5219404"]}, "atlas_dataset"
    )
    assert h is not None and h["kind"] == "table" and h["indicators"] == ["occurrences.gbif_5219404"]
    # a table entity without qualifiers is still a table (its kind says so), never a concept
    k = measured_by_target("atlas:eurostat.nuts_gdp", {}, "data_series")
    assert k is not None and k["kind"] == "table" and k["source"] == "eurostat"


def test_unusable_or_hostile_qualifiers() -> None:
    assert measured_by_target("syn:x", None) is None
    h = measured_by_target(
        "atlas:oecd.reg_pop", {"table": "oecd.reg_pop", "provider_geo": "FR 1; DROP", "classif1": "x y"}
    )
    assert h is not None and h["provider_geo"] is None and h["indicator_contains"] is None


def test_every_measured_by_mapping_is_resolvable() -> None:
    reg = load_registry()
    pids = sorted(p for p, m in reg.mappings.items() if m.family == "measured_by")
    assert {"atlas:measured_by", "econ:measured_by", "ilostat:measured_by", "gbif_occ:measured_by"} <= set(pids)


def test_required_notice_survives_normalisation_and_summary() -> None:
    r = normalise_rights({"licence": "Provider terms of use", "required_notice": "  Keep this notice. "}, basis="t")
    assert r is not None and r["required_notice"] == "Keep this notice." and r["restricted"] is True
    assert "required_notice" not in (normalise_rights({"licence": "CC-BY-4.0"}, basis="t") or {})
    lic = [{"source": "a", **r}, {"source": "a", **r}, {"source": "b", "licence": "CC0"}]
    assert required_notices(lic) == [{"source": "a", "notice": "Keep this notice."}]


def test_nyfed_notice_declared_in_registry_matches_connector() -> None:
    from reva.adapters.connectors.atlas_hf_nyfed import REQUIRED_NOTICE

    assert registry_notices()["nyfed"] == REQUIRED_NOTICE
    base = normalise_rights({"licence": "New York Fed Terms of Use"}, basis="atlas_source_spec")
    assert with_required_notice(base, "nyfed")["required_notice"] == REQUIRED_NOTICE  # type: ignore[index]
    assert with_required_notice(base, "ecb") is base
