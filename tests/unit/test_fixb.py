"""FIXB: dataset-side fixes of the second causal audit (docs/dev/review_v3_causal_audit2.md N4, N5, N6, N8, N10,
N12) -- offline, no database.

Fixtures: ``tests/fixtures/source/atlas.{idmc,ucdp_dyadterm,ucdp_term,cowwars}`` (slices of the real 2026-10-17
downloads; the IDMC slice holds rows 149, 194-195, 198, 240, 650-652, 5355, 6299, 6842, 7431, 8720 and 18030 for these
tests, the COW one a reduced copy of the intra-state v5.1 zip).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.connectors import atlas_cowwars as W
from reva.adapters.connectors import atlas_idmc as I
from reva.adapters.connectors import atlas_ucdp_dyadterm as D
from reva.adapters.connectors import atlas_ucdp_term as T
from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors import causmore_common as cm
from reva.adapters.connectors.atlas_xlsx import Workbook
from reva.evidence.runner import FixtureHttp

PDFTOTEXT = shutil.which("pdftotext")


def _body(name: str, url: str) -> Any:
    return asyncio.run(FixtureHttp(name).get(url))


@pytest.fixture(scope="module")
def idmc_raw() -> Any:
    url, _ = I.resource_url(_body("atlas.idmc", I.PACKAGE_URL).body)
    return _body("atlas.idmc", url)


@pytest.fixture(scope="module")
def idmc(idmc_raw: Any) -> dict[str, Any]:
    return I.build(idmc_raw, country_qids={"CHN": "Q148"}, strict=False)


@pytest.fixture(scope="module")
def dyad() -> dict[str, Any]:
    return D.build(_body("atlas.ucdp_dyadterm", D.DATA_URL), strict=False)


def _rows_of(out: dict[str, Any], prop: str) -> dict[str, Any]:
    return {s.subject: s for s in out["statements"] if s.property == prop}


# ----------------------------------------------------------------------------- N4 anticipatory displacement


def test_n4_anticipatory_displacement_is_context_not_a_causal_claim(idmc: dict[str, Any]) -> None:
    claims = {c.selector["row"]: c for c in idmc["claims"]}
    ctx = {s.selector["row"]: s for s in idmc["statements"] if s.property == "idmc:displacement_in_anticipation_of"}
    # 8720 "Sri Lanka: Risk of Landslide ..." (preventive evacuations), 7431 "Mexico: Tsunami false alarm",
    # 18030 preventive evacuations, 5355 "Turkey: Landslide risk evacuations" (flag says without preventive)
    for row in (8720, 7431, 18030, 5355):
        assert row not in claims, row
        s = ctx[row]
        assert s.object_qid.startswith("idmc:event:") and s.subject.startswith("idmc:displacement:")
        assert "not a causal claim" in s.qualifiers["note"]
        assert s.qualifiers["definition"]["README!A24"] == I.README_A24
    assert ctx[8720].qualifiers["basis"] == ["displacement_occurred", "event_name:risk"]
    assert ctx[7431].qualifiers["basis"] == ["displacement_occurred", "event_name:false alarm"]
    assert ctx[18030].qualifiers["basis"] == ["displacement_occurred"]
    assert ctx[5355].qualifiers["basis"] == ["event_name:risk"] and ctx[5355].qualifiers["source_flags_disagree"]
    # "Nicaragua: Evacuated - ..." names no risk and the flag reports no preventive evacuation: an ordinary claim
    assert 6842 in claims and 6842 not in ctx
    # the figure and the displacement record stay; its label says it is anticipatory
    ent = {e.qid: e for e in idmc["entities"]}
    assert "in anticipation of the hazard" in ent[ctx[8720].subject].labels["en"]
    assert ctx[8720].subject in _rows_of(idmc, "idmc:internal_displacements")
    assert not any(I.anticipatory_basis({"Displacement occurred": c.scope.get("displacement_occurred", ""),
                                         "Event Name": ""}) for c in idmc["claims"])  # fmt: skip
    assert "causal" not in cm.CAUSMORE_PROPERTIES["idmc:displacement_in_anticipation_of"][2].replace("not causal", "")


def test_n4_anticipatory_name_words() -> None:
    def b(name: str, flag: str = "") -> list[str]:
        return I.anticipatory_basis({"Event Name": name, "Displacement occurred": flag})

    assert b("Switzerland: (risk) snow avalanche - Mayens de Conthey - 03/01/2018") == ["event_name:risk"]
    assert b("Viet Nam: Threat of landslide - An Giang - 05/08/2021") == ["event_name:threat of"]
    assert b("France: Pre-emptive evacuation - Occitanie (Aude) - 12/05/2020") == ["event_name:pre-emptive"]
    assert b("Rwanda: Preventative displacement for torrential rain- Countrywide- 12/12/2019")
    assert b("China: Flood - 01/01/2009") == [] and b("Brisk flood") == []
    assert b("x", I.PREVENTIVE) == ["displacement_occurred"] and b("x", I.NOT_PREVENTIVE) == []


# ----------------------------------------------------------------------------- N5 1 January starts


def test_n5_1_january_start_is_never_used_as_a_day(idmc: dict[str, Any]) -> None:
    starts = _rows_of(idmc, "P580")
    names = {e.qid: e.labels["en"] for e in idmc["entities"]}
    by_name = {names[k].split(" (IDMC record")[0]: v for k, v in starts.items()}
    ph = by_name["Philippines: Storm - 01/01/2008"]
    assert ph.value == {"type": "time", "time": "2008-01-01", "precision": 9}
    assert ph.qualifiers["coded_start"] == "2008-01-01" and ph.qualifiers["approximate"] is True
    assert ph.qualifiers["day_not_relied_on"]["name_carries_01_01"] is True
    assert "does not say whether" in ph.qualifiers["day_not_relied_on"]["note"]
    aila = by_name["Cyclone 'aila'"]  # 1 January start, a named event: year precision too, said so
    assert aila.value["precision"] == 9 and aila.qualifiers["day_not_relied_on"]["name_carries_01_01"] is False
    # a name ending 01/01/2019 whose coded start is 10 January keeps the coded day
    my = next(v for k, v in by_name.items() if k.startswith("Myanmar: Riverbank erosion"))
    assert my.value == {"type": "time", "time": "2019-01-10", "precision": 11} and "coded_start" not in my.qualifiers
    assert by_name["China: Yangtze River floods (1st wave) - 30/06/2016"].value["precision"] == 11
    # nothing invented: every year-precision start keeps the coded value verbatim
    assert all(s.qualifiers.get("coded_start") == s.value["time"] for s in starts.values() if s.value["precision"] == 9)


# ----------------------------------------------------------------------------- N12 REVA-made identities


def test_n12_reva_made_identities_are_labelled_and_collapsed_rows_recorded(idmc: dict[str, Any]) -> None:
    ents = {e.qid: e for e in idmc["entities"]}
    noid = [e for q, e in ents.items() if q.startswith("idmc:event:r")]
    assert noid and all(I.REVA_IDENTITY_FR in e.labels["fr"] and I.REVA_IDENTITY_EN in e.labels["en"] for e in noid)
    assert all("identité créée par REVA" in e.descriptions["fr"] for e in noid)
    assert all(not q.startswith("idmc:event:r") for q, e in ents.items() if "Event ID" in e.descriptions.get("en", "")
               and I.REVA_IDENTITY_EN not in e.descriptions["en"])  # fmt: skip
    assert ents["idmc:event:1"].labels == {"en": "China: Yangtze River floods (1st wave) - 30/06/2016"}
    dups = [s for s in idmc["statements"] if s.property == "idmc:source_duplicate_rows"]
    # rows 194/195 (Benin, 2 copies) and 650/651/652 (United Kingdom, 3 copies), each loaded once
    assert sorted(s.qualifiers["rows"] for s in dups) == [[194, 195], [650, 651, 652]]
    assert {s.qualifiers["count"] for s in dups} == {2, 3}
    assert all(s.selector["rows"] == s.qualifiers["rows"] for s in dups)
    disp = Counter(c.effect_id for c in idmc["claims"])
    assert all(n == 1 for n in disp.values())
    assert sorted(idmc["anomalies"]["identical_rows_loaded_once"]) == ["194,195", "650,651,652"]


# ----------------------------------------------------------------------------- N8 quote_source


def test_n8_idmc_quote_source_is_a_cell_of_the_archived_workbook(idmc_raw: Any, idmc: dict[str, Any]) -> None:
    sha = idmc["sha"]
    dl = cm.delivery(
        I.CLAIMS_DATASET, claims=idmc["claims"], raw_sha256s=[sha], retrieved_at=idmc_raw.retrieved_at,
        origin="test_fixture", documents_sha256={"idmc_readme": sha}, archived={"idmc_readme": sha},
    )  # fmt: skip
    src = dl.codebook["sources"]["disaster_displacement"]
    assert src["sha256"] == sha and (src["sheet"], src["cell"]) == ("README", "A24")
    assert f"idmc_readme sha256 {sha}" in dl.codebook["version"]
    readme = Workbook(idmc_raw.body).table("README")
    cells = {f"{chr(65 + j)}{i + 1}": v for i, r in enumerate(readme) for j, v in enumerate(r) if v not in (None, "")}
    for term, d in cm.dataset_entry(I.CLAIMS_DATASET)["definitions"].items():
        s = dl.codebook["sources"][term]
        assert " ".join(str(cells[s["cell"]]).split()) == " ".join(d["text"].split()), term
    assert dl.raw_sha256s == [sha]


def _pdf_page(body: bytes, page: int, tmp: Path) -> str:
    assert PDFTOTEXT is not None
    f = tmp / "cb.pdf"
    f.write_bytes(body)
    out = subprocess.run(
        [PDFTOTEXT, "-layout", "-f", str(page), "-l", str(page), str(f), "-"], capture_output=True, check=True
    ).stdout.decode("utf-8")
    out = "\n".join(ln for ln in out.split("\n") if not re.fullmatch(r"\s*\d{1,3}\s*", ln))
    return re.sub(r"\s+", " ", re.sub(r"-\n\s*", "-", out)).strip()


@pytest.mark.skipif(PDFTOTEXT is None, reason="poppler pdftotext not installed")
def test_n8_cow_and_ucdp_quote_sources_point_at_the_pinned_pdf_pages(tmp_path: Path) -> None:
    for kind in W.KINDS:
        ds, key = W.DATASETS[kind], W.CODEBOOKS[kind]
        body = _body("atlas.cowwars", W.codebook_url(kind)).body
        sha = cm.check_pdf(key, body)["sha256"]
        cb = cm.codebook(ds, {key: sha}, archived={key: sha})
        src = cb["sources"]["Outcome"]
        assert src["sha256"] == sha == cm.document(key)["sha256"] and src["page"] >= 1
        assert " ".join(cb["definitions"]["Outcome"].split()) in _pdf_page(body, src["page"], tmp_path), kind
    body = _body("atlas.ucdp_dyadterm", cm.document("ucdp_term_codebook")["download_url"]).body
    sha = cm.check_pdf("ucdp_term_codebook", body)["sha256"]
    cb = cm.codebook("ucdp_term_dyad", {"ucdp_term_codebook": sha}, archived={"ucdp_term_codebook": sha})
    for k in range(1, 7):
        src = cb["sources"][f"d_outcome={k}"]
        assert src["sha256"] == sha and src["page"] in (3, 4)
        assert cb["definitions"][f"d_outcome={k}"][:60] in _pdf_page(body, src["page"], tmp_path)
    # conflict level (CAUSDATA registry): same convention
    cc = cd.codebook("term_conflict", {"term_codebook": sha}, archived={"term_codebook": sha})
    assert cc["sources"]["c_outcome=1"]["sha256"] == sha and cc["sources"]["c_outcome=1"]["page"] == 3
    # precision texts quoted by N10 are verbatim at their pages
    for (which, level, code), text in cd.UCDP_PRECISION_TEXT.items():
        pages = cd.UCDP_PRECISION_PAGES[(which, "conflict" if which == "start" else level)]
        joined = " ".join(_pdf_page(body, p, tmp_path) for p in pages)
        assert text in joined, (which, level, code)


def test_n8_no_quote_source_without_an_archived_file() -> None:
    assert "sources" not in cm.codebook("cow_inter", {"cow_inter_codebook": "a" * 64})
    assert "sha256 aaaaaaaaaaaa)" in cd.codebook("term_conflict", {"term_codebook": "a" * 64})["version"]


def test_n8_cow_v51_numbering_evidence_from_the_archived_zip() -> None:
    intra = W.parse("intra", _body("atlas.cowwars", W.file_url("intra")).body, strict=False)
    chk = W.v51_numbering_check(intra, _body("atlas.cowwars", W.V51_URL).body)
    assert chk["example_856"] == {"v4.1": "Eighth Colombia", "v5.1": "Romanian War of 1989"}
    assert W.V51_MEMBER in chk["v51_members"]
    assert any(x["WarNum"] == "856" for x in chk["shared_numbers_other_name"])
    assert "836" in chk["v41_numbers_absent_in_v51"] or chk["shared_numbers"] >= 1
    with pytest.raises(cd.CausdataError):
        W.v51_numbering_check(intra, b"not a zip")


# ----------------------------------------------------------------------------- N6 several terminal rows


def test_n6_every_terminal_row_kept_and_differing_outcomes_contest_each_other(dyad: dict[str, Any]) -> None:
    by = {c.effect_id: c for c in dyad["claims"]}
    a, b = by["ucdp:dyadepisode:64901:termination:1967"], by["ucdp:dyadepisode:64901:termination:1973"]
    assert (a.codebook_term, b.codebook_term) == ("d_outcome=3", "d_outcome=2")
    assert a.contests == (f"ds:ucdp.downloads:term_dyad:{b.source_claim_id}",)
    assert b.contests == (f"ds:ucdp.downloads:term_dyad:{a.source_claim_id}",)
    for c in (a, b):
        note = c.scope["source_duplicate_terminal_rows"]
        assert note["outcomes_differ"] and c.scope["contested_by_source"] is True
        assert [r["year"] for r in note["terminal_rows"]] == [1967, 1973]
    assert "ucdp:dyadepisode:64901:termination" not in by
    # same outcome on both rows (1179801, low activity twice): both kept, flagged, no contest
    s1, s2 = by["ucdp:dyadepisode:1179801:termination:2020"], by["ucdp:dyadepisode:1179801:termination:2022"]
    assert not s1.contests and not s2.contests and not s1.scope["source_duplicate_terminal_rows"]["outcomes_differ"]
    ents = {e.qid: e for e in dyad["entities"]}
    assert ents["ucdp:dyadepisode:64901:termination"].kind == "superseded_record"
    dup = _rows_of(dyad, "ucdpterm:source_duplicate_terminal_rows")["ucdp:dyadepisode:64901"]
    assert "1967 (d_outcome 3" in dup.value["value"] and "1973 (d_outcome 2" in dup.value["value"]
    # ordinary episodes keep their 1.0.0 ids and keys
    assert by["ucdp:dyadepisode:39901:termination"].source_claim_id == "39901:d_outcome"
    keys = {s.key for s in dyad["statements"]}
    assert {"de39901:t:start", "de39901:t:end", "de39901:t:t:point"} <= keys
    assert "de64901:t:end" not in keys and {"de64901:t1967:end", "de64901:t1973:t:point"} <= keys


def test_n6_conflict_level_terminal_rows_kept() -> None:
    h = FixtureHttp("atlas.ucdp_term")
    raws = {k: asyncio.run(h.get(u)) for k, u in T.URLS.items()}
    import hashlib

    out = T.kg_and_claims(
        T.parse_term(raws["term"].body, strict=False),
        T.parse_pa(raws["pa"].body, strict=False),
        shas={k: hashlib.sha256(r.body).hexdigest() for k, r in raws.items()},
    )
    by = {c.effect_id: c for c in out["claims"]["term_conflict"]}
    a, b = by["ucdp:episode:20901:termination:1954"], by["ucdp:episode:20901:termination:1995"]
    assert a.contests and b.contests and a.scope["contested_by_source"]
    assert "ucdp:episode:20901:termination" not in by


# ----------------------------------------------------------------------------- N10 precision 6 / 7


def test_n10_ucdp_precision_6_assigned_and_7_missing() -> None:
    six = cd.ucdp_date("1988-12-31", "6", which="end", level="dyad")
    assert six.value == ("1988-01-01", 9) and six.missing is None
    assert six.qualifiers["source_date_certainty"]["certainty"] == "assigned"
    assert "subjective judgment" in six.qualifiers["source_date_certainty"]["text"]
    seven = cd.ucdp_date("1999-12-31", "7", which="end", level="dyad")
    assert seven.value is None and seven.missing is not None
    assert seven.missing["missing"] == {"kind": "missing", "reason": "unavailable"}
    assert "1999-12-31 is that placeholder" in seven.missing["note"] and "conflict dyad" in seven.missing["note"]
    assert (
        cd.ucdp_date("1999-12-31", "1").value == ("1999-12-31", 11) and not cd.ucdp_date("1999-12-31", "1").qualifiers
    )
    assert cd.ucdp_date("1999-12-31", "3").value == ("1999-12-01", 10)
    st = cd.missing_time_statement("x:y", "k", "P585", seven.missing, raw_sha256="a" * 64, selector={"row": 1})
    assert st.value == {"type": "somevalue"} and st.qualifiers["missing"]["kind"] == "missing"


def test_n10_dyad_claims_carry_the_typed_note(dyad: dict[str, Any]) -> None:
    c = next(x for x in dyad["claims"] if x.cause_id == "ucdp:dyadepisode:72501")
    assert c.scope["effect_time_certainty"]["code"] == "6"
    t = _rows_of(dyad, "P585")["ucdp:dyadepisode:72501:termination"]
    assert t.value["precision"] == 9 and t.qualifiers["source_date_certainty"]["certainty"] == "assigned"
    s = next(x for x in dyad["statements"] if x.key == "de72201:t:start")  # start_prec2 = 6
    assert s.value["precision"] == 9 and s.qualifiers["source_date_certainty"]["code"] == "6"


def test_connector_versions_bumped() -> None:
    assert (I.VERSION, D.VERSION, W.VERSION, T.CONNECTOR_VERSION) == ("1.1.0", "1.1.1", "1.1.0", "1.2.1")
    assert D.KG_REVISION.endswith("+reva-1.1.1")
