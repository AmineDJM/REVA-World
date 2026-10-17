"""V6POLITICS connectors offline: parsers on the captured fixture excerpts (real bytes fetched 2026-10-17), event keys,
derived change rows, predicates, families and taxonomy entries (docs/dev/v6_politics.md)."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import (
    atlas_nelda,
    atlas_oxcgrt,
    atlas_parlgov_kg,
    atlas_politics_derived,
    atlas_whogov,
)
from reva.adapters.connectors import v6politics_common as vc
from reva.adapters.connectors.atlas_geopol_common import crosswalk_from_countrycode_tarball, crosswalk_keys
from reva.atlas.tables import ATLAS_SCHEMA, validate_atlas_frame
from reva.kernel.errors import SourceResponseError

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "source"


def _fixture(conn: str, url_end: str) -> bytes:
    for e in json.loads((FIX / conn / "manifest.json").read_text()):
        if e["url"].endswith(url_end):
            body = (FIX / conn / e["file"]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == e["sha256"]
            assert e["licence"]
            return bytes(body)
    raise AssertionError(f"no fixture {conn} {url_end}")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ----------------------------------------------------------------------------- ParlGov
def _parlgov() -> tuple[dict[str, bytes], dict[str, str]]:
    bodies = {
        k: _fixture("atlas.parlgov_cabinets", f"{fid}?format=original")
        for k, (fid, _) in atlas_parlgov_kg.FILES.items()
    }
    return bodies, {k: _sha(v) for k, v in bodies.items()}


def test_parlgov_cabinets_and_events() -> None:
    bodies, shas = _parlgov()
    out = atlas_parlgov_kg.build(bodies, shas, qids={"DEU": "Q183"}, live=False)
    validate_atlas_frame(out["cabinets"], source="parlgov", dataset="cabinets", raw_sha256s=[shas["cabinet"]],
                         geo_scheme="iso3")  # fmt: skip
    validate_atlas_frame(out["cabinets_cy"], source="parlgov", dataset="cabinets_cy", raw_sha256s=[shas["cabinet"]],
                         geo_scheme="iso3")  # fmt: skip
    cab = out["cabinets"]
    merkel1 = cab.filter((pl.col("period_start") == date(2005, 11, 22)) & (pl.col("indicator") == "CABINET_PARTIES"))
    assert merkel1.height == 1 and merkel1["value"][0] == "2"  # CDU/CSU + SPD (ParlGov codes CDU and CSU as one)
    flags = json.loads(merkel1["flags"][0])
    assert flags["cabinet_name"] == "Merkel I" and "computed" in flags
    sts = out["statements"]
    starts = {s.subject: s.value["time"] for s in sts if s.property == "P580"}
    assert all(k.startswith("parlgov:cabinet:") for k in starts)
    assert "2005-11-22" in starts.values()
    # every cabinet points to an election entity that is also loaded, every party object exists
    ent_ids = {e.qid for e in out["entities"]}
    for s in sts:
        if s.object_qid and s.object_qid.startswith("parlgov:") and s.property != "parlgov:previous_cabinet":
            assert s.object_qid in ent_ids or s.property == "parlgov:previous_election", s
    assert {s.object_qid for s in sts if s.property == "P17"} == {"Q183"}
    results = [s for s in sts if s.property == "parlgov:party_result"]
    assert results and all("vote_share_percent" in s.qualifiers or "seats" in s.qualifiers for s in results)


def test_parlgov_refuses_inconsistent_cabinet_rows() -> None:
    bodies, _ = _parlgov()
    text = bodies["cabinet"].decode()
    lines = text.splitlines(keepends=True)
    bad = lines[1].replace(",0,", ",7,", 1)  # caretaker / cabinet_party code outside 0/1
    with pytest.raises(SourceResponseError):
        atlas_parlgov_kg.parse_cabinets("".join([lines[0], bad, *lines[2:]]).encode())


# ----------------------------------------------------------------------------- NELDA
def _crosswalk() -> object:
    return crosswalk_from_countrycode_tarball(_fixture("atlas.nelda", "countrycode_1.9.0.tar.gz"), crosswalk_keys())


def test_nelda_rounds_dates_and_record() -> None:
    tab = _fixture("atlas.nelda", "/2427654")
    rec = _fixture("atlas.nelda", "doi:10.7910/DVN/DDNWOR")
    cb = _fixture("atlas.nelda", "/2427655")
    meta = atlas_nelda.check_record(rec, tab=tab, codebook=cb)
    assert meta["terms_of_use"] == "public" and meta["tab_original"].endswith(".dta")
    with pytest.raises(SourceResponseError):
        atlas_nelda.check_record(rec, tab=tab + b"x", codebook=cb)
    out = atlas_nelda.build(tab, crosswalk=_crosswalk(), qid_for_iso3={"USA": "Q30", "FRA": "Q142"}, live=False)  # type: ignore[arg-type]
    rounds = {r.election_id: r for r in out["rounds"]}
    assert rounds["220-1981-0426-P2"].day == date(1981, 5, 10) and rounds["220-1981-0426-P2"].round == 2
    sudan = rounds["625-1974-0400-L1"]
    assert sudan.precision == 10 and sudan.day == date(1974, 4, 1)
    f = out["election_rounds"]
    validate_atlas_frame(f, source="nelda", dataset="election_rounds", raw_sha256s=[out["sha256"]])
    validate_atlas_frame(out["elections_cy"], source="nelda", dataset="elections_cy", raw_sha256s=[out["sha256"]])
    sd = f.filter(pl.col("indicator") == "ELECTION_ROUND|tr=L1").filter(pl.col("provider_geo") == "gw:625")
    assert sd["period_start"][0] == date(1974, 4, 1) and sd["period_end"][0] == date(1974, 5, 1)
    q24 = f.filter((pl.col("indicator") == "nelda24|tr=P2") & (pl.col("provider_geo") == "gw:220"))
    assert q24["value_text"][0] in ("yes", "no", "N/A", "unclear")
    assert json.loads(q24["flags"][0])["question"] == "Did the incumbent's party lose?"
    dates = [s for s in out["statements"] if s.property == "P585"]
    assert {s.subject for s in dates} == {f"nelda:election:{k}" for k in rounds}
    p17 = {s.subject: s.object_qid for s in out["statements"] if s.property == "P17"}
    assert p17["nelda:election:002-1946-1105-L1"] == "Q30"
    assert all(s.value["value"] in ("yes", "no", "N/A", "unclear") for s in out["statements"]
               if s.property == "nelda:coded_answer")  # fmt: skip


def test_nelda_questions_complete() -> None:
    assert sorted(atlas_nelda.QUESTIONS) == list(range(1, 59))
    assert set(atlas_nelda.KG_VARIABLES) <= set(atlas_nelda.QUESTIONS) - atlas_nelda.FREE_TEXT
    # ligatures restored, never the PDF's broken text layer
    assert not any(t in q for q, _ in atlas_nelda.QUESTIONS.values() for t in (" rst ", " oce", "signicant"))


# ----------------------------------------------------------------------------- OxCGRT
def test_oxcgrt_licence_codebook_and_changes() -> None:
    lic = atlas_oxcgrt.check_licence(_fixture("atlas.oxcgrt", "README.md"), _fixture("atlas.oxcgrt", "LICENSE.txt"))
    assert lic["licence_head"] == "Attribution 4.0 International"
    with pytest.raises(SourceResponseError):
        atlas_oxcgrt.check_licence(b"no licence here", _fixture("atlas.oxcgrt", "LICENSE.txt"))
    cb = atlas_oxcgrt.parse_codebook(_fixture("atlas.oxcgrt", "documentation_and_codebook.md"))
    assert cb["C6"]["coding"].startswith("0 - no measures")
    body = _fixture("atlas.oxcgrt", "OxCGRT_compact_national_v1.csv")
    idx, chg, fis, rep = atlas_oxcgrt.parse_compact(body, raw_sha256=_sha(body), min_rows=1)
    for name, f in (("indices_daily", idx), ("policy_changes", chg), ("fiscal_announcements", fis)):
        validate_atlas_frame(f, source="oxcgrt", dataset=name, raw_sha256s=[_sha(body)], geo_scheme="iso3")
    assert rep["countries"] == 2 and rep["change_rows"] == chg.height > 0
    # Aruba school closing: 0 -> 3 on 2020-03-16 (a change row carries the previous level and date)
    abw = chg.filter((pl.col("provider_geo") == "ABW") & (pl.col("indicator") == "C1M")).sort("period_start")
    first = abw.row(0, named=True)
    fl = json.loads(first["flags"])
    assert first["period_start"] == date(2020, 3, 16) and first["value"] == "3.00"
    assert fl["previous_value"] == "0.00" and fl["previous_date"] == "2020-03-15" and fl["direction"] == "stricter"
    # no row is a "change" to the same level and flag
    for r in chg.iter_rows(named=True):
        f = json.loads(r["flags"])
        assert (f["previous_value"], f.get("previous_flag")) != (r["value"], f.get("flag"))
    assert set(idx["indicator"].unique()) <= set(atlas_oxcgrt.INDICES)


def test_oxcgrt_refuses_subnational_rows() -> None:
    body = _fixture("atlas.oxcgrt", "OxCGRT_compact_national_v1.csv").decode()
    head, first, *rest = body.splitlines(keepends=True)
    with pytest.raises(SourceResponseError):
        atlas_oxcgrt.parse_compact((head + first.replace("NAT_TOTAL", "STATE_TOTAL") + "".join(rest)).encode(),
                                   raw_sha256="0" * 64, min_rows=1)  # fmt: skip


# ----------------------------------------------------------------------------- WhoGov
def test_whogov_frames_and_kg() -> None:
    rec = _fixture("atlas.whogov", "doi:10.18710/OLPOZK")
    cs = _fixture("atlas.whogov", "/290057")
    from reva.adapters.connectors.atlas_society_common import check_dataverse_record

    meta = check_dataverse_record(rec, doi=atlas_whogov.DOI, files={290057: (atlas_whogov.CS_FILE, cs)},
                                  licences=("CC BY 4.0",))  # fmt: skip
    assert meta["licence"] == "CC BY 4.0"
    assert atlas_whogov.README_LICENCE in _fixture("atlas.whogov", "/295623").decode()
    rows = atlas_whogov.parse_cs(cs, min_rows=1)
    cy, gs, rep = atlas_whogov.build_frames(rows, raw_sha256=_sha(cs))
    govs = rep.pop("_govs")
    validate_atlas_frame(cy, source="whogov", dataset="cabinet_cy", raw_sha256s=[_sha(cs)], geo_scheme="iso3")
    validate_atlas_frame(gs, source="whogov", dataset="government_starts", raw_sha256s=[_sha(cs)], geo_scheme="iso3")
    merz = gs.filter((pl.col("indicator") == "LEADER_START") & (pl.col("provider_geo") == "DEU")
                     & (pl.col("period_start") == date(2025, 5, 6)))  # fmt: skip
    assert merz["value_text"][0] == "Friedrich Merz"
    sun = cy.filter(pl.col("provider_geo") == "SUN")
    assert sun.height > 0 and sun["iso3"].null_count() == sun.height  # historical code kept unmapped
    ents, sts, krep = atlas_whogov.kg_records(govs, raw_sha256=_sha(cs), qids={"DEU": "Q183", "ALB": "Q222"})
    assert len(ents) == len(govs) == krep["governments"]
    assert all(e.qid.startswith("whogov:government:") and e.kind == "cabinet" for e in ents)
    assert {s.property for s in sts} <= {"P580", "P582", "P17", "whogov:government_name"}


# ----------------------------------------------------------------------------- derived
def _excerpt(name: str) -> pl.DataFrame:
    return pl.read_csv(REPO / "tests" / "fixtures" / "v6politics" / name, schema=ATLAS_SCHEMA,
                       try_parse_dates=True)  # fmt: skip


def test_bis_rate_changes_from_excerpt() -> None:
    f = _excerpt("bis_cbpol_d_xm_excerpt.csv")
    out, rep = atlas_politics_derived.bis_changes(f, input_sha="a" * 64, compilation={"XM": "test compilation"})
    validate_atlas_frame(out, source="bis", dataset="cbpol_d_changes", raw_sha256s=sorted(set(f["raw_sha256"])))
    rows = out.sort("period_start").rows(named=True)
    assert rep["changes"] == len(rows) >= 2
    for r in rows:
        fl = json.loads(r["flags"])
        assert fl["previous_value"] != r["value"] and fl["bis_compilation"] == "test compilation"
        assert json.loads(r["selector"])["derived_from"] == "atlas.bis.cbpol_d"
    assert json.loads(rows[-1]["flags"])["held_until"] is None or rows[-1]["period_start"] <= date.fromisoformat(
        json.loads(rows[-1]["flags"])["held_until"]
    )


def test_cit_rate_changes_from_excerpt() -> None:
    f = _excerpt("oecd_cts_cit_usa_excerpt.csv")
    out, _ = atlas_politics_derived.cit_changes(f, input_sha="b" * 64)
    validate_atlas_frame(out, source="oecd", dataset="cts_cit_changes", raw_sha256s=sorted(set(f["raw_sha256"])))
    cit = out.filter(pl.col("indicator") == "CIT_CHANGE|targeting=ST|sector=S1311|unit_measure=PT_INC_TAX")
    assert cit.height == 1 and cit["period_start"][0] == date(2018, 1, 1) and cit["value"][0] == "21"
    assert json.loads(cit["flags"][0])["change_pp"] == "-14"


# ----------------------------------------------------------------------------- registry
def test_predicates_families_taxonomy() -> None:
    props = {p.pid for p in vc.load_predicates()}
    assert {"parlgov:party_result", "nelda:coded_answer", "whogov:government_name"} <= props
    fam = yaml.safe_load((REPO / "registry/kg/relations_v4.d/v6politics.yaml").read_text())
    assert fam["agent"] == "V6POLITICS" and {m["property"] for m in fam["mappings"]} <= props
    tax = yaml.safe_load((REPO / "registry/events/taxonomy.yaml").read_text())
    prefixes = {d["id_prefix"]: d["type"] for d in tax["dataset_kinds"]}
    assert prefixes["parlgov:election:"] == "election" and prefixes["nelda:election:"] == "election"
    assert prefixes["parlgov:cabinet:"] == prefixes["whogov:government:"] == "cabinet_formation"
    srcs = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    for spec in (atlas_parlgov_kg.atlas_source(), atlas_nelda.atlas_source(), atlas_oxcgrt.atlas_source(),
                 atlas_whogov.atlas_source(), *atlas_politics_derived.atlas_source()):  # fmt: skip
        assert spec.source_key in srcs, spec.source_key
