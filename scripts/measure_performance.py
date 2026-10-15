#!/usr/bin/env python3
"""Measure REVA performance against the architecture §20.3 initial targets.

Targets (acceptance targets to *measure*, not guaranteed performance):

* cached metadata p95 < 500 ms
* ordinary API p95 < 1 s at 5 concurrent users
* L1 four-country calculation < 2 s excluding queue/IO
* worker queue acknowledgement < 1 s
* 100k demographic records within 60 s and 1 GiB RSS on 1 CPU/2 GiB

Usage::

    uv run python scripts/measure_performance.py            # everything, writes JSON + report
    uv run python scripts/measure_performance.py --only simulation,population
    uv run python scripts/measure_performance.py --requests-per-client 40 --keep-db

Outputs ``var/perf/perf_results.json`` (machine-readable) and ``docs/dev/perf_report.md``.

Method notes
------------
* The four-country numerical benchmark uses the explicitly labelled synthetic
  fixture ``tests/fixtures/benchmark/four_country_l1.json`` (origin
  ``test_fixture``). It is never ingested; it only feeds the pure in-memory
  ``economic.crude_exposure`` / ``simulation.engine.compute`` path.
* API/queue measurements run against a fresh, uniquely named database built
  like the test ``db`` fixture (bootstrap + seed + captured-fixture ingestion +
  ``publish_release(origin=test_fixture)``); the dev database is never written.
  The database is dropped afterwards unless ``--keep-db``.
* The API runs as a separate Uvicorn process (one worker, as in production),
  pinned with ``taskset -c 0`` when available; the load generator is moved off
  CPU 0. The worker/simulation/population phases pin the measuring process to
  CPU 0 with ``os.sched_setaffinity``.
* This module is also imported by ``tests/performance/test_perf_*.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import random
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BENCHMARK_FIXTURE = REPO / "tests" / "fixtures" / "benchmark" / "four_country_l1.json"
DEFAULT_JSON = REPO / "var" / "perf" / "perf_results.json"
DEFAULT_REPORT = REPO / "docs" / "dev" / "perf_report.md"
FIXTURE_LABEL = "SYNTHETIC BENCHMARK — not observed data"

# Architecture §20.3 initial targets. Never weaken these without an ADR.
TARGETS: dict[str, dict[str, Any]] = {
    "cached_metadata_p95_ms": {"limit": 500.0, "text": "cached metadata p95 < 500 ms"},
    "ordinary_api_p95_ms": {"limit": 1000.0, "text": "ordinary API p95 < 1 s at 5 concurrent users"},
    "l1_four_country_ms": {"limit": 2000.0, "text": "L1 four-country calculation < 2 s excluding queue/IO"},
    "queue_ack_p95_ms": {"limit": 1000.0, "text": "worker queue acknowledgement < 1 s"},
    "population_100k_wall_s": {"limit": 60.0, "text": "100k demographic records within 60 s (1 CPU)"},
    "population_100k_peak_rss_mib": {"limit": 1024.0, "text": "100k demographic records within 1 GiB RSS"},
}
CONCURRENT_USERS = 5
API_PLAN_CPU = 0.5  # render.yaml reva-api plan 0.5c-512mb

WB = "worldbank.wdi.indicators"
COMTRADE = "un.comtrade.public_exact"
NATURALEARTH = "naturalearth.admin0_110m"
CROSSREF = "crossref.works.doi"
FIXTURE_CONNECTORS = (WB, COMTRADE, NATURALEARTH, CROSSREF)
POP = "demography.population.wdi_midyear_total"
CRUDE_M = "trade.hs2709.import_value.world_total"
GDP = "economics.gdp.nominal_current_usd"


# =========================================================================== statistics
def percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (numpy 'linear' / Excel PERCENTILE.INC)."""
    if not values:
        raise ValueError("no samples")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarize_ms(values_ms: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values_ms),
        "min_ms": round(min(values_ms), 3),
        "p50_ms": round(percentile(values_ms, 50), 3),
        "mean_ms": round(statistics.fmean(values_ms), 3),
        "p95_ms": round(percentile(values_ms, 95), 3),
        "p99_ms": round(percentile(values_ms, 99), 3),
        "max_ms": round(max(values_ms), 3),
    }


# =========================================================================== hardware / process
def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def hardware_info() -> dict[str, Any]:
    cpuinfo = _read("/proc/cpuinfo") or ""
    model = next((ln.split(":", 1)[1].strip() for ln in cpuinfo.splitlines() if ln.startswith("model name")), None)
    meminfo = _read("/proc/meminfo") or ""
    mem_kib = next((int(ln.split()[1]) for ln in meminfo.splitlines() if ln.startswith("MemTotal:")), None)
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - non-Linux
        affinity = None
    return {
        "nproc": os.cpu_count(),
        "affinity_cpus": affinity,
        "cpu_model": model,
        "mem_total_mib": round(mem_kib / 1024) if mem_kib else None,
        "cgroup_cpu_max": (_read("/sys/fs/cgroup/cpu.max") or "").strip() or None,
        "cgroup_memory_max": (_read("/sys/fs/cgroup/memory.max") or "").strip() or None,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "loadavg_at_start": list(os.getloadavg()),
        "taskset": shutil.which("taskset"),
        "hostname_hash": __import__("hashlib").sha256(socket.gethostname().encode()).hexdigest()[:12],
    }


def proc_status_kib(pid: int) -> dict[str, int]:
    """VmRSS / VmHWM (peak resident set) of a live process from /proc/<pid>/status, in KiB."""
    out: dict[str, int] = {}
    for ln in (_read(f"/proc/{pid}/status") or "").splitlines():
        key, _, rest = ln.partition(":")
        if key in ("VmRSS", "VmHWM"):
            out[key] = int(rest.split()[0])
    return out


@contextlib.contextmanager
def pinned(cpus: set[int] | None) -> Iterator[set[int] | None]:
    """Temporarily restrict the calling thread (and threads it starts meanwhile, which inherit the mask) to
    ``cpus``. Linux sched_setaffinity(0) is per thread."""
    if cpus is None or not hasattr(os, "sched_setaffinity"):
        yield None
        return
    before = os.sched_getaffinity(0)
    usable = cpus & before
    if not usable:
        yield None
        return
    os.sched_setaffinity(0, usable)
    try:
        yield usable
    finally:
        os.sched_setaffinity(0, before)


def _cpu0() -> set[int] | None:
    try:
        return {sorted(os.sched_getaffinity(0))[0]}
    except AttributeError:  # pragma: no cover
        return None


def _not_cpu0() -> set[int] | None:
    try:
        cpus = sorted(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover
        return None
    return set(cpus[1:]) or None


# =========================================================================== L1/L2 numerical benchmark
def load_benchmark_fixture(path: Path = BENCHMARK_FIXTURE) -> dict[str, Any]:
    """Load the labelled synthetic fixture and refuse anything that is not clearly labelled."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("origin") != "test_fixture" or data.get("label") != FIXTURE_LABEL or not data.get("not_evidence"):
        raise ValueError(f"{path} is not a labelled synthetic test fixture; refusing to benchmark with it")
    if sorted(data["countries"]) != ["CHN", "DZA", "FRA", "USA"]:
        raise ValueError("benchmark fixture must hold exactly FRA, DZA, CHN and USA")
    return dict(data)


def benchmark_spec(fx: dict[str, Any], fidelity: str) -> dict[str, Any]:
    """ScenarioSpec v1 dict over the fixture (fixture baseline UUID: never a real release)."""
    from reva.simulation.spec import FIXTURE_BASELINE

    l2 = fidelity == "L2"
    return {
        "schema_version": "1.0.0",
        "title": f"{FIXTURE_LABEL}: four-country {fidelity} crude border-price benchmark",
        "baseline_release_id": FIXTURE_BASELINE,
        "knowledge_cutoff": "2026-10-14T00:00:00Z",
        "model_key": fx["model"]["key"],
        "model_version": fx["model"]["version"],
        "countries": ["FRA", "DZA", "CHN", "USA"],
        "reference_year": fx["reference_year"],
        "horizon": {"periods": 1, "unit": "annual_accounting_period"},
        "intervention": dict(fx["intervention"]),
        "assumptions": {
            "quantity_response": "elasticity_sensitivity" if l2 else "fixed",
            "elasticity_grid": [dict(p) for p in fx["l2_elasticity_grid"]] if l2 else [],
            "no_general_equilibrium": True,
        },
        "fidelity": fidelity,
        "missing_input_policy": "fail",
        "outputs": [
            "import_value_change_usd",
            "export_value_change_usd",
            "net_external_cost_change_usd",
            "exposure_percent_baseline_gdp",
        ],
        "seed": 1729,
    }


def _value_dicts(fx: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    tpl = fx["value_dict_template"]
    return {
        iso3: {n: {**tpl, "basis": dict(tpl["basis"]), "value": v} for n, v in vals.items()}
        for iso3, vals in fx["countries"].items()
    }


def _country_inputs(fx: dict[str, Any], vdicts: dict[str, dict[str, dict[str, Any]]], iso3: str) -> Any:
    from reva.models.economic import crude_exposure as ce

    year = fx["reference_year"]
    return ce.CountryInputs(
        iso3,
        *(
            ce.MonetaryInput.from_value_dict(
                n,
                vdicts[iso3][n],
                reference_year=year,
                aggregation=fx["aggregation"],
                source_flags={"fixture": fx["fixture_id"]},
                estimate_id=f"fixture:{iso3}:{n}",
            )
            for n in ("M", "X", "G")
        ),
    )


def model_core(fx: dict[str, Any], vdicts: dict[str, Any], fidelity: str) -> list[Any]:
    """Tier 1: typed input validation + crude_exposure L1 (or every L2 grid point) for four countries."""
    from reva.models.economic import crude_exposure as ce

    p = fx["intervention"]
    grid = None
    if fidelity == "L2":
        grid = tuple((g["import_elasticity"], g["export_elasticity"]) for g in fx["l2_elasticity_grid"])
    out: list[Any] = []
    for iso3 in ("FRA", "DZA", "CHN", "USA"):
        out += ce.evaluate_country(
            _country_inputs(fx, vdicts, iso3),
            reference_year=fx["reference_year"],
            import_multiplier=p["import_multiplier"],
            export_multiplier=p["export_multiplier"],
            elasticity_grid=grid,
        )
    return out


def engine_compute(
    fx: dict[str, Any], vdicts: dict[str, Any], spec_dict: dict[str, Any], *, validate_spec: bool
) -> tuple[list[Any], str]:
    """Tier 2/3: what the simulation job does between reading inputs and publishing, all in memory:
    (spec validation), intervention compilation, typed inputs, coupling schedule, L1/L2 rows, output digest."""
    from reva.cli.seed import entity_id
    from reva.models.economic import crude_exposure as ce
    from reva.simulation import engine
    from reva.simulation.compile import CompiledScenario, CountryPlan, build_coupling, compile_intervention
    from reva.simulation.spec import ScenarioSpec, validate_scenario_spec

    if validate_spec:
        spec = validate_scenario_spec(spec_dict, allow_fixture_baseline=True)
    else:
        spec = ScenarioSpec.model_validate(spec_dict)
    change = compile_intervention(spec.intervention.model_dump(mode="json"), spec.assumptions.quantity_response)
    plans = []
    for iso3 in spec.countries:
        inputs = _country_inputs(fx, vdicts, iso3)
        plan = CountryPlan(iso3, entity_id(iso3), "fixture", inputs)
        plan.values = ce.validate_country_inputs(
            inputs,
            reference_year=spec.reference_year,
            horizon_periods=spec.horizon.periods,
            horizon_unit=spec.horizon.unit,
        )
        plans.append(plan)
    schedule = build_coupling(plans)
    compiled = CompiledScenario(
        spec=spec,
        release={"id": spec.baseline_release_id, "origin": "test_fixture"},
        change=change,
        countries=plans,
        schedule=schedule,
    )
    rows = engine.compute(compiled, spec.fidelity)
    return rows, engine.output_digest(rows)


def bench_simulation(repeats: int = 300, *, pin: bool = True) -> dict[str, Any]:
    fx = load_benchmark_fixture()
    vdicts = _value_dicts(fx)
    specs = {f: benchmark_spec(fx, f) for f in ("L1", "L2")}
    out: dict[str, Any] = {
        "fixture": str(BENCHMARK_FIXTURE.relative_to(REPO)),
        "fixture_label": fx["label"],
        "fixture_origin": fx["origin"],
        "repeats": repeats,
        "countries": ["FRA", "DZA", "CHN", "USA"],
        "l2_grid_points": len(fx["l2_elasticity_grid"]),
        "tiers": {
            "model_core": "4x crude_exposure.evaluate_country (typed input validation + Decimal L1/L2 math)",
            "engine": "intervention compile + typed inputs + coupling schedule + simulation.engine.compute rows "
            "(values, uncertainty, applicability) + output digest",
            "engine_with_spec_validation": "engine + ScenarioSpec JSON-Schema/scope validation",
        },
    }
    with pinned(_cpu0() if pin else None) as cpus:
        out["pinned_cpus"] = sorted(cpus) if cpus else None
        t0 = time.perf_counter()
        cold_rows, cold_digest = engine_compute(fx, vdicts, specs["L1"], validate_spec=True)
        out["cold_first_call_l1_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        for fidelity in ("L1", "L2"):
            res: dict[str, Any] = {}
            for tier, fn in (
                ("model_core", lambda f=fidelity: model_core(fx, vdicts, f)),
                ("engine", lambda f=fidelity: engine_compute(fx, vdicts, specs[f], validate_spec=False)),
                (
                    "engine_with_spec_validation",
                    lambda f=fidelity: engine_compute(fx, vdicts, specs[f], validate_spec=True),
                ),
            ):
                fn()  # warm
                samples = []
                for _ in range(repeats):
                    t = time.perf_counter()
                    fn()
                    samples.append((time.perf_counter() - t) * 1000)
                res[tier] = summarize_ms(samples)
            rows, digest = engine_compute(fx, vdicts, specs[fidelity], validate_spec=True)
            res["result_rows"] = len(rows)
            res["output_digest"] = digest
            res["variants"] = sorted({r.variant for r in rows})
            out[fidelity] = res
    out["l1_cold_rows"] = len(cold_rows)
    out["l1_cold_digest_matches_warm"] = cold_digest == out["L1"]["output_digest"]
    return out


# =========================================================================== fresh database environment
@dataclass
class PerfEnv:
    settings: Any
    name: str
    owner: Any
    worker: Any
    sim: Any
    store: Any
    release_id: uuid.UUID
    estimates: int
    setup_seconds: float
    admin: Any = None
    keep: bool = False
    timings: dict[str, float] = field(default_factory=dict)


def base_settings() -> Any:
    from reva.settings import Settings

    return Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]


async def _ingest(name: str, worker: Any, store: Any) -> None:
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.evidence.runner import FixtureHttp, post_publish_for, run_connector

    c = builtin_connectors()[name]
    spec = c.describe()
    rep = await run_connector(
        c, FixtureHttp(spec.name), store, worker, persist_cursor=False, post_publish=post_publish_for(spec.template)
    )
    if rep.errors:
        raise RuntimeError(f"fixture ingestion of {name} failed: {rep.errors}")


@contextlib.contextmanager
def perf_database(
    base: Any | None = None, *, label: str = "perf", keep: bool = False, object_root: Path | None = None
) -> Iterator[PerfEnv]:
    """Fresh uniquely named database (never the dev DB): bootstrap, seed, ingest captured fixtures for
    worldbank + comtrade + naturalearth + crossref, publish a test_fixture release."""
    from sqlalchemy import create_engine, text

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.belief.publication import publish_release
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed
    from reva.kernel.context import Origin
    from reva.settings import Settings

    base = base or base_settings()
    if base.reva_migration_database_url is None:
        raise RuntimeError("REVA_MIGRATION_DATABASE_URL not configured")
    t_start = time.perf_counter()
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"reva_test_{label}_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    tmp = None
    if object_root is None:
        tmp = tempfile.mkdtemp(prefix="reva_perf_objects_")
        object_root = Path(tmp)
    url = base.migration_url().set(database=name).render_as_string(hide_password=False)
    settings = Settings.model_validate(
        {
            **base.model_dump(),
            "reva_migration_database_url": url,
            "reva_db_name": name,
            "reva_env": "test",
            "reva_object_root": object_root,
        }
    )
    engines: list[Any] = []
    try:
        for attempt in range(3):  # concurrent sessions may race on cluster-global role DDL
            try:
                bootstrap(settings)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1)
        store = FilesystemArtifactStore(object_root, settings.reva_s3_prefix)
        pooled = settings.model_copy(update={"reva_db_pool_size": 4, "reva_db_max_overflow": 0})
        owner, worker, sim = (
            make_engine("migration", settings),
            make_engine("worker", pooled),
            make_engine("sim", settings),
        )
        engines = [owner, worker, sim]
        timings: dict[str, float] = {}
        t = time.perf_counter()
        seed(worker, store)
        timings["seed_s"] = time.perf_counter() - t
        loop = asyncio.new_event_loop()
        try:
            for conn_name in FIXTURE_CONNECTORS:
                t = time.perf_counter()
                loop.run_until_complete(_ingest(conn_name, worker, store))
                timings[f"ingest_{conn_name}_s"] = time.perf_counter() - t
        finally:
            loop.close()
        t = time.perf_counter()
        rel = publish_release(worker, store, cutoff=datetime.now(UTC), origin=Origin.test_fixture, environment="test")
        timings["publish_release_s"] = time.perf_counter() - t
        env = PerfEnv(
            settings,
            name,
            owner,
            worker,
            sim,
            store,
            rel.release_id,
            rel.estimates,
            time.perf_counter() - t_start,
            admin,
            keep,
            {k: round(v, 3) for k, v in timings.items()},
        )
        yield env
    finally:
        for e in engines:
            e.dispose()
        if not keep:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        admin.dispose()


# =========================================================================== API latency
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def create_api_token(env: PerfEnv, scopes: tuple[str, ...] = ("read",)) -> str:
    """Insert a hashed API token row (owner connection); returns the bearer token."""
    from sqlalchemy import text

    from reva.api.auth import new_api_token

    tok, sha = new_api_token()
    with env.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, :l)"),
            {"h": sha, "s": list(scopes), "l": "perf:benchmark"},
        )
    return tok


def api_child_env(settings: Any) -> dict[str, str]:
    """Allowlisted environment for the API process: API role credentials only, production pool sizes
    (render.yaml: REVA_DB_POOL_SIZE=2, MAX_OVERFLOW=0, statement timeout 10 s), one thread budget."""
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV")}
    env = {
        **keep,
        "REVA_ENV": "test",
        "REVA_LOG_LEVEL": "WARNING",
        "REVA_DB_HOST": settings.reva_db_host,
        "REVA_DB_PORT": str(settings.reva_db_port),
        "REVA_DB_NAME": settings.reva_db_name,
        "REVA_API_DB_PASSWORD": settings.reva_api_db_password.get_secret_value(),
        "REVA_DB_POOL_SIZE": "2",
        "REVA_DB_MAX_OVERFLOW": "0",
        "REVA_DB_STATEMENT_TIMEOUT_MS": "10000",
        "REVA_OBJECT_BACKEND": "filesystem",
        "REVA_OBJECT_ROOT": str(settings.reva_object_root),
        "REVA_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "REVA_SESSION_SECRET": secrets.token_urlsafe(48),
        "REVA_CPU_BUDGET_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "POLARS_MAX_THREADS": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if settings.reva_db_sslmode:
        env["REVA_DB_SSLMODE"] = settings.reva_db_sslmode
    return env


class CpuThrottle(threading.Thread):
    """Approximate a fractional CPU quota (e.g. Render's 0.5 CPU) for one process, like CFS bandwidth control:
    within each ``period`` the process may consume ``fraction * period`` of CPU time (summed over all its
    threads from /proc/<pid>/task/*/schedstat); once spent it is SIGSTOPped until the period ends.
    An emulation, not a cgroup: control granularity is ``tick`` and the monitor runs on another CPU."""

    def __init__(self, pid: int, fraction: float, period: float = 0.1, tick: float = 0.001) -> None:
        super().__init__(name="perf-cpu-throttle", daemon=True)
        if not 0 < fraction <= 1:
            raise ValueError("fraction must be in (0, 1]")
        self.pid, self.fraction, self.period, self.tick = pid, fraction, period, tick
        self._stop_ev = threading.Event()
        self.throttled_periods = 0
        self.periods = 0

    def cpu_ns(self) -> int:
        total = 0
        try:
            tasks = os.listdir(f"/proc/{self.pid}/task")
        except OSError:
            return 0
        for t in tasks:
            raw = _read(f"/proc/{self.pid}/task/{t}/schedstat")
            if raw:
                total += int(raw.split()[0])
        return total

    def run(self) -> None:
        import signal

        budget_ns = self.fraction * self.period * 1e9
        others = _not_cpu0()
        if others and hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, others)  # keep the monitor off the throttled CPU
        try:
            while not self._stop_ev.is_set():
                start, base, stopped = time.monotonic(), self.cpu_ns(), False
                while time.monotonic() - start < self.period and not self._stop_ev.is_set():
                    if not stopped and self.cpu_ns() - base >= budget_ns:
                        os.kill(self.pid, signal.SIGSTOP)
                        stopped = True
                    time.sleep(self.tick)
                self.periods += 1
                if stopped:
                    self.throttled_periods += 1
                    os.kill(self.pid, signal.SIGCONT)
        except ProcessLookupError:
            return
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGCONT)

    def stop(self) -> None:
        self._stop_ev.set()
        self.join(5)


@dataclass
class ApiServer:
    proc: subprocess.Popen[bytes]
    base_url: str
    pinned_cpu0: bool
    workdir: str
    log_path: Path
    throttle: CpuThrottle | None = None

    def status_kib(self) -> dict[str, int]:
        return proc_status_kib(self.proc.pid)


@contextlib.contextmanager
def api_server(
    settings: Any, *, pin: bool = True, cpu_fraction: float | None = None, startup_timeout: float = 60.0
) -> Iterator[ApiServer]:
    """Run ``uvicorn reva.api.app:app`` (one worker, as production) in a subprocess; optionally pinned to
    CPU 0 and throttled to ``cpu_fraction`` of that CPU after it is live."""
    import httpx

    port = _free_port()
    workdir = tempfile.mkdtemp(prefix="reva_perf_api_")  # no .env in cwd: config comes from env only
    log_path = Path(workdir) / "api.log"
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "reva.api.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "1",
        "--no-access-log",
        "--log-level",
        "warning",
        "--no-server-header",
    ]
    taskset = shutil.which("taskset")
    use_pin = bool(pin and taskset and _cpu0())
    if use_pin and taskset:
        cmd = [taskset, "-c", str(sorted(_cpu0() or {0})[0]), *cmd]
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(cmd, cwd=workdir, env=api_child_env(settings), stdout=logf, stderr=subprocess.STDOUT)
    base_url = f"http://127.0.0.1:{port}"
    server = ApiServer(proc, base_url, use_pin, workdir, log_path)
    try:
        deadline = time.monotonic() + startup_timeout
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"API exited with {proc.returncode}: {log_path.read_text()[-2000:]}")
            try:
                if httpx.get(base_url + "/health/live", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"API did not become live: {log_path.read_text()[-2000:]}")
            time.sleep(0.2)
        if cpu_fraction is not None and cpu_fraction < 1:
            server.throttle = CpuThrottle(proc.pid, cpu_fraction)
            server.throttle.start()
        yield server
    finally:
        if server.throttle is not None:
            server.throttle.stop()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(workdir, ignore_errors=True)


def provenance_estimate_id(env: PerfEnv) -> str:
    from sqlalchemy import text

    with env.owner.connect() as c:
        eid = c.execute(
            text(
                """SELECT e.id FROM belief.estimate e JOIN ontology.concept_version cv ON cv.id = e.concept_version_id
               WHERE e.release_id = :r AND cv.concept_id = :c ORDER BY e.id LIMIT 1"""
            ),
            {"r": env.release_id, "c": POP},
        ).scalar_one()
    return str(eid)


def endpoint_mix(estimate_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """(ordinary, cached-metadata) endpoint name -> path."""
    ordinary = {
        "entities": "/v1/entities?limit=50",
        "beliefs_population": f"/v1/beliefs?concept={POP}",
        "beliefs_crude_imports": f"/v1/beliefs?concept={CRUDE_M}",
        "timeseries": f"/v1/timeseries?subject_id=FRA&concept={POP}",
        "locations": "/v1/locations",
        "sources": "/v1/sources",
        "coverage": "/v1/coverage",
        "gaps": "/v1/gaps",
        "provenance_estimate": f"/v1/provenance/estimate/{estimate_id}",
    }
    cached = {"ontology": "/v1/ontology", "sources": "/v1/sources"}
    return ordinary, cached


def drive_load(
    base_url: str, token: str, paths: dict[str, str], *, clients: int, requests_per_client: int, seed: int = 7
) -> dict[str, Any]:
    """Closed-loop load: ``clients`` concurrent users, each issuing requests back to back (no think time)
    over a shuffled cycle of the endpoint mix. Latency = full request/response incl. body read."""
    import httpx

    names = list(paths)
    samples: dict[str, list[float]] = {n: [] for n in names}
    errors: list[dict[str, Any]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(clients)

    def user(i: int) -> None:
        rng = random.Random(seed * 1000 + i)
        seq: list[str] = []
        while len(seq) < requests_per_client:
            cyc = names[:]
            rng.shuffle(cyc)
            seq += cyc
        seq = seq[:requests_per_client]
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        with httpx.Client(base_url=base_url, headers=headers, timeout=60) as cl:
            barrier.wait()
            for n in seq:
                t = time.perf_counter()
                try:
                    r = cl.get(paths[n])
                    _ = r.content
                    ok, status = r.status_code == 200, r.status_code
                    detail = None if ok else r.text[:300]
                except httpx.HTTPError as e:
                    ok, status, detail = False, None, type(e).__name__
                dt = (time.perf_counter() - t) * 1000
                with lock:
                    samples[n].append(dt)
                    if not ok:
                        errors.append({"endpoint": n, "status": status, "detail": detail})

    t0 = time.perf_counter()
    threads = [threading.Thread(target=user, args=(i,)) for i in range(clients)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = time.perf_counter() - t0
    allv = [v for vs in samples.values() for v in vs]
    return {
        "clients": clients,
        "requests_per_client": requests_per_client,
        "requests": len(allv),
        "wall_s": round(wall, 3),
        "throughput_rps": round(len(allv) / wall, 2),
        "overall": summarize_ms(allv),
        "per_endpoint": {n: {"path": paths[n], **summarize_ms(v)} for n, v in samples.items() if v},
        "errors": errors[:20],
        "error_count": len(errors),
    }


def warm_up(base_url: str, token: str, paths: dict[str, str]) -> dict[str, Any]:
    """One sequential request per endpoint (connection pool, imports, plan caches); not counted."""
    import httpx

    out = {}
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=60) as cl:
        for n, p in paths.items():
            t = time.perf_counter()
            r = cl.get(p)
            out[n] = {
                "status": r.status_code,
                "ms": round((time.perf_counter() - t) * 1000, 3),
                "bytes": len(r.content),
            }
    return out


def bench_api(
    env: PerfEnv,
    *,
    requests_per_client: int = 60,
    cached_requests_per_client: int = 40,
    pin: bool = True,
    cpu_fraction: float | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    token = token or create_api_token(env)
    ordinary, cached = endpoint_mix(provenance_estimate_id(env))
    with api_server(env.settings, pin=pin, cpu_fraction=cpu_fraction) as srv:
        rss_after_start = srv.status_kib()
        with pinned(_not_cpu0() if srv.pinned_cpu0 else None) as client_cpus:
            first = warm_up(srv.base_url, token, {**ordinary, **cached})
            bad = {n: w for n, w in first.items() if w["status"] != 200}
            if bad:
                raise RuntimeError(f"endpoints not healthy before load: {bad}")
            load = drive_load(
                srv.base_url, token, ordinary, clients=CONCURRENT_USERS, requests_per_client=requests_per_client
            )
            cached_load = drive_load(
                srv.base_url,
                token,
                cached,
                clients=CONCURRENT_USERS,
                requests_per_client=cached_requests_per_client,
                seed=11,
            )
        rss_end = srv.status_kib()
        thr = srv.throttle
        throttle_info = (
            None
            if thr is None
            else {
                "cpu_fraction": thr.fraction,
                "period_s": thr.period,
                "periods": thr.periods,
                "throttled_periods": thr.throttled_periods,
                "method": "SIGSTOP/SIGCONT quota emulation over /proc/<pid>/task/*/schedstat (not a cgroup)",
            }
        )
        log_tail = srv.log_path.read_text(errors="replace")[-1500:] if srv.log_path.exists() else ""
    return {
        "server": {
            "command": "uvicorn reva.api.app:app --workers 1 (subprocess)",
            "pinned_to_cpu0_with_taskset": srv.pinned_cpu0,
            "cpu_throttle": throttle_info,
            "load_generator_cpus": sorted(client_cpus) if client_cpus else None,
            "db_pool_size": 2,
            "db_max_overflow": 0,
            "rss_after_start_mib": round(rss_after_start.get("VmRSS", 0) / 1024, 1),
            "rss_end_mib": round(rss_end.get("VmRSS", 0) / 1024, 1),
            "peak_rss_vmhwm_mib": round(rss_end.get("VmHWM", 0) / 1024, 1),
            "log_tail": log_tail,
        },
        "first_request_per_endpoint": first,
        "ordinary": load,
        "cached_metadata": cached_load,
        "cached_metadata_note": (
            "/v1/ontology and /v1/sources are read from PostgreSQL on every request; the API "
            "has no application-level cache for them, so these numbers are uncached "
            "server-side latency."
        ),
    }


# =========================================================================== queue acknowledgement
def bench_queue(
    env: PerfEnv, *, jobs_n: int = 40, poll_interval: float = 0.25, pin: bool = True, timeout_s: float = 120.0
) -> dict[str, Any]:
    """Enqueue (committed) -> claim -> handler start with the real Worker loop in a thread and a no-op
    handler substituted for the ``health`` job type (restored afterwards)."""
    from reva.adapters.postgres import queue
    from reva.operations import jobs
    from reva.operations.worker import Worker

    started: dict[str, float] = {}
    started_ev: dict[str, threading.Event] = {}
    real_resolve = jobs.resolve_handler

    def noop(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        started[str(ctx.job_id)] = time.monotonic()
        ev = started_ev.get(str(ctx.job_id))
        if ev is not None:
            ev.set()
        return {"status": "ok", "perf_noop": True}

    def resolve(job_type: str) -> Callable[..., dict[str, Any]]:
        return noop if job_type == "health" else real_resolve(job_type)

    jobs.resolve_handler = resolve
    w = Worker(
        env.settings,
        env.worker,
        env.store,
        env.sim,
        lease_seconds=60,
        heartbeat_seconds=5,
        poll_interval=poll_interval,
        scheduler=False,
        job_types=["health"],
    )
    enq_to_start: list[float] = []
    enq_commit: list[float] = []
    rng = random.Random(3)
    try:
        with pinned(_cpu0() if pin else None) as cpus:
            th = threading.Thread(target=w.run, name="perf-worker", daemon=True)
            th.start()
            time.sleep(poll_interval * 2)  # let the loop reach steady polling
            for i in range(jobs_n):
                time.sleep(rng.uniform(0, poll_interval * 1.2))  # sample all phases of the poll cycle
                t_begin = time.monotonic()
                with env.worker.begin() as c:
                    jid = queue.enqueue(
                        c, "health", {"perf": i}, f"perf-ack-{uuid.uuid4().hex}", created_by="perf:benchmark"
                    )
                    started_ev[str(jid)] = threading.Event()
                t_commit = time.monotonic()
                enq_commit.append((t_commit - t_begin) * 1000)
                if not started_ev[str(jid)].wait(timeout_s):
                    raise RuntimeError(f"job {jid} was not started within {timeout_s}s")
                enq_to_start.append((started[str(jid)] - t_commit) * 1000)
                # wait for completion so the next job measures an idle worker (interactive case)
                deadline = time.monotonic() + timeout_s
                while len(w.outcomes) < i + 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
            w.request_stop("perf benchmark done")
            th.join(30)
    finally:
        jobs.resolve_handler = real_resolve
        w.request_stop("perf benchmark cleanup")
    statuses: dict[str, int] = {}
    for o in w.outcomes:
        statuses[o.status] = statuses.get(o.status, 0) + 1
    return {
        "jobs": jobs_n,
        "poll_interval_s": poll_interval,
        "worker": "reva.operations.worker.Worker.run (thread), concurrency=1, scheduler off, job_types=[health]",
        "pinned_cpus": sorted(cpus) if cpus else None,
        "enqueue_commit_to_handler_start": summarize_ms(enq_to_start),
        "claim_to_handler_start": summarize_ms([a * 1000 for a in w.ack_latencies]),
        "enqueue_transaction": summarize_ms(enq_commit),
        "outcomes": statuses,
    }


# =========================================================================== population 100k
AGES = ["Y_LT1", *[f"Y{k}" for k in range(1, 100)], "Y_OPEN", "UNK"]


def _france_like_cells() -> list[Any]:
    """Synthetic France-like age x sex leaf set (demo_pjan layout; values are random, not observed)."""
    import numpy as np

    from reva.population import SourceCell

    rng = np.random.default_rng(2024)
    cells = []
    for sex in ("F", "M"):
        counts = rng.integers(300_000, 370_000, size=len(AGES))
        for age, c in zip(AGES, counts, strict=True):
            cells.append(SourceCell(age, sex, int(c), ("p",) if age == "UNK" else (), f"{sex}.{age}"))
    total = sum(c.count or 0 for c in cells)
    cells.append(SourceCell("TOTAL", "T", total, (), "T.TOTAL"))
    return cells


def population_child(records: int) -> dict[str, Any]:
    """Runs inside the measured child process."""
    import resource

    t_imp = time.perf_counter()
    from reva.population import (
        build_recipe,
        diagnose,
        exact_total_weight,
        materialise,
        table_logical_hash,
        to_parquet_bytes,
    )

    t0 = time.perf_counter()
    recipe = build_recipe(
        _france_like_cells(),
        geography="FR-like-synthetic",
        reference_date=date(2024, 1, 1),
        source_ref={"dataset": "demo_pjan", "fixture": "synthetic"},
        seed=11,
    )
    t1 = time.perf_counter()
    table = materialise(recipe, records)
    t2 = time.perf_counter()
    exact = exact_total_weight(table) == recipe.total_count
    digest = table_logical_hash(table)
    t3 = time.perf_counter()
    data = to_parquet_bytes(table)
    t4 = time.perf_counter()
    diag = diagnose(table, recipe)
    t5 = time.perf_counter()
    st = proc_status_kib(os.getpid())
    return {
        "records": table.num_rows,
        "exact_total_weight": exact,
        "calibration_max_abs_error": diag["calibration_error"]["joint_age_sex"]["max_abs_error"],
        "stages_s": {
            "import": round(t0 - t_imp, 3),
            "recipe": round(t1 - t0, 3),
            "materialise": round(t2 - t1, 3),
            "table_hash": round(t3 - t2, 3),
            "parquet": round(t4 - t3, 3),
            "diagnose": round(t5 - t4, 3),
        },
        "compute_s": round(t5 - t0, 3),
        "parquet_bytes": len(data),
        "hash_prefix": digest[:12],
        "self_ru_maxrss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "self_vmhwm_mib": round(st.get("VmHWM", 0) / 1024, 1),
        "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
    }


def bench_population(records: int = 100_000, *, pin: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "_population-child", "--records", str(records)]
    taskset = shutil.which("taskset")
    use_pin = bool(pin and taskset and _cpu0())
    if use_pin and taskset:
        cmd = [taskset, "-c", str(sorted(_cpu0() or {0})[0]), *cmd]
    env = {
        **os.environ,
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "POLARS_MAX_THREADS": "1",
        "REVA_CPU_BUDGET_THREADS": "1",
    }
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(REPO))
    # wait4 on this specific child: its own rusage (ru_maxrss in KiB on Linux), unaffected by other children.
    _, status, ru = os.wait4(proc.pid, 0)
    wall = time.perf_counter() - t0
    stdout = proc.stdout.read().decode() if proc.stdout else ""
    stderr = proc.stderr.read().decode() if proc.stderr else ""
    proc.returncode = os.waitstatus_to_exitcode(status)
    if proc.returncode != 0:
        raise RuntimeError(f"population child failed ({proc.returncode}): {stderr[-2000:]}")
    child = json.loads(stdout.strip().splitlines()[-1])
    return {
        "records": records,
        "pinned_with_taskset": use_pin,
        "process_wall_s": round(wall, 3),
        "process_wall_includes": "interpreter start + imports + recipe + materialise + hash + parquet + diagnose",
        "child_peak_rss_mib": round(ru.ru_maxrss / 1024, 1),
        "child_user_cpu_s": round(ru.ru_utime, 3),
        "child_sys_cpu_s": round(ru.ru_stime, 3),
        "child": child,
    }


# =========================================================================== evaluation & report
def evaluate(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}

    def add(key: str, value: float | None, how: str, suffix: str = "") -> None:
        lim = TARGETS[key]["limit"]
        checks[key + suffix] = {
            "target": TARGETS[key]["text"],
            "limit": lim,
            "value": value,
            "measured_as": how,
            "status": "not_measured" if value is None else ("pass" if value < lim else "FAIL"),
        }

    sim = results.get("simulation")
    add(
        "l1_four_country_ms",
        sim["L1"]["engine_with_spec_validation"]["p95_ms"] if sim else None,
        "p95 of the in-memory four-country L1 engine path incl. spec validation (warm)",
    )
    for res_key, suffix, label in (
        ("api", "", "API pinned to 1 full CPU"),
        ("api_half_cpu", "@0.5cpu", f"API at {API_PLAN_CPU} CPU (emulated quota)"),
    ):
        api = results.get(res_key)
        if api is None and res_key == "api_half_cpu":
            continue
        add(
            "ordinary_api_p95_ms",
            api["ordinary"]["overall"]["p95_ms"] if api else None,
            f"p95 over all requests of the ordinary endpoint mix, 5 closed-loop concurrent clients; {label}",
            suffix,
        )
        add(
            "cached_metadata_p95_ms",
            api["cached_metadata"]["overall"]["p95_ms"] if api else None,
            f"p95 over /v1/ontology + /v1/sources, 5 closed-loop concurrent clients; {label}",
            suffix,
        )
    q = results.get("queue")
    add(
        "queue_ack_p95_ms",
        q["enqueue_commit_to_handler_start"]["p95_ms"] if q else None,
        "p95 enqueue-commit -> handler start (real Worker loop, 0.25 s poll)",
    )
    pop = results.get("population")
    add(
        "population_100k_wall_s",
        pop["process_wall_s"] if pop else None,
        "whole child process wall time incl. interpreter start and imports, pinned to 1 CPU",
    )
    add("population_100k_peak_rss_mib", pop["child_peak_rss_mib"] if pop else None, "child ru_maxrss from wait4")
    return checks


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.3f}" if v < 10 else f"{v:,.1f}"
    return str(v)


def render_report(results: dict[str, Any]) -> str:
    hw = results["hardware"]
    checks = results["checks"]
    L: list[str] = []
    L.append("# Performance measurements (architecture §20.3 targets)")
    L.append("")
    L.append(
        f"Generated by `scripts/measure_performance.py` at {results['generated_at']} "
        f"(machine-readable copy: `var/perf/perf_results.json`). **Single run on a development "
        "container, not on the Render reference plans.** Regenerate with "
        "`uv run python scripts/measure_performance.py`."
    )
    L.append("")
    L.append("## Results against targets")
    L.append("")
    L.append("| Target | Measured | Limit | Status | Measured as |")
    L.append("|---|---:|---:|---|---|")
    for c in checks.values():
        L.append(f"| {c['target']} | {_fmt(c['value'])} | {_fmt(c['limit'])} | {c['status']} | {c['measured_as']} |")
    L.append("")
    L.append("Units: rows ending in `_ms` are milliseconds, `wall_s` seconds, `rss` MiB.")
    L.append("")
    L.append("## Hardware actually used")
    L.append("")
    L.append(
        f"- CPU: {hw.get('cpu_model')} — nproc {hw.get('nproc')}, affinity {hw.get('affinity_cpus')}, "
        f"cgroup cpu.max `{hw.get('cgroup_cpu_max')}`"
    )
    L.append(f"- Memory: {hw.get('mem_total_mib')} MiB total, cgroup memory.max `{hw.get('cgroup_memory_max')}`")
    L.append(f"- Kernel {hw.get('kernel')}, Python {hw.get('python')}, PostgreSQL: {hw.get('postgres')}")
    L.append(
        f"- Load average at start {hw.get('loadavg_at_start')}, at end {hw.get('loadavg_at_end')} "
        "(other agents/processes share this machine)"
    )
    L.append(f"- taskset: {hw.get('taskset') or 'not available'}")
    L.append("")
    sim = results.get("simulation")
    if sim:
        L.append("## L1/L2 four-country numerical benchmark")
        L.append("")
        L.append(
            f"Fixture `{sim['fixture']}` — origin `{sim['fixture_origin']}`, label "
            f"“{sim['fixture_label']}”. Synthetic values, never ingested; countries {sim['countries']}; "
            f"L2 grid {sim['l2_grid_points']} points. {sim['repeats']} timed repetitions per tier after one "
            f"warm-up call, measuring process pinned to CPUs {sim.get('pinned_cpus')}. Cold first call "
            f"(includes lazy imports and schema file read): {_fmt(sim['cold_first_call_l1_ms'])} ms."
        )
        L.append("")
        L.append("| Fidelity | Tier | p50 ms | p95 ms | max ms |")
        L.append("|---|---|---:|---:|---:|")
        for fid in ("L1", "L2"):
            for tier in ("model_core", "engine", "engine_with_spec_validation"):
                s = sim[fid][tier]
                L.append(f"| {fid} | {tier} | {_fmt(s['p50_ms'])} | {_fmt(s['p95_ms'])} | {_fmt(s['max_ms'])} |")
        L.append("")
        for tier, desc in sim["tiers"].items():
            L.append(f"- `{tier}`: {desc}")
        L.append(
            f"- L1 rows {sim['L1']['result_rows']}, L2 rows {sim['L2']['result_rows']} "
            f"(variants {len(sim['L2']['variants'])}); excluded: DB reads of baseline estimates, "
            "queue, result publication."
        )
        L.append("")
    for res_key, heading in (
        ("api", "API latency (5 concurrent users) — API pinned to one full CPU"),
        ("api_half_cpu", f"API latency (5 concurrent users) — API at {API_PLAN_CPU} CPU (emulated Render plan quota)"),
    ):
        api = results.get(res_key)
        if not api:
            continue
        srv = api["server"]
        L.append(f"## {heading}")
        L.append("")
        L.append(
            f"Fresh database `{results.get('database', {}).get('name')}` (bootstrap + seed + captured "
            f"fixtures for {', '.join(FIXTURE_CONNECTORS)} + `publish_release(origin=test_fixture)`, "
            f"{results.get('database', {}).get('estimates')} estimates). One Uvicorn worker in a subprocess, "
            f"pinned to CPU 0 with taskset: {srv['pinned_to_cpu0_with_taskset']}; load generator CPUs "
            f"{srv['load_generator_cpus']}; DB pool {srv['db_pool_size']}+{srv['db_max_overflow']} as in "
            "render.yaml. Bearer API token (read scope). Closed loop, no think time, warm-up excluded."
        )
        thr = srv.get("cpu_throttle")
        if thr:
            L.append(
                f"CPU quota emulation: {thr['cpu_fraction']} CPU per {thr['period_s']} s period, "
                f"{thr['throttled_periods']}/{thr['periods']} periods throttled; {thr['method']}."
            )
        L.append("")
        for title, key in (("Ordinary endpoint mix", "ordinary"), ("Cached metadata endpoints", "cached_metadata")):
            d = api[key]
            L.append(
                f"### {title}: {d['requests']} requests in {d['wall_s']} s "
                f"({d['throughput_rps']} req/s), errors {d['error_count']}"
            )
            L.append("")
            L.append("| Endpoint | n | p50 ms | p95 ms | max ms |")
            L.append("|---|---:|---:|---:|---:|")
            for n, s in d["per_endpoint"].items():
                L.append(
                    f"| `{s['path']}` ({n}) | {s['n']} | {_fmt(s['p50_ms'])} | {_fmt(s['p95_ms'])} | "
                    f"{_fmt(s['max_ms'])} |"
                )
            o = d["overall"]
            L.append(f"| **overall** | {o['n']} | {_fmt(o['p50_ms'])} | {_fmt(o['p95_ms'])} | {_fmt(o['max_ms'])} |")
            L.append("")
        L.append(
            f"API process RSS: {srv['rss_after_start_mib']} MiB after start, {srv['rss_end_mib']} MiB at end, "
            f"peak (VmHWM) {srv['peak_rss_vmhwm_mib']} MiB (Render API plan: 512 MB)."
        )
        L.append("")
        L.append(f"Note: {api['cached_metadata_note']}")
        L.append("")
    q = results.get("queue")
    if q:
        L.append("## Worker queue acknowledgement")
        L.append("")
        L.append(
            f"{q['jobs']} `health` jobs enqueued one at a time at random offsets within the poll cycle; "
            f"{q['worker']}; poll interval {q['poll_interval_s']} s; no-op handler substituted in-process "
            f"(restored afterwards); pinned CPUs {q['pinned_cpus']}. Outcomes: {q['outcomes']}."
        )
        L.append("")
        L.append("| Interval | p50 ms | p95 ms | max ms |")
        L.append("|---|---:|---:|---:|")
        for k in ("enqueue_commit_to_handler_start", "claim_to_handler_start", "enqueue_transaction"):
            s = q[k]
            L.append(f"| {k} | {_fmt(s['p50_ms'])} | {_fmt(s['p95_ms'])} | {_fmt(s['max_ms'])} |")
        L.append("")
    pop = results.get("population")
    if pop:
        ch = pop["child"]
        L.append("## Population: 100k demographic records")
        L.append("")
        L.append(
            f"Synthetic France-like age×sex leaf set (demo_pjan layout, random counts — not observed data), "
            f"`reva.population` recipe → materialise {pop['records']:,} records → logical hash → Parquet → "
            f"diagnostics, in a child process pinned with taskset: {pop['pinned_with_taskset']} "
            f"(affinity seen by child {ch.get('affinity')})."
        )
        L.append("")
        L.append(
            f"- Process wall time {pop['process_wall_s']} s ({pop['process_wall_includes']}); in-process compute "
            f"{ch['compute_s']} s; stages {ch['stages_s']}"
        )
        L.append(
            f"- Peak RSS {pop['child_peak_rss_mib']} MiB (wait4 ru_maxrss; child VmHWM {ch['self_vmhwm_mib']} "
            f"MiB); CPU user {pop['child_user_cpu_s']} s, sys {pop['child_sys_cpu_s']} s"
        )
        L.append(
            f"- Exact total weight {ch['exact_total_weight']}; joint age×sex calibration max abs error "
            f"{ch['calibration_max_abs_error']}; Parquet {ch['parquet_bytes']:,} bytes"
        )
        L.append("")
    L.append("## Caveats (read before citing these numbers)")
    L.append("")
    L.append(
        "- **Not the reference hardware.** Render plans are API 0.5 CPU/512 MB, worker 1 CPU/2 GB and "
        "PostgreSQL 0.1 CPU/256 MB. Here `taskset -c 0` gives the API and the measured processes one *full* "
        "core; the API is additionally measured under an emulated 0.5 CPU quota (SIGSTOP/SIGCONT, not a "
        "cgroup, so throttling granularity differs from Render's), memory is not capped (no cgroup limit "
        "was applied; the 2 GiB budget is checked only against measured peak RSS), and PostgreSQL runs in "
        "a local Docker container with unconstrained CPU on the same host and no network latency. Expect "
        "slower API/DB numbers on Render."
    )
    L.append(
        "- **Single run.** Percentiles come from one measurement session on a shared machine where other "
        "processes (parallel agents, test suites) were running; see the load averages above."
    )
    L.append(
        "- **Small data.** The database holds only captured test fixtures (four countries, few concepts), "
        "so query latencies do not reflect a larger live corpus."
    )
    L.append(
        "- **Cached metadata is not cached.** No application/HTTP cache exists for `/v1/ontology` or "
        "`/v1/sources`; the measured values are server-side uncached latency (an upper bound for a cached path)."
    )
    L.append(
        "- **Four-country numbers are synthetic.** The labelled benchmark fixture is used only for the "
        "numerical timing; the live scenario uses the three verified countries (DZA crude flows are typed "
        "Missing in live data)."
    )
    L.append(
        "- **Queue acknowledgement** is measured for an idle worker with concurrency 1; with a long job "
        "occupying the only slot, acknowledgement waits for that job (by design)."
    )
    L.append("")
    return "\n".join(L) + "\n"


def postgres_version(env: PerfEnv | None) -> str | None:
    if env is None:
        return None
    from sqlalchemy import text

    with env.owner.connect() as c:
        v = c.execute(text("SELECT version()")).scalar_one()
        pg = c.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")).scalar_one_or_none()
    return f"{str(v).split(',')[0]}; PostGIS {pg}"


def run_all(
    only: set[str],
    *,
    requests_per_client: int,
    cached_requests_per_client: int,
    queue_jobs: int,
    sim_repeats: int,
    records: int,
    keep_db: bool,
) -> dict[str, Any]:
    results: dict[str, Any] = {
        "schema": "reva.perf_results.v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "targets": TARGETS,
        "hardware": hardware_info(),
        "single_run": True,
        "reference_hardware": False,
    }
    if "simulation" in only:
        print("[perf] simulation: four-country L1/L2 benchmark ...", flush=True)
        results["simulation"] = bench_simulation(sim_repeats)
    if "population" in only:
        print(f"[perf] population: {records:,} records in a pinned child ...", flush=True)
        results["population"] = bench_population(records)
    if only & {"api", "queue"}:
        print("[perf] building fresh database (bootstrap + seed + fixture ingest + release) ...", flush=True)
        with perf_database(keep=keep_db) as env:
            results["database"] = {
                "name": env.name,
                "estimates": env.estimates,
                "setup_s": round(env.setup_seconds, 3),
                "timings": env.timings,
                "kept": keep_db,
            }
            results["hardware"]["postgres"] = postgres_version(env)
            if "api" in only:
                token = create_api_token(env)
                print("[perf] api: 5 concurrent clients, API pinned to 1 CPU ...", flush=True)
                results["api"] = bench_api(
                    env,
                    requests_per_client=requests_per_client,
                    cached_requests_per_client=cached_requests_per_client,
                    token=token,
                )
                print(f"[perf] api: 5 concurrent clients, API at {API_PLAN_CPU} CPU (emulated) ...", flush=True)
                results["api_half_cpu"] = bench_api(
                    env,
                    requests_per_client=requests_per_client,
                    cached_requests_per_client=cached_requests_per_client,
                    cpu_fraction=API_PLAN_CPU,
                    token=token,
                )
            if "queue" in only:
                print("[perf] queue: enqueue -> handler start ...", flush=True)
                results["queue"] = bench_queue(env, jobs_n=queue_jobs)
    results["hardware"]["loadavg_at_end"] = list(os.getloadavg())
    results["checks"] = evaluate(results)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    child = sub.add_parser("_population-child", help=argparse.SUPPRESS)
    child.add_argument("--records", type=int, default=100_000)
    ap.add_argument(
        "--only",
        default="simulation,api,queue,population",
        help="comma-separated subset of simulation,api,queue,population",
    )
    ap.add_argument("--requests-per-client", type=int, default=80)
    ap.add_argument("--cached-requests-per-client", type=int, default=60)
    ap.add_argument("--queue-jobs", type=int, default=40)
    ap.add_argument("--sim-repeats", type=int, default=300)
    ap.add_argument("--records", type=int, default=100_000)
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--keep-db", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "_population-child":
        print(json.dumps(population_child(args.records)))
        return 0
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    unknown = only - {"simulation", "api", "queue", "population"}
    if unknown:
        ap.error(f"unknown measurement(s): {sorted(unknown)}")
    results = run_all(
        only,
        requests_per_client=args.requests_per_client,
        cached_requests_per_client=args.cached_requests_per_client,
        queue_jobs=args.queue_jobs,
        sim_repeats=args.sim_repeats,
        records=args.records,
        keep_db=args.keep_db,
    )
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"[perf] wrote {args.json}")
    if not args.no_report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_report(results))
        print(f"[perf] wrote {args.report}")
    for k, c in results["checks"].items():
        print(f"[perf] {c['status']:>12}  {k}: {c['target']}: {_fmt(c['value'])} (limit {_fmt(c['limit'])})")
    return 1 if any(c["status"] == "FAIL" for c in results["checks"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
