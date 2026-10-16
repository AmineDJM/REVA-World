"""A-SANCTIONS: TIES 4.1, EUSANCT 1.0, EU Sanctions Map and OFAC program list (offline, real captures).

Fixtures (tests/fixtures/source/atlas.{ties,eusanct,eu_sanctionsmap,ofac_programs}) are the live downloads of
2026-10-16: the full TIES workbook and OFAC page, a row slice of the EUSANCT case-level CSV in a tar, and eight
verbatim regimes of the EU map.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_sanctions as S
from reva.adapters.connectors.atlas_geopol_common import (
    StateEntityResolver,
    atlas_frame,
    crosswalk_from_countrycode_tarball,
    crosswalk_keys,
)
from reva.atlas.catalog import load_catalog
from reva.atlas.sources import atlas_sources
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FIXTURE_ROOT
from reva.kernel.errors import SourceResponseError
from reva.kg.model import PID_RE, QID_RE

REPO = Path(__file__).resolve().parents[2]
SHA = "0" * 64
DAY = date(2026, 10, 16)


def _fixture(name: str, url: str) -> bytes:
    man = json.loads((FIXTURE_ROOT / name / "manifest.json").read_text())
    e = next(e for e in man if e["url"] == url)
    return (FIXTURE_ROOT / name / e["file"]).read_bytes()


@pytest.fixture(scope="module")
def crosswalk() -> Any:
    return crosswalk_from_countrycode_tarball(
        _fixture("atlas.ties", "https://cran.r-project.org/src/contrib/countrycode_1.9.0.tar.gz"), crosswalk_keys()
    )


@pytest.fixture(scope="module")
def ties() -> tuple[list[S.Case], str]:
    cases, file = S.build_cases("ties", _fixture("atlas.ties", S.TIES_URL))
    return cases, file


@pytest.fixture(scope="module")
def eus() -> tuple[list[S.Case], str]:
    cases, file = S.build_cases("eusanct", _fixture("atlas.eusanct", S.EUSANCT_URL), strict=False)
    return cases, file


# --------------------------------------------------------------------------- xls reader + TIES parse
def test_xls_reader_reads_ties_workbook(ties: tuple[list[S.Case], str]) -> None:
    sheet, rows = S.read_xls_first_sheet(_fixture("atlas.ties", S.TIES_URL))
    assert sheet == "TIESv4.1.raw" and len(rows) == 1413 and rows[0][0] == "caseid" and len(rows[0]) == 66
    cases, file = ties
    assert len(cases) == 1412 and file == "TIESv4-1.xls#TIESv4.1.raw"
    with pytest.raises(SourceResponseError):
        S.read_xls_first_sheet(b"PK\x03\x04 not an xls")


def test_ties_case_fields(ties: tuple[list[S.Case], str]) -> None:
    by = {c.case_id: c for c in ties[0]}
    c = by["1945121601"]  # US -> USSR (COW 710 in the file is China: target 710), 1945-12-16 .. 1947-05-27
    assert c.start == S.Day(1945, 12, 16, 11) and c.end == S.Day(1947, 5, 27, 11) and c.end_basis == "coded"
    assert [s.ref for s in c.senders] == [("cow", 2)] and c.senders[0].primary and c.target == 710
    assert c.threat and c.imposition and c.imposition_start == S.Day(1946, 7, 29, 11)
    assert c.imposed_types == ["2", "7"] and c.outcome == 8 and c.institutions == ["4400"]
    # month/day 99 in the case id and blank start month/day: year precision only
    assert by["1990999901"].start == S.Day(1990) and by["1990999901"].target == S.EEC_EU_CODE
    # the five TIESCorrection.txt fixes are already in v4.1
    assert by["1990999901"].threat is True and by["1983011301"].imposition is True
    assert by["1973070501"].start == S.Day(1973, 1, 9, 11) and by["1973070501"].outcome == 8
    assert by["2002041106"].end is not None and by["2002041106"].end.year == 2004
    assert by["2002041103"].target == 490
    assert Counter(c.end_basis for c in ties[0]) == {"coded": 961, "no_end_coded": 286, "ongoing_as_of": 165}


def test_ties_truncation_guard() -> None:
    body = _fixture("atlas.ties", S.TIES_URL)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(S, "TIES_MIN_CASES", 5000)
        with pytest.raises(SourceResponseError, match="truncated"):
            S.parse_ties(body)


# --------------------------------------------------------------------------- EUSANCT parse
def test_eusanct_from_tar_and_dates(eus: tuple[list[S.Case], str]) -> None:
    cases, file = eus
    assert file == S.EUSANCT_MEMBER and len(cases) == 23
    with pytest.raises(SourceResponseError, match="truncated"):
        S.parse_eusanct(S.eusanct_csv_from_tar(_fixture("atlas.eusanct", S.EUSANCT_URL))[1])
    assert S._stata_date("30jun1950") == S.Day(1950, 6, 30, 11) and S._stata_date(".", "1991") == S.Day(1991)
    for c in cases:
        labels = {s.label for s in c.senders if s.label}
        assert labels <= {"EU", "US", "UN"} and labels
        for s in c.senders:
            if s.label == "EU":
                assert s.ref == ("org", "EU")
            if s.label == "US":
                assert s.ref == ("cow", 2)
    ongoing = [c for c in cases if c.end_basis == "ongoing_at_end_of_coverage"]
    assert ongoing and all(c.end is None for c in ongoing)
    with pytest.raises(SourceResponseError):
        S.eusanct_csv_from_tar(b"not a tar")


# --------------------------------------------------------------------------- atlas frames
def _frames(cases: list[S.Case], crosswalk: Any, last: int, file: str) -> dict[str, pl.DataFrame]:
    dyad, ctry = S.case_atlas_rows(cases, raw_sha256=SHA, crosswalk=crosswalk, last_year=last, file=file)
    src = cases[0].dataset
    return {
        "dyad_year": validate_atlas_frame(atlas_frame(dyad), source=src, dataset="dyad_year", raw_sha256s=[SHA])[0],
        "country_year": validate_atlas_frame(atlas_frame(ctry), source=src, dataset="country_year", raw_sha256s=[SHA])[
            0
        ],
    }


def test_ties_atlas_tables(ties: tuple[list[S.Case], str], crosswalk: Any) -> None:
    f = _frames(ties[0], crosswalk, 2005, ties[1])
    dy, ct = f["dyad_year"], f["country_year"]
    assert dy.height > 10_000 and ct.height > 5_000
    assert (dy["value"].cast(pl.Int64) > 0).all() and (ct["value"].cast(pl.Int64) > 0).all()  # never a zero
    # US -> Cuba: 1960 has an active case; partner in the indicator and flags
    us_cub = dy.filter((pl.col("indicator") == "CASES_ACTIVE|partner=CUB") & (pl.col("iso3") == "USA"))
    assert us_cub.height > 30
    fl = json.loads(us_cub.row(0, named=True)["flags"])
    assert fl["partner_iso3"] == fl["counterpart_iso3"] == "CUB" and fl["case_ids"] and "computed_from" in fl
    # USSR: historical state, no ISO3, its QID kept; EEC/EU code 1000 kept unmapped
    su = dy.filter(pl.col("provider_geo") == "cow:365").filter(pl.col("period_start") == date(1980, 1, 1))
    assert su.height and su["iso3"].is_null().all() and su["entity_qid"].drop_nulls().len() == su.height
    assert dy.filter(pl.col("provider_geo") == "cow:1000")["iso3"].is_null().all()
    assert dy.filter(pl.col("indicator").str.contains("partner_code=cow:1000")).height > 0
    # a case counted once per sender-year even with several rows of the same partner
    for r in dy.filter(pl.col("indicator").str.starts_with("CASES_STARTED")).head(200).iter_rows(named=True):
        assert int(r["value"]) == json.loads(r["flags"])["cases_total"]


def test_eusanct_atlas_tables(eus: tuple[list[S.Case], str], crosswalk: Any) -> None:
    f = _frames(eus[0], crosswalk, S.EUSANCT_LAST_YEAR, eus[1])
    geos = set(f["dyad_year"]["provider_geo"])
    assert {"org:EU", "org:UN", "cow:2"} <= geos
    org = f["dyad_year"].filter(pl.col("provider_geo").str.starts_with("org:"))
    assert org["iso3"].is_null().all()
    ongoing = f["country_year"].filter(pl.col("flags").str.contains("ongoing_at_end_of_coverage"))
    assert ongoing.height and ongoing["period_start"].max() <= date(2015, 1, 1)


def test_span_rules() -> None:
    assert S._span(S.Day(1990), S.Day(1992), "coded", None, 2005) == [1990, 1991, 1992]
    assert S._span(S.Day(1990), None, "ongoing_as_of", S.Day(1993), 2005) == [1990, 1991, 1992, 1993]
    assert S._span(S.Day(1990), None, "no_end_coded", None, 2005) == [1990]
    assert S._span(S.Day(2010), None, "ongoing_at_end_of_coverage", None, 2015) == list(range(2010, 2016))
    assert S._span(None, None, "coded", None, 2005) == []


# --------------------------------------------------------------------------- KG records + causal claims
def test_kg_records_and_claims(ties: tuple[list[S.Case], str], eus: tuple[list[S.Case], str], crosswalk: Any) -> None:
    from reva.adapters.connectors.atlas_geopol_common import load_qid_for_iso3

    res = StateEntityResolver(crosswalk, load_qid_for_iso3())
    ents, stmts = S.case_kg_records(ties[0], raw_sha256=SHA, resolver=res, file=ties[1])
    e2, s2 = S.case_kg_records(eus[0], raw_sha256=SHA, resolver=res, file=eus[1])
    ents, stmts = ents + e2, stmts + s2
    assert all(QID_RE.match(e.qid) for e in ents) and all(QID_RE.match(s.subject) for s in stmts)
    assert all(PID_RE.match(p) for p in S.SANCTIONS_PROPERTIES)
    used = {s.property for s in stmts}
    allowed = set(S.SANCTIONS_PROPERTIES) | set(S.KG_WIKIDATA_PIDS)
    assert used <= allowed, used - allowed
    assert {"P580", "P582", "P585"} <= used  # dates the event view and the causal grader read
    keys = [(s.subject.split(":")[0], s.key) for s in stmts]
    assert len(keys) == len(set(keys))
    kinds = Counter(e.kind for e in ents)
    assert kinds["sanction_episode"] == 1412 + 23 and kinds["coded_outcome"] > 1000
    # the case -> outcome statement is context; the causal claim goes to kg.causal_claim
    co = [st for st in stmts if st.property == "ties:outcome_record"]
    assert len(co) == sum(1 for c in ties[0] if c.outcome in S.TIES_OUTCOMES)
    assert all("causal_relation" not in st.qualifiers for st in co)
    claims = S.causal_claim_records(ties[0], source_key="ties.unc", dataset_revision="ties-4.1", raw_sha256=SHA,
                                    file=ties[1])  # fmt: skip
    claims += S.causal_claim_records(eus[0], source_key="eusanct.kondata", dataset_revision="eusanct-1.0",
                                     raw_sha256=SHA, file=eus[1])  # fmt: skip
    rel = Counter(c["relation"] for c in claims)
    assert set(rel) <= {"coded_outcome", "contributing_factor"}  # never reaction_to / precipitated
    for c in claims:
        assert c["source_tier"] == "T1" and c["selector"]["row"] >= 2 and c["selector"]["columns"][0] == "caseid"
        cb = S.TIES_CODEBOOK if c["cause"].startswith("ties:") else S.EUSANCT_CODEBOOK
        assert c["codebook_term"] in cb["definitions"]
        if c["relation"] == "contributing_factor":
            assert c["coded"]["code"] in (3, 4) and c["coded"]["hse_score"] >= 9 and c["polarity"] == "promotes"
            assert c["effect"].startswith("eusanct:policy_result:")
        else:
            assert c["polarity"] is None and ":outcome:" in c["effect"]
    # the threat -> imposition sequence is context, never a causal statement
    seq = [s for s in stmts if s.property == "ties:imposition_start"]
    assert seq and all("not a causal claim" in s.qualifiers["stage_sequence"] or "without prior threat"
                       in s.qualifiers["stage_sequence"] for s in seq)  # fmt: skip


def test_no_free_text_or_personal_fields(ties: tuple[list[S.Case], str], eus: tuple[list[S.Case], str],
                                         crosswalk: Any) -> None:  # fmt: skip
    res = StateEntityResolver(crosswalk, {})
    _, stmts = S.case_kg_records(ties[0] + eus[0], raw_sha256=SHA, resolver=res, file="f")
    blob = json.dumps([s.as_dict() for s in stmts], default=str)
    # TIES 'otherissue' coder note and EUSANCT 'link'/'comments' never leave the raw file
    assert "Russia wanted a large amount of compensation" not in blob
    assert "nytimes.com" not in blob and "otherissue" not in blob


def test_same_case_links_need_shared_id_datasource_and_target(
    ties: tuple[list[S.Case], str], eus: tuple[list[S.Case], str]
) -> None:
    links, rep = S.link_eusanct_to_ties(eus[0], ties[0], raw_sha256=SHA, file=eus[1])
    assert rep["linked"] == len(links) == 6 and rep["case_id_shared_but_target_differs"] == 2
    by = {c.case_id: c for c in ties[0]}
    for st in links:
        cid = st.subject.rsplit(":", 1)[1]
        assert st.object_qid == f"ties:case:{cid}" and by[cid].target == next(
            c.target for c in eus[0] if c.case_id == cid
        )


# --------------------------------------------------------------------------- EU map + OFAC
def test_eu_map_regimes() -> None:
    regs = S.parse_eu_map(_fixture("atlas.eu_sanctionsmap", S.EU_MAP_URL), strict=False)
    by = {g.id: g for g in regs}
    assert set(by) == {1, 2, 20, 10, 46, 61, 18, 40}
    assert by[1].last_amended == "2024-09-13"  # 1726178400 = 2024-09-12T22:00Z = 13 Sept, Brussels midnight
    assert by[1].adopted_by == "UN" and by[20].adopted_by == "UN and EU" and by[46].thematic
    rows = S.eu_map_rows(regs, raw_sha256=SHA, day=DAY)
    f = validate_atlas_frame(atlas_frame(rows), source="eusanctionsmap", dataset="regimes", raw_sha256s=[SHA])[0]
    them = f.filter(pl.col("provider_geo").str.starts_with("thematic:"))
    assert them.height == 1 and them["iso3"].is_null().all()
    n = f.filter(pl.col("indicator") == "REGIMES_LISTED").filter(pl.col("iso3") == "IRN")
    assert n["value"].to_list() == ["1"]
    assert all(len(x) <= 4000 for x in f["flags"])
    with pytest.raises(SourceResponseError):
        S.parse_eu_map(_fixture("atlas.eu_sanctionsmap", S.EU_MAP_URL))  # strict: 8 < 40 regimes


def test_fit_flags_shortens_the_longest_list() -> None:
    out = json.loads(S.fit_flags({"measure_types": [f"measure {i:03d} " * 4 for i in range(300)], "x": "y"}))
    assert len(json.dumps(out)) <= 4000 and out["measure_types_total"] == 300 and out["x"] == "y"


def test_ofac_programs() -> None:
    progs = S.parse_ofac(_fixture("atlas.ofac_programs", S.OFAC_URL))
    slugs = {p.slug for p in progs}
    assert len(progs) == 36 and "cuba-sanctions" in slugs and "ukraine-russia-related-sanctions" in slugs
    assert not any("specially-designated" in s or "sdn" in s.split("-") for s in slugs)
    assert country_iso3("cuba-sanctions", "ofac_program") == "CUB"
    assert country_iso3("ukraine-russia-related-sanctions", "ofac_program") is None
    assert country_iso3("chinese-military-companies-sanctions", "ofac_program") is None
    rows = S.ofac_rows(progs, raw_sha256=SHA, day=DAY)
    f = validate_atlas_frame(atlas_frame(rows), source="ofac", dataset="programs", raw_sha256s=[SHA])[0]
    pairs = f.filter(pl.col("indicator").str.starts_with("ACTIVE_PROGRAMS|partner="))
    assert pairs.height == 20 and (pairs["iso3"] == "USA").all()
    every = {r["code"] for r in yaml.safe_load((REPO / "registry/atlas/geo_aliases/a_sanctions.yaml").read_text())[
        "schemes"]["ofac_program"]["aliases"]}  # fmt: skip
    assert every <= slugs  # every alias names a listed program


# --------------------------------------------------------------------------- platform contract
def test_sources_registry_budget_and_no_client() -> None:
    specs = {s.name: s for s in S.atlas_source()}
    assert set(specs) <= set(atlas_sources())
    mine = ("ties.yaml", "eusanct.yaml", "eu_sanctionsmap.yaml", "ofac_programs.yaml")
    keys = {yaml.safe_load((REPO / "registry/sources" / f).read_text())["source_key"] for f in mine}
    req = yaml.safe_load((REPO / "registry/atlas/budget_requests/a_sanctions.yaml").read_text())
    assert {s.source_key for s in specs.values()} <= keys
    assert {r["source_key"] for r in req["requests"]} == {s.source_key for s in specs.values()}
    src = (REPO / "src/reva/adapters/connectors/atlas_sanctions.py").read_text()
    assert not any(b in src for b in ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen(", "requests.get("))
    # designation lists are never requested
    assert not re.search(r"api/v1/fsd\"|sdn\.xml|consolidated\.xml", src)
    for name in specs:
        man = json.loads((FIXTURE_ROOT / name / "manifest.json").read_text())
        assert man and all(e.get("licence") for e in man)


def test_catalog_series_exist_in_fixture_tables(
    ties: tuple[list[S.Case], str], eus: tuple[list[S.Case], str], crosswalk: Any
) -> None:
    cat = load_catalog()
    concepts = list(cat.by_domain("sanctions"))
    assert len(concepts) == 7 and all(c.provider_declared for c in concepts)
    t = _frames(ties[0], crosswalk, 2005, ties[1])
    e = _frames(eus[0], crosswalk, S.EUSANCT_LAST_YEAR, eus[1])
    eu = atlas_frame(S.eu_map_rows(S.parse_eu_map(_fixture("atlas.eu_sanctionsmap", S.EU_MAP_URL), strict=False),
                                   raw_sha256=SHA, day=DAY))  # fmt: skip
    indicators = {
        ("ties", "country_year"): set(t["country_year"]["indicator"]),
        ("eusanct", "country_year"): set(e["country_year"]["indicator"]),
        ("eusanctionsmap", "regimes"): set(eu["indicator"]),
    }
    for c in concepts:
        for s in c.source_order:
            assert s.indicator in indicators[(s.source, s.dataset)], (c.id, s.key)


def test_codebook_quotes_match_the_pinned_pdfs_text() -> None:
    # every category label of the code tables appears in the quoted definition (verbatim quotes, not paraphrase)
    for table, cb in ((S.TIES_OUTCOMES, S.TIES_CODEBOOK), (S.EUSANCT_OUTCOMES, S.EUSANCT_CODEBOOK)):
        text_ = cb["definitions"]["finaloutcome"]
        for code, (label, _stage) in table.items():
            assert f"{code}. {label}:" in text_, (code, label)
    for code, label in S.HSE_CONTRIBUTION.items():
        assert f"{code}. “{label}”" in S.EUSANCT_CODEBOOK["definitions"]["sanctioncontribution"]


def test_end_before_start_is_flagged_not_fixed() -> None:
    a, b = S.Day(2005, 3, 15, 11), S.Day(2000, 9, 7, 11)
    assert S.end_before_start(a, b) and not S.end_before_start(S.Day(1992, 6, 19, 11), S.Day(1992, 6, 1, 10))
    kw, q = S._interval(a, b)
    assert "end" not in kw and q["coded_end"] == "2000-09-07" and "date_inconsistency" in q
    assert S._span(a, b, "coded_end_before_start", None, 2015) == [2005]
