"""Finance back-test (V6 PRED-FIN): no look-ahead, DM-HLN + Holm, winners must beat every benchmark."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np

from reva.evaluation import finance_backtest as B


def rw_days(n_years: int, sigma: float, seed: int) -> list[tuple[date, float]]:
    rng = np.random.default_rng(seed)
    out, d, x = [], date(1998, 1, 1), math.log(1.1)
    while d < date(1998 + n_years, 1, 1):
        if d.weekday() < 5:
            out.append((d, math.exp(x)))
            x += sigma * rng.standard_normal()
        d += timedelta(days=1)
    return out


FIN: dict[str, Any] = {"kind": "fx", "scale": "log"}


def case(series: list[tuple[date, float]]) -> B.LevelCase:
    return B.LevelCase("fin.test_fx", FIN, "month_end", series, (1, 3), ("fin.rw_histvol", "fin.garch11"))


def test_level_case_has_no_look_ahead() -> None:
    s = rw_days(12, 0.006, 1)
    rows = B.run_level_case(case(s), start_year=2003)
    cut = date(2006, 12, 31)
    early = [r for r in rows if r["origin"] + r["h"] <= 2006 * 12 + 11]
    # change every value after the cut: replays whose origin and target are before it must not move
    s2 = [(d, v * (1.3 if d > cut else 1.0)) for d, v in s]
    rows2 = B.run_level_case(case(s2), start_year=2003)
    key = lambda r: (r["model"], r["h"], r["origin"])  # noqa: E731
    later = {key(r): r for r in rows2}
    assert early and all(later[key(r)]["crps"] == r["crps"] for r in early)


def test_no_candidate_wins_on_a_pure_random_walk_and_holm_is_applied() -> None:
    s = rw_days(16, 0.006, 7)
    rows = B.run_level_case(case(s), start_year=2002)
    ev = B.evaluate(rows, [])
    assert ev["winners"] == []
    t = ev["tests_primary"]
    assert t and all(x["reference"] == "fin.rw_histvol" and "holm_better" in x for x in t)
    assert {x["model"] for x in t} == {"fin.garch11"}


def test_compare_and_holm_on_a_clear_winner() -> None:
    rows = []
    rng = np.random.default_rng(0)
    for o in range(200):
        base = 1.0 + rng.random()
        for fam in ("a", "b"):
            rows.append({"family": fam, "pool": "p", "model": "fin.rw_histvol", "h": 1, "origin": o, "crps": base})
            rows.append({"family": fam, "pool": "p", "model": "fin.garch11", "h": 1, "origin": o,
                         "crps": base * (0.8 if fam == "a" else 1.0) + 0.01 * rng.standard_normal()})  # fmt: skip
    ev = B.evaluate(rows, [])
    win = {(w["family"], w["model"], w["h"]) for w in ev["winners"]}
    assert win == {("a", "fin.garch11", 1)}
    pooled = ev["pooled_informational"]
    assert pooled and pooled[0]["pool"] == "p" and pooled[0]["mean_scaled_diff"] < 0
