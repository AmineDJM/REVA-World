"""Energy forecasters and the STEO vintage backtest (V6, PRED-ENERGY): numerics, tests, no look-ahead."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime

import polars as pl
import pytest

from reva.adapters.connectors.vintage_common import Rows, indicator, month_bounds, vintage_flags
from reva.evaluation import energy_backtests as eb
from reva.evaluation.vintages import cells_from_atlas_frame
from reva.models.forecasting import energy_models as em


def _num_crps(cdf, y: float, lo: float, hi: float, n: int = 40000) -> float:  # type: ignore[no-untyped-def]
    step = (hi - lo) / n
    tot = 0.0
    for i in range(n):
        x = lo + (i + 0.5) * step
        tot += (cdf(x) - (1.0 if x >= y else 0.0)) ** 2 * step
    return tot


def test_crps_closed_forms_match_numerical_integration() -> None:
    p = em.Pred("normal", 1.0, 1.0, 2.0)
    num = _num_crps(lambda x: 0.5 * math.erfc(-(x - 1.0) / (2.0 * math.sqrt(2))), 2.5, -20, 22)
    assert em.crps(p, 2.5) == pytest.approx(num, rel=1e-3)
    q = em.Pred("lognormal", math.exp(4.0), 4.0, 0.3)
    cdf = lambda x: 0.0 if x <= 0 else 0.5 * math.erfc(-(math.log(x) - 4.0) / (0.3 * math.sqrt(2)))  # noqa: E731
    assert em.crps(q, 70.0) == pytest.approx(_num_crps(cdf, 70.0, 1e-6, 400.0), rel=1e-3)
    assert em.crps(em.Pred("point", 80.0), 70.0) == 10.0  # a point forecast's CRPS is its absolute error
    assert em.log_score(em.Pred("point", 1.0), 1.0) is None
    assert em.pit(p, 1.0) == pytest.approx(0.5)


def _walk(n: int, start: float, seed: int, sd: float = 0.05) -> dict[int, float]:
    rnd = random.Random(seed)
    v, out = start, {}
    for i in range(n):
        v *= math.exp(rnd.gauss(0, sd))
        out[24000 + i] = v
    return out


def test_random_walk_steo_and_abstentions() -> None:
    s = _walk(80, 60.0, 1)
    last = max(s)
    info = {"BREPUUS": s}
    rw = em.forecast("rw", "BREPUUS", info, {}, last, last + 3)
    assert isinstance(rw, em.Pred) and rw.point == pytest.approx(s[last]) and rw.dist == "lognormal"
    rw1 = em.forecast("rw", "BREPUUS", info, {}, last, last + 1)
    assert isinstance(rw1, em.Pred) and rw.sigma == pytest.approx(rw1.sigma * math.sqrt(3))  # type: ignore[operator]
    assert isinstance(em.forecast("steo", "BREPUUS", info, {}, last, last + 3), em.Abstain)
    st = em.forecast("steo", "BREPUUS", info, {"BREPUUS": {last + 3: 77.0}}, last, last + 3)
    assert isinstance(st, em.Pred) and st.point == 77.0 and st.dist == "point"
    short = {"BREPUUS": dict(list(s.items())[-10:])}
    assert isinstance(em.forecast("rw", "BREPUUS", short, {}, last, last + 1), em.Abstain)
    assert isinstance(em.forecast("inventory", "BREPUUS", info, {}, last, last + 1), em.Abstain)  # no stocks
    with pytest.raises(ValueError):
        em.kind_of("NOT_A_CODE")


def test_inventory_model_recovers_a_planted_relation() -> None:
    rnd = random.Random(7)
    inv = {24000 + i: 2800.0 * math.exp(0.05 * math.sin(i / 7.0)) for i in range(200)}
    price: dict[int, float] = {24000: 60.0}
    for i in range(1, 200):
        t = 24000 + i
        gap = em.inventory_gap(inv, t - 1)
        drift = -0.8 * gap if gap is not None else 0.0
        price[t] = price[t - 1] * math.exp(drift + rnd.gauss(0, 0.01))
    last = 24000 + 199
    p = em.forecast("inventory", "BREPUUS", {"BREPUUS": price, em.INVENTORY: inv}, {}, last, last + 1)
    assert isinstance(p, em.Pred)
    gap = em.inventory_gap(inv, last)
    assert gap is not None
    assert p.mu == pytest.approx(math.log(price[last]) - 0.8 * gap, abs=0.01)


def test_quantity_models() -> None:
    x = {24000 + i: 10.0 + 0.1 * i + (0.5 if (i % 12) == 6 else 0.0) for i in range(120)}
    last = max(x)
    d = em.forecast("drift", "COPRPUS", {"COPRPUS": x}, {}, last, last + 2)
    assert isinstance(d, em.Pred) and d.dist == "normal"
    s = em.forecast("seasonal", "PASC_OECD_T3", {"PASC_OECD_T3": x}, {}, last, last + 7)
    assert isinstance(s, em.Pred)
    assert s.point == pytest.approx(x[last] + 0.7 + 0.5, abs=1e-9)  # trend 0.1/month + the planted month-6 bump
    a = em.forecast("ar_diff", "PAPR_WORLD", {"PAPR_WORLD": _walk(150, 100.0, 3, 0.01)}, {}, 24149, 24152)
    assert isinstance(a, em.Pred) and a.sigma and a.sigma > 0
    c = em.forecast("combo_steo_rw", "COPRPUS", {"COPRPUS": x}, {"COPRPUS": {last + 2: 30.0}}, last, last + 2)
    assert isinstance(c, em.Pred) and c.point == pytest.approx(0.5 * 30.0 + 0.5 * x[last])


def test_dm_hln_and_holm() -> None:
    rnd = random.Random(11)
    same = [rnd.gauss(0, 1) for _ in range(60)]
    t = eb.dm_hln(same, 1)
    assert 0.05 < t["p_challenger_better"] < 0.95
    better = [rnd.gauss(-1.0, 1) for _ in range(60)]
    assert eb.dm_hln(better, 2)["p_challenger_better"] < 0.001
    worse = [rnd.gauss(1.0, 1) for _ in range(60)]
    assert eb.dm_hln(worse, 2)["p_challenger_better"] > 0.999
    assert eb.dm_hln([0.1] * 5, 1)["status"] == "too_few_pairs"
    assert eb.overlap_steps(1) == 1 and eb.overlap_steps(4) == 2 and eb.overlap_steps(12) == 4
    assert eb.holm({"a": 0.001, "b": 0.02, "c": 0.06}) == {"a": True, "b": True, "c": False}
    assert eb.holm({"a": 0.03, "b": 0.04}) == {"a": False, "b": False}  # 0.03 > 0.05 / 2


def _issue_cells(vintage: str, avail: datetime, hist: dict[int, float], proj: dict[int, float]) -> pl.DataFrame:
    rows = Rows()
    for kind, d in (("published_history_boundary_unknown", hist), ("projection", proj)):
        for idx, v in d.items():
            p0, p1 = month_bounds(idx // 12, idx % 12 + 1)
            rows.add(
                source="vintage_steo",
                dataset="steo_archive",
                indicator=indicator("BREPUUS", vintage),
                provider_geo="not_stated",
                start=p0,
                end=p1,
                value=repr(v),
                unit="dollars per barrel",
                flags=vintage_flags(
                    family="eia_steo",
                    vintage=vintage,
                    available_at=avail,
                    basis="steo_archive_release_date",
                    variable="eia_steo:BREPUUS",
                    provider_variable="BREPUUS",
                    estimate_kind=kind,
                ),
                raw_sha256="c" * 64,
                selector={},
            )
    return cells_from_atlas_frame(rows.frame(), dataset_key=eb.VINTAGE_TABLE, table_recorded_at=None)


def test_origins_use_only_what_was_published_then_and_backtest_scores_steo() -> None:
    s = _walk(120, 60.0, 5)
    i1, i2 = 24060, 24063  # issue months (index): 2005-01 and 2005-04
    a = _issue_cells("2005-01", datetime(2005, 1, 11, tzinfo=UTC), {i: s[i] for i in s if i < i1},
                     {i: s[i] for i in range(i1, i1 + 13)})  # fmt: skip
    # the later issue revises an old month: the earlier origin must not see the revision
    rev = {i: s[i] for i in s if i < i2}
    rev[i1 - 5] = 999.0
    b = _issue_cells("2005-04", datetime(2005, 4, 12, tzinfo=UTC), rev, {i: s[i] * 1.5 for i in range(i2, i2 + 13)})
    origins = eb.origins_from_cells(pl.concat([a, b]))
    assert [o.vintage for o in origins] == ["2005-01", "2005-04"]
    o1, o2 = origins
    assert o1.info["BREPUUS"][i1 - 5] == pytest.approx(s[i1 - 5]) and o2.info["BREPUUS"][i1 - 5] == 999.0
    assert o1.last["BREPUUS"] == i1 - 1 and max(o1.info["BREPUUS"]) == i1 - 1  # projections never in the info set
    assert set(o1.steo["BREPUUS"]) == set(range(i1, i1 + 13))
    res = eb.backtest(origins, {"BREPUUS": s}, codes=("BREPUUS",), horizons=(1, 2, 3))
    steo1 = [r for r in res["records"] if r["model"] == "steo" and r["origin"] == "2005-01"]
    assert steo1 and all(r["crps"] == pytest.approx(0.0) for r in steo1)  # issue 1 projected the outcomes exactly
    assert all(r["target"] >= "2005-02" for r in res["records"])
    assert res["outcome_basis"] and res["winners"] == []  # two origins: no test can be significant
    assert eb.result_digest(res) == eb.result_digest(res)


def test_outcomes_ignore_projection_cells() -> None:
    f = pl.DataFrame(
        {
            "indicator": ["STEO.BREPUUS.M", "STEO.BREPUUS.M", "STEO.OTHER.M"],
            "period_start": [date(2026, 8, 1), date(2026, 10, 1), date(2026, 8, 1)],
            "value": ["70", "71", "1"],
            "flags": ["{}", '{"estimate_kind":"projection"}', "{}"],
        }
    )
    out = eb.outcomes_from_frame(f)
    assert out == {"BREPUUS": {2026 * 12 + 7: 70.0}}
