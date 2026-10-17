"""BEAT-NOWCAST: as-of selection, no look-ahead in the second pass, guard, bridges, ledger."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from reva.evaluation.beat_nowcast import cases as C
from reva.evaluation.beat_nowcast import data as D
from reva.evaluation.beat_nowcast import models as M
from reva.evaluation.beat_nowcast import service as S
from reva.evaluation.pred_models.realtime import Case
from reva.evaluation.vintages import CELL_SCHEMA, VintageAccessor
from reva.kernel.errors import LeakageBlocked
from reva.models.forecasting.registry_baselines import Predictive


def _cells(rows: list[dict[str, object]]) -> pl.DataFrame:
    base = {k: None for k in CELL_SCHEMA.names()}
    return pl.DataFrame([{**base, **r} for r in rows], schema=CELL_SCHEMA)


def _vcell(var: str, vint: str, ps: date, value: float, av: datetime, family: str = "philfed_rtdsm_monthly") -> dict[str, object]:
    return {"dataset_key": "atlas.fed_nowcasts.rtdsm_monthly", "family": family, "variable": "x", "provider_variable": var,
            "indicator": f"{var}|v={vint}", "provider_geo": "USA", "iso3": "USA", "period_start": ps,
            "period_end": ps + timedelta(days=28), "vintage": vint, "available_at": av,
            "availability_basis": "rtdsm_monthly_vintage_month_end", "estimate_kind": "published_history",
            "value": str(value)}  # fmt: skip


def test_vintage_series_matches_the_vintage_accessor_at_random_cutoffs() -> None:
    rows = []
    for k in range(1, 13):
        av = datetime(2015, k, 1, tzinfo=UTC) + timedelta(days=31)
        av = datetime(av.year, av.month, 1, tzinfo=UTC)
        for m in range(k):
            rows.append(_vcell("EMPLOY", f"2015M{k:02d}", date(2015, m + 1, 1), 100 + m + 0.1 * k, av))
    cells = _cells(rows)
    vs = D.VintageSeries.from_cells("EMPLOY", cells, "philfed_rtdsm_monthly", "EMPLOY")
    rng = random.Random(7)
    for _ in range(50):
        cut = datetime(2015, 1, 1, tzinfo=UTC) + timedelta(seconds=rng.randrange(0, 400 * 86400))
        got = vs.as_of(cut)
        acc = VintageAccessor(cells, cut).latest(family="philfed_rtdsm_monthly").rows
        if got is None:
            assert acc.height == 0
            continue
        pos, vals = got
        assert vs.instants[pos] <= cut
        # every month the accessor returns has the same value as the selected vintage (latest per key)
        for r in acc.iter_rows(named=True):
            if D.mi_of(r["period_start"]) in vals and r["vintage"] == vs.labels[pos]:
                assert float(r["value"]) == pytest.approx(vals[D.mi_of(r["period_start"])])
        assert max(vals) == max(D.mi_of(r["period_start"]) for r in acc.iter_rows(named=True))


def test_guard_blocks_an_input_published_after_the_cutoff() -> None:
    t = D.Truth(1.0, datetime(2020, 2, 1, tzinfo=UTC), "bea_release_recorded_by_atlanta_fed", date(2020, 1, 31), "x")
    assert D.guard_refs(datetime(2020, 2, 1, tzinfo=UTC), [D.truth_ref(t)]) == 1
    with pytest.raises(LeakageBlocked):
        D.guard_refs(datetime(2020, 1, 31, 23, 59, tzinfo=UTC), [D.truth_ref(t)])


def test_official_book_latest_respects_availability() -> None:
    book = D.OfficialBook("o")
    tgt = date(2020, 1, 1)
    book.updates[tgt] = [(date(2020, 1, d), D.Stamp(float(d), datetime(2020, 1, d + 1, tzinfo=UTC), "r", "x"))
                         for d in (5, 10, 20)]  # fmt: skip
    assert book.latest(tgt, datetime(2020, 1, 6, tzinfo=UTC))[0] == date(2020, 1, 5)  # type: ignore[index]
    assert book.latest(tgt, datetime(2020, 1, 10, 23, tzinfo=UTC))[0] == date(2020, 1, 5)  # type: ignore[index]
    assert book.latest(tgt, datetime(2020, 1, 11, tzinfo=UTC))[0] == date(2020, 1, 10)  # type: ignore[index]
    assert book.latest(tgt, datetime(2020, 1, 5, tzinfo=UTC)) is None


def test_daily_series_lag() -> None:
    s = D.DailySeries("g", 7, "rule", [date(2020, 1, d) for d in range(1, 32)], [1.0 + d for d in range(31)])
    cut = datetime(2020, 1, 20, tzinfo=UTC)
    assert s.last_usable_day(cut) == date(2020, 1, 12)
    mm = s.month_mean(D.mi_of(date(2020, 1, 1)), cut)
    assert mm is not None and mm[1] == 12
    ref = s.ref(cut)
    assert ref is not None and ref.source_available_at <= cut


def _case(task: str, target: date, pub: date, y: float | None, off: float, bridge: float, known: date) -> Case:
    cut = D.midnight(pub)
    return Case(task=task, variable="v", geo="USA", origin=D.midnight(target), horizon="m1", target=str(target),
                outcome=y, outcome_known_at=D.midnight(known) if y is not None else None, scale=1.0,
                preds={"benchmark.o": Predictive("point", off)},
                info={"date": pub.isoformat(), "reva_cut": cut.timestamp(), "raw": {"bridge": bridge}})  # fmt: skip


def test_second_pass_uses_only_outcomes_known_at_the_cutoff() -> None:
    rules = M.Rules("benchmark.o", 12, 4.0, 2, 2, ("stack_reva",))

    def build(late_outcome: float) -> list[Case]:
        cs = []
        for k in range(8):
            tgt = date(2010 + k // 4, 3 * (k % 4) + 1, 1)
            pub = tgt + timedelta(days=40)
            known = tgt + timedelta(days=120)
            y = late_outcome if k == 7 else 1.0 + 0.1 * k
            cs.append(_case("t", tgt, pub, y, 1.0, 2.0, known))
        return cs

    a, b = build(5.0), build(-50.0)
    M.run_second_pass(a, rules)
    M.run_second_pass(b, rules)
    for ca, cb in zip(a, b, strict=True):
        assert ca.info["raw"] == cb.info["raw"]  # the last outcome is known after every case's cutoff
    assert "stack_reva" in a[-1].info["raw"] and "biascorr" in a[-1].info["raw"]
    # no case ever uses its own outcome: the first two targets have no past errors
    assert "stack_reva" not in a[0].info["raw"] and "stack_reva" not in a[1].info["raw"]


def test_shrunk_lambda_is_bounded_and_shrinks_to_half() -> None:
    assert M.shrunk_lambda([(1.0, 1.0)] * 10, 10, 4.0) == pytest.approx((10 * 1 + 4 * 0.5) / 14)
    assert M.shrunk_lambda([(-1.0, 1.0)] * 10, 0, 4.0) == pytest.approx(0.5)
    assert M.shrunk_lambda([(0.0, 0.0)], 1, 4.0) is None


def test_gdp_bridge_recovers_a_linear_relation() -> None:
    rng = random.Random(3)
    panel: dict[str, dict[int, float]] = {k: {} for k in M.GDP_INDICATORS}
    lv = dict.fromkeys(M.GDP_INDICATORS, 100.0)
    start, end = 1990 * 12, 2015 * 12 + 2
    for m in range(start, end + 1):
        for k in M.GDP_INDICATORS:
            lv[k] *= math.exp(rng.gauss(0.002, 0.004))
            panel[k][m] = lv[k]
    g = {}
    for qi in range(1991 * 4, 2015 * 4):
        xs = [M.q_growth({m: math.log(v) for m, v in panel[k].items()}, qi) for k in M.GDP_INDICATORS]
        g[qi] = 0.5 + 0.3 * xs[0] + 0.2 * xs[1]  # type: ignore[operator]
    p = M.gdp_bridge(panel, g, 2015 * 4)
    xs = [M.q_growth({m: math.log(v) for m, v in panel[k].items()}, 2015 * 4) for k in M.GDP_INDICATORS]
    assert isinstance(p, Predictive)
    assert p.point == pytest.approx(0.5 + 0.3 * xs[0] + 0.2 * xs[1], abs=1e-6)  # type: ignore[operator]


def test_buckets() -> None:
    assert C.bucket_gdp(date(2020, 1, 15), date(2020, 1, 1)) == "m0"
    assert C.bucket_gdp(date(2019, 12, 1), date(2020, 1, 1)) == "m-1"
    assert C.bucket_gdp(date(2020, 4, 20), date(2020, 1, 1)) == "m3"
    assert C.bucket_cpi(date(2020, 2, 3), date(2020, 1, 1)) == "m1"


def test_windows_and_selection_rule() -> None:
    assert S.DEV["gdp"][1] < S.HOLDOUT["gdp"][0] and S.DEV["cpi"][1] < S.HOLDOUT["cpi"][0]
    doc = {"development_family": {"tests": [
        {"cell": "c", "loss": "ae", "candidate": "forecast.nc_bridge", "benchmark": "b", "uses_official_forecast": False,
         "relative": 0.9, "p_better": 0.2, "n_origins": 30, "tested": True},
        {"cell": "c", "loss": "ae", "candidate": "forecast.nc_dfm", "benchmark": "b", "uses_official_forecast": False,
         "relative": 0.8, "p_better": 0.3, "n_origins": 30, "tested": True},
        {"cell": "c", "loss": "ae", "candidate": "forecast.nc_stack_reva", "benchmark": "b",
         "uses_official_forecast": True, "relative": 1.01, "p_better": 0.6, "n_origins": 30, "tested": True},
    ]}}  # fmt: skip
    hyps = S.select_hypotheses(doc)
    assert [h["candidate"] for h in hyps] == ["forecast.nc_dfm"]


def test_ledger_refuses_a_second_opening(tmp_path: Path) -> None:
    hyp = {"hypotheses": [{"cell": "c"}], "code_sha256": "x"}
    led = tmp_path / "l.yaml"
    sha = S.check_unopened(hyp, S.load_yaml(led))
    S.append_ledger(led, {"hypotheses_sha256": sha, "opened_at": "now", "status": "opening"})
    with pytest.raises(RuntimeError):
        S.check_unopened(hyp, S.load_yaml(led))


def test_code_digest_covers_the_connector_and_cli() -> None:
    assert "src/reva/adapters/connectors/atlas_fed_nowcasts.py" in S.SOURCE_FILES
    assert len(S.code_sha256()) == 64
    json.dumps(S.SOURCE_FILES)
