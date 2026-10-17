"""V7 BEAT-ENERGY window W3: weekly WPSR data vs the EIA STEO issue in force (pure; agent BEAT-ENERGY).

Declared in ``registry/forecasts/beat_energy/protocol.yaml`` (W3) with p = 1 while ``atlas.eia_wpsr`` was not loaded;
this module (pinned by ``registry/forecasts/beat_energy/w3_addendum.yaml``, committed before it is run on data)
evaluates exactly the declared tests and replaces their p = 1 in the SAME Holm family of 127 tests.

Candidate ``wpsr_nowcast`` (as registered): the mean of the weekly values of the target month (weeks whose ending
Friday falls in the month) published at the origin, the unknown weeks carried forward from the last published
week; for h = 1 every week is unknown -> the last published weekly value. Publication: a week is usable from its
week end + ``AVAILABLE_AFTER_DAYS`` (the WPSR is released the Wednesday after the week; one extra day of margin for
holiday weeks, stricter than the registered "Wednesday + 1 day"). Weekly values are taken as unrevised (the page
publishes the current series only). Units: thousand b/d and thousand barrels -> million (STEO's units).
Origins, aggregation (per issue mean over offsets 7/14/21/28), DM-HLN (k = 1), AE vs STEO's point and the outcome
(final STEO history) are those of W1.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from reva.evaluation import beat_energy as be
from reva.evaluation.energy_backtests import Origin, dm_hln, holm, label

WPSR_SERIES = {"COPRPUS": "WCRFPUS2", "COSXPUS": "WCESTUS1"}
SCALE = 1000.0
AVAILABLE_AFTER_DAYS = 7


def wpsr_point(weekly: Mapping[date, float], t_day: date, target: int) -> float | None:
    """``weekly``: week-end Friday -> value (STEO units). Mean of the target month's weeks, unknown carried forward."""
    known = {d: v for d, v in weekly.items() if d + timedelta(days=AVAILABLE_AFTER_DAYS) <= t_day}
    if not known:
        return None
    last_d = max(known)
    y, m = target // 12, target % 12 + 1
    d = date(y, m, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    vals = []
    while d.month == m:
        vals.append(known[d] if d in known else known[last_d] if d > last_d else None)
        d += timedelta(days=7)
    got = [v for v in vals if v is not None]
    return statistics.fmean(got) if got and len(got) == len(vals) else None


def w3_records(
    origins: Sequence[Origin],
    release_of: Mapping[str, date],
    weekly: Mapping[str, Mapping[date, float]],
    outcomes: Mapping[str, Mapping[int, float]],
) -> list[dict[str, Any]]:
    nxt = be.next_release_map(list(release_of.values()))
    out = []
    for o in origins:
        rel = release_of.get(o.vintage)
        stop = nxt.get(rel) if rel else None
        for k in be.OFFSETS:
            t = o.available_at + timedelta(days=k)
            if stop is None or t.date() >= stop:
                continue
            for code, sid in WPSR_SERIES.items():
                for h in (0, 1):
                    target = o.issue_idx + h
                    s = (o.steo.get(code) or {}).get(target)
                    if s is None:
                        continue
                    out.append({
                        "window": "W3", "code": code, "h": h, "offset": k, "issue": o.vintage, "t": t.isoformat(),
                        "target": label(target), "outcome": outcomes.get(code, {}).get(target), "steo": s,
                        "wpsr_nowcast": wpsr_point(weekly.get(sid, {}), t.date(), target),
                    })  # fmt: skip
    return out


def w3_tests(recs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    tests = []
    for code, h in be.W3_TESTS:
        per: dict[str, list[float]] = defaultdict(list)
        lc, lb = [], []
        for r in recs:
            if r["code"] != code or r["h"] != h or r["outcome"] is None or r["wpsr_nowcast"] is None:
                continue
            a, b = abs(r["wpsr_nowcast"] - r["outcome"]), abs(r["steo"] - r["outcome"])
            per[r["issue"]].append(a - b)
            lc.append(a)
            lb.append(b)
        d = [statistics.fmean(v) for _, v in sorted(per.items())]
        t = dm_hln(d, 1)
        row = {"window": "W3", "code": code, "h": h, "challenger": "wpsr_nowcast", "benchmark": "steo", "loss": "ae",
               "n_issues": len(d), "n_forecasts": len(lc), **t}  # fmt: skip
        if lc:
            row["mean_loss_challenger"], row["mean_loss_benchmark"] = statistics.fmean(lc), statistics.fmean(lb)
            row["ratio"] = row["mean_loss_challenger"] / row["mean_loss_benchmark"]
        tests.append(row)
    return tests


def merged_family(base_tests: Sequence[dict[str, Any]], w3: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The protocol's single Holm family with W3's p = 1 placeholders replaced by the evaluated p-values."""
    new = {(t["code"], t["h"]): t for t in w3}
    tests = [dict(new[(t["code"], t["h"])]) if t["window"] == "W3" else dict(t) for t in base_tests]
    pv = {(t["window"], t["code"], t["h"], t["challenger"]): float(t.get("p_challenger_better", 1.0)) for t in tests}
    if len(pv) != len(be.declared_tests()):
        raise ValueError("family size differs from the protocol")
    sig = holm(pv, be.ALPHA)
    for t in tests:
        t["holm_family_size"] = len(pv)
        t["holm_win"] = sig[(t["window"], t["code"], t["h"], t["challenger"])]
    return {"family_size": len(pv), "tests": tests, "wins": [t for t in tests if t["holm_win"]]}


def weekly_from_frame(frame: Any) -> dict[str, dict[date, float]]:
    import polars as pl

    out: dict[str, dict[date, float]] = {}
    for sid in WPSR_SERIES.values():
        f = frame.filter((pl.col("indicator") == f"EIA_WPSR|series={sid}") & pl.col("value").is_not_null())
        out[sid] = {pe - timedelta(days=1): float(v) / SCALE for pe, v in f.select("period_end", "value").iter_rows()}
    return out


__all__ = ["WPSR_SERIES", "merged_family", "w3_records", "w3_tests", "weekly_from_frame", "wpsr_point"]
