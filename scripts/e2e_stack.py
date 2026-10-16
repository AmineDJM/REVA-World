#!/usr/bin/env python3
"""Start a complete, disposable local REVA stack for the UI end-to-end suite (CI item 10).

What it does, in order:

1. creates a fresh, uniquely named PostgreSQL database with the local migration
   owner from ``.env`` (same pattern as the ``db`` fixture in tests/conftest.py),
   applies migrations/role passwords (``bootstrap``) and seeds the registry;
2. ingests the captured provider fixtures (World Bank WDI, UN Comtrade, Natural
   Earth, Crossref) offline through ``run_connector`` + ``FixtureHttp`` +
   ``post_publish_for`` and publishes a ``test_fixture`` belief release;
3. builds the UI (``npm --prefix ui run build``) into a private directory
   (``--outDir``; the repository's src/reva/api/static is never touched);
4. serves ``create_app`` with uvicorn on a free 127.0.0.1 port, authenticated by a
   freshly generated admin password (Argon2id hash only in memory) and session
   secret, plus a real ``Worker`` thread (worker + simulation roles; scheduler and
   outbox invalidation disabled so nothing touches live providers);
5. signs in once over HTTP and writes a Playwright ``storageState`` file, then
   writes ``stack.json`` (base URL, credentials, database name, release id,
   cutoff, storage-state path) for the Playwright specs;
6. on exit, SIGINT or SIGTERM: stops the worker, drops the database and removes
   the temporary object store and UI build.

Options for the V2 suites (formerly the ui/e2e/recorded_llm_stack.py monkey-patching wrapper,
which now just calls this script with ``--recorded-drafter --v2-fixtures``):

``--recorded-drafter``
    ``POST /v1/scenario-drafts`` replays the recorded provider responses of
    ``tests/fixtures/nl/recorded`` (``reva.nl.recorded.RecordedDrafter``), exactly as the API
    integration tests do. No live LLM is ever called. Without it, drafting stays disabled and the
    composer specs check the typed ``llm_unavailable`` refusal.
``--v2-fixtures``
    also ingests, offline and before the release cutoff is taken, the V2 recorded fixtures:
    all-country World Bank / Comtrade subsets, Eurostat demography (LI, LU since 2008 +
    EUROPOP2023; FR without EUROPOP from tests/fixtures/social) and labour/pensions with their
    dataset-input hooks, plus (``--figaro synthetic``, default) the SYNTHETIC 3x2 FIGARO-format
    table of tests/integration/test_io_price_pipeline.py through the real connector so
    economic.io_price runs end to end (its results are test_fixture data, not FIGARO figures;
    ``--figaro recorded`` ingests the real one-group FIGARO capture instead). After publication it
    enqueues one ``backtest`` job per back-testable model (``--no-backtests`` to skip).
``--fixture-plan`` / ``--no-fixture-plan``
    (default: on with ``--v2-fixtures``) the fixtures of worldbank.wdi.all_economies,
    un.comtrade.public_batch, eurostat.demography and eurostat.figaro.iciot_ii were captured for
    restricted plans, so their default plans cannot be replayed. Fixture-plan mode
    (``reva.adapters.connectors.fixture_plans``) runs each with its captured configuration and
    only the captured requests; dropped items are reported in ``stack.json``. Live behaviour is
    unchanged. With ``--no-fixture-plan`` those connectors are skipped (reason recorded).

Option for the V3 suite (``ui/e2e/v3_live_stack.spec.ts``):

``--v3-fixtures``
    after the release is published (so the release is the same as without it), loads on the
    disposable database: the real-Wikidata links fixture
    (``tests/unit/links/fixtures/wikidata_links_fixture.json``) into ``kg.*`` through
    ``reva.kg.model.load_batch`` (origin ``test_fixture``, source key ``wikidata.links_fixture``,
    the allow-listed properties registered first); then the atlas sources of ``V3_ATLAS_SOURCES``
    (``--v3-atlas-source`` repeatable to choose others) through ``reva.atlas.ingest.run_atlas_ingest``
    in fixture mode (recorded responses, never the network, never charged); then a ``test_fixture``
    atlas snapshot (``reva.atlas.snapshot.take_snapshot``). A source whose fixture run fails is
    recorded with its error in ``stack.json`` (``v3.atlas``), never hidden; the stack still starts.
    ``POST /v1/ask`` stays on the deterministic keyword planner (no LLM planner is wired).

The release cutoff is taken after every ingest has finished, so the release pins all ingested
datasets.

Usage::

    uv run python scripts/e2e_stack.py                 # serve until Ctrl-C
    uv run python scripts/e2e_stack.py --run           # serve, run `npm --prefix ui run e2e`, tear down
    uv run python scripts/e2e_stack.py --run -- --grep provenance   # extra Playwright args
    uv run python scripts/e2e_stack.py --recorded-drafter --v2-fixtures --run   # V2 suites
    uv run python scripts/e2e_stack.py --v3-fixtures --run -- e2e/v3_live_stack.spec.ts   # V3 suite

The default stack file is ``var/e2e/stack.json`` (gitignored); the specs in
ui/e2e read it (or ``REVA_E2E_STACK_FILE``) when ``REVA_E2E_BASE_URL`` is unset.
The live development database ``reva`` is never read or written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

log = logging.getLogger("reva.e2e_stack")

FIXTURE_CONNECTORS = (
    "worldbank.wdi.indicators",
    "un.comtrade.public_exact",
    "naturalearth.admin0_110m",
    "crossref.works.doi",
)
#: V2 recorded fixtures replayed with --v2-fixtures (fixture-plan mode where the capture was restricted).
V2_FIXTURE_PLAN_CONNECTORS = ("worldbank.wdi.all_economies", "un.comtrade.public_batch", "eurostat.demography")
SOCIAL_FIXTURES = ROOT / "tests" / "fixtures" / "social"
NL_RECORDED_FIXTURES = ROOT / "tests" / "fixtures" / "nl" / "recorded"
DEFAULT_STACK_FILE = ROOT / "var" / "e2e" / "stack.json"
#: atlas sources replayed in fixture mode with --v3-fixtures (order matters little; each is independent)
V3_ATLAS_SOURCES = (
    "atlas.imf",
    "atlas.ilostat",
    "atlas.eurostat.earn_mw_cur",
    "atlas.oecd.earnings",
    "atlas.who_gho",
    "atlas.unesco_uis",
    "atlas.undp_hdr",
    "atlas.whr",
    "atlas.owid",
    # 1:10m geography from a derived excerpt of the real layers (closed groups: Ireland+UK, Hispaniola,
    # Cyprus, Saint-Martin; tests/fixtures/source/atlas.naturalearth_10m/cut_excerpt.py), rows flagged ne_excerpt
    "atlas.naturalearth_10m",
    "atlas.geonames_countryinfo",
    "atlas.worldbank",
    "atlas.fao",
)
#: source key of the links fixture in kg.*: the live harvest's key, so the KG treats these real Wikidata
#: statements exactly as harvested ones (community-edited label); only the origin (test_fixture) differs.
#: (tests/integration/test_links_db.py loads the same file under "wikidata.links_fixture", which
#: reva.kg.query does not count as community-edited.)
V3_KG_SOURCE_KEY = "wikidata"
ADMIN_USERNAME = "admin"


def with_connection_retry(what: str, fn: Any, *, deadline: float = 300.0) -> Any:
    """Retry ``fn`` while PostgreSQL refuses logins for a busy role.

    Runtime roles carry a cluster-wide connection limit shared with every other local
    test session; a refused login happens before any transaction, so retrying is safe.
    """
    from sqlalchemy.exc import OperationalError

    t0 = time.monotonic()
    delay = 1.0
    while True:
        try:
            return fn()
        except OperationalError as e:
            if "too many connections" not in str(e) or time.monotonic() - t0 > deadline:
                raise
            log.warning("%s: database role connection limit reached; retrying in %.0fs", what, delay)
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Stack:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.db_name = f"reva_e2e_{uuid.uuid4().hex[:10]}"
        self.tmp = Path(tempfile.mkdtemp(prefix="reva-e2e-"))
        self.object_root = self.tmp / "objects"
        self.ui_dir = self.tmp / "ui"
        self.admin_engine: Any = None
        self.db_created = False
        self.engines: list[Any] = []
        self.worker: Any = None
        self.worker_thread: threading.Thread | None = None
        self.server: Any = None
        self.stack_file: Path = Path(args.stack_file).resolve()
        self.storage_state = self.stack_file.with_name("storage-state.json")
        self._torn_down = False

    # ------------------------------------------------------------------ database
    def create_database(self) -> Any:
        from pydantic import SecretStr
        from sqlalchemy import create_engine, text

        from reva.settings import Settings

        base = Settings(_env_file=ROOT / ".env")
        if base.reva_migration_database_url is None:
            raise SystemExit("REVA_MIGRATION_DATABASE_URL is not configured (.env); cannot create the e2e database")
        self.admin_engine = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
        with self.admin_engine.connect() as c:
            c.execute(text(f'CREATE DATABASE "{self.db_name}"'))
        self.db_created = True
        url = base.migration_url().set(database=self.db_name).render_as_string(hide_password=False)
        return Settings.model_validate(
            {
                **base.model_dump(),
                "reva_migration_database_url": SecretStr(url),
                "reva_db_name": self.db_name,
                "reva_env": "test",
                "reva_object_backend": "filesystem",
                "reva_object_root": self.object_root,
            }
        )

    def prepare_data(self, settings: Any) -> dict[str, Any]:
        from reva.adapters.connectors.registry import builtin_connectors
        from reva.adapters.objects.filesystem import FilesystemArtifactStore
        from reva.adapters.postgres.engine import make_engine
        from reva.belief.publication import publish_release
        from reva.cli.bootstrap import bootstrap
        from reva.cli.seed import seed
        from reva.evidence.runner import FixtureHttp, post_publish_for, run_connector
        from reva.kernel.context import Origin

        boot: dict[str, Any] = {}
        for attempt in range(3):  # concurrent bootstraps may race on cluster-global role DDL
            try:
                boot = bootstrap(settings)
                break
            except Exception as e:  # logged, never swallowed silently; the last attempt raises it
                if attempt == 2:
                    raise
                log.warning("bootstrap attempt %d failed (%s: %s); retrying", attempt + 1, type(e).__name__, e)
                time.sleep(1 + attempt)
        owner_seed = (boot or {}).get("seed") or {}
        log.info(
            "registry seeded by the owner: %s (fingerprint %s)", owner_seed.get("status"), owner_seed.get("fingerprint")
        )
        store = FilesystemArtifactStore(self.object_root, settings.reva_s3_prefix)
        worker = make_engine("worker", settings)
        try:
            # the runtime role only confirms the owner's seed; same process, same registry snapshot
            # (reva.cli.seed.registry_snapshot) and the same contact email as bootstrap's seed
            confirmed = with_connection_retry(
                "seed", lambda: seed(worker, store, contact_email=settings.reva_http_contact_email)
            )
            if owner_seed.get("fingerprint") and confirmed.get("fingerprint") != owner_seed["fingerprint"]:
                raise RuntimeError(
                    f"registry fingerprint changed between the owner's seed ({owner_seed['fingerprint']}) and the "
                    f"worker's confirmation ({confirmed.get('fingerprint')})"
                )
            connectors = builtin_connectors()
            ingest: dict[str, Any] = {}
            for name in FIXTURE_CONNECTORS:
                c = connectors[name]
                spec = c.describe()
                rep = with_connection_retry(
                    f"ingest {name}",
                    lambda c=c, spec=spec: asyncio.run(
                        run_connector(
                            c,
                            FixtureHttp(spec.name),
                            store,
                            worker,
                            persist_cursor=False,
                            post_publish=post_publish_for(spec.template),
                        )
                    ),
                )
                ingest[name] = {
                    "inserted": rep.inserted,
                    "unchanged": rep.unchanged,
                    "errors": rep.errors,
                    "outcomes": {
                        st: sum(1 for o in rep.outcomes if o.status == st)
                        for st in sorted({o.status for o in rep.outcomes})
                    },
                }
                if rep.errors:
                    log.warning("fixture ingest %s reported errors: %s", name, rep.errors)
                log.info("ingested %s: %d inserted, %d unchanged", name, rep.inserted, rep.unchanged)
            v2: dict[str, Any] = {}
            if self.args.v2_fixtures:
                v2 = self.ingest_v2(settings, store, worker)
            # The cutoff is taken only now, after every ingest (base and V2) has committed, so the
            # release sees and pins everything ingested above.
            cutoff = datetime.now(UTC)
            release = with_connection_retry(
                "publish release",
                lambda: publish_release(worker, store, cutoff=cutoff, origin=Origin.test_fixture, environment="test"),
            )
            if self.args.v2_fixtures and self.args.backtests:
                v2["backtest_jobs"] = self.enqueue_backtests(worker, release.release_id)
            v3: dict[str, Any] = {}
            if self.args.v3_fixtures:
                v3 = self.load_v3(settings, store, worker)
        finally:
            worker.dispose()
        llm: dict[str, Any] = {}
        if self.args.recorded_drafter:
            llm = {"llm_drafter": "recorded", "llm_ledger": self.ensure_llm_ledger(settings)}
        rel = {
            "id": str(release.release_id),
            "context_id": str(release.context_id),
            "sequence": release.sequence,
            "estimates": release.estimates,
            "manifest_sha256": release.manifest_sha256,
            "knowledge_cutoff": release.knowledge_cutoff.isoformat(),
            "origin": "test_fixture",
        }
        log.info("published test_fixture release: %s", rel)
        return {
            "ingest": ingest,
            "release": rel,
            **({"v2": v2} if self.args.v2_fixtures else {}),
            **({"v3": v3} if self.args.v3_fixtures else {}),
            **llm,
        }

    # ------------------------------------------------------------------ V3 fixtures
    def load_v3(self, settings: Any, store: Any, worker: Any) -> dict[str, Any]:
        """KG + links fixture, atlas sources in fixture mode, a test_fixture atlas snapshot."""
        out: dict[str, Any] = {"kg": with_connection_retry("kg fixture", lambda: self.load_kg_fixture(store, worker))}
        atlas: dict[str, Any] = {}
        for name in self.args.v3_atlas_sources or V3_ATLAS_SOURCES:
            atlas[name] = self.ingest_atlas_fixture(settings, store, worker, name)
        out["atlas"] = atlas
        from reva.atlas.snapshot import take_snapshot

        out["atlas_snapshot"] = with_connection_retry(
            "atlas snapshot", lambda: take_snapshot(settings, worker, store, origin="test_fixture")
        )
        log.info("V3 atlas snapshot: %s", out["atlas_snapshot"])
        return out

    @staticmethod
    def load_kg_fixture(store: Any, worker: Any) -> dict[str, Any]:
        """The real-Wikidata links fixture into kg.* through load_batch (origin test_fixture)."""
        from reva.adapters.connectors.wikidata import load_allowlist
        from reva.kg.harvest import kg_properties
        from reva.kg.model import KgEntity, KgStatement, KgTime, load_batch, register_properties
        from reva.links.fixture import FIXTURE_FILE, load_fixture

        register_properties(worker, kg_properties(load_allowlist()))
        body = FIXTURE_FILE.read_bytes()
        raw = store.put_bytes(body, "raw", "application/json").sha256
        fx = load_fixture()

        def t(d: Any) -> Any:
            return None if d is None else KgTime(d["time"], d["precision"], d.get("calendar", "gregorian"))

        sts = [
            KgStatement(
                subject_qid=r["subject_qid"],
                property_pid=r["property_pid"],
                object_qid=r["object_qid"],
                value=r["value"],
                start=t(r["start"]),
                end=t(r["end"]),
                point_in_time=t(r["point_in_time"]),
                qualifiers=r["qualifiers"],
                rank=r["rank"],
                references=r["references"],
                source_key=V3_KG_SOURCE_KEY,
                source_revision=r["source_revision"],
                statement_id=r["statement_id"],
                raw_sha256=raw,
            )
            for r in fx.raw_statements
        ]
        ents = [
            KgEntity(q, "fixture_entity", e["labels"], e.get("descriptions") or {}, source_revision=str(e["lastrevid"]))
            for q, e in fx.entities.items()
        ]
        rep = load_batch(
            worker,
            store,
            entities=ents,
            statements=sts,
            source_key=V3_KG_SOURCE_KEY,
            raw_sha256s=[raw],
            retrieved_at=datetime.now(UTC),
            origin="test_fixture",
        )
        if rep.rejected_total:
            log.warning("KG fixture load rejected %d rows: %s", rep.rejected_total, rep.rejected[:5])
        log.info("loaded KG fixture: %d entities, %d statements", len(ents), len(sts))
        return {
            "source_key": V3_KG_SOURCE_KEY,
            "entities": len(ents),
            "statements": len(sts),
            "rejected": rep.rejected_total,
            "raw_sha256": raw,
        }

    @staticmethod
    def ingest_atlas_fixture(settings: Any, store: Any, worker: Any, name: str) -> dict[str, Any]:
        """One atlas source in fixture mode; failures are recorded per source, never fatal."""
        from reva.atlas.ingest import run_atlas_ingest

        t0 = time.monotonic()
        try:
            res = with_connection_retry(
                f"atlas {name}",
                lambda: asyncio.run(run_atlas_ingest(settings, worker, store, name, mode="fixture")),
            )
        except Exception as e:
            log.warning("V3 atlas fixture %s failed: %s", name, e)
            return {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:500]}"}
        tables = res.get("registered") or []
        rows = sum(int(t.get("row_count", 0) or 0) for t in tables if isinstance(t, dict))
        log.info(
            "V3 atlas fixture %s: %s, %d tables, %d rows (%.1fs)",
            name,
            res["status"],
            len(tables),
            rows,
            time.monotonic() - t0,
        )
        return {"status": res["status"], "error": res.get("error"), "tables": len(tables), "rows": rows}

    # ------------------------------------------------------------------ V2 fixtures
    @staticmethod
    def _summary(rep: Any, conn: Any = None) -> dict[str, Any]:
        out: dict[str, Any] = {"inserted": rep.inserted, "unchanged": rep.unchanged, "errors": rep.errors}
        if conn is not None and getattr(conn, "not_captured", None):
            out["not_captured"] = len(conn.not_captured)
        return out

    def ingest_v2(self, settings: Any, store: Any, worker: Any) -> dict[str, Any]:
        """Offline V2 inputs (see module docstring). Errors are recorded per connector, never hidden."""
        from reva.adapters.connectors import eurostat_demography as ed
        from reva.adapters.connectors import fixture_plans as fp
        from reva.evidence.runner import FixtureHttp, post_publish_for, run_connector

        out: dict[str, Any] = {"fixture_plan": bool(self.args.fixture_plan)}

        def ingest(name: str, conn: Any, http: Any) -> dict[str, Any]:
            hook = getattr(conn, "post_publish", None) or post_publish_for(conn.describe().template)
            rep = with_connection_retry(
                f"ingest {name}",
                lambda: asyncio.run(run_connector(conn, http, store, worker, persist_cursor=False, post_publish=hook)),
            )
            if rep.errors:
                log.warning("V2 fixture ingest %s reported errors: %s", name, rep.errors[:5])
            log.info("ingested V2 fixture %s: %d inserted", name, rep.inserted)
            return self._summary(rep, conn)

        restricted = [
            *V2_FIXTURE_PLAN_CONNECTORS,
            *(("eurostat.figaro.iciot_ii",) if self.args.figaro == "recorded" else ()),
        ]
        for name in restricted:
            if not self.args.fixture_plan:
                out[name] = {"skipped": "fixtures captured for a restricted plan; run with --fixture-plan"}
                continue
            out[name] = ingest(name, fp.fixture_plan_connector(name), FixtureHttp(name))
        # France demography (no EUROPOP), recorded for the retirement-age pipeline (tests/fixtures/social)
        fr_conn = ed.EurostatDemographyConnector(countries=("FR",), include_europop=False)
        fr = fp.fixture_plan_connector(ed.NAME, fr_conn, root=SOCIAL_FIXTURES)
        out["eurostat.demography:FR"] = ingest(ed.NAME, fr, FixtureHttp(ed.NAME, root=SOCIAL_FIXTURES))
        # labour/pensions: default plan captured as is; its connector hook registers the dataset inputs
        out["eurostat.labour_pensions"] = ingest(
            "eurostat.labour_pensions",
            fp.fixture_plan_connector("eurostat.labour_pensions"),
            FixtureHttp("eurostat.labour_pensions"),
        )
        if self.args.figaro == "synthetic":
            out["io_dataset"] = self.ingest_synthetic_io(settings, store, worker)
        return out

    def ingest_synthetic_io(self, settings: Any, store: Any, worker: Any) -> dict[str, Any]:
        """SYNTHETIC 3-economy x 2-industry FIGARO-format table (tests/fixtures/io_tables/synthetic.py)
        through the real connector (FixtureHttp), identity checks and dataset registration. The recorded
        FIGARO fixture holds one data group only and cannot form a complete table."""
        import importlib.util

        from reva.adapters.connectors import io_tables as it
        from reva.adapters.postgres import repositories as repo
        from reva.adapters.postgres.engine import make_engine
        from reva.cli.seed import register_connector_version
        from reva.evidence.runner import FixtureHttp, _fixture_fingerprint, run_connector
        from reva.kernel.ids import sha256_bytes

        spec = importlib.util.spec_from_file_location(
            "io_synthetic", ROOT / "tests" / "fixtures" / "io_tables" / "synthetic.py"
        )
        assert spec and spec.loader
        syn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(syn)
        dims = it.TableDims(countries=syn.COUNTRIES, industries=syn.INDUSTRIES)
        root = self.tmp / "io-fixtures"
        d = root / it.NAME
        d.mkdir(parents=True, exist_ok=True)
        conn = it.FigaroConnector(dims=dims)
        bodies = {it.constraint_url("NAIO_10_FCP_II4"): syn.constraint_xml()}
        for item in conn._data_items(syn.YEAR, "NAIO_10_FCP_II4"):
            bodies[item.url] = syn.body(tuple(item.params["origins"]), year=syn.YEAR)
        entries = []
        for url, body in bodies.items():
            fpr = _fixture_fingerprint(url)
            (d / f"{fpr[:16]}.bin").write_bytes(body)
            entries.append(
                {
                    "request_fingerprint": fpr,
                    "url": url,
                    "file": f"{fpr[:16]}.bin",
                    "sha256": sha256_bytes(body),
                    "status": 200,
                    "headers": {"content-type": "application/vnd.sdmx.data+csv"},
                    "retrieved_at": "2026-10-15T00:00:00+00:00",
                    "licence": it.LICENCE,
                }
            )
        (d / "manifest.json").write_text(json.dumps(entries))
        owner = make_engine("migration", settings)
        try:
            with owner.begin() as c:
                act = repo.simple_activity("registry.seed", "review", "maintainer:registry-seed", {"e2e": it.NAME})
                repo.record_activity(c, act)
                register_connector_version(c, store, it.FigaroConnector(dims=dims).describe(), act.id)
        finally:
            owner.dispose()
        rep = asyncio.run(
            run_connector(
                it.FigaroConnector(syn.YEAR, dims=dims),
                FixtureHttp(it.NAME, root=root),
                store,
                worker,
                persist_cursor=False,
            )
        )
        if rep.errors:
            log.warning("synthetic IO ingest reported errors: %s", rep.errors[:5])
            return {"errors": rep.errors}
        out = it.publish_io_dataset(worker, store, year=syn.YEAR, origin="test_fixture", dims=dims)
        return {k: out[k] for k in ("dataset_id", "dataset_key", "artifact_sha256", "row_count") if k in out}

    def enqueue_backtests(self, worker: Any, release_id: Any) -> dict[str, str]:
        """One ``backtest`` job per back-testable model; the stack's worker runs them."""
        from reva.evaluation.backtests import BACKTESTS, enqueue_backtest

        jobs = {}
        for key in sorted(BACKTESTS):
            jobs[key] = str(enqueue_backtest(worker, key, release_id=release_id, created_by="worker:e2e-stack"))
        return jobs

    def ensure_llm_ledger(self, settings: Any) -> str:
        """The LLM budget ledger functions ship with migration 0008; install them only in a database
        whose migrations predate them (as tests/integration/test_nl_drafts_api.py does)."""
        from sqlalchemy import text

        from reva.adapters.postgres.engine import make_engine
        from reva.nl.budget import LEDGER_FUNCTIONS_SQL

        owner = make_engine("migration", settings)
        try:
            with owner.begin() as c:
                present = c.execute(
                    text(
                        "SELECT to_regprocedure('ops.reserve_llm_budget(text,integer,bigint,integer,"
                        "bigint)') IS NOT NULL"
                    )
                ).scalar_one()
                if not present:
                    c.execute(text(LEDGER_FUNCTIONS_SQL))
        finally:
            owner.dispose()
        return "migration" if present else "installed_for_e2e"

    # ------------------------------------------------------------------ UI
    def build_ui(self) -> Path:
        if self.args.static_dir:
            path = Path(self.args.static_dir).resolve()
            if not (path / "index.html").is_file():
                raise SystemExit(f"--static-dir {path} has no index.html")
            return path
        cmd = ["npm", "--prefix", str(ROOT / "ui"), "run", "build", "--", "--outDir", str(self.ui_dir), "--emptyOutDir"]
        log.info("building UI: %s", " ".join(cmd))
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
            raise SystemExit(f"UI build failed (exit {proc.returncode})")
        return self.ui_dir

    # ------------------------------------------------------------------ processes
    def start_worker(self, settings: Any) -> None:
        from reva.adapters.objects.filesystem import FilesystemArtifactStore
        from reva.adapters.postgres.engine import make_engine
        from reva.operations.worker import Worker, wait_for_schema

        ws = settings.model_copy(
            update={"reva_migration_database_url": None, "reva_db_pool_size": 4, "reva_db_max_overflow": 0}
        )
        engine = make_engine("worker", ws)
        sim_engine = make_engine("sim", ws)
        self.engines += [engine, sim_engine]
        with_connection_retry("worker schema check", lambda: wait_for_schema(engine, attempts=5))
        store = FilesystemArtifactStore(self.object_root, ws.reva_s3_prefix)
        # Scheduler off (it would enqueue live acquisitions); invalidation off (it would publish
        # further releases mid-suite). Queue claiming, leases, heartbeats and handlers are real.
        self.worker = Worker(
            ws,
            engine,
            store,
            sim_engine,
            scheduler=False,
            invalidation=False,
            poll_interval=0.2,
            lease_seconds=60,
            heartbeat_seconds=5,
            drain_seconds=5,
        )
        self.worker_thread = threading.Thread(target=self.worker.run, name="reva-e2e-worker", daemon=True)
        self.worker_thread.start()

    def make_server(self, settings: Any, static_dir: Path, password: str, port: int) -> Any:
        import uvicorn
        from pydantic import SecretStr

        from reva.api.app import create_app
        from reva.api.auth import hash_password

        base_url = f"http://127.0.0.1:{port}"
        api_settings = settings.model_copy(
            update={
                "reva_migration_database_url": None,
                "reva_admin_password_hash": SecretStr(hash_password(password)),
                "reva_session_secret": SecretStr(secrets.token_urlsafe(48)),
                "reva_allowed_hosts": "127.0.0.1,localhost",
                "reva_public_base_url": base_url,
                "reva_cors_origins": "",
                "reva_db_pool_size": 3,
            }
        )
        app = create_app(api_settings, static_dir=static_dir, login_rate_limit_per_minute=self.args.login_rate_limit)
        if self.args.v3_fixtures:
            # /v1/ask stays on the deterministic keyword planner, whatever .env says about LLMs.
            app.state.nlqa_planner_factory = lambda _settings: []
        if self.args.recorded_drafter:
            from reva.nl.recorded import RecordedDrafter

            drafter = RecordedDrafter(NL_RECORDED_FIXTURES)
            app.state.nl_drafter_factory = lambda _settings: [drafter]
            app.state.nl_drafter_mode = "recorded"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="on")
        self.server = uvicorn.Server(config)
        return base_url

    # ------------------------------------------------------------------ session
    def wait_ready(self, base_url: str, deadline: float = 60.0) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline:
            try:
                with urllib.request.urlopen(f"{base_url}/health/ready", timeout=5) as r:  # noqa: S310
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.3)
        raise RuntimeError(f"API not ready at {base_url} after {deadline}s")

    def write_storage_state(self, base_url: str, password: str) -> None:
        """One real login; the cookies become the Playwright storageState shared by all specs."""
        req = urllib.request.Request(  # noqa: S310
            f"{base_url}/v1/session",
            method="POST",
            data=json.dumps({"username": ADMIN_USERNAME, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            set_cookies = r.headers.get_all("Set-Cookie") or []
        cookies = []
        for header in set_cookies:
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            for name, morsel in jar.items():
                same_site = (morsel["samesite"] or "Lax").capitalize()
                max_age = morsel["max-age"]
                cookies.append(
                    {
                        "name": name,
                        "value": morsel.value,
                        "domain": "127.0.0.1",
                        "path": morsel["path"] or "/",
                        "expires": time.time() + int(max_age) if max_age else -1,
                        "httpOnly": bool(morsel["httponly"]),
                        "secure": bool(morsel["secure"]),
                        "sameSite": same_site if same_site in ("Strict", "Lax", "None") else "Lax",
                    }
                )
        names = {c["name"] for c in cookies}
        if "reva_session" not in names:
            raise RuntimeError(f"login did not set a session cookie (got {sorted(names)})")
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        self.storage_state.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=2))
        os.chmod(self.storage_state, 0o600)

    # ------------------------------------------------------------------ teardown
    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        if self.worker is not None:
            self.worker.request_stop("e2e teardown")
            if self.worker_thread is not None:
                self.worker_thread.join(timeout=15)
        for e in self.engines:
            try:
                e.dispose()
            except Exception as exc:
                log.debug("engine dispose failed: %s", exc)
        if self.admin_engine is not None:
            if self.db_created and not self.args.keep_db:
                from sqlalchemy import text

                try:
                    with self.admin_engine.connect() as c:
                        c.execute(text(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)'))
                    log.info("dropped database %s", self.db_name)
                except Exception as e:
                    log.error("could not drop database %s: %s", self.db_name, e)
            self.admin_engine.dispose()
        for p in (self.stack_file, self.storage_state):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        if not self.args.keep_db:
            shutil.rmtree(self.tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack-file", default=os.environ.get("REVA_E2E_STACK_FILE", str(DEFAULT_STACK_FILE)))
    ap.add_argument("--port", type=int, default=0, help="API port (default: a free port)")
    ap.add_argument("--static-dir", help="use an existing UI build instead of building")
    ap.add_argument(
        "--login-rate-limit",
        type=int,
        default=10,
        help="login attempts per client per minute (always enforced; default 10)",
    )
    ap.add_argument("--keep-db", action="store_true", help="keep the database and temp files for debugging")
    ap.add_argument("--run", action="store_true", help="run the Playwright suite against the stack, then exit")
    ap.add_argument(
        "--recorded-drafter",
        action="store_true",
        help="replay recorded LLM responses for POST /v1/scenario-drafts (never a live LLM)",
    )
    ap.add_argument(
        "--v2-fixtures",
        action="store_true",
        help="also ingest the V2 recorded fixtures (dataset inputs) before the release cutoff",
    )
    ap.add_argument(
        "--fixture-plan",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="replay restricted-plan fixtures with their captured plans (default: on with --v2-fixtures)",
    )
    ap.add_argument(
        "--figaro",
        choices=("synthetic", "recorded", "none"),
        default="synthetic",
        help="IO table for --v2-fixtures: synthetic 3x2 table (default), the recorded one-group capture, or none",
    )
    ap.add_argument(
        "--no-backtests",
        dest="backtests",
        action="store_false",
        help="with --v2-fixtures, do not enqueue back-test jobs after publication",
    )
    ap.add_argument(
        "--v3-fixtures",
        action="store_true",
        help="also load the V3 KG/links fixture and atlas sources (fixture mode) and take a test_fixture snapshot",
    )
    ap.add_argument(
        "--v3-atlas-source",
        dest="v3_atlas_sources",
        action="append",
        default=None,
        metavar="NAME",
        help="with --v3-fixtures: atlas source to replay (repeatable; default: V3_ATLAS_SOURCES)",
    )
    ap.add_argument("playwright_args", nargs="*", help="extra Playwright arguments (after --)")
    args = ap.parse_args(argv)
    if args.fixture_plan is None:
        args.fixture_plan = args.v2_fixtures
    logging.basicConfig(
        level=os.environ.get("REVA_E2E_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("reva.api.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    stack = Stack(args)

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("received %s; shutting down", signal.Signals(signum).name)
        if stack.server is not None:
            stack.server.should_exit = True
        else:
            stack.teardown()
            raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    exit_code = 0
    try:
        t0 = time.monotonic()
        settings = stack.create_database()
        log.info("created database %s", stack.db_name)
        data = stack.prepare_data(settings)
        static_dir = stack.build_ui()
        stack.start_worker(settings)
        password = secrets.token_urlsafe(18)
        port = args.port or free_port()
        base_url = stack.make_server(settings, static_dir, password, port)

        server_thread = threading.Thread(target=stack.server.run, name="reva-e2e-api", daemon=True)
        # uvicorn installs signal handlers only on the main thread; ours above stay in effect.
        server_thread.start()
        stack.wait_ready(base_url)
        stack.write_storage_state(base_url, password)
        info = {
            "base_url": base_url,
            "username": ADMIN_USERNAME,
            "password": password,
            "storage_state": str(stack.storage_state),
            "database": stack.db_name,
            "release": data["release"],
            "ingest": data["ingest"],
            **{k: data[k] for k in ("v2", "v3", "llm_drafter", "llm_ledger") if k in data},
            "started_at": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
            "login_rate_limit_per_minute": args.login_rate_limit,
        }
        stack.stack_file.parent.mkdir(parents=True, exist_ok=True)
        stack.stack_file.write_text(json.dumps(info, indent=2, default=str))
        os.chmod(stack.stack_file, 0o600)
        log.info("stack ready in %.1fs: %s (stack file %s)", time.monotonic() - t0, base_url, stack.stack_file)
        print(json.dumps({"base_url": base_url, "stack_file": str(stack.stack_file)}), flush=True)

        if args.run:
            env = {
                **os.environ,
                "REVA_E2E_BASE_URL": base_url,
                "REVA_E2E_USERNAME": ADMIN_USERNAME,
                "REVA_E2E_PASSWORD": password,
                "REVA_E2E_STACK_FILE": str(stack.stack_file),
                "REVA_E2E_STORAGE_STATE": str(stack.storage_state),
            }
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
            env.pop("REVA_MIGRATION_DATABASE_URL", None)
            cmd = ["npm", "--prefix", str(ROOT / "ui"), "run", "e2e", "--", *args.playwright_args]
            log.info("running: %s", " ".join(cmd))
            proc = subprocess.Popen(cmd, cwd=ROOT / "ui", env=env)
            while proc.poll() is None and not stack.server.should_exit:
                time.sleep(0.5)
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=30)
            exit_code = proc.returncode or 0
        else:
            while server_thread.is_alive() and not stack.server.should_exit:
                time.sleep(0.5)
        stack.server.should_exit = True
        server_thread.join(timeout=15)
        if stack.worker_thread is not None and not stack.worker_thread.is_alive():
            log.error("worker thread exited unexpectedly")
    except SystemExit as e:
        exit_code = int(e.code) if isinstance(e.code, int) else 1
        if e.code not in (0, None) and not isinstance(e.code, int):
            log.error("%s", e.code)
    except Exception:
        log.exception("e2e stack failed")
        exit_code = 1
    finally:
        stack.teardown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
