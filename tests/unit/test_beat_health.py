"""V7 BEAT-HEALTH: frozen stacking / recalibration of the hub ensembles (pure parts + pre-registration guards)."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from reva.cli import beat_health as CLI
from reva.evaluation.health_registry import beat as B
from reva.models.forecasting import health_quantile as HQ

LV = np.array(HQ.HUB_LEVELS)


def _q(center: float, spread: float) -> list[float]:
    return list(np.maximum(center + spread * (LV - 0.5) * 4, 0.0))


def _rows(n_rounds: int, n_geo: int = 60, *, ens_spread: float = 1.0) -> tuple[list[dict[str, Any]], dict[Any, Any]]:
    """Synthetic hub panel: truth 100 everywhere, ensemble centred at 100 (spread ``ens_spread``), blend at 130."""
    rows, vint = [], {}
    for r in range(n_rounds):
        ref = date.fromordinal(date(2025, 1, 4).toordinal() + 7 * r).isoformat()
        last = 1000 + r
        vint[date.fromisoformat(ref)] = {f"g{g}": {last: 100.0} for g in range(n_geo)}
        for g in range(n_geo):
            hist = [(i, 100.0) for i in range(990, last + 1)]
            for h in range(2):
                rows.append(
                    {
                        "ref": ref,
                        "geo": f"g{g}",
                        "h": h,
                        "target": last + 1 + h,
                        "history": hist,
                        "truth": 100.0,
                        "ens": _q(100.0, ens_spread),
                        "blend": _q(130.0, 1.0),
                    }
                )
    return rows, vint


def test_vectorised_wis_equals_the_registry_wis() -> None:
    rng = np.random.default_rng(1)
    q = np.sort(rng.gamma(2.0, 50.0, size=(5, 23)), axis=1)
    y = rng.gamma(2.0, 50.0, size=5)
    ref = [HQ.wis(HQ.HUB_LEVELS, list(q[i]), float(y[i])) for i in range(5)]
    assert np.allclose(B.wis_rows(q, y), ref)


def test_holm_counts_missing_tests_in_the_family() -> None:
    adj = B.holm({"a": 0.01, "b": 0.04, "c": None})
    assert adj["a"] == pytest.approx(0.03)
    assert adj["b"] == pytest.approx(0.08)
    assert adj["c"] == 1.0


def test_past_uses_only_values_published_in_the_rounds_vintage() -> None:
    rows, vint = _rows(3)
    p = B.build_panel(rows, vint)
    idx, ys = B.past_scored(p, 2)
    # round 2's as-of history ends at week 1002: targets 1001/1002 are scored, round 1 h=1 (week 1003) is not
    assert {(int(p.r[j]), int(p.h[j])) for j in idx} == {(0, 0), (0, 1), (1, 0)}
    assert set(ys) == {100.0}
    assert len(B.past_scored(p, 0)[0]) == 0


def test_candidates_equal_the_ensemble_until_enough_past_and_then_learn() -> None:
    rows, vint = _rows(12, ens_spread=3.0)
    p = B.build_panel(rows, vint)
    fc, chosen = B.run_candidates(p)
    first = p.r == 0
    assert np.allclose(fc["beat.qa_blend"][first], p.ens[first])
    assert np.allclose(fc["beat.width_recal"][first], p.ens[first])
    late = [c for c in chosen if c["n_past"] >= B.MIN_PAST]
    assert late and all(c["qa_weight_ensemble"] == 1.0 for c in late)  # blend is biased: weight stays on ensemble
    assert all(c["width"] == min(B.WIDTH_GRID) for c in late)  # truth always at the median: narrowest width wins


def test_first_release_is_the_earliest_vintage_value() -> None:
    v = {date(2025, 1, 4): {"US": {5: 10.0}}, date(2025, 1, 11): {"US": {5: 12.0, 6: 3.0}}}
    assert B.first_release(v) == {("US", 5): 10.0, ("US", 6): 3.0}


def test_verdict_requires_lower_wis_negative_mean_and_holm() -> None:
    ok = {"relative_wis_vs_ensemble": 0.9, "dm_vs_ensemble": {"status": "ok", "mean_diff": -1.0, "p_value": 0.001}}
    weak = {"relative_wis_vs_ensemble": 0.99, "dm_vs_ensemble": {"status": "ok", "mean_diff": -0.1, "p_value": 0.03}}
    v = B.verdicts({"a": ok, "b": weak, "c": {"status": "not_run"}}, 0.05)
    assert v["family_size"] == 3
    assert v["tests"]["a"]["win"] is True
    assert v["tests"]["b"]["win"] is False  # 0.03 * 2 > 0.05 after Holm
    assert v["tests"]["c"]["holm_p"] == 1.0


def test_protocol_freezes_the_module_and_declares_one_family() -> None:
    proto = yaml.safe_load(CLI.PROTOCOL.read_text(encoding="utf-8"))
    sha = hashlib.sha256(Path(B.__file__).read_bytes()).hexdigest()
    assert proto["frozen_code"]["module_sha256"] == sha, "beat.py changed after pre-registration"
    fam = proto["decision_rule"]["family"]
    assert len(fam) == len(set(fam)) == 6
    assert set(proto["candidates"]) == set(B.CANDIDATES)
    assert proto["holdouts"]["H1"]["evaluate_not_before"] > proto["registered_on"]


def test_holdout_is_refused_before_its_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CLI, "load_family", lambda key: pytest.fail("data must not be read"))
    with pytest.raises(SystemExit, match="holdout H1 may be evaluated once"):
        CLI.evaluate("holdout")
