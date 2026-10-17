"""V7 BEAT-ENERGY: pure helpers of the pre-registered evaluation (synthetic data, offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from reva.evaluation import beat_energy as be
from reva.evaluation.energy_backtests import Origin


def _idx(y: int, m: int) -> int:
    return y * 12 + m - 1


def test_family_is_declared_once() -> None:
    t = be.declared_tests()
    assert len(t) == len(set(t)) == 3 * 3 * 3 + 8 * 4 * 3 + 4
    assert all(loss == "crps" for (_, _, _, c, _, loss) in t if c.endswith(("_ln", "_n")))
    assert all(loss == "ae" for (_, _, _, c, b, loss) in t if b == "steo")


def test_daily_lag_and_month_mean() -> None:
    daily = {date(2020, 1, 1) + timedelta(days=i): 50.0 + i for i in range(60)}
    t = datetime(2020, 1, 20, tzinfo=UTC)
    kn = be.known_daily(daily, t)
    assert max(kn) == date(2020, 1, 11)  # 9-day weekly-release lag
    p = be.daily_point(kn, _idx(2020, 1))
    assert p is not None and 50 < p < 61
    assert be.daily_point(kn, _idx(2020, 2)) == kn[date(2020, 1, 11)]
    assert be.month_mean_known(kn, _idx(2020, 1)) is None  # month not complete in the known data
    kn2 = be.known_daily(daily, datetime(2020, 2, 10, tzinfo=UTC))
    assert be.month_mean_known(kn2, _idx(2020, 1)) == sum(50.0 + i for i in range(31)) / 31


def test_w1_origins_stop_at_next_release() -> None:
    o = Origin("2020-01", "01/14/2020", datetime(2020, 1, 15, tzinfo=UTC), _idx(2020, 1))
    o.steo = {c: {_idx(2020, 1) + h: 60.0 for h in range(3)} for c in be.SPOT_SERIES}
    rel = {"2020-01": date(2020, 1, 14), "2020-02": date(2020, 2, 6)}
    rows, skipped = be.w1_rows([o], rel, be.next_release_map(list(rel.values())), {}, {})
    offsets = sorted({r.offset for r in rows})
    assert offsets == [0, 7, 14, 21]  # Jan 15 + 21 = Feb 5 < Feb 6 release; +28 = Feb 12 dropped
    assert skipped


def test_bias_correction_uses_only_published_targets() -> None:
    origins = []
    for q in range(16):
        y, m = 2014 + q // 4, 1 + 3 * (q % 4)
        idx = _idx(y, m)
        o = Origin(f"{y}-{m:02d}", None, datetime(y, m, 10, tzinfo=UTC), idx)
        o.info = {"COPR_OPEC": {i: 30.0 for i in range(idx - 200, idx)}}
        o.last = {"COPR_OPEC": idx - 1}
        o.steo = {"COPR_OPEC": {idx + h: 29.0 for h in range(0, 13)}}  # STEO always 1 too low
        origins.append(o)
    recs = [r for r in be.w2_forecasts(origins, {}) if r["code"] == "COPR_OPEC" and r["h"] == 1]
    late = [r for r in recs if "steo_bc" in r]
    assert late and all(abs(r["steo_bc"] - 30.0) < 1e-9 for r in late)
    assert all(r["n_past"] <= be.QTY_WINDOW for r in recs)
    first = recs[0]
    assert first["n_past"] == 0 and "steo_bc" not in first


def test_seal_only_inside_the_registered_window_and_on_existing_questions() -> None:
    import pytest

    from reva.evaluation import beat_energy_seal as bs

    o = Origin("2026-10", "10/06/2026", datetime(2026, 10, 7, tzinfo=UTC), _idx(2026, 10))
    o.steo = {c: {_idx(2026, 10): 100.0} for c in be.SPOT_SERIES}
    with pytest.raises(ValueError, match="7 days"):
        bs.seal_records([o], {}, {}, datetime(2026, 10, 10, tzinfo=UTC))
    with pytest.raises(ValueError, match="current month"):
        bs.seal_records([o], {}, {}, datetime(2026, 11, 20, tzinfo=UTC))
    q = {"type": "quantity", "reference_period": "2027-01"}
    mans = [
        {"supplement_of": "x", "questions": [{**q, "family": f} for f in bs.WIN_CODES.values()]},
        {"questions": [{**q, "family": f} for f in bs.WIN_CODES.values()]},
    ]
    found = bs.find_questions(mans, "2027-01")
    assert set(found) == set(bs.WIN_CODES.values()) and all(m is mans[1] for m, _ in found.values())
    assert bs.find_questions(mans, "2026-10") == {}


def test_w3_weekly_nowcast_and_family_merge() -> None:
    from reva.evaluation import beat_energy_w3 as w3

    weekly = {date(2026, 10, 2): 13.0, date(2026, 10, 9): 14.0, date(2026, 10, 16): 15.0}
    # 2026-10-17: only the weeks ending 10-02 and 10-09 are published (week end + 7 days); Oct has 5 Fridays
    p = w3.wpsr_point(weekly, date(2026, 10, 17), _idx(2026, 10))
    assert p == (13.0 + 14.0 * 4) / 5
    assert w3.wpsr_point(weekly, date(2026, 10, 17), _idx(2026, 11)) == 14.0
    base = [
        {"window": w, "code": c, "h": h, "challenger": ch, "p_challenger_better": 0.5 if w != "W3" else 1.0}
        for w, c, h, ch, _b, _l in be.declared_tests()
    ]
    tests = [{"code": c, "h": h, "window": "W3", "challenger": "wpsr_nowcast", "p_challenger_better": 1e-9}
             for c, h in be.W3_TESTS]  # fmt: skip
    fam = w3.merged_family(base, tests)
    assert fam["family_size"] == 127 and len(fam["wins"]) == 4
