"""REVA headline concepts end to end (agent ATLASOPS): cross-source fallback, derived values, BCE periods.

Synthetic atlas tables (citing World Bank fixture acquisitions) in a throwaway database, a synthetic
``source_order`` directory, a snapshot, then the query layer and ``GET /v1/atlas/headline/*`` plus the
dossier's headline section. Checks: the first listed series reporting a (country, year) answers and is
named; every listed series is reported as consulted with its status (answered / also_reported /
no_value / projection_only / unit_mismatch / series_not_in_snapshot); exact scaling (thousands ->
persons); projections never answer; derived values (ratio, same-source difference) only fill cells no
listed series reports, cite both inputs and never mix years; BCE periods use astronomical years.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.atlas.query import AtlasQuery
from reva.atlas.snapshot import create_atlas_snapshot
from reva.atlas.source_order import load_headlines
from reva.atlas.tables import ATLAS_SCHEMA, frame_with_year_periods, json_obj, register_atlas_table
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.errors import ValidationProblem
from reva.settings import Settings

pytestmark = pytest.mark.db

POPULATION = """
theme: population
concepts:
  - id: reva.population.total
    labels: {fr: "Population", en: "Population"}
    definition: "Synthetic de facto mid-year population."
    unit: persons
    series:
      - series: "tpop.wpp.POP"
        reported_unit: thousands
        scale: "1000"
        justification: "Synthetic WPP-like series published in thousands."
      - series: "tpop.wdi.SP.POP.TOTL"
        justification: "Synthetic WDI-like series in persons, same definition."
      - series: "tpop.absent.POP"
        justification: "A declared series that is not in the snapshot."
    not_equivalent:
      - series: "tpop.other.POP"
        reason: "Different definition; never answers."
  - id: reva.population.density
    labels: {fr: "Densité", en: "Density"}
    definition: "Synthetic population per km2 of land."
    unit: persons per km2
    series:
      - series: "tpop.wdi.EN.POP.DNST"
        justification: "Synthetic reported density."
    derived:
      formula: ratio
      alternatives:
        - [{concept: reva.population.total}, {concept: reva.population.land}]
      justification: "Exact by definition: population / land area, same country and year."
  - id: reva.population.land
    labels: {fr: "Superficie", en: "Land area"}
    definition: "Synthetic land area."
    unit: km2
    series:
      - series: "tpop.wdi.AG.LND.TOTL.K2"
        justification: "Synthetic land area series."
"""
TRADE = """
theme: trade
concepts:
  - id: reva.trade.goods_balance
    labels: {fr: "Solde des biens", en: "Goods balance"}
    definition: "Synthetic goods exports minus imports."
    unit: US dollars
    series:
      - series: "tbop.bop.G|entry=NET"
        justification: "Synthetic net goods balance."
    derived:
      formula: difference
      alternatives:
        - [{series: "tbop.bop.G|entry=CD"}, {series: "tbop.bop.G|entry=DB"}]
      justification: "Exact by definition: credit minus debit of the same source and year."
"""
HISTORY = """
theme: history
concepts:
  - id: reva.history.population_hyde
    labels: {fr: "Population (HYDE)", en: "Population (HYDE)"}
    definition: "Synthetic long-run population including BCE years."
    unit: inhabitants
    projections: allow
    series:
      - series: "thyde.h.popc"
        justification: "Synthetic HYDE-like series with BCE time steps."
"""


def _row(src: str, ds: str, ind: str, geo: str, year: int, value: str, raw: str, **kw: Any) -> dict[str, Any]:
    return {
        "source": src,
        "dataset": ds,
        "indicator": ind,
        "concept_id": None,
        "iso3": None,
        "provider_geo": geo,
        "entity_qid": None,
        "year": year,
        "value": value,
        "value_text": None,
        "unit": kw.get("unit", "1"),
        "flags": kw.get("flags", "{}"),
        "raw_sha256": raw,
        "selector": json_obj(geo=geo, year=year, ind=ind),
    }


@dataclass
class Env:
    q: AtlasQuery
    reg: Any
    client: TestClient
    token: str


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.api.app import create_app
    from reva.api.auth import new_api_token
    from reva.cli.seed import seed

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    root = tmp_path_factory.mktemp("headline_objects")
    sodir = tmp_path_factory.mktemp("source_order")
    for name, doc in (("population", POPULATION), ("trade", TRADE), ("history", HISTORY)):
        (sodir / f"{name}.yaml").write_text(doc, encoding="utf-8")
    reg = load_headlines(sodir)
    mp = pytest.MonkeyPatch()
    cache = tmp_path_factory.mktemp("headline_cache")
    mp.setenv("REVA_ATLAS_CACHE_DIR", str(cache))
    mp.setattr("reva.api.routes.atlas._headlines", lambda: reg)
    engines: list[Engine] = []
    with throwaway_database(base, prefix="reva_test_headline") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker = make_engine("migration", settings), make_engine("worker", settings)
        engines += [owner, worker]
        seed(owner, store)
        wb = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, worker, persist_cursor=False))
        with worker.connect() as c:
            raw = c.execute(text("SELECT min(raw_sha256) FROM evidence.acquisition")).scalar_one()
        proj = '{"estimate_kind":"projection"}'
        tables = {
            ("tpop", "wpp"): [
                _row("tpop", "wpp", "POP", "FRA", 2020, "67571.107", raw, unit="thousands"),
                _row("tpop", "wpp", "POP", "DEU", 2020, "83000", raw, unit="millions"),  # unit mismatch
                _row("tpop", "wpp", "POP", "ITA", 2030, "58000", raw, unit="thousands", flags=proj),
                _row("tpop", "wpp", "POP", "ESP", 2020, "47000", raw, unit="thousands"),
            ],
            ("tpop", "wdi"): [
                _row("tpop", "wdi", "SP.POP.TOTL", "FRA", 2020, "67600000", raw),
                _row("tpop", "wdi", "SP.POP.TOTL", "DZA", 2020, "43451666", raw),
                _row("tpop", "wdi", "AG.LND.TOTL.K2", "DZA", 2020, "2381741", raw),
                _row("tpop", "wdi", "AG.LND.TOTL.K2", "ESP", 2019, "499564", raw),  # other year: no density
                _row("tpop", "wdi", "AG.LND.TOTL.K2", "FRA", 2020, "547557", raw),
                _row("tpop", "wdi", "EN.POP.DNST", "FRA", 2020, "123.4", raw),
            ],
            ("tbop", "bop"): [
                _row("tbop", "bop", "G|entry=NET", "FRA", 2022, "-100", raw),
                _row("tbop", "bop", "G|entry=CD", "FRA", 2022, "600", raw),
                _row("tbop", "bop", "G|entry=DB", "FRA", 2022, "650", raw),
                _row("tbop", "bop", "G|entry=CD", "DZA", 2022, "64.5", raw),
                _row("tbop", "bop", "G|entry=DB", "DZA", 2022, "40.25", raw),
            ],
            ("thyde", "h"): [
                _row("thyde", "h", "popc", "FRA", -9999, "1000", raw),
                _row("thyde", "h", "popc", "FRA", 0, "500000", raw),
            ],
        }
        for (src, ds), rows in tables.items():
            register_atlas_table(
                worker, store, source=src, dataset=ds, frame=frame_with_year_periods(rows), raw_sha256s=[raw],
                parser_version="test-headline/1", origin="test_fixture", geo_scheme="iso3",
            )  # fmt: skip
        snap = create_atlas_snapshot(worker, store, origin="test_fixture")
        q = AtlasQuery(snap, store, cache)
        tok, sha = new_api_token()
        with owner.begin() as c:
            c.execute(
                text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'hl')"),
                {"h": sha, "s": ["read"]},
            )
        app = create_app(
            Settings.model_validate(
                {
                    **settings.model_dump(),
                    "reva_session_secret": SecretStr("s" * 48),
                    "reva_allowed_hosts": "testserver,localhost",
                    "reva_cors_origins": "",
                }
            )
        )
        try:
            with TestClient(app) as client:
                yield Env(q, reg, client, tok)
        finally:
            q.close()
            app.state.engine.dispose()
            for e in engines:
                e.dispose()
            mp.undo()


def _get(env: Env, path: str, **params: Any) -> Any:
    r = env.client.get(path, params=params, headers={"Authorization": f"Bearer {env.token}"})
    assert r.status_code == 200, r.text
    return r.json()


def _status(cell: dict[str, Any]) -> dict[str, str]:
    return {c["series"]: c["status"] for c in cell["consulted"]}


def test_first_listed_source_answers_scaled_exactly_and_names_itself(env: Env) -> None:
    m = env.q.headline_map(env.reg, "reva.population.total", 2020)
    by = {c["iso3"]: c for c in m["values"]}
    fra = by["FRA"]
    assert fra["value"] == "67571107" and fra["unit"] == "persons" and fra["method"] == "reported"
    assert fra["reported"] == {"value": "67571.107", "unit": "thousands", "scale": "1000"}
    assert fra["answered_by"]["series"] == "tpop.wpp.POP" and fra["answered_by"]["source_rank"] == 0
    assert _status(fra) == {
        "tpop.wpp.POP": "answered",
        "tpop.wdi.SP.POP.TOTL": "also_reported",
        "tpop.absent.POP": "series_not_in_snapshot",
    }
    assert fra["provenance"]["raw_sha256"] and fra["provenance"]["selector"]["geo"] == "FRA"
    # DZA: not in the first source -> "cherche ailleurs": the WDI-like series answers, the first is named missing
    dza = by["DZA"]
    assert dza["value"] == "43451666" and dza["answered_by"]["series"] == "tpop.wdi.SP.POP.TOTL"
    assert _status(dza)["tpop.wpp.POP"] == "no_value"
    # DEU: the only cell has an unexpected unit -> never scaled, never an answer; reported as unit_mismatch
    missing = {x["iso3"]: x for x in m["countries_without_value"]}
    assert "DEU" not in by and _status(missing["DEU"])["tpop.wpp.POP"] == "unit_mismatch"
    assert m["by_source"] == {"tpop.wdi.SP.POP.TOTL": 1, "tpop.wpp.POP": 2}


def test_projections_never_answer(env: Env) -> None:
    m = env.q.headline_map(env.reg, "reva.population.total", 2030)
    assert m["values"] == []
    ita = {x["iso3"]: x for x in m["countries_without_value"]}["ITA"]
    assert _status(ita)["tpop.wpp.POP"] == "projection_only"


def test_derived_ratio_only_fills_missing_cells_and_cites_both_inputs(env: Env) -> None:
    m = env.q.headline_map(env.reg, "reva.population.density", 2020)
    by = {c["iso3"]: c for c in m["values"]}
    assert by["FRA"]["method"] == "reported" and by["FRA"]["value"] == "123.4"  # reported wins over derivable
    d = by["DZA"]
    assert d["method"] == "derived" and d["answered_by"] is None
    assert d["value"] == "18.2436570559"  # 43451666 / 2381741, 12 significant digits
    # same_source (default): the concept pair expands to series pairs of one provider
    assert d["derived"]["expression"] == "tpop.wdi.SP.POP.TOTL / tpop.wdi.AG.LND.TOTL.K2"
    assert "inputs_flags" in d["flags"]
    a, b = d["derived"]["inputs"]
    assert (
        a["answered_by"]["series"] == "tpop.wdi.SP.POP.TOTL" and b["answered_by"]["series"] == "tpop.wdi.AG.LND.TOTL.K2"
    )
    assert a["year"] == b["year"] == 2020
    assert _status(d)["tpop.wdi.EN.POP.DNST"] == "no_value"
    assert d["consulted"][-1]["status"] == "answered" and d["consulted"][-1]["series"].startswith("derived:")
    assert "ESP" not in by  # population 2020 and land area 2019: never combined across years
    assert m["by_source"] == {"derived": 1, "tpop.wdi.EN.POP.DNST": 1}


def test_derived_difference_uses_one_source_and_exact_decimals(env: Env) -> None:
    m = env.q.headline_map(env.reg, "reva.trade.goods_balance", 2022)
    by = {c["iso3"]: c for c in m["values"]}
    assert by["FRA"]["method"] == "reported" and by["FRA"]["value"] == "-100"
    assert by["DZA"]["method"] == "derived" and by["DZA"]["value"] == "24.25"
    assert by["DZA"]["derived"]["precision"] == "exact"
    assert [i["answered_by"]["series"] for i in by["DZA"]["derived"]["inputs"]] == [
        "tbop.bop.G|entry=CD",
        "tbop.bop.G|entry=DB",
    ]


def test_bce_years_are_astronomical(env: Env) -> None:
    s = env.q.headline_series(env.reg, "reva.history.population_hyde", "FRA")
    assert [(c["year"], c["period_start"], c["period_end"]) for c in s["best"]] == [
        (-9999, "-9999-01-01", "-9998-01-01"),
        (0, "0000-01-01", "0001-01-01"),
    ]


def test_dossier_series_and_coverage(env: Env) -> None:
    d = env.q.headline_dossier(env.reg, "FRA")
    items = {c["concept_id"]: c for c in d["concepts"]}
    assert items["reva.population.total"]["latest"]["answered_by"]["series"] == "tpop.wpp.POP"
    assert items["reva.population.total"]["sources_used"] == ["tpop.wpp.POP"]
    s = env.q.headline_series(env.reg, "reva.population.total", "DZA")
    assert s["sources_without_data"] == ["tpop.wpp.POP", "tpop.absent.POP"]
    cov = {c["concept_id"]: c for c in env.q.headline_coverage(env.reg)}
    pop = cov["reva.population.total"]
    assert pop["countries"] == 3 and pop["series_not_in_snapshot"] == ["tpop.absent.POP"]
    assert pop["countries_answered_by"] == {"tpop.wdi.SP.POP.TOTL": 1, "tpop.wpp.POP": 2}
    assert cov["reva.population.density"]["derived_cells"] == 1
    with pytest.raises(ValidationProblem):
        env.q.headline_map(env.reg, "reva.nope", 2020)


def test_api_headline_endpoints_and_dossier_section(env: Env) -> None:
    lst = _get(env, "/v1/atlas/headline/concepts")
    assert lst["total"] == 5 and all(
        c["review_status"] == "agent_declared_equivalence_not_human_reviewed" for c in lst["data"]
    )
    m = _get(env, "/v1/atlas/headline/map", concept="reva.population.density", year=2020)
    assert {v["iso3"]: v["method"] for v in m["data"]["values"]} == {"FRA": "reported", "DZA": "derived"}
    assert m["snapshot"]["origin"] == "test_fixture" and m["warnings"]
    s = _get(env, "/v1/atlas/headline/series", concept="reva.history.population_hyde", iso3="fra")
    assert s["data"]["best"][0]["period_start"] == "-9999-01-01"
    c = _get(env, "/v1/atlas/headline/coverage", theme="population")
    assert {x["concept_id"] for x in c["data"]} == {
        "reva.population.total",
        "reva.population.density",
        "reva.population.land",
    }
    d = _get(env, "/v1/atlas/countries/DZA")
    h = {x["concept_id"]: x for x in d["data"]["headline"]["concepts"]}
    assert h["reva.population.density"]["latest"]["method"] == "derived"
    assert any(n.startswith("REVA headline concepts") for n in d["notes"])
    r = env.client.get(
        "/v1/atlas/headline/map",
        params={"concept": "reva.nope", "year": 2020},
        headers={"Authorization": f"Bearer {env.token}"},
    )
    assert r.status_code == 404


def test_registered_headline_files_are_valid_and_cover_the_requested_concepts() -> None:
    from reva.atlas.catalog import load_catalog

    reg = load_headlines()  # strict
    cat = load_catalog()
    assert not set(reg.concepts) & set(cat.concepts)
    for c in reg.concepts.values():
        assert all(x in cat.concepts for x in c.related_catalog_concepts), c.id
        assert c.series or c.derived
        assert all(len(s.justification) >= 20 for s in c.series)
    wanted = {
        "reva.population.total", "reva.population.density", "reva.geography.land_area", "reva.geography.surface_area",
        "reva.economy.gdp_usd", "reva.economy.gdp_per_capita_usd", "reva.labour.unemployment_rate",
        "reva.labour.minimum_wage_monthly_lcu", "reva.labour.average_monthly_earnings_lcu",
        "reva.health.life_expectancy", "reva.society.hdi", "reva.education.literacy_rate_adult",
        "reva.environment.co2_per_capita", "reva.finance.government_net_lending_gdp", "reva.trade.current_account_gdp",
        "reva.trade.goods_balance_usd", "reva.economy.inflation_cpi", "reva.society.gini_income",
        "reva.science.internet_users", "reva.law.homicide_rate",
    }  # fmt: skip
    assert wanted <= set(reg.concepts)
    _ = pl, ATLAS_SCHEMA
