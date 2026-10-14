"""Performance: worker queue acknowledgement < 1 s (architecture §20.3).

Measures enqueue commit -> claim -> handler start with the real Worker loop (thread,
concurrency 1, default 0.25 s poll) and a no-op handler substituted for the ``health``
job type through ``reva.operations.jobs.resolve_handler``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from reva.operations import jobs

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


def test_queue_acknowledgement_under_1s(db: Any, tmp_path_factory: pytest.TempPathFactory,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness()
    # The harness swaps resolve_handler itself and restores it; monkeypatch guarantees restoration
    # even if the harness is interrupted.
    monkeypatch.setattr(jobs, "resolve_handler", jobs.resolve_handler)
    with h.perf_database(db.settings, label="perfq", object_root=tmp_path_factory.mktemp("perf_q_objects")) as env:
        res = h.bench_queue(env, jobs_n=25)
    e2e, claim = res["enqueue_commit_to_handler_start"], res["claim_to_handler_start"]
    print(f"\n[perf] queue ack ms: enqueue->start p50={e2e['p50_ms']} p95={e2e['p95_ms']} max={e2e['max_ms']}; "
          f"claim->start p50={claim['p50_ms']} p95={claim['p95_ms']}; outcomes {res['outcomes']}")
    assert res["outcomes"] == {"succeeded": 25}
    limit = h.TARGETS["queue_ack_p95_ms"]["limit"]
    assert e2e["p95_ms"] < limit
    assert claim["p95_ms"] < limit
