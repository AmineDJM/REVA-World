"""LINKS3 end to end on a dedicated throwaway database: A-INFRA's physical-network atlas tables (Wikidata submarine
cables, OpenFlights June-2014 routes, GCAT payloads — trimmed real downloads) ingested through the atlas platform,
the real-Wikidata links fixture plus the Q55 → Q29999 hop statement loaded into the KG, a test_fixture atlas
snapshot, one materialisation of the link store, then ``/v1/links/{iso3}``, ``/v1/links/pair``, the on-demand
country view and the path engine read back. The dev database is never touched."""

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

SOURCES = ("atlas.wikidata_cables", "atlas.openflights", "atlas.gcat")
HOP_STATEMENT = "q55$3F58B793-57C8-4C66-87ED-403C7E298EBF"


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


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from reva.adapters.connectors import atlas_flights as fl
    from reva.adapters.connectors import atlas_space as sp
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.api.app import create_app
    from reva.api.auth import new_api_token
    from reva.atlas.ingest import run_atlas_ingest
    from reva.atlas.snapshot import create_atlas_snapshot
    from reva.atlas.sources import atlas_sources, ensure_registered
    from reva.cli.seed import seed
    from reva.links.fixture import FIXTURE_FILE, HOP_FIXTURE_FILE, load_into_kg
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
    mp.setenv("REVA_ATLAS_CACHE_DIR", str(tmp_path_factory.mktemp("links3_atlas_cache")))
    for mod, names in ((fl, ("MIN_OF_AIRPORTS", "MIN_OF_ROUTES")), (sp, ("MIN_LAUNCHES", "MIN_OBJECTS"))):
        for n in names:  # the fixtures are trimmed excerpts: lower the truncation guards
            mp.setattr(mod, n, 10)
    root = tmp_path_factory.mktemp("links3_objects")
    with throwaway_database(base, prefix="reva_test_links3") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker, api = (make_engine(k, settings) for k in ("migration", "worker", "api"))  # type: ignore[arg-type]
        seed(owner, store)
        ingests: dict[str, str] = {}
        for name in SOURCES:
            ensure_registered(owner, store, atlas_sources()[name].spec)
            out = asyncio.run(run_atlas_ingest(settings, worker, store, name, mode="fixture"))
            ingests[name] = out["status"]
        load_into_kg(worker, store)
        assert FIXTURE_FILE.exists()
        load_into_kg(worker, store, path=HOP_FIXTURE_FILE, source_key="wikidata.links_hop_fixture")
        snap = create_atlas_snapshot(worker, store, origin="test_fixture")
        res = materialise(worker, store, atlas_sha256=snap.sha256, top_k=5, duckdb_memory_limit="1GB")
        tok, sha = new_api_token()
        with owner.begin() as c:
            c.execute(
                text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'links3-read')"),
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
    r = env.client.get(path, params=params, headers={"Authorization": f"Bearer {env.token}"})
    assert r.status_code == 200, r.text
    return r.json()


def test_materialisation_stores_the_three_kinds_in_full_with_the_cited_hop(env: Env) -> None:
    assert set(env.ingests.values()) == {"completed"}, env.ingests
    rep = env.materialisation.report
    counts = rep["counts"]
    for kind in ("submarine_cable", "air_routes_2014", "launch_service"):
        assert counts.get(kind, 0) > 0, (kind, counts)
    assert rep["mapping_hops"] == {"Q55": HOP_STATEMENT}
    cab = rep["atlas"]["submarine_cable"]
    assert cab["mapped_via_hop"] == {"Q55": 10} and "mapping_hop_without_kg_evidence" not in cab["skipped"]
    with env.api.connect() as c:
        rows = c.execute(
            text(
                """SELECT link_type, count(*) AS n,
                          count(*) FILTER (WHERE jsonb_array_length(evidence) = 0 OR weight_num = 0) AS bad,
                          count(*) FILTER (WHERE NOT basis ? 'caveat' OR NOT basis ? 'licence') AS no_caveat
                     FROM kg.link_row WHERE materialisation_id = :m
                      AND link_type IN ('submarine_cable', 'air_routes_2014', 'launch_service')
                    GROUP BY link_type"""
            ),
            {"m": env.materialisation.id},
        ).mappings()
        by = {r["link_type"]: r for r in rows}
    assert all(r["bad"] == 0 and r["no_caveat"] == 0 for r in by.values()), by
    # stored in full (top_k = 5 would keep fewer): every derived link is a stored row
    assert {k: by[k]["n"] for k in by} == {k: counts[k] for k in by}


def test_country_view_lists_cables_with_the_hop_and_the_community_warning(env: Env) -> None:
    body = get(env, "/v1/links/GBR", kind="submarine_cable", limit=10)
    d = body["data"]
    assert d["mode"] == "materialised"
    (grp,) = d["by_kind"]["submarine_cable"]["groups"]
    assert grp["direction"] == "both" and grp["total"] >= 10
    nld = next(lk for lk in grp["links"] if "NLD" in lk["iso3"])
    assert nld["weight"] == "2" and nld["basis"]["mapping_hop"]["statement_id"] == HOP_STATEMENT
    assert any(e["kind"] == "kg_statement" and e["statement_id"] == HOP_STATEMENT for e in nld["evidence"])
    assert any(e.get("note", "").startswith("Wikidata cable items:") for e in nld["evidence"])
    assert "community" in nld["basis"]["caveat"]["en"].lower() and nld["text"]["fr"]
    codes = [w["code"] for w in body["warnings"]]
    assert codes[0] == "link_not_causal" and "community_edited" in codes and "fixture_data" in codes


def test_pair_view_reads_air_routes_both_ways_with_the_2014_caveat(env: Env) -> None:
    d = get(env, "/v1/links/pair", a="GBR", b="MAR")["data"]
    air = d["kinds"]["air_routes_2014"]
    assert air["directed"] and air["count"] == 2 and air["first_year"] == air["last_year"] == 2014
    for lk in air["links"]:
        assert lk["weight"] == "25" and lk["unit"] == "routes" and lk["temporal"] == {"kind": "snapshot", "year": 2014}
        assert "June 2014" in lk["basis"]["caveat"]["en"] and lk["basis"]["licence"].startswith("ODbL")
        assert "juin 2014" in lk["text"]["fr"] and len(lk["evidence"]) == 3
    assert "submarine_cable" in d["kinds"] or "submarine_cable" in d["kinds_without_links"]
    nl = get(env, "/v1/links/pair", a="GBR", b="NLD", kind="submarine_cable")["data"]
    (lk,) = nl["kinds"]["submarine_cable"]["links"]
    assert lk["basis"]["first_service_year"] == 1999 and lk["basis"]["mapping_hop"]["provider_code"] == "Q55"


def test_launch_service_is_owner_to_site_state_per_year(env: Env) -> None:
    d = get(env, "/v1/links/USA", kind="launch_service", limit=10)["data"]
    groups = {g["direction"]: g for g in d["by_kind"]["launch_service"]["groups"]}
    out = groups["outgoing"]
    assert out["period"] and all(lk["iso3"][0] == "USA" for lk in out["links"])
    ws = [int(lk["weight"]) for lk in out["links"]]
    assert ws == sorted(ws, reverse=True) and "GCAT" in out["links"][0]["basis"]["caveat"]["en"]
    pv = get(env, "/v1/links/pair", a="USA", b="RUS", kind="launch_service")["data"]
    if "launch_service" in pv["kinds"]:
        for lk in pv["kinds"]["launch_service"]["links"]:
            assert lk["basis"]["source_flags"]["direction"] == "owner->site"


def test_on_demand_country_view_applies_the_hop_from_the_kg(env: Env) -> None:
    from reva.atlas.query import query_for
    from reva.links import service

    with env.api.connect() as conn:
        d = service._country_on_demand(
            conn, "NOR", ["submarine_cable"], None, None, 10, lambda _sha: query_for(env.snapshot, env.store), False
        )
    assert d["mode"] == "on_demand"
    links = d["by_kind"]["submarine_cable"]["groups"][0]["links"]
    assert sorted(x for lk in links for x in lk["iso3"] if x != "NOR") == ["DNK", "NLD", "SWE"]  # NOR: 3 partners
    nld = [lk for lk in links if "NLD" in (lk["iso3"] or [])]
    assert any(e.get("statement_id") == HOP_STATEMENT for e in nld[0]["evidence"])


def test_paths_traverse_stored_physical_links(env: Env) -> None:
    from reva.links import service

    with env.api.connect() as c:
        res = service.path_between(c, "GBR", "MAR", k=1, max_hops=1, kinds=frozenset({"link:air_routes_2014"}))
    if res.reason == "unknown_node":
        pytest.skip("GBR/MAR not in the KG fixture")
    assert res.found, res.reason
    hop = res.paths[0].hops[0]
    assert hop.edge.kind == "link" and hop.edge.type_id == "air_routes_2014"
    assert json.dumps(hop.edge.link.basis["caveat"]).count("2014") >= 1  # type: ignore[union-attr]
