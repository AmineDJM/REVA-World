"""Atlas source registry + the budget-metered atlas ingest entry point (offline, throwaway DB).

A synthetic atlas module (inserted in ``sys.modules``) declares an ``AtlasSourceSpec`` budgeted on
the registered ``worldbank.wdi`` source; its "live" client is a local fake (no network). Checks:
the reservation happens before any request and is settled with the actual usage, raw bytes become
live acquisitions of the atlas connector version, the table is registered, an over-budget run is
refused before the client is even built, and fixture mode is refused in production.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.http.safe_client import HttpResult
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.atlas import sources as atlas_sources
from reva.atlas.ingest import AtlasRun, run_atlas_ingest
from reva.atlas.sources import AtlasSourceSpec, ensure_registered
from reva.atlas.tables import ATLAS_SCHEMA, json_obj, year_period
from reva.kernel.errors import PolicyDenied, ValidationProblem
from reva.operations.budgets import BudgetExhausted
from reva.settings import Settings

pytestmark = pytest.mark.db

MOD = "reva_test_fake_atlas_module"
SPEC = AtlasSourceSpec(
    name="atlas.testsrc",
    version="1.0.0",
    source_key="worldbank.wdi",
    atlas_source="testsrc",
    provider="Synthetic test provider",
    licence="CC0-1.0",
    allowed_hosts=("api.worldbank.org",),
    datasets=("demo", "other"),
    cadence_seconds=86400,
    planned_requests=2,
    parser_version="atlas-testsrc/1",
)
GREEDY = SPEC.model_copy(update={"name": "atlas.testsrc_greedy", "planned_requests": 5000})


async def fake_atlas_ingest(run: AtlasRun) -> dict[str, Any]:
    raws = []
    for i, url in enumerate(("https://api.worldbank.org/a.csv", "https://api.worldbank.org/b.csv")):
        raw = await run.get(url, key=f"file{i}", media_types=("text/csv",))
        raws.append(run.archive(raw))
    s, e = year_period(2021)
    rows = [
        {"source": "testsrc", "dataset": "demo", "indicator": "X", "concept_id": None, "iso3": None,
         "provider_geo": g, "entity_qid": None, "period_start": s, "period_end": e, "value": v, "value_text": None,
         "unit": "1", "flags": "{}", "raw_sha256": raws[0], "selector": json_obj(row=n)}
        for n, (g, v) in enumerate((("FRA", "1.5"), ("DZA", "2")))
    ]  # fmt: skip
    if run.wants("demo"):
        run.register(
            dataset="demo", frame=pl.DataFrame(rows, schema=ATLAS_SCHEMA), raw_sha256s=[raws[0]], geo_scheme="iso3"
        )
    return {"files": len(raws)}


class FakeLive:
    origin = "live"
    max_bytes = 1_000_000

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, url: str, **kw: Any) -> HttpResult:
        self.calls.append(url)
        body = f"payload for {url}".encode()
        return HttpResult(url, url, 200, {"content-type": "text/csv"}, body, datetime.now(UTC), 0.01, (), 1)

    async def aclose(self) -> None:
        return None


@dataclass
class Env:
    settings: Settings
    owner: Engine
    worker: Engine
    store: FilesystemArtifactStore


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import seed

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    mod = types.ModuleType(MOD)
    mod.ATLAS_SOURCES = [SPEC, GREEDY]  # type: ignore[attr-defined]
    mod.atlas_ingest = fake_atlas_ingest  # type: ignore[attr-defined]
    sys.modules[MOD] = mod
    atlas_sources.clear_cache()
    atlas_sources._CACHE["v"] = atlas_sources.discover([MOD])
    root = tmp_path_factory.mktemp("atlas_ingest_objects")
    with throwaway_database(base, prefix="reva_test_atlas_ingest") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner, worker = make_engine("migration", settings), make_engine("worker", settings)
        seed(owner, store)
        for spec in (SPEC, GREEDY):
            ensure_registered(owner, store, spec)
        try:
            yield Env(settings, owner, worker, store)
        finally:
            atlas_sources.clear_cache()
            sys.modules.pop(MOD, None)
            for e in (owner, worker):
                e.dispose()


def test_discovery_reports_specs_and_errors(env: Env) -> None:
    found, errors = atlas_sources.discover([MOD, "reva_test_no_such_atlas_module"])
    assert set(found) >= {"atlas.testsrc", "atlas.testsrc_greedy"}
    assert errors and errors[0]["module"] == "reva_test_no_such_atlas_module"
    with pytest.raises(ValueError):
        AtlasSourceSpec(**{**SPEC.model_dump(), "atlas_source": "with.dot"})
    with pytest.raises(ValueError):
        AtlasSourceSpec(**{**SPEC.model_dump(), "allowed_hosts": ("http://evil",)})
    assert SPEC.budget_key == "worldbank.wdi" and SPEC.template == "atlas_bulk"


def test_live_run_is_reserved_metered_settled_and_archived(env: Env) -> None:
    fake = FakeLive()
    out = asyncio.run(
        run_atlas_ingest(env.settings, env.worker, env.store, "atlas.testsrc", http_factory=lambda s, st: fake)
    )
    assert out["status"] == "completed" and out["archived_raw"] == 2 and len(fake.calls) == 2
    assert out["registered"][0]["dataset_key"] == "atlas.testsrc.demo"
    assert out["budget"]["consumed_requests"] == 2 and out["budget"]["source_key"] == "worldbank.wdi"
    with env.owner.connect() as c:
        ledger = {
            r["unit"]: r
            for r in c.execute(
                text(
                    "SELECT unit, reserved, consumed FROM ops.budget_ledger WHERE account_key LIKE 'source:worldbank.wdi:%'"
                )
            ).mappings()
        }
        acqs = c.execute(
            text("""SELECT a.origin FROM evidence.acquisition a JOIN evidence.connector_version v
                    ON v.id = a.connector_version_id WHERE v.name = 'atlas.testsrc'""")
        ).scalars().all()  # fmt: skip
    assert ledger["requests"]["consumed"] == 2
    assert ledger["bytes"]["consumed"] == sum(len(f"payload for {u}".encode()) for u in fake.calls)
    assert acqs == ["live", "live"]
    again = asyncio.run(
        run_atlas_ingest(
            env.settings, env.worker, env.store, "atlas.testsrc", dataset="other", http_factory=lambda s, st: FakeLive()
        )
    )
    assert again["registered"] == []  # dataset filter: nothing registered for "demo"


def test_over_budget_run_is_refused_before_any_request(env: Env) -> None:
    built: list[Any] = []

    def factory(spec: Any, settings: Any) -> FakeLive:
        built.append(spec)
        return FakeLive()

    with pytest.raises(BudgetExhausted) as e:
        asyncio.run(run_atlas_ingest(env.settings, env.worker, env.store, "atlas.testsrc_greedy", http_factory=factory))
    assert e.value.details["reason"] == "source_limit" and built == []


def test_typed_refusals(env: Env) -> None:
    with pytest.raises(ValidationProblem, match="unknown atlas source"):
        asyncio.run(run_atlas_ingest(env.settings, env.worker, env.store, "atlas.nope"))
    with pytest.raises(ValidationProblem, match="dataset not declared"):
        asyncio.run(run_atlas_ingest(env.settings, env.worker, env.store, "atlas.testsrc", dataset="zzz"))
    prod = env.settings.model_copy(update={"reva_env": "production"})
    with pytest.raises(PolicyDenied):
        asyncio.run(run_atlas_ingest(prod, env.worker, env.store, "atlas.testsrc", mode="fixture"))
    assert json.loads(json.dumps(SPEC.model_dump(mode="json")))["template"] == "atlas_bulk"
