"""CIA World Factbook profiles connector (agent A-FACTBOOK): offline, on recorded fixtures.

Fixtures ``tests/fixtures/source/atlas.factbook_profiles`` are the blob bytes of the pinned mirror
commit (identical to the raw.githubusercontent.com responses of the live run; the France file has
the same sha256 as A-LAW's live capture) for FR, TW, KV, GL, WI, WE, VT, XX, KN, SV;
``atlas.factbook_disputes`` holds FR, WI, TW at the 2024-04-18 commit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_factbook_profiles as m
from reva.atlas.catalog import load_catalog
from reva.atlas.sources import discover
from reva.atlas.tables import country_iso3, validate_atlas_frame
from reva.evidence.runner import FixtureHttp

REPO = Path(__file__).resolve().parents[2]
FIX_GECS = ["fr", "tw", "kv", "gl", "wi", "we", "vt", "xx", "kn", "sv"]


class _Run:
    """Fetch through the fixture client; archive = sha256; register = ATLASCORE validation only."""

    def __init__(self, name: str, options: dict[str, Any] | None = None) -> None:
        self.spec = next(s for s in m.atlas_source() if s.name == name)
        self.http = FixtureHttp(name)
        self.options = options or {}
        self.frames: dict[str, pl.DataFrame] = {}
        self.unmapped: dict[str, list[dict[str, Any]]] = {}

    def wants(self, dataset: str) -> bool:
        assert dataset in self.spec.datasets
        return True

    async def get(self, url: str, **kw: Any) -> Any:
        r = await self.http.get(url)

        class Raw:
            body = r.body

        return Raw()

    def archive(self, raw: Any) -> str:
        return hashlib.sha256(raw.body).hexdigest()

    def register(self, *, dataset: str, frame: pl.DataFrame, raw_sha256s: list[str], geo_scheme: str, **kw: Any):
        f, unm = validate_atlas_frame(
            frame, source=m.ATLAS_SOURCE, dataset=dataset, raw_sha256s=raw_sha256s, geo_scheme=geo_scheme
        )
        self.frames[dataset] = f
        self.unmapped[dataset] = unm
        return {"dataset_key": f"atlas.factbook.{dataset}", "row_count": f.height, "created": True}


@pytest.fixture(scope="module")
def profiles() -> tuple[_Run, dict[str, Any]]:
    run = _Run(m.NAME_PROFILES, {"gecs": ",".join(FIX_GECS)})
    out = asyncio.run(m.atlas_ingest(run))
    return run, out


def _cell(frame: pl.DataFrame, geo: str, indicator: str) -> list[dict[str, Any]]:
    return frame.filter((pl.col("provider_geo") == geo) & (pl.col("indicator") == indicator)).to_dicts()


# ------------------------------------------------------------------------------------------ specs
def test_two_atlas_sources_are_discovered_and_fit_the_budget():
    found, errors = discover(["reva.adapters.connectors.atlas_factbook_profiles"])
    assert errors == []
    assert set(found) == {m.NAME_PROFILES, m.NAME_DISPUTES}
    prof, disp = (found[n].spec for n in (m.NAME_PROFILES, m.NAME_DISPUTES))
    assert prof.planned_requests == 261 and disp.planned_requests == 259
    assert prof.source_key == disp.source_key == "factbook.json" and prof.allowed_hosts == (
        "raw.githubusercontent.com",
    )
    import yaml

    budgets = yaml.safe_load((REPO / "registry/policies/budgets.yaml").read_text())
    assert max(prof.planned_requests, disp.planned_requests) <= budgets["per_source_daily_requests"]["factbook.json"]
    assert "source_key: factbook.json" in (REPO / "registry/sources/factbook.yaml").read_text()


def test_entity_tables_are_complete_and_dated():
    ents = m.entities()
    assert len(ents) == 261 and len({g for _, g in ents}) == 261
    assert len(m.entities(m.DISPUTES_PATHS)) == 259
    assert m.as_published("wi") == (date(2021, 1, 1), date(2021, 1, 2))
    assert m.as_published("gg") == (date(2026, 1, 22), date(2026, 1, 23))  # moved only
    assert m.url_for("europe", "fr").endswith(f"/{m.COMMIT}/europe/fr.json")


def test_geo_scheme_maps_gec_codes_and_keeps_partial_territories_unmapped():
    assert country_iso3("FR", m.GEO_SCHEME) == "FRA"
    assert country_iso3("NO", m.GEO_SCHEME) == "NOR"  # YAML 'NO' must stay a string
    assert country_iso3("KV", m.GEO_SCHEME) == "XKX"
    assert country_iso3("WI", m.GEO_SCHEME) == "ESH"
    assert country_iso3("SF", m.GEO_SCHEME) == "ZAF" and country_iso3("ZA", m.GEO_SCHEME) == "ZMB"
    for code in ("WE", "GZ", "SV", "JN", "UM", "TW", "EE", "XX", "PF", "PG"):
        assert country_iso3(code, m.GEO_SCHEME) is None, code


# ---------------------------------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    ("indicator", "text", "expected"),
    [
        ("gdp.real_ppp", "$3.732 trillion (2024 est.)", "3732000000000"),
        ("current_account_balance", "-$30.334 billion (2023 est.)", "-30334000000"),
        ("population.total", "68,512,806 (2025 est.)", "68512806"),
        ("labor_force", "31.725 million (2024 est.)", "31725000"),
        ("area.total", "643,801 sq km ; 551,500 sq km (metropolitan France)", "643801"),
        ("land_use.arable", "arable land: 31.4% (2023 est.)", "31.4"),
        ("birth_rate", "10.88 births/1,000 population (2025 est.)", "10.88"),
        ("elevation.lowest", "Rhone River delta -2 m", "-2"),
        ("elevation.highest", "Mont Blanc 4,810", "4810"),
        ("gdp_end_use.imports", "-36.3% (2023 est.)", "-36.3"),
        ("population.total", "uninhabited", None),
        ("irrigated_land", "NA", None),
        ("area.water", "negligible", None),
        ("population.total", "12% (2020)", None),  # wrong unit: refused, not coerced
    ],
)
def test_parse_number(indicator, text, expected):
    value, _ = m.parse_number(text, m.NUMERIC[indicator])
    assert value == expected


def test_year_of_ranges_and_estimates():
    assert m.year_of("0.67% annual rate of change (2020-25 est.)") == ((date(2020, 1, 1), date(2026, 1, 1)), True)
    assert m.year_of("14,236 sq km (2020)") == ((date(2020, 1, 1), date(2021, 1, 1)), False)
    assert m.year_of("643,801 sq km") == (None, False)
    assert m.year_of("652,271 (July 2020 est.)") == ((date(2020, 1, 1), date(2021, 1, 1)), True)


def test_list_splitting_keeps_parentheses_thousands_and_inverted_names():
    assert m.split_top("Libya 989 km; Mali 1,359 km") == ["Libya 989 km", "Mali 1,359 km"]
    assert m.split_top("French (official) 100%, regional (Breton, Basque)") == [
        "French (official) 100%",
        "regional (Breton, Basque)",
    ]
    items, skipped, period, est = m.parse_shares("China 26%, Congo, Republic of the 5%, other <1% (2023)")
    assert [(n, v) for _, n, v, _ in items] == [("China", "26"), ("Congo, Republic of the", "5")]
    assert skipped == 1 and period == (date(2023, 1, 1), date(2024, 1, 1)) and not est
    borders, sk = m.parse_borders("Algeria 1,941 km; Spain (Ceuta) 8 km and Spain (Melilla) 10.5 km")
    assert [(n, v) for _, n, v, _ in borders] == [("Algeria", "1941")] and sk == 1


def test_clean_text_removes_tags_keeps_wording():
    assert m.clean_text("R&eacute;publique <strong>fran&ccedil;aise</strong><br>x") == "République française x"


# ------------------------------------------------------------------------------- fixture ingests
def test_figures_table_from_fixtures(profiles):
    run, out = profiles
    assert out["failed"] == [] and out["files"] == len(FIX_GECS)
    f = run.frames[m.DS_FIGURES]
    (pop,) = _cell(f, "FR", "population.total")
    assert pop["value"] == "68512806" and pop["iso3"] == "FRA" and pop["period_start"] == date(2025, 1, 1)
    flags = json.loads(pop["flags"])
    assert flags["estimate"] is True and flags["est_marker"] is True and "not an official" in flags["provider_nature"]
    assert "783ed27" in flags["edition"]
    (male,) = _cell(f, "FR", "population.male")
    assert male["period_start"] == date(2025, 1, 1) and json.loads(male["flags"])["year_from_sibling"] == "total"
    (area,) = _cell(f, "FR", "area.total")
    assert area["value"] == "643801" and area["unit"] == "km2"
    assert json.loads(area["flags"])["period_basis"].startswith("as_published")
    gdp = _cell(f, "FR", "gdp.real_ppp")
    assert {r["period_start"].year for r in gdp} == {2022, 2023, 2024}
    assert all(json.loads(r["flags"]).get("provider_note") for r in gdp)  # "data in 2021 dollars"
    (ger,) = _cell(f, "FR", "land_boundary|partner=Germany")
    assert ger["value"] == "418" and json.loads(ger["flags"])["partner_iso3"] == "DEU"
    (exp,) = _cell(f, "FR", "export_partner_share|name=USA")
    assert json.loads(exp["flags"])["partner_iso3"] == "USA"
    (rc,) = _cell(f, "FR", "religion_share|name=Roman Catholic")
    assert rc["value"] == "47" and rc["period_start"] == date(2021, 1, 1)
    # gap fillers: Taiwan (unmapped, kept), Greenland, Vatican, North Korea
    (tw,) = _cell(f, "TW", "population.total")
    assert tw["iso3"] is None and json.loads(tw["flags"])["disputed_territory"].startswith("Taiwan")
    assert _cell(f, "GL", "population.total")[0]["iso3"] == "GRL"
    assert _cell(f, "VT", "area.total")[0]["iso3"] == "VAT"
    assert _cell(f, "KN", "population.total")[0]["iso3"] == "PRK"
    (we,) = _cell(f, "WE", "population.total")
    assert we["iso3"] is None and json.loads(we["flags"])["partial_of_iso3"] == "PSE"
    assert {u["provider_geo"] for u in run.unmapped[m.DS_FIGURES]} == {"TW", "WE", "XX", "SV"}
    assert f.filter(pl.col("value").is_not_null() & pl.col("value").str.contains(",")).height == 0


def test_texts_table_is_verbatim_and_flags_disputes(profiles):
    run, _ = profiles
    t = run.frames[m.DS_TEXTS]
    (clim,) = _cell(t, "FR", "geography.climate")
    assert clim["value_text"].startswith("metropolitan France: generally cool winters")
    assert not t.filter(pl.col("indicator").str.starts_with("government.diplomatic_representation")).height
    (wi,) = _cell(t, "WI", "introduction.background")
    wf = json.loads(wi["flags"])
    assert wf["stale_profile"].startswith("the Factbook dropped Western Sahara") and wf["disputed_territory"]
    assert wi["period_start"] == date(2021, 1, 1) and wi["iso3"] == "ESH"
    (wpop,) = _cell(run.frames[m.DS_FIGURES], "WI", "population.total")  # older layout: Population is a text
    assert wpop["value"] == "652271" and wpop["period_start"] == date(2020, 1, 1)
    assert all(len(v) <= m.TEXT_LIMIT for v in t["value_text"].to_list())
    assert t.filter(pl.col("flags").str.contains('"mentions_dispute":true')).height > 0


def test_disputes_table_from_fixtures():
    run = _Run(m.NAME_DISPUTES, {"gecs": ["fr", "wi", "tw"]})
    out = asyncio.run(m.atlas_ingest(run))
    assert out["failed"] == [] and out["stats"] == {"dispute_rows": 3}
    d = run.frames[m.DS_DISPUTES]
    (fr,) = _cell(d, "FR", "transnational_issues.disputes_international")
    assert "Madagascar claims the French territories" in fr["value_text"]
    assert json.loads(fr["flags"])["disputed_wording_verbatim"] is True
    assert fr["period_start"] == date(2024, 4, 18)


def test_missing_fixture_is_reported_not_silent():
    run = _Run(m.NAME_PROFILES, {"gecs": "fr,us"})  # 'us' has no recorded fixture here
    out = asyncio.run(m.atlas_ingest(run))
    assert out["files"] == 1 and [x["file"] for x in out["failed"]] == ["north-america/us.json"]


def test_catalog_concepts_point_at_factbook_series():
    cat = load_catalog()
    fb = cat.by_domain("factbook")
    assert len(fb) >= 100 and all(c.provider_declared for c in fb)
    ids = {c.id for c in fb}
    assert {"factbook.population.total", "factbook.text.climate", "factbook.text.disputes_international"} <= ids
    numeric = {f"factbook.{k}" for k in m.NUMERIC}
    assert numeric <= ids
