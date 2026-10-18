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


def test_sealed_batch_labels_and_question_ids() -> None:
    from reva.evaluation.beat_agri import seal as SE

    v = _synthetic(last=date(2026, 10, 10))
    now = datetime(2026, 10, 17, tzinfo=UTC)
    out = S.prospective_records(v, None, now)
    b = SE.build_batch(out, now=now, confirmed=["us_yield|mid|bc|ae"], pins={})
    assert len({q["question_id"] for q in b["questions"]}) == len(b["questions"]) == len(out["records"])
    f = [x for x in b["forecasts"] if x["model"] == "forecast.beat_agri_bc" and x["status"] == "issued"]
    lab = {x["question_id"]: x["label"] for x in f}
    qy = next(q for q in b["questions"] if q["series"] == "us.corn.yield")
    assert "confirmed on the holdout: us_yield|mid|bc|ae" in lab[qy["question_id"]]
    qw = next(q for q in b["questions"] if q["series"] == "world.corn.production")
    assert "NOT confirmed" in lab[qw["question_id"]]
    ref = next(x for x in b["forecasts"] if x["model"] == "forecast.reference_usda_wasde")
    assert "predictive" not in ref and "USDA" in ref["attribution"]


def test_wx_issued_only_on_us_corn_soybeans_yield_production() -> None:
    from reva.evaluation.beat_agri import seal as SE

    rec = {
        "series": "world.corn.production",
        "marketing_year": 2026,
        "k": 5,
        "window": "mid",
        "release": "2026-10-09",
        "truth_rule": "x",
        "wasde_point": 1.0,
        "points": {"wx": 1.1, "bc": 1.0, "rev": 1.0},
        "sds": {"wx": 0.1},
    }
    b = SE.build_batch(
        {"release_in_force": "2026-10-09", "records": [rec]},
        now=datetime(2026, 10, 18, tzinfo=UTC),
        confirmed=[],
        pins={},
    )
    wx = next(f for f in b["forecasts"] if f["model"] == "forecast.beat_agri_wx")
    assert wx["status"] == "abstained"


def _weather() -> C.Weather:
    import random as _r

    rng = _r.Random(3)
    vals = {reg: {(y, m): (70 + rng.gauss(0, 2), 3 + rng.gauss(0, 1)) for y in range(1990, 2027) for m in range(1, 13)}
            for reg in (261, 262)}  # fmt: skip
    return C.Weather(vals)


def test_wx_has_no_forecast_outside_its_scope_at_the_source() -> None:
    cs, _ = S.all_cases(_synthetic(), _weather())
    got = {c.series for c in cs if c.points.get("wx") is not None}
    assert got and got <= set(C.WX_SERIES)
    assert all(c.sds.get("wx") is None for c in cs if c.series not in C.WX_SERIES)


def _registry_batch(now: datetime) -> dict:
    from reva.evaluation.beat_agri import registry as BR

    v = _synthetic(last=date(2026, 10, 10))
    prosp = S.prospective_records(v, _weather(), now)
    pin = {"dataset_key": BR.WASDE_TABLE, "artifact_sha256": "a" * 64, "recorded_at": "2026-10-17T00:00:00+00:00"}
    return BR.build_registry_batch(prosp, now=now, confirmed=[], pins={BR.WASDE_TABLE: pin}, origin="live")


def test_registry_batch_format() -> None:
    from reva.evaluation.registry.forecasts import predictive_from_record
    from reva.evaluation.registry.service import BATCH_SCHEMA

    b = _registry_batch(datetime(2026, 10, 18, tzinfo=UTC))
    assert b["schema"] == BATCH_SCHEMA and b["resolver"] == "beat_agri" and b["cycle"] == "beat_agri:2026-10-10"
    q = next(x for x in b["questions"] if x["family"] == "beat_agri.us.corn.yield")
    assert (
        q["reference_period"] == "2026"
        and q["period_end"] == "2027-01-01"
        and q["resolution"]["truth_date"] == "2027-01-01"
    )
    for k in (
        "question_key",
        "cycle",
        "label_fr",
        "label_en",
        "geo",
        "geo_label_fr",
        "geo_label_en",
        "unit",
        "text_fr",
        "text_en",
    ):
        assert q[k]
    qw = next(x for x in b["questions"] if x["family"] == "beat_agri.world.corn.production")
    assert qw["resolution"]["truth_date"] == "2027-11-01"
    fan = next(
        f for f in b["forecasts"] if f["question_id"] == q["question_id"] and f["model"] == "forecast.wasde_fair_fan"
    )
    assert fan["predictive"]["family"] == "normal" and predictive_from_record(fan["point"], fan["predictive"]).sigma
    ref = next(
        f
        for f in b["forecasts"]
        if f["question_id"] == q["question_id"] and f["model"] == "forecast.reference_usda_wasde"
    )
    assert ref["predictive"] == {"family": "point"} and "USDA" in ref["attribution"]
    wxw = next(
        f for f in b["forecasts"] if f["question_id"] == qw["question_id"] and f["model"] == "forecast.beat_agri_wx"
    )
    assert wxw["status"] == "abstained"


def test_resolution_refuses_before_the_truth_date_and_reads_the_first_release_on_or_after() -> None:
    from reva.evaluation.beat_agri import registry as BR

    b = _registry_batch(datetime(2026, 10, 18, tzinfo=UTC))
    v = _synthetic(last=date(2028, 3, 1))  # the table already holds later releases
    pin = {"dataset_key": BR.WASDE_TABLE, "artifact_sha256": "b" * 64}
    before = BR.resolve_manifest(b, set(), v, pin, today=date(2026, 12, 31))
    assert before == {"outcomes": [], "scores": []}
    jan = BR.resolve_manifest(b, set(), v, pin, today=date(2027, 1, 20))
    res = {o["question_id"]: o for o in jan["outcomes"]}
    q = next(x for x in b["questions"] if x["family"] == "beat_agri.us.corn.yield")
    qw = next(x for x in b["questions"] if x["family"] == "beat_agri.world.corn.production")
    assert res[q["question_id"]]["status"] == "resolved" and qw["question_id"] not in res
    truth = v.truth("us.corn.yield", 2026)
    assert truth is not None and truth[2] == date(2027, 1, 10)
    assert float(res[q["question_id"]]["value"]) == pytest.approx(truth[0])
    assert res[q["question_id"]]["source"]["cells"][0]["indicator"] == "wasde_esmis:us.corn.yield|v=2027-01-10"
    sc = [s for s in jan["scores"] if s["question_id"] == q["question_id"]]
    assert {s["model"] for s in sc} >= {
        "forecast.reference_usda_wasde",
        "forecast.wasde_fair_fan",
        "forecast.beat_agri_bc",
    }
    fan = next(s for s in sc if s["model"] == "forecast.wasde_fair_fan")
    assert fan["crps"] is not None and fan["abs_error"] is not None
    # already handled questions are not scored twice; a missing truth after void_after -> void
    again = BR.resolve_manifest(b, set(res), v, pin, today=date(2027, 1, 20))
    assert not again["outcomes"]
    empty = C.Vintages([r for r in v.releases if r.release < date(2027, 1, 1)])
    void = BR.resolve_manifest(b, set(), empty, pin, today=date(2028, 2, 10))
    assert {o["status"] for o in void["outcomes"] if o["question_id"] == q["question_id"]} == {"void"}


def test_issue_job_runs_beat_agri_before_the_ensemble(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from reva.evaluation.beat_agri import registry as BA
    from reva.evaluation.beat_macro import seal as SEM
    from reva.evaluation.pred_models import issue as PI
    from reva.evaluation.registry import ensemble as EN
    from reva.evaluation.registry import service as RS

    calls: list[str] = []
    monkeypatch.setattr(RS, "issue_cycle", lambda *a, **k: calls.append("cycle") or {})
    monkeypatch.setattr(PI, "issue_supplements", lambda *a, **k: calls.append("pred_models") or {})
    monkeypatch.setattr(SEM, "issue_monthly", lambda *a, **k: calls.append("beat_macro") or {})
    monkeypatch.setattr(BA, "issue_release", lambda *a, **k: calls.append("beat_agri") or {})
    monkeypatch.setattr(EN, "issue_ensemble", lambda *a, **k: calls.append("ensemble") or {})
    RS.handle_forecast_registry({"action": "issue", "origin": "live"}, SimpleNamespace(worker_engine=None, store=None))
    assert calls[-1] == "ensemble" and calls.index("beat_agri") < calls.index("ensemble")


def test_beat_agri_models_are_registry_models() -> None:
    from reva.evaluation.beat_agri import seal as SE
    from reva.evaluation.registry.forecasts import all_models

    assert set(SE.MODELS) <= set(all_models())
