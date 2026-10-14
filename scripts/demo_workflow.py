#!/usr/bin/env python3
"""REVA World §17.3 demonstration workflow, end to end, against a FRESH database.

Runs the twelve demonstration steps of architecture §17.3 plus the separate
negative demonstration (all four countries -> explicit Algerian data blocker)
through the real code paths: connectors + ``publish_acquisition`` for ingestion,
``publish_release`` for beliefs, the world-graph library, ``sim.create_scenario``
/ ``ops.submit_job`` (reva_api role) and the durable ``Worker`` (claim -> fenced
handler -> completion) for simulation, discovery, population and evaluation jobs,
the replay library for the two-vintage replay, and the in-process HTTP API
(``reva.api.app.create_app`` behind a FastAPI TestClient, reva_api role, admin
session) for the user-facing displays (relations, simulation status,
provenance, gaps, proposals, beliefs, entity profile).

Modes
-----
``--fixtures`` (default)
    Offline. Captured provider responses under ``tests/fixtures/source`` are
    replayed through ``FixtureHttp``; everything carries origin ``test_fixture``.
``--live``
    Real provider requests through ``SafeHttpClient`` (origin ``live``). Nothing
    is written to ``tests/fixtures``. Step 8 still replays the *recorded*
    synthetic vintage sequence (origin ``test_fixture``, labelled as such) because
    providers do not serve historical vintages on demand.

Honesty rules
-------------
Every step records ``pass``/``fail``/``blocked`` with evidence (ids, hashes,
values). A step that cannot run (missing precondition, autonomy kill switch,
network failure in live mode, a failed upstream step) is ``blocked`` with the
reason; it is never reported as a pass. A step passes only when every one of
its checks passed.

Usage::

    uv run python scripts/demo_workflow.py                 # fixtures, writes var/acceptance/demo_fixtures.json
    uv run python scripts/demo_workflow.py --live          # live providers, writes var/acceptance/demo_live.json
    uv run python scripts/demo_workflow.py --keep          # keep the database and object store for inspection

Exit status: 0 when no step failed (blocked steps allowed), 1 when any step
failed, 2 when the environment could not be set up at all.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import Engine, create_engine, text  # noqa: E402

SCHEMA = "reva-demo-workflow/1"
DEFAULT_OUT_DIR = ROOT / "var" / "acceptance"
VINTAGES = ROOT / "tests" / "fixtures" / "vintages"
EXAMPLE_SCENARIO = ROOT / "contracts" / "scenario.example.json"

CONNECTOR_ORDER = ("worldbank.wdi.indicators", "un.comtrade.public_exact", "naturalearth.admin0_110m",
                   "crossref.works.doi")
TRADE_COUNTRIES = ("FRA", "CHN", "USA")
ALL_FOUR = ("FRA", "DZA", "CHN", "USA")
LABELS = {"FRA": "France", "CHN": "China", "USA": "United States", "DZA": "Algeria"}
M_CONCEPT = "trade.hs2709.import_value.world_total"
X_CONCEPT = "trade.hs2709.export_value.world_total"
G_CONCEPT = "economics.gdp.nominal_current_usd"
POP = "demography.population.wdi_midyear_total"
JAN1 = "demography.population.jan1_age_sex"
TRADE_LAYER = "trade.crude_petroleum"
OUTPUTS = ("import_value_change_usd", "export_value_change_usd", "net_external_cost_change_usd",
           "exposure_percent_baseline_gdp")
EUROSTAT_CONNECTOR = "eurostat.demo_pjan.fr"
ACTOR = "human:acceptance-demo"

STEP_TITLES: dict[str, tuple[str, str]] = {
    "1": ("fetch_and_archive", "Fetch and archive real source responses"),
    "2": ("source_flags_units", "Show source flags, units, classifications and release uncertainty"),
    "3": ("publish_beliefs", "Publish source-selected baseline beliefs"),
    "4": ("trade_hyperedges", "Explore trade hyperedges (product, reporter, counterpart, interval)"),
    "5": ("scenario_l1", "Apply a +25% border-price scenario (L1, FRA/CHN/USA) through the job path"),
    "6": ("l2_sensitivity", "Deterministic exposure plus labelled elasticity sensitivity range (L2)"),
    "7": ("provenance_drill", "Drill from a result to exact response fields and raw artifacts"),
    "8": ("two_vintage_replay", "Replay a two-vintage recorded sequence; later revisions excluded"),
    "9": ("eurostat_discovery", "Discover and validate the Eurostat connector; reject false equivalence"),
    "10": ("demographic_recipe", "Materialise/calibrate/coarsen/refine the public demographic recipe"),
    "11": ("forecast_tournament", "Naive-versus-state-space tournament on a labelled retrospective panel"),
    "12": ("gaps_and_proposal", "Unresolved knowledge gaps and a reviewable connector change proposal"),
    "N": ("negative_all_four", "Negative demo: all four countries -> explicit Algerian data blocker"),
}


# --------------------------------------------------------------------------- recording
class Blocked(Exception):
    """Raised inside a step when it cannot run; the step is recorded as blocked."""


@dataclass
class Step:
    id: str
    key: str
    title: str
    status: str = "pending"
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    blocked_reason: str | None = None
    duration_s: float = 0.0
    interfaces: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: Any = None) -> bool:
        self.checks.append({"name": name, "status": "pass" if ok else "fail",
                            **({"detail": _plain(detail)} if detail is not None else {})})
        return ok

    def expect(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run ``fn``; record a failed check (not a crash of the step) when it raises."""
        try:
            return fn()
        except Exception as e:
            self.check(name, False, {"error": f"{type(e).__name__}: {str(e)[:600]}"})
            return None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "key": self.key, "title": self.title, "status": self.status,
                "interfaces": self.interfaces, "checks": self.checks, "evidence": _plain(self.evidence),
                "error": self.error, "blocked_reason": self.blocked_reason, "duration_s": round(self.duration_s, 3)}


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (uuid.UUID, Decimal)):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes sha256={hashlib.sha256(obj).hexdigest()}>"
    return obj


class Recorder:
    def __init__(self) -> None:
        self.steps: dict[str, Step] = {}

    @contextmanager
    def step(self, sid: str, *, needs: tuple[str, ...] = ()) -> Iterator[Step]:
        key, title = STEP_TITLES[sid]
        st = Step(sid, key, title)
        self.steps[sid] = st
        t0 = time.monotonic()
        print(f"[demo] step {sid}: {title} ...", flush=True)
        try:
            # a dependency is satisfied when it produced its outputs (``_ready``), even if a
            # display check of that step failed; otherwise this step is blocked, never faked
            bad = [d for d in needs if not (self.steps.get(d) and self.steps[d].evidence.get("_ready"))]
            if bad:
                raise Blocked(f"depends on step(s) {', '.join(bad)} which did not produce the required inputs")
            yield st
            if not st.checks:
                st.status = "fail"
                st.error = "step recorded no checks"
            elif all(c["status"] == "pass" for c in st.checks):
                st.status = "pass"
            else:
                st.status = "fail"
        except Blocked as b:
            st.status = "blocked"
            st.blocked_reason = str(b)
        except Exception as e:
            st.status = "fail"
            st.error = f"{type(e).__name__}: {str(e)[:1500]}"
            st.evidence["traceback_tail"] = traceback.format_exc().splitlines()[-12:]
        finally:
            st.duration_s = time.monotonic() - t0
            print(f"[demo] step {sid}: {st.status.upper()}"
                  + (f" ({st.blocked_reason or st.error})" if st.status != "pass" else "")
                  + f" [{st.duration_s:.1f}s]", flush=True)


# --------------------------------------------------------------------------- environment
@dataclass
class Env:
    mode: str
    origin: str
    settings: Any
    db_name: str
    admin_url: Any
    owner: Engine
    api: Engine
    worker: Engine
    sim: Engine
    store: Any
    object_root: Path
    client: Any = None
    client_error: str | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def now(self) -> datetime:
        with self.worker.connect() as c:
            return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def _base_settings(**over: Any) -> Any:
    from reva.settings import Settings

    s = Settings(_env_file=ROOT / ".env")  # type: ignore[call-arg]
    data = s.model_dump()
    data.update(over)
    return Settings.model_validate(data)


def create_environment(mode: str, object_root: Path | None) -> Env:
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    base = _base_settings()
    if base.reva_migration_database_url is None:
        raise RuntimeError("REVA_MIGRATION_DATABASE_URL is not configured (.env)")
    if base.reva_env == "production":
        raise RuntimeError("the demonstration refuses to run with REVA_ENV=production")
    admin_url = base.migration_url().set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("SELECT 1"))
    name = f"reva_demo_{mode[:4]}_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    root = object_root or Path(tempfile.mkdtemp(prefix=f"{name}_objects_"))
    root.mkdir(parents=True, exist_ok=True)
    url = base.migration_url().set(database=name).render_as_string(hide_password=False)
    env_name = "test" if mode == "fixtures" else base.reva_env
    settings = _base_settings(reva_migration_database_url=url, reva_db_name=name, reva_env=env_name,
                              reva_object_root=root)
    for attempt in range(3):  # concurrent sessions may race on cluster-global role DDL
        try:
            bootstrap(settings)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.0)
    store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
    env = Env(mode=mode, origin="test_fixture" if mode == "fixtures" else "live", settings=settings, db_name=name,
              admin_url=admin_url, owner=make_engine("migration", settings), api=make_engine("api", settings),
              worker=make_engine("worker", settings), sim=make_engine("sim", settings), store=store,
              object_root=root)
    env.state["seed"] = seed(env.worker, store, contact_email=settings.reva_http_contact_email)
    return env


def open_api_client(env: Env) -> None:
    """In-process HTTP API (reva_api role) with an admin session; failure is recorded, not fatal."""
    try:
        from fastapi.testclient import TestClient
        from pydantic import SecretStr

        from reva.api.app import create_app
        from reva.api.auth import hash_password

        password = secrets.token_urlsafe(24)
        data = env.settings.model_dump()
        data.update(reva_admin_password_hash=SecretStr(hash_password(password)),
                    reva_session_secret=SecretStr(secrets.token_urlsafe(48)),
                    reva_allowed_hosts="testserver,localhost", reva_cors_origins="")
        api_settings = type(env.settings).model_validate(data)
        app = create_app(api_settings)
        client = TestClient(app)
        client.__enter__()
        r = client.post("/v1/session", json={"username": "admin", "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"admin login failed: HTTP {r.status_code} {r.text[:300]}")
        client.headers["X-CSRF-Token"] = client.cookies.get("reva_csrf") or ""
        env.client = client
        env.state["api_app"] = app
    except Exception as e:
        env.client_error = f"{type(e).__name__}: {str(e)[:500]}"


def api_get(env: Env, st: Step, path: str, **params: Any) -> dict[str, Any] | None:
    """GET through the in-process API; records a failed check when the API is unavailable or errors."""
    if "api" not in st.interfaces:
        st.interfaces.append("api")
    if env.client is None:
        st.check(f"api GET {path}", False, {"error": f"API unavailable: {env.client_error}"})
        return None
    r = env.client.get(path, params=params or None)
    if r.status_code != 200:
        st.check(f"api GET {path}", False, {"status": r.status_code, "body": r.text[:600]})
        return None
    return r.json()


def close_environment(env: Env, keep: bool) -> dict[str, Any]:
    info: dict[str, Any] = {"name": env.db_name, "kept": keep, "object_root": str(env.object_root) if keep else None}
    with suppress(Exception):
        if env.client is not None:
            env.client.__exit__(None, None, None)
        app = env.state.get("api_app")
        if app is not None and getattr(app.state, "engine", None) is not None:
            app.state.engine.dispose()
    for e in (env.owner, env.api, env.worker, env.sim):
        e.dispose()
    if not keep:
        admin = create_engine(env.admin_url, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{env.db_name}" WITH (FORCE)'))
            info["dropped"] = True
        finally:
            admin.dispose()
        shutil.rmtree(env.object_root, ignore_errors=True)
    return info


# --------------------------------------------------------------------------- job helpers
def submit_job(env: Env, job_type: str, payload: dict[str, Any]) -> uuid.UUID:
    """Submit through the API role's narrow ops.submit_job (the HTTP API's own path)."""
    key = f"demo-{job_type}-{uuid.uuid4().hex}"
    with env.api.begin() as c:
        jid = c.execute(text("SELECT ops.submit_job(:t, CAST(:p AS jsonb), :k, 0, :a)"),
                        {"t": job_type, "p": json.dumps(payload), "k": key, "a": ACTOR}).scalar_one()
    return uuid.UUID(str(jid))


def enqueue_internal(env: Env, job_type: str, payload: dict[str, Any]) -> uuid.UUID:
    """Worker-side enqueue (scheduler path) for job types the API may not submit (e.g. gaps)."""
    from reva.adapters.postgres import queue

    with env.worker.begin() as c:
        return queue.enqueue(c, job_type, payload, f"demo-{job_type}-{uuid.uuid4().hex}",
                             created_by="scheduler:acceptance-demo")


def run_job(env: Env, job_id: uuid.UUID, job_type: str) -> dict[str, Any]:
    """Claim and execute queued jobs of ``job_type`` with the real Worker until ``job_id`` was executed."""
    from reva.operations.worker import Worker

    w = Worker(env.settings, env.worker, env.store, env.sim, worker_id=f"acceptance-demo:{job_type}",
               concurrency=1, lease_seconds=600, heartbeat_seconds=10, scheduler=False, invalidation=False,
               job_types=[job_type])
    outcome = None
    for _ in range(25):
        job = w.claim_one()
        if job is None:
            break
        out = w.execute(job)
        if job.id == job_id:
            outcome = out
            break
    with env.worker.connect() as c:
        row = c.execute(text("SELECT id, job_type, status, attempts, result_ref, last_error, worker_id, "
                             "fencing_token FROM ops.job WHERE id = :j"), {"j": job_id}).mappings().one()
    res = dict(row)
    res["worker_outcome"] = None if outcome is None else {"status": outcome.status, "error": outcome.error,
                                                          "duration_s": round(outcome.duration_seconds, 3)}
    return res


def count(env: Env, sql: str, **params: Any) -> int:
    with env.worker.connect() as c:
        return int(c.execute(text(sql), params).scalar_one())


def entity(iso3: str) -> uuid.UUID:
    from reva.cli.seed import entity_id

    return entity_id(iso3)


# --------------------------------------------------------------------------- step 1
def step1_fetch(env: Env, rec: Recorder) -> None:
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.evidence.runner import FixtureHttp, make_live_client, post_publish_for, run_connector

    with rec.step("1") as st:
        st.interfaces.append("library:evidence.runner.run_connector")
        connectors = builtin_connectors(env.settings.reva_http_contact_email)
        per: dict[str, Any] = {}
        network_failures = []
        for name in CONNECTOR_ORDER:
            c = connectors[name]
            spec = c.describe()
            http: Any = FixtureHttp(spec.name) if env.mode == "fixtures" else make_live_client(spec, env.settings)

            async def go(c: Any = c, http: Any = http, spec: Any = spec) -> Any:
                try:
                    return await run_connector(c, http, env.store, env.worker, persist_cursor=env.mode == "live",
                                               post_publish=post_publish_for(spec.template))
                finally:
                    await http.aclose()

            rep = asyncio.run(go())
            statuses: dict[str, int] = {}
            for o in rep.outcomes:
                statuses[o.status] = statuses.get(o.status, 0) + 1
            shas = sorted({o.raw_sha256 for o in rep.outcomes if o.raw_sha256})
            per[name] = {"outcomes": statuses, "inserted": rep.inserted, "unchanged": rep.unchanged,
                         "relations": rep.relations, "errors": rep.errors[:5], "raw_sha256": shas[:6],
                         "raw_artifacts": len(shas)}
            ok = bool(rep.outcomes) and all(o.status in ("published", "unchanged") for o in rep.outcomes)
            if not ok and env.mode == "live" and rep.outcomes and all(o.status == "failed" for o in rep.outcomes):
                network_failures.append(name)
            st.check(f"{name}: every planned item published/unchanged", ok,
                     {"outcomes": statuses, "errors": rep.errors[:3]})
            st.check(f"{name}: new observations or relations recorded", rep.inserted + rep.unchanged > 0
                     or rep.relations > 0 or name in ("naturalearth.admin0_110m", "crossref.works.doi"),
                     {"inserted": rep.inserted, "unchanged": rep.unchanged})
        with env.worker.connect() as c:
            acq = [dict(r) for r in c.execute(text(
                """SELECT cv.name AS connector, a.id, a.origin, a.raw_sha256, a.retrieved_at
                   FROM evidence.acquisition a JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
                   ORDER BY cv.name, a.retrieved_at""")).mappings()]
        origins = sorted({a["origin"] for a in acq})
        st.check("all acquisitions carry the mode's origin", origins == [env.origin], {"origins": origins})
        verified, bad = 0, []
        for a in acq:
            try:
                body = env.store.get("raw", a["raw_sha256"])
                if hashlib.sha256(body).hexdigest() == a["raw_sha256"]:
                    verified += 1
                else:
                    bad.append(a["raw_sha256"])
            except Exception as e:
                bad.append(f"{a['raw_sha256']}: {type(e).__name__}")
        st.check("every archived raw response is retrievable and matches its SHA-256", not bad and verified > 0,
                 {"verified": verified, "problems": bad[:5]})
        by_conn: dict[str, int] = {}
        for a in acq:
            by_conn[a["connector"]] = by_conn.get(a["connector"], 0) + 1
        st.evidence.update(connectors=per, acquisitions_by_connector=by_conn, acquisitions=len(acq),
                           sample_acquisition={k: acq[0][k] for k in ("id", "connector", "raw_sha256", "retrieved_at")}
                           if acq else None,
                           observations=count(env, "SELECT count(*) FROM evidence.observation"),
                           relations=count(env, "SELECT count(*) FROM world.relation"))
        if network_failures and len(network_failures) == len(CONNECTOR_ORDER):
            raise Blocked(f"live providers unreachable: {network_failures}")
        st.evidence["_ready"] = len(acq) > 0


# --------------------------------------------------------------------------- step 2
def _observation(env: Env, iso3: str, concept: str, year: int) -> dict[str, Any] | None:
    with env.worker.connect() as c:
        row = c.execute(text(
            """SELECT o.id, o.logical_key, o.revision_no, o.value, o.uncertainty, o.source_flags, o.raw_selector,
                      o.source_published_at, o.source_available_at, o.availability_basis, o.parser_version,
                      s.resolution, s.temporal_extent, s.aggregation, src.source_key, cv.name AS connector
               FROM evidence.observation o
               JOIN world.support s ON s.id = o.support_id
               JOIN ontology.concept_version cvn ON cvn.id = o.concept_version_id
               JOIN evidence.source src ON src.id = o.source_id
               JOIN evidence.connector_version cv ON cv.id = o.connector_version_id
               WHERE o.subject_id = :s AND cvn.concept_id = :c AND o.origin = :o
                 AND s.temporal_extent->>'start' LIKE :y
               ORDER BY o.revision_no DESC LIMIT 1"""),
            {"s": entity(iso3), "c": concept, "o": env.origin, "y": f"{year}-%"}).mappings().one_or_none()
    return dict(row) if row else None


def step2_flags(env: Env, rec: Recorder) -> None:
    with rec.step("2", needs=("1",)) as st:
        st.interfaces.append("sql:evidence.observation")
        m = _observation(env, "FRA", M_CONCEPT, 2023)
        x = _observation(env, "FRA", X_CONCEPT, 2023)
        g = _observation(env, "FRA", G_CONCEPT, 2023)
        dza = _observation(env, "DZA", M_CONCEPT, 2023)
        st.check("FRA 2023 crude import world total observed", m is not None)
        st.check("FRA 2023 crude export world total observed", x is not None)
        st.check("FRA 2023 nominal GDP observed (WDI)", g is not None)
        if m:
            v = m["value"]
            st.check("import value carries unit unit.usd", v.get("unit_ref") == "unit.usd", v.get("unit_ref"))
            st.check("import valuation flag is CIF", (v.get("basis") or {}).get("valuation") == "CIF", v.get("basis"))
            st.check("HS classification + heading 2709 on the support",
                     (m["resolution"] or {}).get("classification") == "HS"
                     and (m["resolution"] or {}).get("heading") == "2709", m["resolution"])
            st.check("provider flags are preserved", bool(m["source_flags"]), m["source_flags"])
            st.check("source uncertainty is explicit (status present, never a fabricated interval)",
                     "status" in (m["uncertainty"] or {}), m["uncertainty"])
        if x:
            st.check("export valuation flag is FOB", (x["value"].get("basis") or {}).get("valuation") == "FOB",
                     x["value"].get("basis"))
        if g:
            st.check("GDP unit and WDI flags recorded", bool(g["value"].get("unit_ref")) and bool(g["source_flags"]),
                     {"unit_ref": g["value"].get("unit_ref"), "flags": g["source_flags"]})
        if dza is not None:
            st.check("DZA 2023 crude imports are a typed Missing, never zero",
                     dza["value"].get("kind") == "missing" and "value" not in dza["value"], dza["value"])
        else:
            st.check("DZA 2023 crude imports recorded (typed Missing expected)", env.mode == "live",
                     "no DZA observation stored" + (" (live provider may have changed)" if env.mode == "live" else ""))
        st.evidence.update(fra_import_2023=m, fra_export_2023=x, fra_gdp_2023=g, dza_import_2023=dza)


# --------------------------------------------------------------------------- step 3
def step3_publish(env: Env, rec: Recorder) -> None:
    from reva.belief.publication import publish_release
    from reva.belief.snapshots import estimates, release_as_of
    from reva.kernel.context import Origin

    with rec.step("3", needs=("1",)) as st:
        st.interfaces.append("library:belief.publication.publish_release")
        cutoff = env.now()
        rel = publish_release(env.worker, env.store, cutoff=cutoff, origin=Origin(env.origin),
                              environment=env.settings.reva_env, actor="worker:acceptance-demo")
        env.state["release"] = rel
        st.evidence.update(release_id=rel.release_id, sequence=rel.sequence, estimates=rel.estimates,
                           manifest_sha256=rel.manifest_sha256, knowledge_cutoff=rel.knowledge_cutoff,
                           origin=env.origin)
        st.check("release has estimates", rel.estimates > 0, rel.estimates)
        with env.api.connect() as c:
            latest = release_as_of(c, None, env.origin)
            st.check("release is visible to the API role as the latest release",
                     latest is not None and str(latest["id"]) == str(rel.release_id))
            fra_m = [r for r in estimates(c, rel.release_id, subject_id=entity("FRA"), concept_id=M_CONCEPT)
                     if r["temporal_extent"].get("label") == "2023"]
            dza_m = [r for r in estimates(c, rel.release_id, subject_id=entity("DZA"), concept_id=M_CONCEPT)
                     if r["temporal_extent"].get("label") == "2023"]
        head = env.store.head("belief", rel.manifest_sha256)
        st.check("release manifest archived in the object store", head is not None)
        obs = _observation(env, "FRA", M_CONCEPT, 2023)
        st.check("FRA 2023 import estimate is source-selected from the observation",
                 len(fra_m) == 1 and obs is not None and fra_m[0]["value"] == obs["value"]
                 and fra_m[0]["modality"] == "selected_report",
                 {"estimate": fra_m[0] if fra_m else None})
        st.check("DZA 2023 import estimate stays a typed Missing",
                 len(dza_m) == 1 and dza_m[0]["value"].get("kind") == "missing",
                 dza_m[0]["value"] if dza_m else None)
        if fra_m:
            env.state["fra_m_estimate_id"] = fra_m[0]["id"]
        disp = api_get(env, st, "/v1/belief-releases")
        if disp is not None:
            st.check("api lists the published release", any(str(r.get("id")) == str(rel.release_id)
                                                             for r in disp.get("data", [])))
        st.evidence["_ready"] = True


# --------------------------------------------------------------------------- step 4
def step4_hyperedges(env: Env, rec: Recorder) -> None:
    from reva.world import queries

    with rec.step("4", needs=("3",)) as st:
        st.interfaces.append("library:world.queries.neighbourhood")
        rel = env.state["release"]
        y23 = (datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC))
        with env.api.connect() as c:
            ctx_rel = queries.GraphContext.release(rel.release_id)
            nb = queries.neighbourhood(c, entity("FRA"), context=ctx_rel, layers=[TRADE_LAYER], valid_during=y23,
                                       max_edges=200)
            context_used = "release"
            release_pinned_edges = len(nb.edges)
            if not nb.edges:
                nb = queries.neighbourhood(c, entity("FRA"), context=queries.GraphContext.evidence(
                    env.now(), env.origin), layers=[TRADE_LAYER], valid_during=y23, max_edges=200)
                context_used = "evidence_as_of_now"
        st.check("FRA has 2023 crude-trade hyperedges", len(nb.edges) > 0, {"edges": len(nb.edges),
                                                                           "context": context_used})
        role_sets = [sorted({p.role for p in e.participants}) for e in nb.edges]
        st.check("every hyperedge keeps reporter, partner and product roles",
                 bool(role_sets) and all({"role.reporter", "role.partner", "role.product"} <= set(r)
                                         for r in role_sets),
                 role_sets[:3])
        st.check("every hyperedge has an explicit temporal support (interval)",
                 bool(nb.edges) and all(e.support for e in nb.edges), nb.edges[0].support if nb.edges else None)
        st.check("partners are listed as distinct neighbours", len(nb.neighbours) > 1, len(nb.neighbours))
        sample = nb.edges[0].as_dict() if nb.edges else None
        st.evidence["release_pinned_edges"] = release_pinned_edges
        if not release_pinned_edges:
            st.evidence["note"] = ("publish_release did not pin any world.relation rows to the release "
                                   "(world.relation.release_id); the release-pinned graph context is empty, so the "
                                   "edges shown come from the evidence context as known now (same origin)")
        st.evidence.update(context=context_used, edges=len(nb.edges), neighbours=len(nb.neighbours),
                           truncated=nb.truncated, sample_edge=sample,
                           partners=[n.label for n in nb.neighbours][:10])
        api = api_get(env, st, "/v1/relations", entity_id="FRA", period=2023)
        if api is not None:
            rels = api["data"]["relations"]
            st.check("api /v1/relations shows role-bound participants and support",
                     bool(rels) and all({"role.reporter", "role.partner", "role.product"}
                                        <= {p["role"] for p in r["participants"]} for r in rels),
                     {"relations": len(rels), "truncated": api["data"].get("truncated")})


# --------------------------------------------------------------------------- steps 5/6
def scenario_spec(env: Env, *, countries: tuple[str, ...], fidelity: str, title: str,
                  grid: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rel = env.state["release"]
    spec = json.loads(EXAMPLE_SCENARIO.read_text())
    spec["title"] = title
    spec["baseline_release_id"] = str(rel.release_id)
    spec["knowledge_cutoff"] = env.now().isoformat()
    spec["countries"] = list(countries)
    spec["intervention"].update(import_multiplier=1.25, export_multiplier=1.25)
    spec["fidelity"] = fidelity
    if fidelity == "L2":
        spec["assumptions"] = {"quantity_response": "elasticity_sensitivity", "no_general_equilibrium": True,
                               "elasticity_grid": grid or []}
    return spec


def create_scenario(env: Env, spec_dict: dict[str, Any]) -> uuid.UUID:
    """Validated spec -> sim.create_scenario through the API role (the HTTP API's own write path)."""
    from reva.simulation.spec import validate_scenario_spec

    spec = validate_scenario_spec(spec_dict)
    with env.api.begin() as c:
        sid = c.execute(text("SELECT sim.create_scenario(CAST(:s AS jsonb), :b, :d, :a)"),
                        {"s": json.dumps(spec.as_json()), "b": spec.canonical_bytes(), "d": spec.digest(),
                         "a": ACTOR}).scalar_one()
    return uuid.UUID(str(sid))


def results_for(env: Env, run_id: Any) -> list[dict[str, Any]]:
    with env.worker.connect() as c:
        return [dict(r) for r in c.execute(text(
            """SELECT r.id, r.output_key, r.variant, r.value, r.uncertainty, r.applicability, r.artifact_sha256,
                      e.label FROM sim.result r JOIN world.entity e ON e.id = r.subject_id
               WHERE r.run_id = :r ORDER BY r.output_key, e.label, r.variant"""), {"r": run_id}).mappings()]


def hand_computed(env: Env, iso3: str) -> dict[str, Decimal]:
    from reva.belief.snapshots import estimates

    rel = env.state["release"]
    vals: dict[str, Decimal] = {}
    with env.worker.connect() as c:
        for k, concept in (("M", M_CONCEPT), ("X", X_CONCEPT), ("G", G_CONCEPT)):
            rows = [r for r in estimates(c, rel.release_id, subject_id=entity(iso3), concept_id=concept)
                    if r["temporal_extent"].get("label") == "2023"]
            vals[k] = Decimal(rows[0]["value"]["value"])
    with localcontext() as ctx:
        ctx.prec = 50
        d_m = vals["M"] * (Decimal("1.25") - 1)
        d_x = vals["X"] * (Decimal("1.25") - 1)
        d_net = d_m - d_x
        expo = 100 * d_net / vals["G"]
    return {"import_value_change_usd": d_m, "export_value_change_usd": d_x, "net_external_cost_change_usd": d_net,
            "exposure_percent_baseline_gdp": expo, **{f"input_{k}": v for k, v in vals.items()}}


def step5_l1(env: Env, rec: Recorder) -> None:
    with rec.step("5", needs=("3",)) as st:
        st.interfaces += ["sql:sim.create_scenario(reva_api)", "sql:ops.submit_job(reva_api)", "worker:simulation"]
        counts_before = {t: count(env, f"SELECT count(*) FROM {t}")
                         for t in ("evidence.observation", "belief.estimate", "belief.release")}
        sid = create_scenario(env, scenario_spec(env, countries=TRADE_COUNTRIES, fidelity="L1",
                                                 title="Acceptance demo: +25% crude border-price stress (L1)"))
        job_id = submit_job(env, "simulation", {"scenario_id": str(sid), "fidelity": "L1"})
        job = run_job(env, job_id, "simulation")
        st.evidence.update(scenario_id=sid, job=job)
        st.check("simulation job executed by the worker and succeeded", job["status"] == "succeeded",
                 {"status": job["status"], "last_error": job["last_error"]})
        run_id = (job["result_ref"] or {}).get("run_id")
        if not run_id:
            raise RuntimeError(f"no run_id in job result_ref: {job['result_ref']}")
        rows = results_for(env, run_id)
        st.check("12 results (3 countries x 4 outputs)", len(rows) == 12, len(rows))
        by = {(r["label"], r["output_key"]): r for r in rows}
        table: dict[str, dict[str, str]] = {}
        for iso in TRADE_COUNTRIES:
            want = hand_computed(env, iso)
            got = {k: by.get((LABELS[iso], k)) for k in OUTPUTS}
            ok = all(got[k] is not None and Decimal(got[k]["value"]["value"]) == want[k] for k in OUTPUTS)
            st.check(f"{iso}: deterministic exposure equals the hand computation from the pinned baseline", ok,
                     {k: {"engine": got[k]["value"]["value"] if got[k] else None, "hand": str(want[k])}
                      for k in OUTPUTS})
            st.check(f"{iso}: results are assumption-conditioned scenario outputs",
                     all(got[k] and got[k]["applicability"].get("modality") == "assumption_conditioned_scenario"
                         for k in OUTPUTS))
            table[iso] = {k: got[k]["value"]["value"] for k in OUTPUTS if got[k]}
            table[iso].update({f"input_{n}": str(want[f"input_{n}"]) for n in ("M", "X", "G")})
        counts_after = {t: count(env, f"SELECT count(*) FROM {t}") for t in counts_before}
        st.check("scenario run wrote no evidence or beliefs (no scenario number on canonical endpoints)",
                 counts_after == counts_before, {"before": counts_before, "after": counts_after})
        env.state["l1"] = {"scenario_id": sid, "job_id": job_id, "run_id": run_id, "rows": rows}
        st.evidence.update(run_id=run_id, results=table)
        api = api_get(env, st, f"/v1/simulations/{job_id}")
        if api is not None:
            st.check("api shows the result in a SIMULATION context with the scenario banner",
                     api.get("context_mode") == "simulation"
                     and any(w["code"] == "scenario_context" for w in api.get("warnings", []))
                     and len(api["data"]["results"]) == 12,
                     {"context_mode": api.get("context_mode"), "warnings": [w["code"] for w in api.get("warnings", [])],
                      "modality": api.get("modality"), "baseline_release": api.get("baseline_release")})
        st.evidence["_ready"] = True


def step6_l2(env: Env, rec: Recorder) -> None:
    with rec.step("6", needs=("5",)) as st:
        st.interfaces += ["sql:sim.create_scenario(reva_api)", "sql:ops.submit_job(reva_api)", "worker:simulation"]
        grid = [{"import_elasticity": ei, "export_elasticity": ex, "basis": "scenario_assumption"}
                for ei in (-0.5, -0.25, 0.0) for ex in (0.0, 0.25)]
        sid = create_scenario(env, scenario_spec(env, countries=TRADE_COUNTRIES, fidelity="L2", grid=grid,
                                                 title="Acceptance demo: +25% crude border price, L2 sensitivity"))
        job_id = submit_job(env, "simulation", {"scenario_id": str(sid), "fidelity": "L2"})
        job = run_job(env, job_id, "simulation")
        st.evidence.update(scenario_id=sid, job_id=job_id, job_status=job["status"])
        st.check("L2 job succeeded", job["status"] == "succeeded", job["last_error"])
        run_id = (job["result_ref"] or {}).get("run_id")
        rows = results_for(env, run_id) if run_id else []
        l1_ref = {(r["label"], r["output_key"]): r["value"]["value"] for r in env.state["l1"]["rows"]}
        summary: dict[str, Any] = {}
        for iso in TRADE_COUNTRIES:
            label = LABELS[iso]
            for k in OUTPUTS:
                mine = [r for r in rows if r["label"] == label and r["output_key"] == k]
                l1 = [r for r in mine if r["variant"] == "L1"]
                points = [r for r in mine if r["variant"].startswith("L2:") and r["value"].get("kind") == "decimal"]
                ranges = [r for r in mine if (r["uncertainty"] or {}).get("kind") == "sensitivity_range"]
                st.check(f"{iso} {k}: deterministic L1 value present and identical to the L1 run",
                         len(l1) == 1 and l1[0]["value"].get("value") == l1_ref.get((label, k)),
                         {"l2_run": l1[0]["value"].get("value") if l1 else None, "l1_run": l1_ref.get((label, k))})
                st.check(f"{iso} {k}: one value per labelled elasticity grid point", len(points) >= len(grid),
                         {"grid_points": len(grid), "variant_rows": [r["variant"] for r in points]})
                if len(ranges) != 1:
                    st.check(f"{iso} {k}: exactly one range labelled sensitivity_range", False,
                             [r["variant"] for r in ranges])
                    continue
                u = ranges[0]["uncertainty"]
                vals = [Decimal(r["value"]["value"]) for r in points] + (
                    [Decimal(l1[0]["value"]["value"])] if l1 else [])
                st.check(f"{iso} {k}: range is labelled sensitivity_range with no probability/coverage level",
                         u.get("level") is None and u.get("kind") == "sensitivity_range",
                         {"variant": ranges[0]["variant"], "kind": u.get("kind"), "level": u.get("level")})
                st.check(f"{iso} {k}: range bounds are the min/max over the grid and the fixed-quantity point",
                         bool(vals) and Decimal(u["lower"]) == min(vals) and Decimal(u["upper"]) == max(vals),
                         {"lower": u.get("lower"), "upper": u.get("upper"), "min": str(min(vals)) if vals else None,
                          "max": str(max(vals)) if vals else None})
                zero = [r for r in points if r["variant"] == "L2:eM=0;eX=0"]
                if zero:
                    st.check(f"{iso} {k}: zero-elasticity variant equals L1 exactly",
                             zero[0]["value"]["value"] == l1_ref.get((label, k)))
                if k == "exposure_percent_baseline_gdp":
                    summary[iso] = {"exposure_percent_L1": l1_ref.get((label, k)), "sensitivity_range":
                                    [u.get("lower"), u.get("upper")], "kind": u.get("kind"),
                                    "range_row_variant": ranges[0]["variant"]}
        st.evidence.update(run_id=run_id, exposure=summary, grid=grid)
        api = api_get(env, st, f"/v1/simulations/{job_id}")
        if api is not None:
            shown = [r for r in api["data"]["results"] if (r.get("uncertainty") or {}).get("kind")
                     == "sensitivity_range"]
            st.check("api shows the labelled sensitivity-range rows (no level) next to the L1 values",
                     len(shown) == 3 * len(OUTPUTS) and all(r["uncertainty"].get("level") is None for r in shown)
                     and api.get("context_mode") == "simulation",
                     {"range_rows": len(shown), "summary": api.get("uncertainty"),
                      "variants": sorted({r["variant"] for r in shown})})


# --------------------------------------------------------------------------- step 7
def _find_record(doc: Any, match: dict[str, Any]) -> list[dict[str, Any]]:
    """Records in a provider JSON document whose fields equal every selector match key."""
    found: list[dict[str, Any]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if all(k in o and str(o[k]) == str(v) for k, v in match.items()):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)
    return found


def step7_provenance(env: Env, rec: Recorder) -> None:
    with rec.step("7", needs=("5",)) as st:
        st.interfaces.append("sql:belief.input->evidence.observation->evidence.acquisition")
        res = next(r for r in env.state["l1"]["rows"]
                   if r["label"] == "France" and r["output_key"] == "import_value_change_usd")
        inp = (res["applicability"].get("inputs") or {}).get("M") or {}
        est_id = inp.get("estimate_id")
        st.check("result applicability names its pinned input estimate", bool(est_id), inp)
        with env.worker.connect() as c:
            row = c.execute(text(
                """SELECT e.id AS estimate_id, e.value AS estimate_value, e.release_id, o.id AS observation_id,
                          o.logical_key, o.revision_no, o.raw_selector, o.value AS observation_value,
                          a.id AS acquisition_id, a.raw_sha256, a.origin, a.retrieved_at, cv.name AS connector,
                          cv.version AS connector_version, o.parser_version
                   FROM belief.estimate e JOIN belief.input i ON i.estimate_id = e.id
                   JOIN evidence.observation o ON o.id = i.observation_id
                   JOIN evidence.acquisition a ON a.id = o.acquisition_id
                   JOIN evidence.connector_version cv ON cv.id = o.connector_version_id
                   WHERE e.id = :e"""), {"e": est_id}).mappings().one()
        row = dict(row)
        sel = row["raw_selector"]
        st.check("selector names the raw artifact of the acquisition", sel.get("artifact_sha256") == row["raw_sha256"],
                 sel)
        body = env.store.get("raw", row["raw_sha256"])
        st.check("raw bytes re-hash to the archived SHA-256", hashlib.sha256(body).hexdigest() == row["raw_sha256"])
        doc = json.loads(body)
        recs = _find_record(doc, sel.get("match") or {})
        fld = sel.get("field")
        raw_vals = sorted({str(r.get(fld)) for r in recs if fld in r})
        st.check("selector resolves to exactly one provider record field", len(recs) == 1 and fld in recs[0],
                 {"matches": len(recs), "field": fld})
        st.check("raw response field equals the observation and the baseline estimate",
                 len(raw_vals) == 1 and Decimal(raw_vals[0]) == Decimal(row["observation_value"]["value"])
                 == Decimal(row["estimate_value"]["value"]),
                 {"raw": raw_vals, "observation": row["observation_value"].get("value"),
                  "estimate": row["estimate_value"].get("value")})
        st.check("result input value equals the drilled raw value", inp.get("value") is not None
                 and Decimal(inp["value"]) == Decimal(row["estimate_value"]["value"]), inp)
        st.evidence.update(sim_result_id=res["id"], chain=row, raw_field={"field": fld, "value": raw_vals})
        api = api_get(env, st, f"/v1/provenance/sim_result/{res['id']}")
        if api is not None:
            kinds = {n["kind"] for n in api["data"]["nodes"]}
            st.check("api provenance of the result reaches run, scenario and baseline release",
                     {"run", "scenario", "release"} <= kinds, sorted(kinds))
        api = api_get(env, st, f"/v1/provenance/estimate/{est_id}")
        if api is not None:
            g = api["data"]
            edge = next((e for e in g["edges"] if e["relation"] == "selected_from"), None)
            st.check("api provenance drawer: estimate -> observation -> raw artifact + selector",
                     edge is not None and edge["target"] == f"artifact:{row['raw_sha256']}"
                     and (edge["attributes"].get("selector") or {}).get("artifact_sha256") == row["raw_sha256"],
                     edge)
            st.check("api provenance drawer shows storage location of the raw bytes",
                     any(e["relation"] == "stored_at" for e in g["edges"]))


# --------------------------------------------------------------------------- step 8
def _vintage_loader() -> Any:
    if "vintage_loader" not in sys.modules:
        spec = importlib.util.spec_from_file_location("vintage_loader", VINTAGES / "vintage_loader.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["vintage_loader"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["vintage_loader"]


def step8_replay(env: Env, rec: Recorder) -> None:
    from reva.belief.publication import publish_release
    from reva.evaluation.replay import operational_as_known
    from reva.kernel.context import Origin

    with rec.step("8", needs=("1",)) as st:
        st.interfaces += ["library:tests/fixtures/vintages/vintage_loader", "library:evaluation.replay"]
        vl = _vintage_loader()
        name, key = "pop_fra_1990_1991_two_vintages", "wb:SP.POP.TOTL:FRA:1991"
        s1, _ = asyncio.run(vl.ingest_vintage(env.worker, env.store, name, "v1"))
        r1 = publish_release(env.worker, env.store, cutoff=env.now(), origin=Origin.test_fixture,
                             environment=env.settings.reva_env, concepts=[POP])
        t_between = env.now()
        s2, _ = asyncio.run(vl.ingest_vintage(env.worker, env.store, name, "v2"))
        r2 = publish_release(env.worker, env.store, cutoff=env.now(), origin=Origin.test_fixture,
                             environment=env.settings.reva_env, concepts=[POP])
        t_after = env.now()
        with env.worker.connect() as c:
            scope = [dict(r) for r in c.execute(text(
                """SELECT cv.version, e.decision, e.applicability FROM evidence.eligibility e
                   JOIN evidence.connector_version cv ON cv.id = e.connector_version_id
                   JOIN ontology.concept_version c ON c.id = e.concept_version_id
                   WHERE cv.name = 'worldbank.wdi.indicators' AND c.concept_id = :c"""), {"c": POP}).mappings()]
            in_release = c.execute(text(
                """SELECT count(*) FROM belief.input i JOIN belief.estimate e ON e.id = i.estimate_id
                   JOIN evidence.observation o ON o.id = i.observation_id
                   WHERE e.release_id = :r AND o.logical_key = :k"""), {"r": r1.release_id, "k": key}).scalar_one()
        years = [list((x["applicability"] or {}).get("years") or []) for x in scope]
        st.check("recorded vintage years (1990-1991) are admitted into the belief release",
                 in_release > 0, {"eligibility_years": years, "vintage_years": [1990, 1991],
                                  "vintage_observation_in_release_1": in_release})
        st.check("vintage 2 revises exactly one value (1991)", (s1.inserted, s2.inserted, s2.unchanged) == (2, 1, 1),
                 {"v1": {"inserted": s1.inserted}, "v2": {"inserted": s2.inserted, "unchanged": s2.unchanged}})
        with env.api.connect() as c:
            a = operational_as_known(c, t_between, origin="test_fixture", concepts=[POP], store=env.store)
            b = operational_as_known(c, t_after, origin="test_fixture", concepts=[POP], store=env.store)

        def value_for(m: dict[str, Any]) -> tuple[str | None, int | None]:
            by_obs = {i["observation_id"]: i for i in m.get("inputs", [])}
            for o in m.get("outputs", []):
                for oid in o["input_observation_ids"]:
                    if by_obs.get(oid, {}).get("logical_key") == key:
                        return o["value"].get("value"), by_obs[oid]["revision_no"]
            return None, None

        va, ra = value_for(a)
        vb, rb = value_for(b)
        st.check("both replays resolve a published release", a.get("status") == b.get("status") == "ok",
                 {"between": a.get("status"), "after": b.get("status")})
        st.check("cutoff between vintages uses release 1 and revision 1 (later revision excluded)",
                 a.get("release", {}).get("release_id") == str(r1.release_id) and ra == 1,
                 {"release": a.get("release", {}).get("release_id"), "value": va, "revision": ra})
        st.check("cutoff after vintage 2 uses release 2 and revision 2",
                 b.get("release", {}).get("release_id") == str(r2.release_id) and rb == 2,
                 {"release": b.get("release", {}).get("release_id"), "value": vb, "revision": rb})
        st.check("the two as-of answers differ", va is not None and vb is not None and va != vb, [va, vb])
        st.check("replayed release manifests verify against stored bytes",
                 a.get("manifest_verified") is True and b.get("manifest_verified") is True)
        st.evidence.update(series=name, logical_key=key, cutoff_between=t_between, cutoff_after=t_after,
                           release_v1=r1.release_id, release_v2=r2.release_id, value_between=va, value_after=vb,
                           replay_mode=a.get("mode"), origin="test_fixture (recorded synthetic vintages)")


# --------------------------------------------------------------------------- step 9
def step9_eurostat(env: Env, rec: Recorder) -> None:
    from reva.autonomy.jobs import promoted_connectors
    from reva.evidence.runner import FixtureHttp, make_live_client, run_connector

    with rec.step("9") as st:
        st.interfaces += ["sql:ops.submit_job(reva_api)", "worker:discovery", "library:autonomy.jobs"]
        mode = "fixture" if env.mode == "fixtures" else "live"
        job_id = submit_job(env, "discovery", {"requirement_id": "R-POP-JAN-AGESEX", "mode": mode})
        job = run_job(env, job_id, "discovery")
        out = job["result_ref"] or {}
        st.evidence.update(job_id=job_id, job_status=job["status"], discovery={
            k: out.get(k) for k in ("status", "gap", "connector", "validation", "sandbox", "gate", "mappings",
                                    "rejected_equivalences", "value_estimate")})
        if out.get("status") == "blocked":
            raise Blocked(f"autonomy guard blocked discovery: {out.get('reason')}")
        st.check("discovery job succeeded", job["status"] == "succeeded", job["last_error"])
        st.check("demo_pjan discovered from the Eurostat catalogue",
                 (out.get("connector") or {}).get("name") == EUROSTAT_CONNECTOR, out.get("connector"))
        st.check("captured/live sample validated", (out.get("validation") or {}).get("outcome") == "pass",
                 out.get("validation"))
        st.check("connector promoted by the independent gate", out.get("status") == "promoted"
                 and (out.get("gate") or {}).get("outcome") == "promote", out.get("gate"))
        rej = out.get("rejected_equivalences") or []
        st.check("false equivalence with WDI midyear population rejected",
                 any(r.get("outcome") == "reject" and "wdi_midyear" in r.get("target", "") for r in rej), rej)
        with env.worker.connect() as c:
            kind = c.execute(text("SELECT mapping_kind FROM ontology.mapping WHERE mapping_key = "
                                  "'eurostat.demo_pjan.to_wdi_midyear'")).scalar_one_or_none()
        st.check("incompatible mapping recorded in the ontology", kind == "incompatible", kind)
        conns = promoted_connectors(env.worker, env.store)
        conn_obj = conns.get(EUROSTAT_CONNECTOR)
        st.check("promoted connector is loadable from its approved manifest", conn_obj is not None, sorted(conns))
        if conn_obj is None:
            return
        http: Any = (FixtureHttp(EUROSTAT_CONNECTOR) if env.mode == "fixtures"
                     else make_live_client(conn_obj.describe(), env.settings))

        async def go() -> Any:
            try:
                return await run_connector(conn_obj, http, env.store, env.worker, persist_cursor=False)
            finally:
                await http.aclose()

        rep = asyncio.run(go())
        with env.worker.connect() as c:
            rows = [dict(r) for r in c.execute(text(
                """SELECT o.logical_key, o.value, o.source_flags FROM evidence.observation o
                   JOIN evidence.connector_version cv ON cv.id = o.connector_version_id WHERE cv.name = :n"""),
                {"n": EUROSTAT_CONNECTOR}).mappings()]
        total = next((r for r in rows if r["logical_key"].endswith(":T:TOTAL:2023")), None)
        leaves = sum(int(r["value"]["value"]) for r in rows
                     if r["source_flags"].get("cell_type") == "leaf" and r["value"].get("kind") == "decimal")
        st.check("Eurostat sample ingested through the normal evidence path", len(rows) > 0,
                 {"inserted": rep.inserted, "observations": len(rows), "errors": rep.errors[:3]})
        st.check("disjoint leaf cells sum to the published national total",
                 total is not None and leaves == int(total["value"]["value"]),
                 {"leaf_sum": leaves, "total": total["value"] if total else None})
        st.evidence.update(eurostat_observations=len(rows), leaf_sum=leaves,
                           total=total["value"] if total else None,
                           total_flags=total["source_flags"] if total else None)
        st.evidence["_ready"] = bool(rows)


# --------------------------------------------------------------------------- step 10
def step10_population(env: Env, rec: Recorder) -> None:
    import io
    from datetime import date

    import numpy as np
    import pyarrow.parquet as pq

    from reva.compression.refine import UnsupportedRefinement, refine
    from reva.compression.stratified import coarsen
    from reva.population import build_recipe, rake
    from reva.population.service import CONCEPT, FIVE_YEAR_BANDS, load_cells

    with rec.step("10", needs=("9",)) as st:
        st.interfaces += ["sql:ops.submit_job(reva_api)", "worker:population", "library:population/compression"]
        job_id = submit_job(env, "population", {"geo": "FRA", "year": 2023, "n_records": 100_000,
                                                "origin": env.origin})
        job = run_job(env, job_id, "population")
        out = job["result_ref"] or {}
        st.evidence.update(job_id=job_id, job_status=job["status"], result=out)
        st.check("population job (materialise + diagnostics + coarsen + refine) succeeded",
                 job["status"] == "succeeded", job["last_error"])
        if job["status"] != "succeeded":
            return
        with env.worker.connect() as c:
            ds = dict(c.execute(text("SELECT id, record_count, diagnostics, supported_attributes, privacy_class "
                                     "FROM model.population_dataset WHERE id = :i"),
                                {"i": out["population_dataset_id"]}).mappings().one())
            certs = [dict(r) for r in c.execute(text(
                "SELECT operation, certificate, output_sha256 FROM model.compression_certificate "
                "WHERE population_dataset_id = :i ORDER BY operation"), {"i": out["population_dataset_id"]}).mappings()]
        st.check("100k weighted records materialised", ds["record_count"] == 100_000, ds["record_count"])
        st.check("only age x recorded sex attributes; synthetic, not linked to individuals",
                 sorted(ds["supported_attributes"]) == ["age", "sex"]
                 and ds["privacy_class"] == "synthetic_public_aggregate_derived",
                 {"attributes": ds["supported_attributes"], "privacy_class": ds["privacy_class"]})
        ops_ = {c_["operation"]: c_["certificate"] for c_ in certs}
        st.check("coarsen and refine certificates conserve total weight exactly",
                 set(ops_) == {"coarsen", "refine"} and all(v.get("conserved") is True for v in ops_.values()),
                 {k: {"conserved": v.get("conserved"), "measured_error": v.get("measured_error"),
                      "conservation": v.get("conservation")} for k, v in ops_.items()})
        table = pq.read_table(io.BytesIO(env.store.get("models", out["materialisation_sha256"])))
        weight_total = float(np.sum(table.column("weight").to_numpy()))
        # calibration: rake uniform start weights to PUBLISHED Eurostat margins (sex totals; age totals for sex=T)
        cells = load_cells(env.worker, geo_entity="FRA", year=2023, cutoff=env.now(), origin=env.origin)
        ages = table.column("age_code").to_pylist()
        sexes = table.column("sex_code").to_pylist()
        age_codes, sex_codes = set(ages), set(sexes)
        sex_m = {c_.sex_code: c_.count for c_ in cells if c_.age_code == "TOTAL" and c_.sex_code in sex_codes}
        age_m = {c_.age_code: c_.count for c_ in cells if c_.sex_code == "T" and c_.age_code in age_codes}
        cal = rake(np.ones(table.num_rows), {"age": ages, "sex": sexes}, {"age": age_m, "sex": sex_m})
        st.check("raking to published age and sex margins is feasible and converges",
                 cal.feasible and cal.converged,
                 {k: v for k, v in cal.to_dict().items() if k != "targets"})
        if cal.feasible and cal.weights is not None:
            st.check("calibrated total equals the published national total",
                     abs(float(np.sum(cal.weights)) - sum(sex_m.values())) <= 1e-6 * sum(sex_m.values()),
                     {"calibrated_total": float(np.sum(cal.weights)), "published_sex_total": sum(sex_m.values()),
                      "materialised_weight_total": weight_total})
        # refinement along a dimension without source evidence is refused and reported
        recipe = build_recipe(cells, geography="FRA", reference_date=date(2023, 1, 1),
                              source_ref={"concept": CONCEPT, "dataset": "eurostat demo_pjan"}, seed=20260101)
        coarse = coarsen(table, FIVE_YEAR_BANDS, task_scope="age_band_reporting")
        try:
            refine(coarse.table, recipe, "income", task_scope="age_band_reporting")
            st.check("refinement to unsupported new detail (income) is refused", False, "refine returned a result")
        except UnsupportedRefinement as e:
            st.check("refinement to unsupported new detail (income) is refused and reported", True,
                     getattr(e, "details", {}).get("report") or e.message)
        st.evidence.update(population_dataset_id=out["population_dataset_id"],
                           recipe_sha256=out.get("recipe_sha256"),
                           materialisation_sha256=out.get("materialisation_sha256"), leaf_cells=out.get("leaf_cells"),
                           ess=out.get("ess"), weight_total=weight_total,
                           calibration={k: v for k, v in cal.to_dict().items() if k != "targets"},
                           coarse_rows=coarse.table.num_rows)


# --------------------------------------------------------------------------- step 11
def step11_tournament(env: Env, rec: Recorder) -> None:
    from reva.evaluation.tournament_service import LABEL, REPLAY_MODE

    with rec.step("11", needs=("3",)) as st:
        st.interfaces += ["sql:ops.submit_job(reva_api)", "worker:evaluation",
                          "library:evaluation.tournament_service"]
        rel = env.state["release"]
        job_id = submit_job(env, "evaluation", {"release_id": str(rel.release_id), "concepts": [POP, G_CONCEPT],
                                                "countries": list(ALL_FOUR)})
        job = run_job(env, job_id, "evaluation")
        out = job["result_ref"] or {}
        st.evidence.update(job_id=job_id, job_status=job["status"], result={
            k: out.get(k) for k in ("evaluation_status", "label", "replay_mode", "manifest_sha256", "incumbent",
                                    "series", "dropped", "decisions", "holdout")})
        st.check("evaluation job succeeded", job["status"] == "succeeded", job["last_error"])
        st.check("panel labelled revised-data retrospective (not a real-time backtest)",
                 out.get("label") == LABEL and out.get("replay_mode") == REPLAY_MODE,
                 {"label": out.get("label"), "replay_mode": out.get("replay_mode")})
        models = set((out.get("evaluation_ids") or {}).keys())
        st.check("naive last-value baseline evaluated", any("last_value" in m for m in models), sorted(models))
        st.check("state-space (local level) challenger evaluated", any("local_level" in m for m in models),
                 sorted(models))
        st.check("promotion decisions recorded for challengers", bool(out.get("decisions")), out.get("decisions"))
        if out.get("manifest_sha256"):
            man = json.loads(env.store.get("evaluation", out["manifest_sha256"]))
            st.check("stored evaluation manifest carries the label",
                     LABEL in json.dumps(man, ensure_ascii=False), {"manifest_sha256": out["manifest_sha256"]})


# --------------------------------------------------------------------------- step 12
def step12_gaps(env: Env, rec: Recorder) -> None:
    with rec.step("12", needs=("3",)) as st:
        st.interfaces += ["worker:gaps", "library:autonomy.jobs.handle_gaps"]
        job_id = enqueue_internal(env, "gaps", {"origin": env.origin})
        job = run_job(env, job_id, "gaps")
        out = job["result_ref"] or {}
        if out.get("status") == "blocked":
            raise Blocked(f"autonomy guard blocked the gap detector: {out.get('reason')}")
        st.check("gap detector job succeeded", job["status"] == "succeeded" and out.get("status") == "ok",
                 {"status": job["status"], "result": out, "last_error": job["last_error"]})
        report = json.loads(env.store.get("evaluation", out["artifact_sha256"])) if out.get("artifact_sha256") else {}
        gaps = report.get("gaps", [])
        st.check("unresolved knowledge gaps are reported with value-of-information estimates",
                 bool(gaps) and all("value_estimate" in g for g in gaps), {"gaps": len(gaps)})
        dza = [g for g in gaps if "DZA" in g.get("geography", []) and "hs2709" in g.get("concept", "")]
        st.check("Algerian crude-trade gap is among the unresolved gaps", bool(dza), dza[:2])
        rel = env.state["release"]
        api = api_get(env, st, "/v1/gaps", release_id=str(rel.release_id))
        api_dza = []
        if api is not None:
            api_dza = [g for g in api["data"] if g["entity_key"] == "DZA" and g["concept"] == M_CONCEPT]
            st.check("api /v1/gaps lists DZA crude imports as typed_missing with an action",
                     bool(api_dza) and all(g["gap_kind"] == "typed_missing" and g["action"] for g in api_dza),
                     api_dza[:2])
        with env.worker.connect() as c:
            props = [dict(r) for r in c.execute(text(
                """SELECT p.id, p.proposal_type, p.permission_tier, p.manifest_sha256, p.author, p.summary,
                          (SELECT jsonb_agg(jsonb_build_object('decision', g.decision, 'evaluator', g.evaluator))
                             FROM ops.gate_result g WHERE g.proposal_id = p.id) AS gates
                   FROM ops.proposal p ORDER BY p.recorded_at""")).mappings()]
        conn_props = [p for p in props if p["proposal_type"] == "connector_promotion"]
        st.check("a connector change proposal exists with gate results", bool(conn_props) and all(
            p["gates"] for p in conn_props), [{k: p[k] for k in ("id", "proposal_type", "permission_tier", "gates")}
                                              for p in props])
        if not conn_props:
            if rec.steps.get("9") and rec.steps["9"].status == "blocked":
                raise Blocked("no connector proposal: discovery (step 9) was blocked")
            return
        prop = conn_props[-1]
        ns = None
        with env.worker.connect() as c:
            ns = c.execute(text("SELECT namespace FROM meta.artifact_location WHERE artifact_sha256 = :s LIMIT 1"),
                           {"s": prop["manifest_sha256"]}).scalar_one_or_none()
        change = json.loads(env.store.get(ns or "models", prop["manifest_sha256"]))
        st.check("proposal manifest is reviewable (changed paths, manifest, mapping, candidate)",
                 isinstance(change, dict) and bool(change), sorted(change)[:12] if isinstance(change, dict) else None)
        st.evidence.update(gap_report_sha256=out.get("artifact_sha256"), gaps=len(gaps),
                           gap_examples=gaps[:3], api_dza_gaps=api_dza[:4], proposals=props, change_keys=sorted(change)
                           if isinstance(change, dict) else None)
        if env.client is None:
            st.check("api POST /v1/proposals/{id}/review", False, {"error": f"API unavailable: {env.client_error}"})
            return
        lst = api_get(env, st, "/v1/proposals")
        if lst is not None:
            st.check("api /v1/proposals lists the proposal with its gate results",
                     any(str(p["id"]) == str(prop["id"]) and p["gate_results"] for p in lst["data"]))
        r = env.client.post(f"/v1/proposals/{prop['id']}/review",
                            json={"decision": "approve", "comment": "acceptance demo: human review of the "
                                                                    "gate-promoted declarative connector change"})
        st.check("a named human reviewer can record a review through the API",
                 r.status_code == 201 and r.json().get("reviewer", "").startswith("human:"),
                 {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith(
                     "application/") else r.text[:300]})


# --------------------------------------------------------------------------- negative demo
def step_negative(env: Env, rec: Recorder) -> None:
    with rec.step("N", needs=("3",)) as st:
        st.interfaces += ["sql:sim.create_scenario(reva_api)", "sql:ops.submit_job(reva_api)", "worker:simulation",
                          "api"]
        sid = create_scenario(env, scenario_spec(env, countries=ALL_FOUR, fidelity="L1",
                                                 title="Acceptance demo (negative): all four countries"))
        st.evidence["scenario_id"] = sid
        if env.client is not None:
            r = env.client.post("/v1/simulations", json={"scenario_id": str(sid), "fidelity": "L1"},
                                headers={"Idempotency-Key": f"demo-four-{uuid.uuid4()}"})
            body = r.json() if r.headers.get("content-type", "").endswith("json") else {"text": r.text[:300]}
            missing = {m.get("country") for m in (body.get("details") or {}).get("missing", [])}
            st.check("api rejects the all-four run with 422 missing_input naming only DZA",
                     r.status_code == 422 and body.get("code") == "missing_input" and missing == {"DZA"},
                     {"status": r.status_code, "code": body.get("code"), "missing": sorted(x for x in missing if x)})
            st.evidence["api_problem"] = body
        else:
            st.check("api POST /v1/simulations", False, {"error": f"API unavailable: {env.client_error}"})
        # the job path (as if the API pre-check were bypassed) also refuses to impute
        job_id = submit_job(env, "simulation", {"scenario_id": str(sid), "fidelity": "L1"})
        job = run_job(env, job_id, "simulation")
        ref = job["result_ref"] or {}
        probs = [p for p in ref.get("problems", []) if p.get("code") == "missing_input"]
        st.check("worker job ends failed with result status missing_input",
                 job["status"] == "failed" and ref.get("status") == "missing_input"
                 and (job["last_error"] or {}).get("code") == "missing_input",
                 {"status": job["status"], "result_status": ref.get("status"), "last_error": job["last_error"]})
        st.check("only Algeria is reported missing", {p.get("country") for p in probs} == {"DZA"}, probs)
        with env.worker.connect() as c:
            n_res = c.execute(text("SELECT count(*) FROM sim.result r JOIN sim.run u ON u.id = r.run_id "
                                   "WHERE u.scenario_id = :s"), {"s": sid}).scalar_one()
        st.check("no numbers were produced for the blocked run (no imputation, no zero fill)", n_res == 0, n_res)
        prof = api_get(env, st, "/v1/entities/DZA")
        if prof is not None:
            st.check("Algeria's country profile still exists (profile exists != model applicable)",
                     prof["data"]["label"] == "Algeria", {"components": len(prof["data"].get("components", []))})
        bel = api_get(env, st, "/v1/beliefs", release_id=str(env.state["release"].release_id), subject_id="DZA",
                      concept=M_CONCEPT, period=2023)
        if bel is not None:
            v = bel["data"][0]["value"] if bel["data"] else {}
            st.check("DZA 2023 crude-import belief is a typed Missing in the API", v.get("kind") == "missing", v)
        st.evidence.update(job_id=job_id, job_status=job["status"], result_ref=ref)


# --------------------------------------------------------------------------- main
def git_head() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        return {"head": None, "dirty_worktree": None}
    try:
        head = subprocess.run([git, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        dirty = bool(subprocess.run([git, "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
                                    timeout=20).stdout.strip())
        return {"head": head, "dirty_worktree": dirty}
    except Exception:
        return {"head": None, "dirty_worktree": None}


def run_demo(mode: str = "fixtures", *, out_path: Path | None = None, keep: bool = False,
             object_root: Path | None = None) -> dict[str, Any]:
    """Run the full workflow; returns (and writes) the machine-readable result document."""
    if mode not in ("fixtures", "live"):
        raise ValueError("mode must be 'fixtures' or 'live'")
    out_path = out_path or DEFAULT_OUT_DIR / f"demo_{mode}.json"
    started = datetime.now(UTC)
    doc: dict[str, Any] = {"schema": SCHEMA, "mode": mode, "origin": "test_fixture" if mode == "fixtures" else "live",
                           "started_at": started.isoformat(), "git": git_head(),
                           "spec_ref": "architecture §17.3 (12-step demonstration) + negative demo; §17.4 gates"}
    rec = Recorder()
    env: Env | None = None
    try:
        env = create_environment(mode, object_root)
        doc["database"] = {"name": env.db_name, "fresh": True, "seed": _plain(env.state.get("seed"))}
        open_api_client(env)
        doc["api_client"] = {"available": env.client is not None, "error": env.client_error}
        for fn in (step1_fetch, step2_flags, step3_publish, step4_hyperedges, step5_l1, step6_l2, step7_provenance,
                   step8_replay, step9_eurostat, step10_population, step11_tournament, step12_gaps, step_negative):
            fn(env, rec)
    except Exception as e:
        doc["setup_error"] = f"{type(e).__name__}: {str(e)[:1500]}"
        doc["setup_traceback_tail"] = traceback.format_exc().splitlines()[-15:]
    finally:
        if env is not None:
            doc["database"] = {**doc.get("database", {}), **close_environment(env, keep)}
    for sid in STEP_TITLES:
        if sid not in rec.steps:
            key, title = STEP_TITLES[sid]
            rec.steps[sid] = Step(sid, key, title, status="blocked",
                                  blocked_reason="not run: environment setup failed" if doc.get("setup_error")
                                  else "not run")
    steps = [rec.steps[s].as_dict() for s in STEP_TITLES]
    for s in steps:
        s["evidence"].pop("_ready", None)
    summary = {k: sum(1 for s in steps if s["status"] == k) for k in ("pass", "fail", "blocked")}
    doc["steps"] = steps
    doc["summary"] = summary
    doc["negative_demo"] = {"status": rec.steps["N"].status,
                            "holds": rec.steps["N"].status == "pass"}
    doc["overall"] = ("fail" if summary["fail"] or doc.get("setup_error") else
                      "pass" if summary["blocked"] == 0 else "pass_with_blocked_steps")
    doc["finished_at"] = datetime.now(UTC).isoformat()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_plain(doc), indent=2, default=str) + "\n")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fixtures", dest="mode", action="store_const", const="fixtures",
                   help="offline: captured fixtures, origin test_fixture (default)")
    g.add_argument("--live", dest="mode", action="store_const", const="live",
                   help="real provider requests, origin live")
    ap.add_argument("--out", type=Path, help="output JSON (default var/acceptance/demo_<mode>.json)")
    ap.add_argument("--keep", action="store_true", help="keep the fresh database and object store")
    ap.add_argument("--object-root", type=Path, help="object store root (default: a new temporary directory)")
    args = ap.parse_args(argv)
    mode = args.mode or "fixtures"
    os.environ.setdefault("REVA_LOG_LEVEL", "WARNING")
    doc = run_demo(mode, out_path=args.out, keep=args.keep, object_root=args.object_root)
    out = args.out or DEFAULT_OUT_DIR / f"demo_{mode}.json"
    print(f"\nREVA demonstration workflow ({mode}) -> {out}")
    for s in doc["steps"]:
        why = s.get("blocked_reason") or s.get("error") or ""
        failed = [c["name"] for c in s["checks"] if c["status"] == "fail"]
        print(f"  {s['id']:>2} {s['status']:<8} {s['title']}" + (f"  [{why}]" if why else "")
              + (f"  failed checks: {failed}" if failed else ""))
    print(f"  summary: {doc['summary']}  overall: {doc['overall']}")
    if doc.get("setup_error"):
        print(f"  setup error: {doc['setup_error']}")
        return 2
    return 1 if doc["summary"]["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
