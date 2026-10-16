"""A-TREATIES connectors (UNTC status pages, OHCHR dashboard) on trimmed recorded fixtures -- offline, no database.

Fixtures (``tests/fixtures/source/atlas.{untc,ohchr}``) are the real responses of 2026-10-16, trimmed (UNTC:
``__VIEWSTATE`` and script bodies emptied, parse result identical; OHCHR: 12 areas kept), see each manifest note.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_ohchr as O
from reva.adapters.connectors import atlas_treaties as T
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "source"
REPO = Path(__file__).resolve().parents[2]
ASOF = date(2026, 10, 16)


def _body(name: str, url: str) -> bytes:
    return asyncio.run(FixtureHttp(name).get(url)).body


@pytest.fixture(scope="module")
def pages() -> dict[str, T.Page]:
    return {k: T.parse_page(_body("atlas.untc", T.BY_MTDSG[k].url), T.BY_MTDSG[k]) for k in T.FIXTURE_TREATIES}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------------------------------------ catalog
def test_treaty_list_is_bounded_unique_and_urls_are_direct() -> None:
    assert 40 <= len(T.TREATIES) <= 60
    assert len({t.mtdsg for t in T.TREATIES}) == len(T.TREATIES) == len({t.key for t in T.TREATIES})
    assert sum(1 for t in T.TREATIES if t.ohchr) == 18
    assert {t.ohchr: t.mtdsg for t in T.TREATIES if t.ohchr} == O.ACRONYM_MTDSG
    assert T.BY_MTDSG["V-2"].url.endswith("ViewDetailsII.aspx?src=TREATY&mtdsg_no=V-2&chapter=5&Temp=mtdsg2&clang=_en")
    assert "ViewDetailsIII.aspx" in T.BY_MTDSG["XXVII-7"].url and "Temp=mtdsg3" in T.BY_MTDSG["XXVII-7"].url
    assert T.BY_MTDSG["XVIII-10"].url == (
        "https://treaties.un.org/Pages/ViewDetails.aspx?src=TREATY&mtdsg_no=XVIII-10&chapter=18&clang=_en"
    )
    spec = T.atlas_source()
    assert spec.planned_requests >= len(T.TREATIES) + 1 and set(spec.allowed_hosts) == {"treaties.un.org", "www.un.org"}


def test_catalog_series_point_at_declared_indicators() -> None:
    doc = yaml.safe_load((REPO / "registry/atlas/catalog/treaties.yaml").read_text())
    keys = {t.key: t for t in T.TREATIES}
    for c in doc["concepts"]:
        key = c["id"].split(".")[1]
        assert key in keys, c["id"]
        for s in c["source_order"]:
            src, ds, ind = s.split(".", 2)
            if src == "untc":
                assert ds == T.DATASET and ind.split("|")[0] == keys[key].mtdsg
            else:
                assert (src, ds) == ("ohchr", O.DATASET) and O.ACRONYM_MTDSG[ind.split("|")[0]] == keys[key].mtdsg


# ------------------------------------------------------------------------------------------------ UNTC parsing
def test_legend_and_cells() -> None:
    codes, default = T.legend("Approval(AA), Acceptance(A), Accession(a), Succession(d), Ratification")
    assert codes == {"AA": "Approval", "A": "Acceptance", "a": "Accession", "d": "Succession"}
    assert default == "Ratification"
    assert T.legend("Accession(a), Succession(d)") == ({"a": "Accession", "d": "Succession"}, None)
    c = T._cell("[20 Jan\t 2021 A]", {"A": "Acceptance"}, "Ratification")
    assert c is not None and (c.date, c.code, c.term, c.bracketed) == (date(2021, 1, 20), "A", "Acceptance", True)
    with pytest.raises(T.UntcParseError):
        T._cell("20 Jan 2021 Z", {"A": "Acceptance"}, "Ratification")
    with pytest.raises(T.UntcParseError):
        T._cell("soon", {}, "Ratification")


def test_pages_match_the_providers_own_counts(pages: dict[str, T.Page]) -> None:
    for k, p in pages.items():
        s = T.page_summary(p)
        assert s["listed_consents"] == s["provider_parties"], k
        assert s["listed_signatures"] == s["provider_signatories"], k
        assert not s["problems"], (k, s["problems"])
    assert pages["XVIII-10"].title == "Rome Statute of the International Criminal Court"
    assert pages["XXVII-7-d"].title == "Paris Agreement"


def test_rome_statute_withdrawals_from_the_footnote_table(pages: dict[str, T.Page]) -> None:
    p = pages["XVIII-10"]
    ev = {e.participant: e for e in p.withdrawals}
    assert ev["Burundi"].effect == date(2017, 10, 27) and ev["Burundi"].basis == "note_table"
    assert ev["Philippines"].notification == date(2018, 3, 17) and ev["Philippines"].effect == date(2019, 3, 17)
    assert ev["South Africa"].revoked and ev["Hungary"].revoked and ev["Hungary"].effect is None
    by = {x.name: x for x in p.participants}
    assert by["Burundi"].consent.bracketed and not by["Hungary"].consent.bracketed
    st = T.participant_status(by["Burundi"], [ev["Burundi"]], ASOF)
    assert st == {"status": "withdrawn", "withdrawal_effect": "2017-10-27"}
    st = T.participant_status(by["Niger"], [ev["Niger"]], ASOF)  # notified 2026, effect 2027: still a party
    assert st == {"status": "party", "scheduled_withdrawal_effect": "2027-06-18"}
    assert T.participant_status(by["Hungary"], [ev["Hungary"]], ASOF) == {"status": "party"}
    us = by["United States of America"]
    assert us.consent is None and us.signature.date == date(2000, 12, 31)
    assert "does not intend to become a party" in p.notes["4"]  # Israel's note, kept verbatim


def test_withdrawals_from_footnote_sentences(pages: dict[str, T.Page]) -> None:
    op1 = {(e.participant, e.notification, e.effect) for e in pages["IV-5"].withdrawals}
    assert ("Trinidad and Tobago", date(1998, 5, 26), date(1998, 8, 26)) in op1
    assert ("Trinidad and Tobago", date(2000, 3, 27), date(2000, 6, 27)) in op1
    assert ("Belarus", date(2022, 11, 8), date(2023, 2, 8)) in op1
    assert ("Jamaica", date(1997, 10, 23), None) in op1
    assert all(e.basis == "note_text" for e in pages["IV-5"].withdrawals)
    paris = [(e.notification, e.effect) for e in pages["XXVII-7-d"].withdrawals]
    assert paris == [(date(2019, 11, 4), date(2020, 11, 4)), (date(2025, 1, 27), date(2026, 1, 27))]
    kyoto = pages["XXVII-7-a"].withdrawals
    assert [(e.participant, e.effect) for e in kyoto] == [("Canada", date(2012, 12, 15))]
    # a withdrawn reservation is not a treaty withdrawal
    assert T._note_withdrawals("9", "On 1 May 2000, the Government of X notified the Secretary-General that it "
                               "had decided to withdraw its reservation.", "", ["X"]) == []  # fmt: skip


def test_status_of_withdrawn_participants(pages: dict[str, T.Page]) -> None:
    def st(k: str, name: str) -> dict[str, Any]:
        p = pages[k]
        part = next(x for x in p.participants if x.name == name)
        return T.participant_status(part, [e for e in p.withdrawals if e.participant == name], ASOF)

    assert st("XXVII-7-d", "United States of America") == {"status": "withdrawn", "withdrawal_effect": "2026-01-27"}
    assert st("XXVII-7-a", "Canada") == {"status": "withdrawn", "withdrawal_effect": "2012-12-15"}
    assert st("IV-5", "Trinidad and Tobago") == {"status": "withdrawn", "withdrawal_effect": "2000-06-27"}
    assert st("IV-5", "Jamaica") == {"status": "withdrawn", "withdrawal_effect": None}
    assert st("XXVII-7-d", "France")["status"] == "party"


def test_untc_names_map_through_the_alias_scheme(pages: dict[str, T.Page]) -> None:
    names = {x.name for p in pages.values() for x in p.participants}
    unmapped = sorted(n for n in names if country_iso3(n, T.GEO_SCHEME) is None)
    assert unmapped == ["European Union"]
    assert country_iso3("Naoero", "untc") == "NRU" and country_iso3("Czech Republic", "untc") == "CZE"
    assert country_iso3("Türkiye", "untc") == "TUR" and country_iso3("State of Palestine", "untc") == "PSE"


def test_atlas_rows_validate(pages: dict[str, T.Page]) -> None:
    raws = {k: _sha(_body("atlas.untc", T.BY_MTDSG[k].url)) for k in pages}
    rows = T.atlas_rows(list(pages.values()), raws, ASOF)
    fr, unmapped = validate_atlas_frame(T.frame(rows), source="untc", dataset="treaty_status",
                                        raw_sha256s=sorted(raws.values()), geo_scheme="untc")  # fmt: skip
    assert {u["provider_geo"] for u in unmapped} == {"European Union"}
    ind = Counter(r["indicator"].split("|", 1)[1].split("=")[0] for r in rows)
    assert ind["status"] == sum(len(p.participants) for p in pages.values())
    us = [
        r for r in rows if r["provider_geo"] == "United States of America" and r["indicator"].startswith("XXVII-7-d|")
    ]
    got = {r["indicator"].split("|", 1)[1]: (r["period_start"], r["value_text"]) for r in us}
    assert got["action=consent"] == (date(2021, 1, 20), "acceptance")
    assert got["action=withdrawal_effect"][0] == date(2026, 1, 27) and got["status"][1] == "withdrawn"
    assert json.loads(next(r for r in us if r["indicator"].endswith("action=consent"))["flags"])["bracketed_withdrawn"]
    fra = fr.filter(fr["provider_geo"] == "France")
    assert set(fra["iso3"].to_list()) == {"FRA"}


def test_kg_records(pages: dict[str, T.Page]) -> None:
    raws = {k: _sha(_body("atlas.untc", T.BY_MTDSG[k].url)) for k in pages}
    names = {x.name for p in pages.values() for x in p.participants}
    b = T.kg_records(list(pages.values()), raws, asof=ASOF, iso3_of={n: country_iso3(n, "untc") for n in names},
                     country_qids={"USA": "Q30"}, treaty_qids={"XXVII-7-d": "Q21707860"}, revision="untc@2026-10-16")  # fmt: skip
    sts = {(s.property_pid, s.statement_id): s for s in b.statements}
    us = sts[("untc:party", "party:XXVII-7-d:united_states_of_america")]
    assert us.subject_qid == "Q21707860" and us.object_qid == "Q30"
    assert us.start.iso == "2021-01-20" and us.end.iso == "2026-01-27"
    assert len(us.qualifiers["withdrawal_events"]) == 2
    sig = sts[("P1891", "sig:XVIII-10:united_states_of_america")]
    assert sig.subject_qid == "untc:treaty:XVIII-10" and sig.point_in_time.iso == "2000-12-31"
    assert sig.object_qid == "Q30"
    fr = sts[("untc:party", "party:XVIII-10:france")]
    assert fr.object_qid == "iso3:FRA" and fr.end is None
    jam = sts[("untc:former_party", "party:IV-5:jamaica")]
    assert jam.end is None and "end_not_stated" in jam.qualifiers
    niger = sts[("untc:party", "party:XVIII-10:niger")]
    assert niger.end is None and niger.qualifiers["scheduled_withdrawal_effect"] == "2027-06-18"
    eu = [s for s in b.statements if s.object_qid == "untc:participant:european_union"]
    assert eu and any(e.qid == "untc:participant:european_union" for e in b.entities)
    assert all(s.raw_sha256 in raws.values() for s in b.statements)
    assert {s.property_pid for s in b.statements} <= {"P1891", "untc:party", "untc:former_party", "untc:mtdsg_number"}


def test_terms_check() -> None:
    t = T.check_terms(_body("atlas.untc", T.TERMS_URL))
    assert t["phrase_found"] and t["open_licence"] is False


def test_parse_refuses_layout_drift() -> None:
    with pytest.raises(T.UntcParseError):
        T.parse_page(b"<html><body>maintenance</body></html>", T.BY_MTDSG["IV-4"])


async def test_fixture_subset_is_selected_offline() -> None:
    from reva.atlas.ingest import AtlasRun

    run = AtlasRun.offline(T.atlas_source(), FixtureHttp("atlas.untc"))
    assert [t.mtdsg for t in T.selected_treaties(run)] == list(T.FIXTURE_TREATIES)
    run.options = {"treaties": "IV-5"}
    assert [t.mtdsg for t in T.selected_treaties(run)] == ["IV-5"]


# ------------------------------------------------------------------------------------------------ OHCHR
@pytest.fixture(scope="module")
def ohchr() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    return O.parse_treaties(_body("atlas.ohchr", O.URL_TREATIES)), O.parse_countries(
        _body("atlas.ohchr", O.URL_COUNTRIES)
    )


def test_ohchr_instruments_and_dates(ohchr: Any) -> None:
    treaties, countries = ohchr
    assert sorted(t["Acronym"] for t in treaties) == sorted(O.ACRONYM_MTDSG)
    assert O.ddmmyyyy("01/01/1753") is None and O.ddmmyyyy("24/01/1983") == date(1983, 1, 24)
    with pytest.raises(O.OhchrParseError):
        O.ddmmyyyy("1983-01-24")
    assert {c["AreaId"] for c in countries.values()} >= {"USA", "PLS", "ERU"}
    assert country_iso3("PLS", "ohchr") == "PSE" and country_iso3("ERU", "ohchr") is None


def test_ohchr_state_party_type_matches_untc(ohchr: Any, pages: dict[str, T.Page]) -> None:
    """StatePartyType 0/1/2 = accession/succession/ratification: re-checked on ICCPR-OP1 against UNTC IV-5."""
    treaties, countries = ohchr
    t = next(x for x in treaties if x["Acronym"] == "ICCPR-OP1")
    url = O.ratification_url(t["Id"])
    body = _body("atlas.ohchr", url)
    rows, summary = O.rows_for_treaty(t, body, countries, _sha(body), url)
    untc = {country_iso3(p.name, "untc"): p for p in pages["IV-5"].participants}
    checked = 0
    for r in rows:
        if not r["indicator"].endswith("|field=consent"):
            continue
        p = untc.get(country_iso3(r["provider_geo"], "ohchr"))
        assert p is not None and p.consent is not None and p.consent.date == r["period_start"], r["provider_geo"]
        expect = {"a": "accession", "d": "succession", None: "ratification"}[p.consent.code]
        assert r["value_text"] == expect
        checked += 1
    assert checked >= 3 and summary["untc_mtdsg"] == "IV-5"


def test_ohchr_rows_validate(ohchr: Any) -> None:
    treaties, countries = ohchr
    rows, raws = [], []
    for t in treaties:
        url = O.ratification_url(t["Id"])
        body = _body("atlas.ohchr", url)
        raws.append(_sha(body))
        rows += O.rows_for_treaty(t, body, countries, raws[-1], url)[0]
    fr, unmapped = validate_atlas_frame(O.frame(rows), source="ohchr", dataset="hr_ratification",
                                        raw_sha256s=raws, geo_scheme="ohchr")  # fmt: skip
    assert {u["provider_geo"] for u in unmapped} <= {"ERU"}
    usa = fr.filter((fr["iso3"] == "USA") & (fr["indicator"] == "ICCPR|field=consent"))
    assert usa["value_text"].to_list() == ["ratification"] and usa["period_start"].to_list() == [date(1992, 6, 8)]
    st = fr.filter((fr["iso3"] == "USA") & (fr["indicator"] == "ICESCR|field=status"))["value_text"].to_list()
    assert st == ["signatory"]
    assert fr.filter(fr["indicator"].str.ends_with("|field=reservations")).height > 0


def test_ohchr_refuses_unexpected_shapes() -> None:
    with pytest.raises(O.OhchrParseError):
        O.parse_treaties(b'{"not": "a list"}')
    with pytest.raises(O.OhchrParseError):
        O.parse_countries(b"[{}]")
