"""BEAT-MACRO: bridges, publication-lag rule, combination weights from past errors only, holdout opened once."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from reva.evaluation.beat_macro import cases as C
from reva.evaluation.beat_macro import combine as K
from reva.evaluation.beat_macro import inputs as I
from reva.evaluation.beat_macro import service as S
from reva.evaluation.pred_models.realtime import Case
from reva.models.forecasting.registry_baselines import Predictive


def _idx(start: int, n: int, monthly: float) -> dict[int, float]:
    return {start + k: 100.0 * (1 + monthly) ** k for k in range(n)}


def test_cpi_bridge_constant_inflation_is_exact() -> None:
    y = 2020
    idx = _idx((y - 3) * 12, 3 * 12 + 4, 0.002)  # published through April of y
    b = C.cpi_bridge_index(idx, y)
    assert b is not None and b["months_known"] == 4
    assert b["pi"] == pytest.approx(100 * ((1.002) ** 12 - 1), rel=1e-9)


def test_cpi_bridge_oil_adjustment_shifts_filled_months() -> None:
    y = 2020
    idx = _idx((y - 3) * 12, 3 * 12 + 4, 0.002)
    L = max(idx)
    oil = {m: 50.0 for m in range(L - 24, L + 1)}
    oil[L + 1] = 50.0 * 1.10  # +10 % y/y one month later -> +0.4 pp on the filled months
    oil[L + 1 - 12] = 50.0
    adj = C.oil_adjustment(oil, L)
    assert adj == pytest.approx(0.4)
    b0, b1 = C.cpi_bridge_index(idx, y), C.cpi_bridge_index(idx, y, oil)
    assert b0 is not None and b1 is not None and b1["pi"] > b0["pi"]


def test_gdp_and_unemp_bridges() -> None:
    y = 2015
    g = {q: 0.5 for q in range(y * 4 - 10, y * 4 + 1)}  # q/q 0.5 % through Q1 of y
    b = C.gdp_bridge(g, y)
    assert b is not None and b["pi"] == pytest.approx(100 * (1.005**4 - 1), rel=1e-9)
    u = {m: 5.0 for m in range((y - 2) * 12, y * 12 + 3)}
    ub = C.unemp_bridge(u, y, {y - 1: 5.5})
    assert ub is not None and ub["pi"] == pytest.approx(5.5) and ub["level_gap"] == pytest.approx(0.5)
    assert C.unemp_bridge(u, y, {}) is None  # no observed WEO year -> no level anchor -> abstain


def test_lag_rule_never_reads_unpublished_months() -> None:
    rec = datetime(2026, 10, 17, tzinfo=UTC)
    vals = {"AAA": {I.mi(datetime(2026, 9, 1)) - k: 1.0 + k for k in range(40)}}
    s = I.LaggedMonthly("x", vals, I._lags(vals, rec, 3, 2, 12), "rule", rec)
    assert s.lag["AAA"] == 3  # gap 1 -> max(3, 1 + 2)
    t = datetime(2026, 9, 1, tzinfo=UTC)
    got = s.usable("AAA", t)
    assert max(got) == I.mi(t) - 3  # June usable from 1 September
    t2 = datetime(2026, 8, 31, 23, 59, tzinfo=UTC)
    assert max(s.usable("AAA", t2)) == I.mi(t2) - 3


def test_z_transform() -> None:
    assert C.z(0.0) == 0.0
    assert C.z(-100.0) is None
    assert C.z(100.0) == pytest.approx(100 * math.log(2))


def _case(t: datetime, y: float, w: float, b: float, known: datetime) -> Case:
    c = Case("t", "cpi_inflation", "AAA", t, "cy", str(t.year), y, known, 1.0, info={"raw": {"bridge": b}})
    c.preds[C.WEO] = Predictive("point", w)
    return c


def test_stack_uses_only_errors_published_at_the_origin() -> None:
    cs = []
    for yr in range(2000, 2010):
        t = datetime(yr, 9, 1, tzinfo=UTC)
        known = datetime(yr + 1, 10, 1, tzinfo=UTC)
        cs.append(_case(t, y=2.0, w=1.0, b=2.0, known=known))  # bridge always right
    K.add_second_pass(cs, K.Rules(C.WEO, 3, 3))
    # 2000..2003: fewer than 3 published past origins -> no stack (2003's origin sees 2000 and 2001 only)
    assert all("stack" not in c.info["raw"] for c in cs[:4])
    c = cs[4]  # origin 2004-09: errors of 2000, 2001, 2002 published (2003's outcome only on 2004-10-01)
    lam = (3 * 1.0 + K.SHRINK_N0 * 0.5) / (3 + K.SHRINK_N0)
    assert c.info["stack_lambda"] == pytest.approx(lam, abs=1e-4)
    # changing a later outcome never changes an earlier forecast
    cs2 = [_case(x.origin, (x.outcome or 0) + (50 if x.origin.year >= 2004 else 0), 1.0, 2.0, x.outcome_known_at)  # type: ignore[arg-type]
           for x in cs]  # fmt: skip
    K.add_second_pass(cs2, K.Rules(C.WEO, 3, 3))
    assert cs2[4].info["raw"]["stack"] == pytest.approx(c.info["raw"]["stack"])
    assert isinstance(c.preds[K.fan_key(C.WEO)], Predictive)


def test_holdout_opened_once() -> None:
    hyp = {"hypotheses": [], "code_sha256": S.code_sha256()}
    sha = S.check_unopened(hyp, {"openings": []})
    with pytest.raises(RuntimeError):
        S.check_unopened(hyp, {"openings": [{"hypotheses_sha256": sha}]})
    with pytest.raises(RuntimeError):
        S.check_unopened({**hyp, "code_sha256": "0" * 64}, {"openings": []})


def test_holdout_origins_exclude_v6_dates() -> None:
    ho = [t for t in S.origins("weo_cpi", "holdout") if t.date() >= S.SPLIT]
    assert ho and all(t.month not in (6, 12) for t in ho)
    assert all(t.date() < S.SPLIT for t in S.origins("g7rt", "development"))
    assert not C.gdp_fair(datetime(2020, 7, 1, tzinfo=UTC)) and C.gdp_fair(datetime(2021, 7, 1, tzinfo=UTC))
