"""A-COUPS connectors offline: Powell & Thyne coups, Archigos 4.1 (Stata reader), REIGN-format leader list.

Fixtures are byte-faithful subsets of the real 2026-10-16 captures (``tests/unit/atlas_coups_fixtures.py``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors import atlas_coups as C
from reva.adapters.connectors import atlas_leaders as L
from reva.adapters.connectors.atlas_geopol_common import (
    StateCodeCrosswalk,
    atlas_frame,
    crosswalk_from_countrycode_tarball,
    crosswalk_keys,
    load_qid_for_iso3,
)
from reva.atlas.tables import validate_atlas_frame
from reva.kernel.errors import SourceResponseError

sys.path.insert(0, str(Path(__file__).parent))
from atlas_coups_fixtures import subset_csv, subset_dta, subset_xlsx

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"


def _fixture(name: str, url_suffix: str) -> bytes:
    man = json.loads((FIX / name / "manifest.json").read_text())
    e = next(x for x in man if x["url"].endswith(url_suffix))
    return (FIX / name / e["file"]).read_bytes()


@lru_cache(maxsize=1)
def _crosswalk() -> StateCodeCrosswalk:
    return crosswalk_from_countrycode_tarball(_fixture("atlas.coups", "countrycode_1.9.0.tar.gz"), crosswalk_keys())


PAGE = _fixture("atlas.coups", "/coups/")
PT = _fixture("atlas.coups", "pt_20260829.csv")
DTA = _fixture("atlas.archigos", "Archigos_4.1_stata14.dta")
LL = _fixture("atlas.reign_leaders", "leaderlist.xlsx")


# ----------------------------------------------------------------------------- Powell & Thyne


def test_page_link_definitions_and_note() -> None:
    p = C.parse_page(PAGE)
    assert p.csv_url == "https://jonathanmpowell.com/wp-content/uploads/2026/08/pt_20260829.csv"
    assert p.definition_coup == C.DEFINITION_COUP and p.definition_success == C.DEFINITION_SUCCESS
    assert p.note_date == date(2026, 8, 29) and "tentatively" in (p.note_text or "")


def test_page_definition_change_fails_closed() -> None:
    changed = PAGE.replace(b"illegal and overt attempts", b"overt attempts")
    with pytest.raises(SourceResponseError, match="definition"):
        C.parse_page(changed)
    with pytest.raises(SourceResponseError, match="pt_YYYYMMDD"):
        C.parse_page(PAGE.replace(b"pt_20260829.csv", b"coups.csv"))


def test_csv_parse_and_drift() -> None:
    ev = C.parse_csv(PT, strict=False)
    assert len(ev) == 69 and {e.version for e in ev} == {"V2026.08.29"}
    chile = [e for e in ev if e.ccode == 155]
    assert any(e.day == date(1973, 9, 11) and e.outcome == "successful" for e in chile)
    with pytest.raises(SourceResponseError, match="truncated"):
        C.parse_csv(PT, strict=True)
    lines = PT.split(b"\r\n")
    bad = b"\r\n".join([lines[0], lines[1].replace(b",2,V2026", b",3,V2026"), *lines[2:]])
    with pytest.raises(SourceResponseError, match="coup code"):
        C.parse_csv(bad, strict=False)
    dup = b"\r\n".join([lines[0], lines[1], lines[1], *lines[2:]])
    with pytest.raises(SourceResponseError, match="same code and day"):
        C.parse_csv(dup, strict=False)
    with pytest.raises(SourceResponseError, match="columns missing"):
        C.parse_csv(PT.replace(b"ccode,", b"cow,", 1), strict=False)


def test_coup_atlas_tables_validate() -> None:
    out = C.build(PAGE, PT, csv_url=C.parse_page(PAGE).csv_url, crosswalk=_crosswalk(), qid_for_iso3={}, strict=False)
    for ds in ("coup_events", "coups_cy"):
        frame, unmapped = validate_atlas_frame(
            atlas_frame(out[ds]), source="powell_thyne", dataset=ds, raw_sha256s=[out["sha256"]]
        )
        assert frame.height > 0
        assert {u["provider_geo"] for u in unmapped} <= {"cow:365", "cow:817"}  # USSR 1991, South Vietnam
    cy = {(r["indicator"], r["iso3"], r["period_start"].year): r["value"] for r in out["coups_cy"]}
    assert cy[("COUPS_SUCCESSFUL", "CHL", 1973)] == "1"
    total = sum(int(v) for (ind, _i, _y), v in cy.items() if ind == "COUP_EVENTS")
    assert total == len(out["events"])  # exact counts, nothing dropped
    niger = [r for r in out["coup_events"] if r["period_start"] == date(2026, 8, 29)]
    assert niger and "provisional" in json.loads(niger[0]["flags"])
    ussr = next(r for r in out["coup_events"] if r["provider_geo"] == "cow:365")
    assert ussr["iso3"] is None and ussr["entity_qid"] == "Q15180"


def test_coup_kg_records() -> None:
    out = C.build(PAGE, PT, csv_url=C.parse_page(PAGE).csv_url, crosswalk=_crosswalk(),
                  qid_for_iso3=load_qid_for_iso3(), strict=False)  # fmt: skip
    eid = "ptcoup:155:1973-09-11"
    st = {(s.subject, s.property): s for s in out["statements"]}
    assert st[(eid, "coup:outcome")].value == {"type": "string", "value": "successful"}
    assert st[(eid, "P585")].value == {"type": "time", "time": "1973-09-11", "precision": 11}
    assert st[(eid, "coup:against_state")].object_qid.startswith("Q")
    assert all(e.kind == "event" for e in out["entities"] if e.qid.startswith("ptcoup:"))
    keys = [s.key for s in out["statements"]]
    assert len(keys) == len(set(keys))


# ----------------------------------------------------------------------------- Stata reader


def test_dta_reader_and_subset_round_trip() -> None:
    fr = L.read_dta(DTA)
    assert fr.release == 118 and fr.nobs == 165 and "dbpediauri" in fr.names
    sub = subset_dta(DTA, [0, 5])
    fr2 = L.read_dta(sub)
    assert fr2.nobs == 2
    for name in fr.names:
        assert fr2.columns[name] == [fr.columns[name][0], fr.columns[name][5]]


def test_dta_reader_refuses_truncated_and_unknown() -> None:
    with pytest.raises(SourceResponseError, match=r"section map|data section"):
        L.read_dta(DTA[: len(DTA) - 5000])
    with pytest.raises(SourceResponseError, match="release"):
        L.read_dta(DTA.replace(b"<release>118</release>", b"<release>115</release>", 1))
    with pytest.raises(SourceResponseError):
        L.read_dta(b"not a stata file")


# ----------------------------------------------------------------------------- Archigos


def test_archigos_parse() -> None:
    spells, rep = L.parse_archigos(DTA, strict=False)
    assert rep["rows"] == 165 and rep["release"] == 118
    hon = [s for s in spells if s.obsid == "HON-2014"]
    assert hon and hon[0].leader == "Hernández"  # Windows-1252 bytes in Archigos 4.1, decoded and reported
    assert any(f["variable"] == "leader" for f in rep["encoding_fallbacks"])
    usa = next(s for s in spells if s.obsid == "USA-1869")
    assert usa.enwiki_title == "Ulysses S. Grant" and usa.yrborn == 1822
    assert all(s.yrdied is None or s.yrdied > 0 for s in spells)  # -777 / -999 codes are not years
    with pytest.raises(SourceResponseError, match="truncated"):
        L.parse_archigos(DTA, strict=True)


def test_archigos_category_drift() -> None:
    bad = DTA.replace(
        b"Removed by Military, without Foreign Support", b"Removed by Militaryy without Foreign Support", 1
    )
    with pytest.raises(SourceResponseError, match="categories outside the codebook"):
        L.parse_archigos(bad, strict=False)


def test_dbpedia_title() -> None:
    wrapped = (
        "https://urldefense.proofpoint.com/v2/url?u=http-3A__dbpedia.org_resource_Alfonso-5FL-25C3-25B3pez-5F"
        "Pumarejo&d=BQID-g&c=x&r=y&m=z&s=w&e="
    )
    assert L.dbpedia_title(wrapped) == "Alfonso López Pumarejo"
    assert L.dbpedia_title("http://dbpedia.org/resource/Getúlio_Vargas") == "Getúlio Vargas"
    assert L.dbpedia_title("NA") is None
    with pytest.raises(SourceResponseError):
        L.dbpedia_title("http://example.org/resource/X")


def test_archigos_atlas_table() -> None:
    out = L.build_archigos(DTA, crosswalk=_crosswalk(), qid_for_iso3={}, pt_csv=PT, pt_file="pt_20260829.csv",
                           strict=False)  # fmt: skip
    validate_atlas_frame(
        atlas_frame(out["leaders_cy"]), source="archigos", dataset="leaders_cy", raw_sha256s=[out["sha256"]]
    )
    cells = {(r["indicator"], r["iso3"], r["period_start"].year): r["value"] for r in out["leaders_cy"]}
    assert cells[("LEADER_EXITS|exitcode=removed_by_military_without_foreign_support", "CHL", 1973)] == "1"
    assert int(cells[("LEADERS_IN_OFFICE", "CHL", 1973)]) == 2  # Allende then Pinochet
    # still-in-office spells (censored 2015-12-31) are not exits
    still = [s for s in out["spells"] if s.censored]
    assert still and all(("LEADER_EXITS", None, 2015) != k for k in cells)


def test_coup_removals_claims_rest_on_archigos_alone() -> None:
    out = L.build_archigos(DTA, crosswalk=_crosswalk(), qid_for_iso3=load_qid_for_iso3(), pt_csv=PT,
                           pt_file="pt_20260829.csv", strict=False)  # fmt: skip
    removals = out["removals"]
    allende = next(r for r in removals if r.spell.ccode == 155 and r.spell.end == date(1973, 9, 11))
    assert allende.pt_event is not None and allende.pt_event.event_id == "ptcoup:155:1973-09-11"
    assert allende.perpetrator == "military" and not allende.foreign_support
    claims = out["claims"]
    assert len(claims) == len(removals) == out["join"]["coup_removals"]
    c = next(x for x in claims if x["cause_id"] == allende.event_id)
    assert c["effect_id"] == f"archigos:exit:{allende.spell.obsid}" and c["relation"] == "coded_outcome"
    assert c["raw_sha256"] == out["sha256"] and c["tier"] == "T1"
    assert c["codebook_term"] in L.CODEBOOK_DEFINITIONS and c["mechanism"] == allende.spell.exitcode
    assert "joined_event" not in c["selector"] and "ptcoup" not in json.dumps(c)  # no join inside a claim
    # the P&T event is reached only through a context edge
    ctx = [s for s in out["statements"] if s.property == "coup:same_state_and_day_as"]
    assert len(ctx) == out["join"]["same_state_and_day_as_successful_pt_coup"] > 0
    assert all(s.subject.startswith("archigos:coup:") and s.object_qid.startswith("ptcoup:") for s in ctx)
    # the leader removed hangs off Archigos's own coup event
    lr = [s for s in out["statements"] if s.property == "coup:leader_removed"]
    assert {s.subject for s in lr} == {r.event_id for r in removals}
    keys = [s.key for s in out["statements"]]
    assert len(keys) == len(set(keys))
    assert out["join"]["pt_events_with_several_matches"] == {}


def test_codebook_passages_and_claim_kwargs() -> None:
    cb = L.codebook()
    assert cb["version"] == "4.1" and "classic “military coup.”" in cb["definitions"]["exitcode_military"]
    try:
        from reva.causal import Codebook, DatasetClaim
    except ImportError:  # CAUSCORE not merged yet: the dicts are still checked above
        return
    Codebook(**cb)
    out = L.build_archigos(DTA, crosswalk=_crosswalk(), qid_for_iso3={}, strict=False)
    for c in out["claims"][:5]:
        DatasetClaim(**c)


# ----------------------------------------------------------------------------- same_as (Wikidata)


def test_wiki_iri_encoding() -> None:
    assert L.wiki_iri("Rutherford B. Hayes") == "https://en.wikipedia.org/wiki/Rutherford_B._Hayes"
    assert L.wiki_iri("Alfonso López Pumarejo") == "https://en.wikipedia.org/wiki/Alfonso_L%C3%B3pez_Pumarejo"
    assert L.wiki_iri("John Smith (politician)") == "https://en.wikipedia.org/wiki/John_Smith_(politician)"


def _wdqs(rows: list[dict[str, Any]]) -> tuple[bytes, str]:
    body = json.dumps({"head": {"vars": ["article", "item", "human", "birth"]}, "results": {"bindings": rows}}).encode()
    return body, hashlib.sha256(body).hexdigest()


def _b(title: str, qid: str, human: bool, birth: str | None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "article": {"type": "uri", "value": L.wiki_iri(title)},
        "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"},
        "human": {"type": "literal", "value": "true" if human else "false"},
    }
    if birth:
        d["birth"] = {"type": "literal", "value": birth}
    return d


def test_same_as_requires_identifier_human_and_birth_year() -> None:
    spells, _ = L.parse_archigos(DTA, strict=False)
    titled = [s for s in spells if s.enwiki_title and s.yrborn]
    a, b, c = titled[0], titled[1], titled[2]
    resp = _wdqs(
        [
            _b(a.enwiki_title or "", "Q100", True, f"+{a.yrborn}-04-27T00:00:00Z"),
            _b(b.enwiki_title or "", "Q200", True, f"+{(b.yrborn or 0) + 3}-01-01T00:00:00Z"),
            _b(c.enwiki_title or "", "Q300", False, None),
        ]
    )
    out, reasons = L.resolve_same_as(spells, [resp])
    assert out[a.leadid]["qid"] == "Q100" and out[a.leadid]["evidence"]["birth_year_check"] == "equal"
    assert b.leadid not in out and c.leadid not in out
    assert reasons["birth_year_disagrees"] == 1 and reasons["item_not_instance_of_human_Q5"] == 1
    assert reasons["linked"] == 1


def test_same_as_statement_names_its_wdqs_response() -> None:
    spells, _ = L.parse_archigos(DTA, strict=False)
    a = next(s for s in spells if s.enwiki_title and s.yrborn)
    resp = _wdqs([_b(a.enwiki_title or "", "Q100", True, f"+{a.yrborn}-01-01T00:00:00Z")])
    out = L.build_archigos(DTA, crosswalk=_crosswalk(), qid_for_iso3={}, same_as_responses=[resp], strict=False)
    st = [s for s in out["statements"] if s.property == "archigos:same_as"]
    assert len(st) == 1 and st[0].object_qid == "Q100" and st[0].raw_sha256 == resp[1]


# ----------------------------------------------------------------------------- REIGN-format leader list


def test_leaderlist_parse_and_defects() -> None:
    spells, rep = L.parse_leaderlist(LL, strict=False)
    assert rep["updated"] == "2026-04-14"
    defects = {d["row"]: d["defect"] for d in rep["provider_defects"]}
    assert set(defects) == {1117, 1530, 2242}
    isl = next(s for s in spells if s.row == 1530)
    assert isl.start_precision == 9 and isl.start == date(2024, 1, 1)  # day/month not "corrected"
    mne = next(s for s in spells if s.row == 1117)
    assert mne.end_month is None
    out = L.build_leaderlist(LL, file="leaderlist.xlsx", crosswalk=_crosswalk(), qid_for_iso3={}, strict=False)
    frame, _ = validate_atlas_frame(
        atlas_frame(out["leaders_cy"]), source="reign", dataset="leaders_cy", raw_sha256s=[out["sha256"]]
    )
    assert frame.height > 100
    st = [s for s in out["statements"] if s.selector["row"] == 2242]
    assert st and st[0].end_precision == 9


def test_leaderlist_header_drift() -> None:
    import io
    import zipfile

    sub = subset_xlsx(LL, "leaderlist", [2])
    L.parse_leaderlist(sub, strict=False)
    src = zipfile.ZipFile(io.BytesIO(sub))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            data = src.read(n)
            z.writestr(n, data.replace(b">stateabb<", b">state<") if n.endswith("sheet1.xml") else data)
    with pytest.raises(SourceResponseError, match="header"):
        L.parse_leaderlist(buf.getvalue(), strict=False)


# ----------------------------------------------------------------------------- specs, catalog, fixtures


def test_specs_discovered() -> None:
    from reva.atlas.sources import get_source

    for name, module in (
        (C.CONNECTOR_NAME, "atlas_coups"),
        (L.ARCHIGOS_NAME, "atlas_leaders"),
        (L.LEADERLIST_NAME, "atlas_leaders"),
    ):
        e = get_source(name)
        assert e.module.endswith(module) and e.spec.max_response_bytes <= 10_000_000


def test_catalog_series_exist_in_tables() -> None:
    from reva.atlas.catalog import load_catalog

    cat = load_catalog(strict=False)
    concepts = cat.by_domain("regime_events")
    assert len(concepts) == 11 and all(c.theme == "politics" for c in concepts)
    pt = C.build(PAGE, PT, csv_url=C.parse_page(PAGE).csv_url, crosswalk=_crosswalk(), qid_for_iso3={}, strict=False)
    ar = L.build_archigos(DTA, crosswalk=_crosswalk(), qid_for_iso3={}, strict=False)
    ll = L.build_leaderlist(LL, file="leaderlist.xlsx", crosswalk=_crosswalk(), qid_for_iso3={}, strict=False)
    have = {("powell_thyne", "coups_cy", r["indicator"]) for r in pt["coups_cy"]}
    have |= {("archigos", "leaders_cy", r["indicator"]) for r in ar["leaders_cy"]}
    have |= {("reign", "leaders_cy", r["indicator"]) for r in ll["leaders_cy"]}
    for c in concepts:
        for s in c.source_order:
            assert (s.source, s.dataset, s.indicator) in have, (c.id, s)


def test_fixture_subsets_are_documented() -> None:
    for name in ("atlas.coups", "atlas.archigos", "atlas.reign_leaders"):
        man = json.loads((FIX / name / "manifest.json").read_text())
        for e in man:
            body = (FIX / name / e["file"]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == e["sha256"] and e["licence"]
    # the P&T fixture keeps the provider's original lines (CRLF) byte for byte
    assert subset_csv(PT, [2, 3]).split(b"\r\n")[1] == PT.split(b"\r\n")[1]
