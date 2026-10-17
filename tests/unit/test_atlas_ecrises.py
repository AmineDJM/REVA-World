"""A-ECRISES: economic and financial crisis datasets (Laeven-Valencia, Reinhart-Rogoff, JST, ESRB) -- offline tests.

Fixtures ``tests/fixtures/source/atlas.ecrises.*`` are live captures of 2026-10-16. The Reinhart-Rogoff and JST
workbooks are TRIMMED to a few countries (rows of ``xl/worksheets/sheet1.xml`` filtered; the manifests say which);
Laeven-Valencia, ESRB and the JST licence page are complete.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from reva.adapters.connectors import atlas_ecrises as m
from reva.atlas.ingest import AtlasRun
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp

REPO = Path(__file__).resolve().parents[2]


def _spec(key: str) -> AtlasSourceSpec:
    return next(s for s in m.atlas_source() if s.name == m.SOURCES[key].name)


def _fetch(key: str, url: str) -> bytes:
    run = AtlasRun.offline(_spec(key), FixtureHttp(m.SOURCES[key].name))
    raw = asyncio.run(run.get(url, key="t", media_types=(*m.XLSX_MEDIA, "text/html")))
    return bytes(raw.body)


_BUILDS: dict[str, m.Build] = {}


def build(key: str) -> m.Build:
    if key not in _BUILDS:
        _BUILDS[key] = m.parse(key, _fetch(key, m.SOURCES[key].url))
    return _BUILDS[key]


def valid(b: m.Build) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    return validate_atlas_frame(
        b.frame(), source=b.src.atlas_source, dataset=b.src.dataset, raw_sha256s=[b.raw_sha256], geo_scheme=b.geo_scheme
    )


def flags(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["flags"])


def ev(b: m.Build, eid: str) -> m.CrisisEvent:
    return next(e for e in b.events if e.eid == eid)


# ------------------------------------------------------------------------------------------- platform
def test_four_atlas_sources_are_declared_and_discovered() -> None:
    from reva.atlas.sources import get_source

    specs = m.atlas_source()
    assert {s.name for s in specs} == {s.name for s in m.SOURCES.values()}
    for s in specs:
        assert get_source(s.name).spec.source_key == s.source_key
        assert s.datasets == (m.BY_NAME[s.name].dataset,) and s.code_digest.startswith("sha256:")
        assert s.max_response_bytes == m.MAX_XLSX_BYTES
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    req = yaml.safe_load((REPO / "registry/atlas/budget_requests/a_ecrises.yaml").read_text())["requests"]
    assert {s.source_key for s in specs} <= keys
    assert {r["source_key"] for r in req} == {s.source_key for s in specs}
    assert all(r["daily_requests"] >= _spec_by_key(r["source_key"]).planned_requests for r in req)


def _spec_by_key(source_key: str) -> AtlasSourceSpec:
    return next(s for s in m.atlas_source() if s.source_key == source_key)


def test_module_builds_no_http_client() -> None:
    src = Path(m.__file__).read_text()
    for needle in ("make_live_client(", "SafeHttpClient(", "httpx.", "urlopen("):
        assert needle not in src


def test_rights_are_recorded_and_jst_is_non_commercial() -> None:
    for key in m.SOURCES:
        assert m.RIGHTS[key]["open_licence"] is False and m.RIGHTS[key]["attribution"] == m.CITATIONS[key]
    assert m.RIGHTS[m.JST]["non_commercial"] is True and "NON-COMMERCIAL" in m.RIGHTS[m.JST]["restriction"]
    doc = yaml.safe_load((REPO / "registry/sources/jst_macrohistory.yaml").read_text())
    assert doc["rights"]["non_commercial"] is True and doc["rights"]["licence"] == "CC BY-NC-SA 4.0"


# ------------------------------------------------------------------------------------------- helpers
def test_cell_helpers() -> None:
    assert m.years_in_cell("1980, 1989,1995 ") == [1980, 1989, 1995]
    with pytest.raises(ValueError):
        m.years_in_cell("n.a.")
    assert m.split_footnote("Argentina 8/ ") == ("Argentina", "8/")
    assert m.split_footnote("1994 7/") == ("1994", "7/") and m.split_footnote("Chad") == ("Chad", None)
    assert (
        m.month_token("Mar-80") == (1980, 3) and m.month_token("Nov-01") == (2001, 11) and m.month_token("80") is None
    )
    assert str(m.excel_serial_date("29281")) == "1980-03-01" and m.excel_serial_date("1989") is None
    assert m.ym_text("2007-12") == (2007, 12) and m.ym_text("1997-05 ") == (1997, 5) and m.ym_text("n.a.") is None
    assert m.runs([1890, 1891, 1893, 1894, 1895]) == [(1890, 1891), (1893, 1895)]


def test_flags_are_bounded() -> None:
    text = m.bounded_flags({"a": "x" * 5000, "b": "short"})
    assert len(text) <= 4000 and json.loads(text)["flags_truncated"] is True and json.loads(text)["b"] == "short"


def test_twin_definition_on_a_synthetic_country() -> None:
    sets = m.lv_twin_sets(
        {"systemic_banking": [1990, 2000], "currency": [1991, 1999, 2010], "sovereign_debt": [2001, 2011]}
    )
    assert sets["triple"] == [(2000, (1999,), (2001,))]  # banking 2000 + currency 1999 + sovereign 2001
    assert sets["twin_banking_currency"] == [(1990, (1991,), ())]  # 1990: no sovereign crisis near
    assert sets["twin_currency_sovereign"] == [(2010, (2011,), ())]  # 1999 belongs to the triple: excluded
    assert "twin_banking_sovereign" not in sets


def test_accelerator_objectives_read_only_the_coded_part() -> None:
    t = "Mismatches and market illiquidity.\n\nMotivation: All vulnerabilities played a role (except infrastructure resilience)."
    assert m.accelerator_objectives(t) == ["mismatches_and_market_illiquidity"]
    assert m.accelerator_objectives(
        "Excessive credit growth and leverage, exposure concentration, mismatches and market illiquidity, misaligned "
        "incentives.\n\nMotivation: the narrative may name anything."
    ) == [
        "excessive_credit_growth_and_leverage",
        "exposure_concentration",
        "mismatches_and_market_illiquidity",
        "misaligned_incentives",
    ]
    # audit M7 (AUDFIX): no pattern search in free text -- only an exact list of the five objective names
    for text in (
        "Excessive credit growth, misaligned incentives, currency mismatch.",  # abbreviated / unknown item
        "Excessive credit growth and leverage (in particular CRE), mismatches in balance sheets",  # loose mapping
        "Excessive credit growth and leverage, mismatches.",
        "Russia's sovereign default ... losses were further amplified by the exposure concentration, adverse ...",
        'The main vulnerability comes under the heading "mismatches and market illiquidity".',
        "1) Excessive credit growth and leverage. \n2) High reliance on the market funding",
        "Exogenous health crisis due to Covid-19 outbreak",
    ):
        assert m.accelerator_objectives(text) == [], text


def test_esrb_mechanism_is_the_verbatim_cell_or_marked_truncated() -> None:
    b = build(m.ESRB)
    rels = [r for r in b.relations if r.family == "causal"]
    assert rels
    for r in rels:
        t = r.qualifiers.get("mechanism_truncated")
        if t:
            assert len(r.mechanism) == t["stored_chars"] == m.MECHANISM_MAX < t["full_chars"]
        else:
            assert len(r.mechanism) <= m.MECHANISM_MAX
    claims = m.dataset_claims(b)
    for c in claims:
        assert ("mechanism_truncated" in c.scope) == (len(c.mechanism) == m.MECHANISM_MAX)
    assert m.CODEBOOKS[m.ESRB]["definitions"]["accelerator"].endswith("Format: text.")


# ------------------------------------------------------------------------------------------- Laeven-Valencia
def test_laeven_valencia_tables_and_events() -> None:
    b = build(m.LV)
    f, unmapped = valid(b)
    assert {u["provider_geo"] for u in unmapped} == {"Yugoslavia, SFR", m.LV_WORLD_GEO}
    starts = f.filter(pl.col("indicator") == "crisis_start|type=systemic_banking")
    assert starts.height == 151 and set(starts["value"].to_list()) == {"1"}
    arg = starts.filter(pl.col("iso3") == "ARG")["period_start"].dt.year().to_list()
    assert sorted(arg) == [1980, 1989, 1995, 2001]
    # names written differently in other sheets resolve to the same country (alias table)
    out = f.filter((pl.col("indicator") == "output_loss") & (pl.col("iso3") == "COD"))
    assert out.height == 3 and set(out["provider_geo"].to_list()) == {"Congo, Dem Rep"}
    end = f.filter((pl.col("indicator") == "banking_crisis_end_year") & (pl.col("iso3") == "AUT")).to_dicts()[0]
    assert end["value"] == "2012" and "truncate" in flags(end)["end_footnote_text"]
    ukr = f.filter((pl.col("indicator") == "banking_crisis_end_year") & (pl.col("value_text") == "ongoing"))
    assert sorted(ukr["iso3"].to_list()) == ["GNB", "MDA", "UKR"]
    # nothing is zero-filled: '...' cells are omitted
    assert b.report["omitted_cells"]["Crisis Resolution and Outcomes:..."] > 100
    assert f.filter(pl.col("indicator").str.starts_with("detail|")).height > 1000
    a80 = ev(b, "ecrises:lv:systemic_banking:ARG:1980")
    assert (
        a80.start == ("1980-03-01", 10) and a80.end == ("1982-01-01", 9) and "GDP growth only" in (a80.end_kind or "")
    )
    assert a80.attributes["description"].startswith("A banking crisis erupted")
    assert ev(b, "ecrises:lv:systemic_banking:ARG:1995").attributes["borderline"] is True
    assert ev(b, "ecrises:lv:sovereign_debt:Yugoslavia_SFR:1983").iso3 is None
    assert b.report["events_by_type"] == {
        "currency": 240,
        "sovereign_debt": 80,
        "sovereign_debt_restructuring": 75,
        "systemic_banking": 151,
    }


def test_laeven_valencia_twins_are_context_edges_checked_against_published_counts() -> None:
    b = build(m.LV)
    chk = b.report["twin_check"]
    assert (
        chk["triple"]["matches"]
        and chk["twin_banking_sovereign"]["matches"]
        and chk["twin_currency_sovereign"]["matches"]
    )
    assert (
        chk["twin_banking_currency"]["computed_total"] == 32 and chk["twin_banking_currency"]["published_total"] == 31
    )
    assert chk["twin_banking_currency"]["years_differing"] == {1992: [2, 3]}
    # the authors' yearly currency count differs for the same year (reported, never "fixed")
    assert b.report["count_check"]["currency"]["years_differing"][1992] == [6, 9]
    assert b.report["count_check"]["banking"]["years_differing"] == {}
    assert b.relations and {(r.family, r.relation) for r in b.relations} == {("context", "same_period")}
    tri = [
        r for r in b.relations if r.rule_id == "lv2020_triple" and r.subject == "ecrises:lv:systemic_banking:ARG:2001"
    ]
    assert {r.object for r in tri} == {"ecrises:lv:currency:ARG:2002", "ecrises:lv:sovereign_debt:ARG:2001"}
    assert all("footnote" in r.coded_as and r.qualifiers["window"] == [2000, 2002] for r in tri)
    kg = m.kg_records(b, qid_for_iso3={"ARG": "Q414"})
    ctx = [s for s in kg["statements"] if s.property_pid == "ecrises:same_crisis_window"]
    assert len(ctx) == len(b.relations) and all(s.qualifiers["not_causal"] is True for s in ctx)
    assert not any(s.property_pid in {"P828", "P1542", "P1478", "P1479", "P1536", "P1537"} for s in kg["statements"])


def test_laeven_valencia_kg_statements() -> None:
    b = build(m.LV)
    kg = m.kg_records(b, qid_for_iso3={"ARG": "Q414"})
    by = {(s.subject_qid, s.property_pid): s for s in kg["statements"]}
    e = "ecrises:lv:systemic_banking:ARG:1980"
    assert by[(e, "P31")].object_qid == "Q806729" and by[(e, "P17")].object_qid == "Q414"
    assert by[(e, "P580")].value == {"type": "time", "time": "1980-03-01", "precision": 10}
    assert by[(e, "P582")].value["time"] == "1982-01-01"
    assert by[("ecrises:lv:sovereign_debt_restructuring:ARG:1993", "P31")].object_qid == "Q1364290"
    assert all(x.kind == "event" for x in kg["entities"])
    assert m.events_table(b).num_rows == len(b.events) and m.relations_table(b).num_rows == len(b.relations)


def test_laeven_valencia_layout_drift_is_refused() -> None:
    body = _fetch(m.LV, m.LV_URL)
    zin = zipfile.ZipFile(io.BytesIO(body))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/sharedStrings.xml":
                data = data.replace(b"Currency Crisis", b"Exchange Rate Crisis")
            z.writestr(info, data)
    with pytest.raises(m.EcrisesError, match="header"):
        m.parse(m.LV, out.getvalue())
    with pytest.raises(m.EcrisesError):
        m.parse(m.LV, b"not a workbook")


# ------------------------------------------------------------------------------------------- Reinhart-Rogoff
def test_reinhart_rogoff_rows_episodes_and_comments() -> None:
    b = build(m.RR)
    f, unmapped = valid(b)
    assert not unmapped
    assert set(f["iso3"].unique().to_list()) == {"ARG", "USA", "GBR", "HUN", "TWN", "ISL", "GRC"}
    assert "HUN " in f["provider_geo"].unique().to_list()  # verbatim provider code (trailing space)
    code2 = f.filter((pl.col("indicator") == "currency_crisis") & (pl.col("value") == "2"))
    assert code2.height > 0 and all("undocumented_code" in flags(r) for r in code2.to_dicts())
    assert "<" not in {flags(r)["column"] for r in f.head(2000).to_dicts()}  # undefined column skipped
    exch = f.filter(pl.col("indicator") == "exch_usd").to_dicts()[0]
    assert "exch_sources" in flags(exch)
    usa = [e for e in b.events if e.eid.startswith("ecrises:rr:banking:USA:")]
    assert usa and all(e.derivation and "consecutive" in e.derivation for e in usa)
    gfc = next(e for e in usa if e.start_year == 2007)
    assert gfc.end_year is not None and gfc.end_year >= 2008
    assert any(e.attributes.get("provider_cell_comments") for e in b.events)
    arg_def = [e for e in b.events if e.eid.startswith("ecrises:rr:external_default:ARG:")]
    assert arg_def and all("years_also_in_series_1" in e.attributes for e in arg_def)
    assert b.relations == []  # RR code no relations


# ------------------------------------------------------------------------------------------- JST
def test_jst_licence_page_is_checked() -> None:
    page = _fetch(m.JST, m.JST_LICENCE_URL)
    m.check_jst_licence(page)
    with pytest.raises(m.EcrisesError, match="licence"):
        m.check_jst_licence(re.sub(rb"(?i)noncommercial", b"Commercial", page))


def test_jst_series_and_crisis_events() -> None:
    b = build(m.JST)
    f, unmapped = valid(b)
    assert not unmapped and set(f["iso3"].unique().to_list()) == {"USA", "GBR", "DEU", "SWE", "JPN"}
    assert set(f["indicator"].unique().to_list()) <= set(m.JST_VARIABLES)
    c = f.filter(pl.col("indicator") == "crisisJST")
    assert set(c["value"].unique().to_list()) == {"0", "1"} and "first year" in flags(c.to_dicts()[0])["definition"]
    usa = sorted(e.start_year for e in b.events if e.iso3 == "USA")
    assert usa == [1873, 1893, 1907, 1930, 1984, 2007]
    e = ev(b, "ecrises:jst:systemic_banking:USA:2007")
    assert e.end is None and e.start == ("2007-01-01", 9)
    assert f.filter(pl.col("indicator") == "peg_type")["value_text"].null_count() == 0


# ------------------------------------------------------------------------------------------- ESRB
def test_esrb_events_and_accelerator_claims() -> None:
    b = build(m.ESRB)
    f, unmapped = valid(b)
    assert not unmapped and "GBR" in f["iso3"].to_list()
    assert b.report["events_by_type"]["systemic_financial"] > 70
    at = ev(b, "ecrises:esrb:systemic:AUT:2007-12")
    assert at.start == ("2007-12-01", 10) and at.end == ("2014-12-01", 10)
    assert at.attributes["domestic_vs_imported"] == "both" and "banking" in at.attributes["categories"]
    claims = [r for r in b.relations if r.object == at.eid]
    assert {r.subject for r in claims} == {
        m.condition_id(k)
        for k in (
            "excessive_credit_growth_and_leverage",
            "mismatches_and_market_illiquidity",
            "exposure_concentration",
            "misaligned_incentives",
        )
    }
    assert all(r.family == "causal" and r.relation == "contributing_factor" and r.tier == "T1" for r in claims)
    assert all(r.mechanism and r.mechanism.startswith("Excessive credit growth") for r in claims)
    covid = [e for e in b.events if e.start[0].startswith("2020-03")]
    assert covid and not any(r.object in {e.eid for e in covid} for r in b.relations)
    assert any("2020-03" in x for x in b.report["accelerator_not_coded"])
    # the claims are well-formed for CAUSCORE's loader (codebook term defined, ids valid)
    dc = m.dataset_claims(b)
    assert len(dc) == len(b.relations) and {d.codebook_term for d in dc} == {"accelerator"}
    assert all(d.codebook_term in m.CODEBOOKS[m.ESRB]["definitions"] for d in dc)
    kg = m.kg_records(b, qid_for_iso3={})
    assert {x.qid for x in kg["entities"] if x.kind == "condition"} == {r.subject for r in b.relations}
    assert not any(s.property_pid == "ecrises:same_crisis_window" for s in kg["statements"])  # claims stay out of KG


# ------------------------------------------------------------------------------------------- registries
def test_relations_registry_declares_every_rule_and_uses_the_closed_vocabulary() -> None:
    doc = yaml.safe_load((REPO / "registry/causal/relations_ecrises.yaml").read_text())
    rules = {r["id"]: r for r in doc["rules"]}
    used = {r.rule_id for k in (m.LV, m.ESRB) for r in build(k).relations}
    assert used <= set(rules)
    vocab = yaml.safe_load((REPO / "registry/causal/relations.yaml").read_text())
    causal_relations = set(vocab.get("relations", vocab))
    for r in rules.values():
        assert r["family"] in ("causal", "context")
        if r["family"] == "causal":
            assert r["relation"] in causal_relations and r["tier"] == "T1" and r["codebook"]
        else:
            assert r["relation"] in ("same_period", "follows", "part_of")
    assert set(m.CLAIM_TERMS) == {k for k, r in rules.items() if r["family"] == "causal"}


def test_catalog_series_exist_in_the_parsed_tables() -> None:
    from reva.atlas.catalog import load_catalog

    cat = load_catalog()
    ours = [c for c in cat.concepts.values() if c.file == "crises_economic.yaml"]
    assert len(ours) >= 20 and all(c.theme == "economy" for c in ours)
    tables = {(b.src.atlas_source, b.src.dataset): {r["indicator"] for r in b.rows} for b in map(build, m.SOURCES)}
    for c in ours:
        for s in c.source_order:
            assert s.indicator in tables[(s.source, s.dataset)], (c.id, s.key)


def test_geo_aliases_cover_every_laeven_valencia_name() -> None:
    b = build(m.LV)
    assert b.report["unmapped_names"] == ["Yugoslavia, SFR"]
    from reva.atlas.tables import country_iso3

    assert country_iso3("Korea", "laeven_valencia") == "KOR" and country_iso3("UK*", "esrb") == "GBR"
    assert country_iso3("GR", "esrb") == "GRC"
