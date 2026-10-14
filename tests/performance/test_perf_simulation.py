"""Performance: L1 four-country crude-exposure calculation (architecture §20.3: < 2 s excluding queue/IO).

Uses the explicitly labelled synthetic fixture tests/fixtures/benchmark/four_country_l1.json
("SYNTHETIC BENCHMARK — not observed data", origin test_fixture). The fixture only feeds the
pure in-memory numerical path; it is never ingested or published as evidence.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "benchmark" / "four_country_l1.json"


def harness() -> ModuleType:
    name = "reva_measure_performance"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "measure_performance.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def test_benchmark_fixture_is_labelled_synthetic_and_never_a_source(tmp_path: Path) -> None:
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fx["origin"] == "test_fixture"
    assert fx["label"] == "SYNTHETIC BENCHMARK — not observed data"
    assert fx["not_evidence"] is True
    assert sorted(fx["countries"]) == ["CHN", "DZA", "FRA", "USA"]
    assert all(set(v) == {"M", "X", "G"} for v in fx["countries"].values())
    assert len(fx["l2_elasticity_grid"]) == 6
    # Not reachable by any connector: captured source fixtures never reference it.
    for manifest in (REPO / "tests" / "fixtures" / "source").glob("*/manifest.json"):
        assert "benchmark" not in manifest.read_text(encoding="utf-8")
    # The harness refuses an unlabelled copy.
    bad = dict(fx, label="observed")
    p = tmp_path / "unlabelled.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="not a labelled synthetic test fixture"):
        harness().load_benchmark_fixture(p)


@pytest.mark.perf
def test_l1_four_country_calculation_under_2s() -> None:
    h = harness()
    res = h.bench_simulation(repeats=100)
    l1, l2 = res["L1"], res["L2"]
    print(
        f"\n[perf] L1 four-country p50/p95 ms: core {l1['model_core']['p50_ms']}/{l1['model_core']['p95_ms']} "
        f"engine {l1['engine']['p50_ms']}/{l1['engine']['p95_ms']} "
        f"engine+spec {l1['engine_with_spec_validation']['p50_ms']}/{l1['engine_with_spec_validation']['p95_ms']} "
        f"cold {res['cold_first_call_l1_ms']} | L2 6-pt grid engine+spec "
        f"{l2['engine_with_spec_validation']['p50_ms']}/{l2['engine_with_spec_validation']['p95_ms']} "
        f"rows L1={l1['result_rows']} L2={l2['result_rows']}"
    )
    assert l1["result_rows"] == 4 * 4  # four countries x four outputs, nothing silently dropped
    assert res["l1_cold_digest_matches_warm"]
    assert l2["result_rows"] > l1["result_rows"] and "L1" in l2["variants"]
    limit = h.TARGETS["l1_four_country_ms"]["limit"]
    assert l1["engine_with_spec_validation"]["p95_ms"] < limit
    assert l1["engine_with_spec_validation"]["max_ms"] < limit
