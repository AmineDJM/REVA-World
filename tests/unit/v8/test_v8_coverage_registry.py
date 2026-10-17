"""V8 COUVERTURE: the domains grid and the queued-loads / owner-decisions registry are valid and consistent."""

from __future__ import annotations

import pytest
import yaml

from reva.v8.coverage.domains import DOMAINS_PATH, load_domains, load_places, problems
from reva.v8.coverage.pending import PENDING_PATH, Item, load_pending
from reva.v8.coverage.pending import problems as pending_problems


def test_domains_registry_valid_and_covers_every_atlas_theme() -> None:
    from reva.atlas.catalog import load_themes

    doc = yaml.safe_load(DOMAINS_PATH.read_text(encoding="utf-8"))
    themes = load_themes().labels
    assert problems(doc, known_themes=themes) == []
    reg = load_domains()
    assert len(reg.ids) == len(set(reg.ids)) >= 20
    assert reg.other.id == "other"


def test_domains_problems_detect_duplicates_and_missing_themes() -> None:
    doc = {
        "schema": "reva-v8-coverage-domains/1",
        "domains": [
            {"id": "a", "fr": "A", "en": "A", "themes": ["economy"], "kg_kinds": ["taxon"]},
            {"id": "b", "fr": "B", "en": "B", "themes": ["economy"], "kg_kinds": ["taxon"]},
        ],
        "other": {"id": "other", "fr": "Non classé", "en": "Unclassified"},
    }
    out = problems(doc, known_themes=["economy", "health"])
    assert any("theme 'economy' is in two domains" in p for p in out)
    assert any("kind 'taxon' is in two domains" in p for p in out)
    assert any("without a domain" in p and "health" in p for p in out)


def test_table_and_kind_assignment_rules() -> None:
    reg = load_domains()
    # explicit table beats source and theme
    assert reg.domain_of_table("atlas.eurostat.nuts_pop", "eurostat", ["economy"]) == ("population", "table")
    # declaring connector for shared atlas source keys
    assert reg.domain_of_table("atlas.eurostat.asylum_applicants_m", "eurostat", (), ["atlas.eurostat_asylum"]) == (
        "migration",
        "connector",
    )
    assert reg.domain_of_table("atlas.ucdp_ged.events", "ucdp_ged") == ("conflict", "source")
    # majority theme, ties broken by domain order
    assert reg.domain_of_table("atlas.x.y", "x", ["health", "health", "economy"]) == ("health", "catalog_theme")
    assert reg.domain_of_table("atlas.x.y", "x", []) == ("other", "unclassified")
    assert reg.domain_of_kind("taxon") == "biology"
    assert reg.domain_of_kind("event", "harvard.dataverse.navco") == "politics"
    assert reg.domain_of_kind("event", "reinhart_rogoff.bffs") == "finance"
    assert reg.domain_of_kind("event", "somewhere.else") == "other"
    assert reg.domain_of_kind("never_seen_kind") == "other"


def test_places_have_regions_and_reva_placements_are_labelled() -> None:
    reg = load_domains()
    places = load_places()
    assert len(places) >= 250
    assert all(p.region in reg.regions and p.subregion in reg.subregions for p in places)
    reva = {p.iso3: p for p in places if p.placement == "reva"}
    assert set(reva) >= {"TWN", "XKX"} and all(p.placement_basis for p in reva.values())
    fra = next(p for p in places if p.iso3 == "FRA")
    assert (fra.region, fra.subregion) == ("Europe", "Western Europe")


def test_pending_registry_valid_and_consistent() -> None:
    from reva.atlas.sources import atlas_sources

    reg = load_pending(PENDING_PATH)
    assert len(reg.items) >= 60
    assert pending_problems(reg, domains=[*load_domains().ids, "other"], connectors=atlas_sources()) == []
    kinds = {i.kind for i in reg.items}
    assert {
        "licence",
        "account_key",
        "permission",
        "disk",
        "bigger_machine",
        "attribution_contact",
        "policy_choice",
    } <= kinds
    for i in reg.items:
        if i.type == "decision":
            assert not i.check
        else:
            assert i.commands


def _item(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "x.test",
        "type": "decision",
        "kind": "licence",
        "title": "Une décision",
        "unlocks": {"text": "quelque chose"},
        "action": "Décider ceci",
        "who": "owner",
        "status": "open",
        "source_docs": ["docs/dev/HANDOFF_V3.md"],
    }
    base.update(over)
    return base


def test_pending_item_rules() -> None:
    Item.model_validate(_item())
    with pytest.raises(ValueError, match="e-mail"):
        Item.model_validate(_item(action="Écrire à someone@example.org"))
    with pytest.raises(ValueError, match="no connector check"):
        Item.model_validate(_item(check=[{"connector": "atlas.x"}]))
    with pytest.raises(ValueError, match="exact commands"):
        Item.model_validate(_item(type="load", kind="disk"))
    with pytest.raises(ValueError, match="are loads"):
        Item.model_validate(_item(kind="disk"))
    with pytest.raises(ValueError):
        Item.model_validate(_item(kind="vibes"))
    with pytest.raises(ValueError, match="MAJOR"):
        Item.model_validate(
            _item(type="load", kind="disk", commands=["x y"], check=[{"connector": "atlas.x", "min_version": "1.0"}])
        )
