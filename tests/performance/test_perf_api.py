"""Performance: API latency at 5 concurrent users (architecture §20.3).

Targets: ordinary API p95 < 1 s; cached metadata p95 < 500 ms. The API runs as a real
Uvicorn subprocess (one worker) against a fresh database holding captured fixtures and a
test_fixture release, pinned to one CPU with taskset when available, and again under an
emulated 0.5 CPU quota (the Render API plan). See docs/dev/perf_report.md for caveats.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = [pytest.mark.db, pytest.mark.perf]
REPO = Path(__file__).resolve().parents[2]


def harness() -> ModuleType:
    name = "reva_measure_performance"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "measure_performance.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def perf_env(db: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    with harness().perf_database(db.settings, label="perfapi",
                                 object_root=tmp_path_factory.mktemp("perf_api_objects")) as env:
        yield env


def _check(h: ModuleType, res: dict[str, Any], label: str) -> None:
    o, c, srv = res["ordinary"], res["cached_metadata"], res["server"]
    print(f"\n[perf] API {label}: ordinary n={o['requests']} p50={o['overall']['p50_ms']} "
          f"p95={o['overall']['p95_ms']} max={o['overall']['max_ms']} ms; cached n={c['requests']} "
          f"p50={c['overall']['p50_ms']} p95={c['overall']['p95_ms']} ms; rss peak {srv['peak_rss_vmhwm_mib']} MiB")
    for n, s in o["per_endpoint"].items():
        print(f"[perf]   {n:<24} p50={s['p50_ms']:>8} p95={s['p95_ms']:>8} max={s['max_ms']:>8}")
    assert o["error_count"] == 0, o["errors"]
    assert c["error_count"] == 0, c["errors"]
    assert set(o["per_endpoint"]) == set(h.endpoint_mix("x")[0])
    assert o["overall"]["p95_ms"] < h.TARGETS["ordinary_api_p95_ms"]["limit"]
    assert c["overall"]["p95_ms"] < h.TARGETS["cached_metadata_p95_ms"]["limit"]


def test_api_latency_one_cpu(perf_env: Any) -> None:
    h = harness()
    res = h.bench_api(perf_env, requests_per_client=40, cached_requests_per_client=30)
    _check(h, res, "pinned 1 CPU")


def test_api_latency_half_cpu_quota(perf_env: Any) -> None:
    h = harness()
    res = h.bench_api(perf_env, requests_per_client=40, cached_requests_per_client=30,
                      cpu_fraction=h.API_PLAN_CPU)
    assert res["server"]["cpu_throttle"]["cpu_fraction"] == 0.5
    _check(h, res, f"{h.API_PLAN_CPU} CPU emulated")
