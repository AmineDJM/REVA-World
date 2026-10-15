"""V2 step 1 / V3 XWALK: ISO 3166-1 <-> M49 <-> Comtrade <-> World Bank crosswalk, with UN membership
(offline, archived raw sources)."""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest
import yaml

from reva.adapters.connectors import country_crosswalk as cw

REG = Path(__file__).resolve().parents[2] / "registry"
DOC = cw.load_crosswalk()
BY_KEY = {e["key"]: e for e in DOC["entities"]}
UNMAPPED = {(u["provider"], u["code"]): u for u in DOC["unmapped_provider_codes"]}


def test_committed_crosswalk_is_a_rebuild_of_the_archived_provider_lists() -> None:
    rebuilt = cw.build_from_archive()
    assert rebuilt["content_sha256"] == DOC["content_sha256"]
    assert [s["name"] for s in DOC["sources"]] == [
        "unsd_m49",
        "comtrade_reporters",
        "comtrade_partners",
        "worldbank_countries",
        "iso3166_1",
        "iso3166_1_fr",
        "unsd_m49_home",
        "un_member_states",
        "un_non_member_states",
    ]
    assert all(len(s["sha256"]) == 64 and s["retrieved_at"] for s in DOC["sources"])
    assert DOC["version"] == cw.CROSSWALK_VERSION == "1.1.0"
    assert DOC["review_status"] == "machine_derived_pending_maintainer_review"
    assert DOC["iso3166_1_mirror"]["version"] == "4.20.1"


def test_composite_and_special_codes_are_flagged_and_never_mapped() -> None:
    assert "nes_area" in UNMAPPED[("comtrade_reporter", "490")]["flags"]  # Other Asia, nes
    assert "group" in UNMAPPED[("comtrade_reporter", "97")]["flags"]  # European Union
    assert "nes_area" in UNMAPPED[("comtrade_partner", "899")]["flags"]  # Areas, nes
    assert "nes_area" in UNMAPPED[("comtrade_partner", "490")]["flags"]
    # The provider ISO3 field of "Europe EU, nes" says MCO: it is still never mapped to Monaco.
    eu_nes = UNMAPPED[("comtrade_partner", "492")]
    assert eu_nes["provider_iso3_field"] == "MCO" and eu_nes["mapped_iso3"] is None
    assert BY_KEY["MCO"]["comtrade_partner"] != "492"
    for code in ("810", "890", "736", "230", "841", "58"):  # USSR, Yugoslavia, Fmr Sudan/Ethiopia, ...
        assert "historical" in UNMAPPED[("comtrade_reporter", code)]["flags"], code
    mapped = {e["comtrade_reporter"] for e in DOC["entities"] if e["comtrade_reporter"]}
    assert not mapped & {code for (p, code) in UNMAPPED if p == "comtrade_reporter"}
    assert all(u["mapped_iso3"] is None for u in DOC["unmapped_provider_codes"])


def test_worldbank_aggregates_excluded_and_wb_only_economies_flagged() -> None:
    agg = set(DOC["excluded_worldbank_aggregates"])
    assert {"WLD", "EUU", "HIC", "LMY", "SSF", "OED"} <= agg
    assert not agg & set(BY_KEY)
    assert sum(1 for e in DOC["entities"] if e["worldbank"]) == 217
    for key in ("XKX", "CHI"):
        assert BY_KEY[key]["iso3"] is None and "not_iso3166" in BY_KEY[key]["flags"]
        assert {a["provider"] for a in BY_KEY[key]["aliases"]} == {"worldbank"}


def test_comtrade_codes_differ_from_m49_and_territory_notes_are_flagged() -> None:
    for key, ct, m49 in (
        ("FRA", "251", "250"),
        ("USA", "842", "840"),
        ("CHE", "757", "756"),
        ("NOR", "579", "578"),
        ("IND", "699", "356"),
    ):
        e = BY_KEY[key]
        assert (e["comtrade_reporter"], e["m49"]) == (ct, m49)
        assert "comtrade_code_differs_from_m49" in e["flags"]
    for key in ("FRA", "USA", "CHE", "NOR"):
        assert "comtrade_territory_note_lists_additional_areas" in BY_KEY[key]["flags"]
    # Territories folded into another reporter have no reporter of their own.
    for key in ("PRI", "VIR", "MCO", "LIE"):
        assert BY_KEY[key]["comtrade_reporter"] is None and "no_comtrade_reporter" in BY_KEY[key]["flags"]
    # Taiwan has no Comtrade reporter: 490 "Other Asia, nes" stays an unmapped "nes" area, never TWN.
    assert BY_KEY["TWN"]["comtrade_reporter"] is None and "no_comtrade_reporter" in BY_KEY["TWN"]["flags"]


def test_reviewed_entities_agree_with_the_crosswalk() -> None:
    reviewed = yaml.safe_load((REG / "entities" / "countries.yaml").read_text())["entities"]
    for e in reviewed:
        if e["kind"] != "entity.country":
            continue
        aliases = {a["provider"]: str(a["key"]) for a in e["aliases"]}
        x = BY_KEY[e["key"]]
        assert x["reviewed_entry"] is True
        assert x["comtrade_reporter"] == aliases["comtrade_reporter"]
        assert x["m49"] == aliases["un_m49"] and x["worldbank"] == aliases["worldbank"]
    assert {e["key"] for e in cw.seed_entities()}.isdisjoint({e["key"] for e in reviewed})


def test_mapping_subject_scopes_are_the_crosswalk() -> None:
    ct = yaml.safe_load((REG / "mappings" / "comtrade_batch.yaml").read_text())
    wb = yaml.safe_load((REG / "mappings" / "worldbank_all.yaml").read_text())
    assert ct["connector"]["name"] == "un.comtrade.public_batch"
    for m in ct["mappings"]:
        assert m["applicability"]["subjects"] == sorted(cw.comtrade_reporters())
        assert m["applicability"]["classifications"] == ["H4", "H5", "H6"]
    for m in wb["mappings"]:
        assert m["applicability"]["subjects"] == sorted(cw.worldbank_economies())


def test_tampered_archive_or_content_fails_closed(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    shutil.copytree(cw.REFERENCE_DIR, ref)
    doc = json.loads(cw.CROSSWALK_PATH.read_text())
    cw.verify_sources(doc, ref)
    (ref / "comtrade_Reporters.json").write_bytes((ref / "comtrade_Reporters.json").read_bytes() + b" ")
    with pytest.raises(cw.CrosswalkError, match="sha256"):
        cw.verify_sources(doc, ref)
    doc2 = json.loads(cw.CROSSWALK_PATH.read_text())
    doc2["entities"][0]["comtrade_reporter"] = "490"
    with pytest.raises(cw.CrosswalkError, match="content_sha256"):
        cw.verify_sources(doc2)


def test_partial_m49_table_is_refused() -> None:
    html = gzip.decompress((cw.REFERENCE_DIR / "unsd_m49_overview.html.gz").read_bytes()).decode()
    assert len(cw.parse_m49(html.encode())) == 248
    cut = html[: html.index("Algeria") + 2000] + "</table>"
    with pytest.raises(cw.CrosswalkError, match="partial"):
        cw.parse_m49(cut.encode())


# --------------------------------------------------------------------------- V3 XWALK (crosswalk 1.1.0)
ISO = cw.parse_iso3166(cw.read_source(next(s for s in cw.SOURCES if s.name == "iso3166_1")))


def test_every_iso3166_1_code_is_an_entity_plus_the_two_world_bank_codes() -> None:
    assert len(ISO) == 249
    assert set(ISO) <= set(BY_KEY)
    assert len(DOC["entities"]) == DOC["stats"]["entities"] == 251
    assert {k for k, e in BY_KEY.items() if not e["in_iso3166_1"]} == {"CHI", "XKX"}
    assert {k for k, e in BY_KEY.items() if "not_listed_in_un_m49" in e["flags"]} == {"TWN"}
    for k in "TWN ATA BVT HMD UMI IOT ATF SGS CCK CXR NFK PCN TKL ESH VAT BES SJM ALA GGY JEY IMN".split():
        assert BY_KEY[k]["in_iso3166_1"] and BY_KEY[k]["iso_name"] == ISO[k]["name"], k
    for k, e in BY_KEY.items():  # ISO and M49 codes agree wherever M49 lists the entity
        if e["m49"] is not None:
            assert e["iso_numeric"] is not None and int(e["iso_numeric"]) == int(e["m49"]), k


def test_taiwan_entity_keeps_iso_name_verbatim_with_a_neutral_label() -> None:
    t = BY_KEY["TWN"]
    assert (t["iso3"], t["iso2"], t["iso_numeric"]) == ("TWN", "TW", "158")
    assert t["iso_name"] == "Taiwan, Province of China"  # ISO 3166-1 English short name, verbatim
    assert (t["label"], t["label_fr"], t["label_source"]) == ("Taiwan", "Taïwan", "iso3166_1_common_name")
    assert t["m49"] is None and t["m49_region"] is None and t["worldbank"] is None
    assert t["un_membership"] == "not_listed" and t["un_name"] is None
    assert "provider_iso3_not_iso3166" not in t["flags"]
    assert t["aliases"] == [{"provider": "iso3166_alpha3", "key": "TWN"}]
    # Comtrade partner 158 is labelled Taiwan by the provider; 490 "Other Asia, nes" is not Taiwan.
    assert t["comtrade_partner"] == "158" and ("comtrade_partner", "158") not in UNMAPPED
    assert ("comtrade_partner", "490") in UNMAPPED and ("comtrade_reporter", "490") in UNMAPPED
    assert not any(u["provider_iso3_field"] == "TWN" for u in DOC["unmapped_provider_codes"])
    # no World Bank economy: the all-countries scope (and its typed gaps) is unchanged
    assert "TWN" not in cw.worldbank_economies() and "TWN" not in cw.comtrade_reporters()
    assert "TWN" in {e["key"] for e in cw.seed_entities()}


def test_m49_overlap_notes_are_recorded_on_both_sides_never_summed() -> None:
    for part, whole, code in (("TWN", "CHN", "158"), ("XKX", "SRB", "412")):
        for k, other in ((part, whole), (whole, part)):
            (note,) = BY_KEY[k]["aggregation_notes"]
            assert "aggregation_overlap" in BY_KEY[k]["flags"]
            assert note["with"] == other and note["source"] == "unsd_m49_home"
            assert "considered part of" in note["quote"] and f"numerical code {code}" in note["quote"]
            assert "never sums" in note["reva_policy"]
    assert (
        "Taiwan Province of China is considered part of China (numerical code 156)"
        in (BY_KEY["TWN"]["aggregation_notes"][0]["quote"])
    )
    flagged = {k for k, e in BY_KEY.items() if "aggregation_overlap" in e["flags"] or e["aggregation_notes"]}
    assert flagged == {"TWN", "CHN", "XKX", "SRB"}


def test_un_membership_is_copied_from_the_un_pages() -> None:
    members = {k for k, e in BY_KEY.items() if e["un_membership"] == "member_state"}
    observers = {k for k, e in BY_KEY.items() if e["un_membership"] == "non_member_observer_state"}
    assert len(members) == 193 and observers == {"VAT", "PSE"}
    assert {"CHN", "FRA", "SRB", "SSD", "MNE", "CHE"} <= members
    assert not members & {"TWN", "XKX", "CHI", "HKG", "MAC", "ESH", "GRL", "PRI", "VAT", "PSE"}
    assert BY_KEY["SSD"]["un_admission_date"] == "2011-07-14"
    assert BY_KEY["CHN"]["un_name"] == "China (the People's Republic of)"
    assert BY_KEY["TUR"]["un_name"] == "Türkiye"
    assert all(e["un_admission_date"] is None for e in BY_KEY.values() if e["un_membership"] != "member_state")
    assert DOC["stats"]["un_member_states"] == 193 and DOC["stats"]["un_non_member_observer_states"] == 2


def _raw() -> dict[str, bytes]:
    return {s.name: cw.read_source(s) for s in cw.SOURCES}


def _rebuild(raw: dict[str, bytes]) -> dict:
    return cw.build(raw, retrieved={}, reviewed_keys=set())


def test_iso_m49_disagreement_and_partial_un_lists_fail_closed() -> None:
    raw = _raw()
    iso = json.loads(raw["iso3166_1"])
    fra = next(r for r in iso["3166-1"] if r["alpha_3"] == "FRA")
    fra["numeric"] = "251"
    with pytest.raises(cw.CrosswalkError, match="FRA"):
        _rebuild({**raw, "iso3166_1": json.dumps(iso).encode()})
    iso["3166-1"] = [r for r in iso["3166-1"] if r["alpha_3"] != "FRA"]
    with pytest.raises(cw.CrosswalkError, match="FRA"):
        _rebuild({**raw, "iso3166_1": json.dumps(iso).encode()})
    page = raw["un_member_states"].decode()
    cut = page[: page.index("Date of Admission", page.index("Algeria"))] + "Date of Admission: 08-10-1962</div>"
    with pytest.raises(cw.CrosswalkError, match="partial"):
        _rebuild({**raw, "un_member_states": cut.encode()})
    # a UN name that would match one code while the card's flag says another is refused
    swapped = page.replace("flags flags-al", "flags flags-xx", 1)
    with pytest.raises(cw.CrosswalkError, match="Albania"):
        _rebuild({**raw, "un_member_states": swapped.encode()})
    m49_home = raw["unsd_m49_home"].replace(b"Why is Taiwan Province of China not shown", b"Why is X not shown")
    with pytest.raises(cw.CrosswalkError, match="Taiwan"):
        _rebuild({**raw, "unsd_m49_home": m49_home})


def test_atlas_geo_index_maps_taiwan_codes_but_never_490() -> None:
    from reva.atlas.tables import country_iso3

    assert country_iso3("TWN", "iso3") == country_iso3("TW", "iso2") == "TWN"
    assert country_iso3("158", "m49") == country_iso3("158", "iso_numeric") == "TWN"  # ISO numeric
    assert country_iso3("TW", "fips") == "TWN"  # GeoNames countryInfo row "Taiwan" (country_codes_extra)
    assert country_iso3("156", "m49") == "CHN" and country_iso3("004", "m49") == "AFG"
    assert country_iso3("490", "comtrade") is None and country_iso3("490", "m49") is None
