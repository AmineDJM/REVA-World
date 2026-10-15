"""V2 step 1: ISO3 <-> M49 <-> Comtrade <-> World Bank crosswalk (offline, archived raw sources)."""

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
    ]
    assert all(len(s["sha256"]) == 64 and s["retrieved_at"] for s in DOC["sources"])


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
    assert "TWN" not in BY_KEY  # M49 lists no separate Taiwan; Comtrade 490 stays an unmapped "nes" area


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
