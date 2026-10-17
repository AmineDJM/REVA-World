"""GET /v1/atlas/tables/{source}/{dataset}/cells (V6 INTEG measured_by bridge) and providers' required notices.

A KG ``measured_by`` statement of V4ECON / V4MIND / V4ECO points at an atlas table entity
``atlas:<source>.<dataset>`` with the identifiers that select the subject's cells (provider region code, indicator
or ``classif1=`` code); this endpoint reads exactly those cells from the pinned atlas snapshot. A table whose terms
require a notice (New York Fed reference rates) carries it on every cell, in ``licences`` and as a
``required_notice`` warning.

Offline, throwaway database: raw artifacts are the World Bank fixture acquisitions (test_fixture origin); the atlas
tables are small synthetic tables citing them. Values are illustrative, not sourced.
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
from sqlalchemy import create_engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.atlas import catalog as atlas_catalog
from reva.atlas.snapshot import create_atlas_snapshot
from reva.atlas.tables import ATLAS_SCHEMA, json_obj, register_atlas_table, year_period
from reva.evidence.runner import FixtureHttp, run_connector
from reva.settings import Settings

pytestmark = pytest.mark.db

NOTICE = "FIXTURE notice: synthetic rates subject to the provider's terms (test only)."
CATALOG = """
domain: economy
labels: {fr: Économie, en: Economy}
concepts:
  - id: economy.population_test
    labels: {fr: "Population (test)", en: "Population (test)"}
    definition: "Synthetic test concept."
    unit: "persons"
    domain: economy
    source_order: ["regtest.reg_pop.POP|unit=PS"]
    provider_declared: true
"""


@dataclass
class Env:
    client: TestClient
    token: str


def _row(source: str, dataset: str, indicator: str, geo: str, start: Any, end: Any, value: str, raw: str) -> dict:
    return {
        "source": source, "dataset": dataset, "indicator": indicator, "concept_id": None, "iso3": None,
        "provider_geo": geo, "entity_qid": None, "period_start": start, "period_end": end, "value": value,
        "value_text": None, "unit": "persons", "flags": "{}", "raw_sha256": raw, "selector": json_obj(geo=geo),
    }  # fmt: skip


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from datetime import date

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
    root = tmp_path_factory.mktemp("atlas_tc_objects")
    catdir = tmp_path_factory.mktemp("atlas_tc_catalog")
    (catdir / "economy.yaml").write_text(CATALOG, encoding="utf-8")
    mp = pytest.MonkeyPatch()
    mp.setenv("REVA_ATLAS_CACHE_DIR", str(tmp_path_factory.mktemp("atlas_tc_cache")))
    mp.setattr("reva.api.routes.atlas._catalog", lambda: atlas_catalog.load_catalog(catdir))
    with throwaway_database(base, prefix="reva_test_atlas_tc") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker = (make_engine(k, settings) for k in ("migration", "worker"))
        seed(owner, store)
        wb = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, worker, persist_cursor=False))
        with worker.connect() as c:
            raw = c.execute(
                text("SELECT raw_sha256 FROM evidence.acquisition WHERE origin='test_fixture' ORDER BY 1 LIMIT 1")
            ).scalar_one()
        rows = []
        for geo, vals in (("FRA", ("67000000", "67400000")), ("FRL04", ("2040000", "2050000"))):
            for yr, v in zip((2021, 2022), vals, strict=True):
                s, e = year_period(yr)
                rows.append(_row("regtest", "reg_pop", "POP|unit=PS", geo, s, e, v, raw))
        rows.append(_row("regtest", "reg_pop", "POP|unit=PS|classif1=OCU_X", "FRA", *year_period(2022), "5", raw))
        register_atlas_table(
            worker, store, source="regtest", dataset="reg_pop", frame=pl.DataFrame(rows, schema=ATLAS_SCHEMA),
            raw_sha256s=[raw], parser_version="test-tc/1", origin="test_fixture", geo_scheme="iso3",
            rights={"licence": "CC-BY-4.0"},
        )  # fmt: skip
        daily = [
            _row("ratetest", "rates_d", "EFFR|field=percentRate", "US", date(2026, 10, d), date(2026, 10, d + 1),
                 v, raw)
            for d, v in ((14, "4.08"), (15, "4.09"))
        ]  # fmt: skip
        register_atlas_table(
            worker, store, source="ratetest", dataset="rates_d", frame=pl.DataFrame(daily, schema=ATLAS_SCHEMA),
            raw_sha256s=[raw], parser_version="test-tc/1", origin="test_fixture", geo_scheme="iso2",
            rights={"licence": "Provider Terms of Use", "required_notice": NOTICE},
        )  # fmt: skip
        create_atlas_snapshot(worker, store, origin="test_fixture")
        tok, sha = new_api_token()
        with owner.begin() as c:
            c.execute(
                text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'atlas-tc')"),
                {"h": sha, "s": ["read"]},
            )
        api_settings = Settings.model_validate(
            {
                **settings.model_dump(),
                "reva_session_secret": SecretStr("s" * 48),
                "reva_allowed_hosts": "testserver,localhost",
                "reva_cors_origins": "",
            }
        )
        app = create_app(api_settings)
        try:
            with TestClient(app) as client:
                yield Env(client, tok)
        finally:
            app.state.engine.dispose()
            for e in (owner, worker):
                e.dispose()
            mp.undo()


def get(env: Env, path: str, **params: Any) -> Any:
    return env.client.get(path, params=params, headers={"Authorization": f"Bearer {env.token}"})


def test_region_cells_by_provider_geo(env: Env) -> None:
    r = get(env, "/v1/atlas/tables/regtest/reg_pop/cells", provider_geo="FRL04")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["in_snapshot"] is True and d["concepts"] == ["economy.population_test"]
    assert [(c["provider_geo"], c["iso3"], c["year"], c["value"]) for c in d["cells"]] == [
        ("FRL04", None, 2021, "2040000"),
        ("FRL04", None, 2022, "2050000"),
    ]  # a region is never a country: no iso3
    (ind,) = d["indicators"]
    assert (ind["indicator"], ind["cells"], ind["geographies"], ind["countries"]) == ("POP|unit=PS", 2, 1, 0)
    assert d["cells"][0]["provenance"]["raw_sha256"] and d["licences"][0]["licence"] == "CC-BY-4.0"
    assert "required_notice" not in [w["code"] for w in r.json()["warnings"]]


def test_indicator_substring_summary_without_geography(env: Env) -> None:
    d = get(env, "/v1/atlas/tables/regtest/reg_pop/cells", indicator_contains="classif1=OCU_X").json()["data"]
    assert [i["indicator"] for i in d["indicators"]] == ["POP|unit=PS|classif1=OCU_X"]
    assert d["cells"] == [] and d["indicators"][0]["countries"] == 1  # summary only: no geography was named
    d = get(env, "/v1/atlas/tables/regtest/reg_pop/cells", indicator="POP|unit=PS", iso3="fra").json()["data"]
    assert [c["value"] for c in d["cells"]] == ["67000000", "67400000"]


def test_absent_table_is_typed_not_empty(env: Env) -> None:
    r = get(env, "/v1/atlas/tables/oecd/reg_pop/cells", provider_geo="FRL04")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["in_snapshot"] is False and r.json()["data"]["cells"] == []
    assert "table_not_in_snapshot" in [w["code"] for w in r.json()["warnings"]]
    assert get(env, "/v1/atlas/tables/oecd/REG/cells").status_code == 422


def test_required_notice_on_cells_licences_and_warnings(env: Env) -> None:
    r = get(env, "/v1/atlas/tables/ratetest/rates_d/cells", provider_geo="US")
    assert r.status_code == 200, r.text
    body = r.json()
    assert all(c["rights"]["required_notice"] == NOTICE for c in body["data"]["cells"])
    assert body["data"]["licences"][0]["required_notice"] == NOTICE
    notes = [w["message"] for w in body["warnings"] if w["code"] == "required_notice"]
    assert notes == [f"ratetest: {NOTICE}"]
