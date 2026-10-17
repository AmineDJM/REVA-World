"""V7 BEAT-AGRI evaluation mechanics on synthetic WASDE vintages (no look-ahead, truth rule, candidates, gate)."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from reva.evaluation.beat_agri import cases as C
from reva.evaluation.beat_agri import service as S


def _synthetic(bias: float = -0.03, seed: int = 7, last: date = date(2026, 10, 1)) -> C.Vintages:
    """Monthly releases 1998-01..last; MY y projection at k = 0..: truth * (1 + bias * (1 - k/12) + noise);
    from the truth month on, the printed value is the truth."""
    rng = random.Random(seed)
    truth = {
        (s, y): 100.0 * (1 + 0.02 * (y - 1998)) * (1 + 0.05 * rng.gauss(0, 1))
        for s in C.SERIES
        for y in range(1990, 2030)
    }
    noise = {(s, y, k): rng.gauss(0, 0.01) for s in C.SERIES for y in range(1990, 2030) for k in range(0, 40)}
    rels = []
    d = date(1998, 1, 10)
    while d <= last:
        vals = {}
        for s in C.SERIES:
            latest = d.year if d.month >= 5 else d.year - 1
            for y in (latest - 2, latest - 1, latest):
                k = (d.year - y) * 12 + d.month - 5
                cut = date(y + 1, C.TRUTH_MONTH[s], 1)
                if d >= cut:
                    v = truth[(s, y)]
                else:
                    v = truth[(s, y)] * (1 + bias * max(0.0, 1 - k / 12) + noise[(s, y, k)] * max(0.2, 1 - k / 12))
                st = "projection" if y == latest else "estimate" if y == latest - 1 else "published_history"
                vals[(s, y)] = (v, st)
        rels.append(C.Release(d, datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1), d.month, vals))
        d = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 10)
    return C.Vintages(rels)


def test_truth_rule_first_release_on_or_after_cut() -> None:
    v = _synthetic()
    tr = v.truth("us.corn.production", 2010)
    assert tr is not None and tr[2] == date(2011, 1, 10)
    tr2 = v.truth("world.wheat.ending_stocks", 2010)
    assert tr2 is not None and tr2[2] == date(2011, 11, 10)
    # a cancelled January report (e.g. January 2019): the first release on/after the cut is February
    v2 = C.Vintages([r for r in v.releases if r.release != date(2019, 1, 10)])
    assert v2.truth("us.corn.yield", 2018)[2] == date(2019, 2, 10)  # type: ignore[index]


def test_cases_windows_and_no_case_after_truth() -> None:
    v = _synthetic()
    cs, _ = C.build_cases(v, first_my=2005, last_my=2006)
    ks = {(c.series, c.k) for c in cs}
    assert ("us.corn.yield", 7) in ks and ("us.corn.yield", 8) not in ks
    assert ("us.wheat.production", 4) in ks and ("us.wheat.production", 5) not in ks
    assert ("world.rice.ending_stocks", 11) in ks
    assert all(c.t < c.truth_available for c in cs)


def test_candidates_use_only_information_published_by_the_origin() -> None:
    full = _synthetic()
    cut = datetime(2012, 3, 1, tzinfo=UTC)
    trunc = C.Vintages([r for r in full.releases if r.available_at <= cut])
    a, _ = C.build_cases(full, first_my=1998, last_my=2012)
    b, _ = C.build_cases(trunc, first_my=1998, last_my=2012)
    C.compute_candidates(a, None)
    C.compute_candidates(b, None)
    pa = {(c.series, c.my, c.k): c for c in a}
    n = 0
    for c in b:
        if c.t <= cut:
            other = pa[(c.series, c.my, c.k)]
            for m in ("bc", "rev", "trend"):
                assert c.points.get(m) == other.points.get(m)
            assert c.sds.get("wasde") == other.sds.get("wasde")
            n += 1
    assert n > 100


def test_bias_correction_learns_a_persistent_bias() -> None:
    cs, _ = S.all_cases(_synthetic(bias=-0.03), None)
    rows = S.stage_rows(cs, "development")
    t = S.paired([r for r in rows if r[0].k <= 2], "bc", "ae")
    assert t["relative"] < 0.8 and t["p_better"] < 0.05
    unbiased, _ = S.all_cases(_synthetic(bias=0.0), None)
    t0 = S.paired([r for r in S.stage_rows(unbiased, "development") if r[0].k <= 2], "bc", "ae")
    assert t0["relative"] > 0.95  # nothing to learn: no material gain


def test_fair_fan_is_wasde_point_with_own_error_spread() -> None:
    cs, _ = S.all_cases(_synthetic(), None)
    c = next(x for x in cs if x.my == 2010 and x.series == "us.corn.yield" and x.k == 0)
    assert c.points["wasde"] == c.official and c.sds["wasde"] is not None
    past = [p for p in cs if p.series == c.series and p.k == 0 and p.my < 2010 and p.truth_available <= c.t]
    rms = math.sqrt(sum(p.err**2 for p in past) / len(past))
    assert c.sds["wasde"] == pytest.approx(rms * c.official)


def test_weather_month_usable_rule() -> None:
    assert not C.Weather.usable(2015, 7, datetime(2015, 8, 11, 23, tzinfo=UTC))
    assert C.Weather.usable(2015, 7, datetime(2015, 8, 12, tzinfo=UTC))


def test_selection_and_single_opening() -> None:
    tests = [
        {
            "group": "us_yield",
            "window": "early",
            "candidate": "bc",
            "loss": "ae",
            "tested": True,
            "relative": 0.9,
            "p_better": 0.04,
        },
        {
            "group": "us_yield",
            "window": "early",
            "candidate": "rev",
            "loss": "ae",
            "tested": True,
            "relative": 0.8,
            "p_better": 0.08,
        },
        {
            "group": "us_yield",
            "window": "mid",
            "candidate": "bc",
            "loss": "ae",
            "tested": True,
            "relative": 0.7,
            "p_better": 0.2,
        },
        {
            "group": "us_yield",
            "window": "late",
            "candidate": "bc",
            "loss": "crps",
            "tested": True,
            "relative": 1.1,
            "p_better": 0.01,
        },
    ]
    h = S.select_hypotheses(tests)
    assert [x["key"] for x in h] == ["us_yield|early|rev|ae"]
    S.check_unopened({"openings": []}, "abc")
    with pytest.raises(RuntimeError):
        S.check_unopened({"openings": [{"hypotheses_sha256": "abc"}]}, "abc")


def test_prospective_records_for_the_open_year() -> None:
    v = _synthetic(last=date(2026, 10, 10))
    out = S.prospective_records(v, None, datetime(2026, 10, 17, tzinfo=UTC))
    assert out["release_in_force"] == "2026-10-10"
    series = {r["series"] for r in out["records"]}
    assert "us.corn.yield" in series and "us.wheat.yield" not in series  # wheat yield window ends in September
    r = next(x for x in out["records"] if x["series"] == "world.corn.production")
    assert r["k"] == 5 and r["points"]["bc"] is not None and "2027-11-01" in r["truth_rule"]
