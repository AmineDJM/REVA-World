"""Atlas end to end: register_atlas_table -> atlas snapshot -> DuckDB queries -> GET /v1/atlas/*.

Offline: the raw artifacts are the World Bank fixture acquisitions (test_fixture origin); the
atlas tables are small synthetic tables that cite them. Runs in its own throwaway database so
atlas datasets never reach other modules' belief releases.
"""

from __future__ import annotations

import asyncio
import json
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
from reva.atlas import catalog as atlas_catalog
from reva.atlas.snapshot import SnapshotUnavailable, create_atlas_snapshot, resolve_snapshot
from reva.atlas.tables import ATLAS_SCHEMA, json_obj, missing_flags, register_atlas_table, year_period
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.errors import ValidationProblem
from reva.settings import Settings

pytestmark = pytest.mark.db

CATALOG = """
domain: labour
labels: {fr: Travail, en: Labour}
concepts:
  - id: labour.unemployment_rate
    labels: {fr: "Taux de chômage", en: "Unemployment rate"}
    definition: "Synthetic test concept."
    unit: "%"
    domain: labour
    subdomain: unemployment
    source_order: ["ilo.une.UR|sex=T", "wb.wdi.SL.UEM.TOTL.ZS"]
    provider_declared: true
  - id: labour.min_wage_statutory
    labels: {fr: "Salaire minimum légal", en: "Statutory minimum wage"}
    definition: "Synthetic text concept."
    unit: "1"
    domain: labour
    value_kind: text
    source_order: [{source: wb, dataset: wdi, indicator: MINWAGE.EXISTS}]
    provider_declared: true
"""


@dataclass
class Env:
    settings: Settings
    owner: Engine
    worker: Engine
    api: Engine
    store: FilesystemArtifactStore
    raws: list[str]
    client: TestClient
    token: str


def _rows(source: str, dataset: str, indicator: str, cells: dict[tuple[str, int], str | None], raw: str, **kw: Any):
    out = []
    for (geo, year), value in cells.items():
        s, e = year_period(year)
        out.append(
            {
                "source": source,
                "dataset": dataset,
                "indicator": indicator,
                "concept_id": None,
                "iso3": None,
                "provider_geo": geo,
                "entity_qid": None,
                "period_start": s,
                "period_end": e,
                "value": value,
                "value_text": kw.get("text"),
                "unit": kw.get("unit", "%"),
                "flags": kw.get("flags")
                or (missing_flags("not_reported") if value is None and not kw.get("text") else "{}"),
                "raw_sha256": raw,
                "selector": json_obj(geo=geo, year=year),
            }
        )
    return out


def ilo_frame(raw: str, fra_2021: str = "7.9") -> pl.DataFrame:
    return pl.DataFrame(
        _rows("ilo", "une", "UR|sex=T", {("FRA", 2020): "8.0", ("FRA", 2021): fra_2021, ("DEU", 2021): "3.6"}, raw),
        schema=ATLAS_SCHEMA,
    )


def wb_frame(raw: str) -> pl.DataFrame:
    rows = _rows(
        "wb",
        "wdi",
        "SL.UEM.TOTL.ZS",
        {("FRA", 2019): "8.4", ("FRA", 2021): "7.8", ("DZA", 2021): "12.7", ("WLD", 2021): "6.2", ("DEU", 2020): None},
        raw,
    )
    rows += _rows("wb", "wdi", "MINWAGE.EXISTS", {("FRA", 2021): None}, raw, text="Yes", unit="1")
    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


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
    root = tmp_path_factory.mktemp("atlas_objects")
    catdir = tmp_path_factory.mktemp("atlas_catalog")
    (catdir / "labour.yaml").write_text(CATALOG, encoding="utf-8")
    mp = pytest.MonkeyPatch()
    mp.setenv("REVA_ATLAS_CACHE_DIR", str(tmp_path_factory.mktemp("atlas_cache")))
    mp.setattr("reva.api.routes.atlas._catalog", lambda: atlas_catalog.load_catalog(catdir))
    with throwaway_database(base, prefix="reva_test_atlas") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker, api = (make_engine(k, settings) for k in ("migration", "worker", "api"))
        seed(owner, store)
        wb = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, worker, persist_cursor=False))
        with worker.connect() as c:
            raws = list(
                c.execute(
                    text("SELECT DISTINCT raw_sha256 FROM evidence.acquisition WHERE origin='test_fixture' ORDER BY 1")
                ).scalars()
            )
        assert len(raws) >= 2
        tok, sha = new_api_token()
        with owner.begin() as c:
            c.execute(
                text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'atlas-read')"),
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
                yield Env(settings, owner, worker, api, store, raws, client, tok)
        finally:
            app.state.engine.dispose()
            for e in (owner, worker, api):
                e.dispose()
            mp.undo()


def get(env: Env, path: str, **params: Any) -> Any:
    r = env.client.get(path, params=params, headers={"Authorization": f"Bearer {env.token}"})
    return r


# --------------------------------------------------------------------------- ordered scenario
def test_01_no_snapshot_is_a_typed_unavailable(env: Env) -> None:
    r = get(env, "/v1/atlas/map", concept="labour.unemployment_rate", year=2021)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "atlas_snapshot_unavailable"


def test_02_register_tables_idempotent_with_unmapped_listed(env: Env) -> None:
    a = register_atlas_table(
        env.worker, env.store, source="ilo", dataset="une", frame=ilo_frame(env.raws[0]), raw_sha256s=[env.raws[0]],
        parser_version="test-atlas/1", origin="test_fixture", geo_scheme="iso3",
    )  # fmt: skip
    b = register_atlas_table(
        env.worker, env.store, source="wb", dataset="wdi", frame=wb_frame(env.raws[1]), raw_sha256s=[env.raws[1]],
        parser_version="test-atlas/1", origin="test_fixture", geo_scheme="wb", coverage_note="synthetic",
    )  # fmt: skip
    assert a["created"] and b["created"]
    assert b["dataset_key"] == "atlas.wb.wdi" and b["row_count"] == 6
    assert b["unmapped_provider_geo"] == [{"provider_geo": "WLD", "rows": 1}]
    again = register_atlas_table(
        env.worker, env.store, source="wb", dataset="wdi", frame=wb_frame(env.raws[1]).reverse(),
        raw_sha256s=[env.raws[1]], parser_version="test-atlas/1", origin="test_fixture", geo_scheme="wb",
        coverage_note="synthetic",
    )  # fmt: skip
    assert not again["created"] and again["dataset_id"] == b["dataset_id"]
    manifest = json.loads(env.store.get("normalised", b["manifest_sha256"]))
    assert manifest["coverage"]["unmapped_provider_geo"] == [{"provider_geo": "WLD", "rows": 1}]
    assert manifest["coverage"]["missing_cells"] == 1 and manifest["coverage"]["text_cells"] == 1
    assert manifest["source_raw_sha256"] == [env.raws[1]]


def test_03_refuses_unarchived_raw_and_wrong_origin(env: Env) -> None:
    fake = "f" * 64
    with pytest.raises(ValidationProblem, match="not archived"):
        register_atlas_table(
            env.worker, env.store, source="ilo", dataset="une2", frame=ilo_frame(fake).with_columns(dataset=pl.lit("une2")),
            raw_sha256s=[fake], parser_version="t/1", origin="test_fixture",
        )  # fmt: skip
    with pytest.raises(ValidationProblem, match="not archived"):
        register_atlas_table(
            env.worker, env.store, source="ilo", dataset="une3",
            frame=ilo_frame(env.raws[0]).with_columns(dataset=pl.lit("une3")), raw_sha256s=[env.raws[0]],
            parser_version="t/1", origin="live",
        )  # fmt: skip
    with env.worker.connect() as c:
        n = c.execute(text("SELECT count(*) FROM evidence.dataset WHERE dataset_key LIKE 'atlas.ilo.une_'")).scalar()
    assert n == 0


def test_04_snapshot_and_api_views(env: Env) -> None:
    snap = create_atlas_snapshot(env.worker, env.store, origin="test_fixture")
    assert {t["dataset_key"] for t in snap.tables} == {"atlas.ilo.une", "atlas.wb.wdi"}
    with env.api.connect() as c:
        assert resolve_snapshot(c, env.store, origin="test_fixture").sha256 == snap.sha256
        with pytest.raises(SnapshotUnavailable):
            resolve_snapshot(c, env.store, origin="live")

    r = get(env, "/v1/atlas/map", concept="labour.unemployment_rate", year=2021)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshot"]["snapshot_sha256"] == snap.sha256
    assert [w["code"] for w in body["warnings"]] == ["fixture_data"]
    vals = {v["iso3"]: v for v in body["data"]["values"]}
    assert set(vals) == {"FRA", "DEU", "DZA"}  # WLD aggregate is unmapped: never on a country map
    assert vals["FRA"]["value"] == "7.9" and vals["FRA"]["series"] == "ilo.une.UR|sex=T"
    assert vals["FRA"]["sources_reporting"] == 2 and vals["FRA"]["source_rank"] == 0
    assert vals["DZA"]["value"] == "12.7" and vals["DZA"]["series"] == "wb.wdi.SL.UEM.TOTL.ZS"  # ILO lacks DZA
    assert vals["FRA"]["provenance"]["raw_sha256"] == env.raws[0]
    assert vals["FRA"]["provenance"]["selector"] == {"geo": "FRA", "year": 2021}
    assert vals["FRA"]["provenance"]["dataset_key"] == "atlas.ilo.une"
    assert "USA" in body["data"]["countries_without_value"]

    r = get(env, "/v1/atlas/map", concept="labour.unemployment_rate", year=2020).json()
    deu = [v for v in r["data"]["values"] if v["iso3"] == "DEU"]
    assert deu == []  # WB reports DEU 2020 as missing (flagged); ILO has no 2020 DEU: absent, not zero

    r = get(env, "/v1/atlas/series", concept="labour.unemployment_rate", iso3="fra").json()["data"]
    assert [(c["year"], c["value"], c["series"]) for c in r["best"]] == [
        (2019, "8.4", "wb.wdi.SL.UEM.TOTL.ZS"),
        (2020, "8.0", "ilo.une.UR|sex=T"),
        (2021, "7.9", "ilo.une.UR|sex=T"),
    ]
    assert [p["series"] for p in r["per_source"]] == ["ilo.une.UR|sex=T", "wb.wdi.SL.UEM.TOTL.ZS"]
    deu = get(env, "/v1/atlas/series", concept="labour.unemployment_rate", iso3="DEU").json()["data"]
    wb_deu = next(p for p in deu["per_source"] if p["source"] == "wb")["cells"]
    assert wb_deu[0]["value"] is None and wb_deu[0]["flags"] == {"missing": "not_reported"}

    d = get(env, "/v1/atlas/countries/FRA").json()["data"]
    assert d["label"] == "France" and d["concepts_with_data"] == 2
    lab = {c["concept_id"]: c for c in d["domains"][0]["concepts"]}
    assert lab["labour.unemployment_rate"]["latest"]["year"] == 2021
    assert lab["labour.min_wage_statutory"]["latest"]["value_text"] == "Yes"
    assert len(lab["labour.unemployment_rate"]["series"]) == 3
    dza = get(env, "/v1/atlas/countries/DZA", series="false").json()["data"]
    assert dza["domains"][0]["without_data"] == ["labour.min_wage_statutory"]
    assert dza["domains"][0]["concepts"][0].get("series") is None

    cov = get(env, "/v1/atlas/coverage", concept="labour.unemployment_rate").json()
    assert cov["data"][0]["countries"] == 3 and cov["data"][0]["year_min"] == 2019
    by = {r["iso3"]: r for r in cov["by_country"]}
    assert by["FRA"]["years"] == 3 and by["FRA"]["best_sources_used"] == ["ilo.une.UR|sex=T", "wb.wdi.SL.UEM.TOTL.ZS"]
    assert by["USA"]["years"] == 0 and cov["countries_total"] == 251  # crosswalk 1.1.0 (adds TWN)

    c = get(env, "/v1/atlas/concepts", q="chômage").json()
    assert [x["id"] for x in c["data"]] == ["labour.unemployment_rate"]
    assert c["data"][0]["review_status"] == "provider_declared_not_human_reviewed"

    assert get(env, "/v1/atlas/countries/XXX").status_code == 404
    assert get(env, "/v1/atlas/map", concept="nope.nope", year=2021).status_code == 404
    assert get(env, "/v1/atlas/map", concept="labour.unemployment_rate", year=2021, origin="live").status_code == 404
    assert env.client.get("/v1/atlas/concepts").status_code == 401


def test_05_new_version_needs_a_new_snapshot_old_one_stays_pinned(env: Env) -> None:
    with env.api.connect() as c:
        old = resolve_snapshot(c, env.store, origin="test_fixture")
    rec = register_atlas_table(
        env.worker, env.store, source="ilo", dataset="une", frame=ilo_frame(env.raws[0], fra_2021="7.3"),
        raw_sha256s=[env.raws[0]], parser_version="test-atlas/2", origin="test_fixture", geo_scheme="iso3",
    )  # fmt: skip
    assert rec["created"]
    fra = lambda body: next(v for v in body["data"]["values"] if v["iso3"] == "FRA")["value"]  # noqa: E731
    assert fra(get(env, "/v1/atlas/map", concept="labour.unemployment_rate", year=2021).json()) == "7.9"
    new = create_atlas_snapshot(env.worker, env.store, origin="test_fixture")
    assert new.sha256 != old.sha256
    assert fra(get(env, "/v1/atlas/map", concept="labour.unemployment_rate", year=2021).json()) == "7.3"
    pinned = get(env, "/v1/atlas/map", concept="labour.unemployment_rate", year=2021, snapshot=old.sha256).json()
    assert fra(pinned) == "7.9" and pinned["snapshot"]["snapshot_sha256"] == old.sha256


def test_06_tampered_table_bytes_are_refused(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.atlas.query import AtlasQuery
    from reva.kernel.errors import IntegrityFailure

    with env.api.connect() as c:
        snap = resolve_snapshot(c, env.store, origin="test_fixture")

    class Tampered:
        def open(self, namespace: str, sha: str) -> Iterator[bytes]:
            yield b"PAR1 not the pinned bytes"

    with pytest.raises(IntegrityFailure):
        AtlasQuery(snap, Tampered(), tmp_path)  # type: ignore[arg-type]
    assert not list(tmp_path.glob("atlas-*.duckdb"))


def test_07_api_role_cannot_register(env: Env) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises((DBAPIError, ValidationProblem)):
        register_atlas_table(
            env.api, env.store, source="ilo", dataset="une9",
            frame=ilo_frame(env.raws[0]).with_columns(dataset=pl.lit("une9")), raw_sha256s=[env.raws[0]],
            parser_version="t/1", origin="test_fixture",
        )  # fmt: skip
