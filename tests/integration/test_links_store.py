"""LINKS2 end to end on a dedicated throwaway database: atlas fixtures (UNGA votes, CShapes, UCDP, UN DESA IMS,
Natural Earth 10m, IMF IMTS, BACI — trimmed real downloads) ingested through the atlas platform, the real-Wikidata
links fixture loaded into the KG, a test_fixture atlas snapshot, one materialisation of the link store, then
``/v1/links/{iso3}`` and ``/v1/links/pair`` read back through the HTTP API. The dev database is never touched."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.settings import Settings

pytestmark = pytest.mark.db

SOURCES = (
    "atlas_unga_votes",
    "atlas_cshapes",
    "atlas_ucdp",
    "atlas_un",
    "atlas_naturalearth10m",
    "atlas_imf",
    "atlas_baci",
)


@dataclass
class Env:
    owner: Engine
    worker: Engine
    api: Engine
    store: Any
    client: Any
    token: str
    materialisation: Any
    snapshot: Any
    ingests: dict[str, str]


def _specs(module: Any) -> list[Any]:
    sp = module.atlas_source()
    return list(sp) if isinstance(sp, list | tuple) else [sp]


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    import importlib

    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.api.app import create_app
    from reva.api.auth import new_api_token
    from reva.atlas.ingest import run_atlas_ingest
    from reva.atlas.snapshot import create_atlas_snapshot
    from reva.atlas.sources import ensure_registered
    from reva.cli.seed import seed
    from reva.links.fixture import FIXTURE_FILE, load_into_kg
    from reva.links.store import materialise

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    mp = pytest.MonkeyPatch()
    mp.setenv("REVA_ATLAS_CACHE_DIR", str(tmp_path_factory.mktemp("links_atlas_cache")))
    root = tmp_path_factory.mktemp("links_objects")
    with throwaway_database(base, prefix="reva_test_links2") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker, api = (make_engine(k, settings) for k in ("migration", "worker", "api"))  # type: ignore[arg-type]
        seed(owner, store)
        ingests: dict[str, str] = {}
        for mod in SOURCES:
            m = importlib.import_module(f"reva.adapters.connectors.{mod}")
            for spec in _specs(m):
                if spec.name == "atlas.un_ims" or mod != "atlas_un":
                    ensure_registered(owner, store, spec)
                    out = asyncio.run(run_atlas_ingest(settings, worker, store, spec.name, mode="fixture"))
                    ingests[spec.name] = out["status"]
        if FIXTURE_FILE.exists():
            load_into_kg(worker, store)
        snap = create_atlas_snapshot(worker, store, origin="test_fixture")
        res = materialise(worker, store, atlas_sha256=snap.sha256, top_k=5, duckdb_memory_limit="1GB")
        tok, sha = new_api_token()
        with owner.begin() as c:
            c.execute(
                text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'links2-read')"),
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
        app.state.atlas_store = store
        try:
            with TestClient(app) as client:
                yield Env(owner, worker, api, store, client, tok, res, snap, ingests)
        finally:
            app.state.engine.dispose()
            for e in (owner, worker, api):
                e.dispose()
            mp.undo()


def get(env: Env, path: str, **params: Any) -> Any:
    return env.client.get(path, params=params, headers={"Authorization": f"Bearer {env.token}"})


def test_materialisation_records_snapshot_watermark_and_counts(env: Env) -> None:
    assert set(env.ingests.values()) == {"completed"}, env.ingests
    counts = env.materialisation.report["counts"]
    for kind in ("bilateral_trade", "migrant_stock", "land_border", "unga_vote_agreement", "land_border_cshapes",
                 "colonial_rule_cshapes", "shared_official_language"):  # fmt: skip
        assert counts.get(kind, 0) > 0, (kind, counts)
    with env.api.connect() as c:
        m = c.execute(text("SELECT * FROM kg.link_materialisation ORDER BY id DESC LIMIT 1")).mappings().one()
        assert m["atlas_snapshot_sha256"] == env.snapshot.sha256 and m["atlas_origin"] == "test_fixture"
        assert m["kg_load_watermark"] is not None and m["recorded_by"] == "reva_worker"
        n = c.execute(text("SELECT count(*) FROM kg.link_row WHERE materialisation_id = :m"), {"m": m["id"]}).scalar_one()
        assert n == m["link_count"] == env.materialisation.link_count
        # every stored row has evidence; no zero-weight link; weights are decimal strings with a unit
        bad = c.execute(
            text(
                """SELECT count(*) FROM kg.link_row WHERE materialisation_id = :m AND (jsonb_array_length(evidence) = 0
                     OR weight_num = 0 OR (weight IS NOT NULL AND unit IS NULL))"""
            ),
            {"m": m["id"]},
        ).scalar_one()
        assert bad == 0


def test_store_is_append_only_and_read_only_for_the_api(env: Env) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        with env.api.begin() as c:
            c.execute(text("DELETE FROM kg.link_row"))
    with pytest.raises(DBAPIError):
        with env.worker.begin() as c:
            c.execute(text("UPDATE kg.link_row SET weight = '1' WHERE weight IS NOT NULL"))
    with pytest.raises(DBAPIError):  # rows of a sealed (earlier-transaction) materialisation
        with env.worker.begin() as c:
            c.execute(
                text(
                    """INSERT INTO kg.link_row (materialisation_id, link_type, subject, object, directed, temporal,
                                                evidence, origin)
                       VALUES (:m, 'land_border', 'iso3:AAA', 'iso3:BBB', false, '{"kind":"undated"}',
                               '[{"kind":"kg_statement","source":"x","statement_id":"y"}]', 'live')"""
                ),
                {"m": env.materialisation.id},
            )


def test_country_view_reads_the_store_with_top_n_per_kind(env: Env) -> None:
    r = get(env, "/v1/links/FRA", limit=3)
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["data"]
    assert d["mode"] == "materialised" and d["store"]["atlas_snapshot_sha256"] == env.snapshot.sha256
    assert body["warnings"][0]["code"] == "link_not_causal"
    assert "fixture_data" in {w["code"] for w in body["warnings"]}
    trade = d["by_kind"]["bilateral_trade"]
    out = next(g for g in trade["groups"] if g["direction"] == "outgoing")
    assert 1 <= len(out["links"]) <= 3 and out["period"]
    ws = [float(lk["weight"]) for lk in out["links"]]
    assert ws == sorted(ws, reverse=True)
    lk = out["links"][0]
    assert lk["evidence"][0]["raw_sha256"] and lk["evidence"][0]["selector"] and lk["text"]["en"]
    assert lk["iso3"][0] == "FRA" and lk["weight_kind"] == "source_quantity"
    r = get(env, "/v1/links/FRA", kind="unga_vote_agreement", facet="issue=co", limit=2)
    assert r.status_code == 200, r.text
    v = r.json()["data"]["by_kind"].get("unga_vote_agreement")
    if v:
        g = v["groups"][0]
        assert g["facet"] == "issue=co" and g["links"][0]["basis"]["n_common"] >= 10
        assert g["links"][0]["weight_kind"] == "derived_index"
        assert {w["code"] for w in r.json()["warnings"]} >= {"derived_index"}
    assert get(env, "/v1/links/FRA", kind="no_such_kind").status_code == 422
    assert get(env, "/v1/links/FRA", limit=50).status_code == 422
    assert get(env, "/v1/links/ZZZ").status_code == 404


def test_pair_view_reads_full_history_with_evidence(env: Env) -> None:
    r = get(env, "/v1/links/pair", a="FRA", b="DZA")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["atlas_snapshot"]["snapshot_sha256"] == env.snapshot.sha256
    kinds = d["kinds"]
    assert "bilateral_trade" in kinds, sorted(kinds)
    years = {lk["temporal"]["year"] for lk in kinds["bilateral_trade"]["links"]}
    assert len(years) >= 1
    for k in kinds.values():
        for lk in k["links"]:
            assert lk["evidence"] and lk["text"]["fr"]
            assert lk["weight"] is None or float(lk["weight"]) != 0
    assert "colonial_rule_cshapes" in kinds or "land_border_cshapes" in kinds
    assert get(env, "/v1/links/pair", a="FRA", b="FRA").status_code == 422
    assert get(env, "/v1/links/pair", a="FRA", b="ZZZ").status_code == 404
    r = get(env, "/v1/links/pair", a="FRA", b="DZA", votes_detail_from=1960, votes_detail_to=1980)
    assert r.status_code == 422


def test_pair_vote_agreement_by_year_and_decade(env: Env) -> None:
    with env.api.connect() as c:
        row = c.execute(
            text(
                """SELECT subject_iso3, object_iso3, facet, year_lo FROM kg.link_row
                    WHERE link_type = 'unga_vote_agreement' AND subject_iso3 IS NOT NULL AND object_iso3 IS NOT NULL
                    ORDER BY (basis->>'n_common')::int DESC LIMIT 1"""
            )
        ).first()
    if row is None:
        pytest.skip("UNGA fixture slice has no pair with >= 10 common roll calls")
    a, b, facet, lo = row
    r = get(env, "/v1/links/pair", a=a, b=b, kind="unga_vote_agreement", votes_detail_from=lo, votes_detail_to=lo + 9)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    links = d["kinds"]["unga_vote_agreement"]["links"]
    grains = {lk["basis"]["grain"] for lk in links}
    assert "decade" in grains
    lk = next(x for x in links if x["basis"]["grain"] == "decade" and x["temporal"]["start"] == f"{lo:04d}")
    sel = json.loads(lk["evidence"][0]["selector"])
    assert sel["method"] == "links-vote-agreement/1" and lk["basis"]["formula"].startswith("n_identical / n_common")
    assert d["vote_detail"]["rollcalls"] and "alliance" not in json.dumps(lk["basis"]).replace("not an alliance", "")


def test_paths_use_stored_links(env: Env) -> None:
    from reva.links import service

    with env.api.connect() as c:
        res = service.path_between(c, "FRA", "DZA", k=3, max_hops=2, kinds=frozenset({"link:bilateral_trade"}))
    if res.reason == "unknown_node":
        pytest.skip("FRA/DZA not in the KG fixture")
    assert res.found, res.reason
    hop = res.paths[0].hops[0]
    assert hop.edge.kind == "link" and hop.edge.type_id == "bilateral_trade"
