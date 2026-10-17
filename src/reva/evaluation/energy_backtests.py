"""Energy forecasts vs the EIA Short-Term Energy Outlook, on STEO archive vintages (V6, agent PRED-ENERGY).

**Design (real-time, as-of).** Each archived STEO issue (``atlas.vintage_steo.steo_archive``) is a forecast
origin. At the origin's availability instant (the day after the issue's release date, see
:mod:`reva.adapters.connectors.atlas_vintage_steo`) a :class:`reva.evaluation.vintages.VintageAccessor`
(``available_then``) returns (a) the monthly history then published (the issue's own history columns, older months
from earlier issues) — the **information set** of every REVA forecaster — and (b) the projections of the issue in
force — the **STEO** forecast, attributed to EIA. Horizon ``h`` = months after the issue month (``h = 1`` is the
month after the release month; the registry's monthly cycle horizons have the same meaning for an issue made in
the first month of a quarter).

**Outcomes.** The month's value in EIA's *current* published history: the latest ``atlas.eia_steo.steo_observed``
version (periods up to each series' declared ``lastHistoricalPeriod`` only; STEO forecast cells are never
outcomes). Outcomes are therefore the final (revised) vintage of the *target*, while inputs and forecasts are as
published at the origin — stated in every result (``outcome_basis``). A target month later than the last declared
history month is not scored (counted).

**Scores and tests.** CRPS (proper; a point forecast's CRPS is its absolute error, so EIA's point forecasts and
REVA's densities are compared on the same scale), squared error, absolute error, log score and PIT for densities.
Pairwise comparisons per (variable, horizon) on the origins where both forecasters issued: Diebold-Mariano with the
Harvey-Leybourne-Newbold small-sample correction (rectangular HAC with ``k - 1`` lags, ``k`` = overlap of the
``h``-month forecasts in origin steps; Bartlett weights if that variance is not positive), Student-t with ``n - 1``
degrees of freedom; one-sided p-value for "the challenger has the lower expected loss". **Winners**: a REVA
candidate whose CRPS advantage over the random walk is significant at 5 % after a Holm-Bonferroni correction over
every (candidate, horizon) test of that variable. Only winners (per variable and horizon) may be issued to the
registry besides the baselines and the attributed STEO projection.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import polars as pl

from reva.evaluation.cutoff import CutoffMode
from reva.evaluation.vintages import VintageAccessor
from reva.models.forecasting import energy_models as em

LABEL = "real_time_vintage_backtest_energy"
FAMILY = "eia_steo"
VINTAGE_TABLE = "atlas.vintage_steo.steo_archive"
OUTCOME_TABLE = "atlas.eia_steo.steo_observed"
HORIZONS = tuple(range(1, 13))
ALPHA = 0.05
ORIGIN_STEP_MONTHS = 3  # quarterly issues (January, April, July, October)
HONESTY = (
    "Real-time backtest on EIA STEO archive vintages: at each origin only issues whose evidenced, conservative "
    "availability instant (day after the release date) is at/before the origin are used (available_then "
    "reconstruction; REVA did not exist at these origins). Inputs and STEO forecasts as published then; outcomes "
    "from EIA's current published history (final vintage of the target). STEO forecasts are EIA's, attributed; "
    "REVA's models are candidates scored against them. No futures prices are used."
)
OUTCOME_BASIS = (
    "EIA STEO current published history (latest atlas.eia_steo.steo_observed: periods <= the series' declared "
    "lastHistoricalPeriod), i.e. the final vintage of the target month"
)


# --------------------------------------------------------------------------- data shaping (pure)


@dataclass
class Origin:
    vintage: str
    release_text: str | None
    available_at: datetime
    issue_idx: int
    info: dict[str, dict[int, float]] = field(default_factory=dict)
    steo: dict[str, dict[int, float]] = field(default_factory=dict)
    last: dict[str, int] = field(default_factory=dict)


def month_idx(d: Any) -> int:
    return int(d.year) * 12 + int(d.month) - 1


def label(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _series(rows: pl.DataFrame) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for var, ps, v in rows.select("variable", "period_start", "value").iter_rows():
        code = str(var).split(":", 1)[1]
        out[code][month_idx(ps)] = float(v)
    return dict(out)


def origins_from_cells(cells: pl.DataFrame, codes: Sequence[str] = em.INPUT_CODES) -> list[Origin]:
    """One :class:`Origin` per STEO issue with proven availability, read through the vintage accessor."""
    variables = [f"eia_steo:{c}" for c in codes]
    cells = cells.filter((pl.col("family") == FAMILY) & pl.col("variable").is_in(variables))
    issues = (
        cells.filter(pl.col("available_at").is_not_null())
        .group_by("vintage")
        .agg(pl.col("available_at").max(), pl.col("vintage_date_text").first())
        .sort("available_at")
    )
    out: list[Origin] = []
    for vintage, avail, rel in issues.iter_rows():
        acc = VintageAccessor(cells, avail, CutoffMode.available_then)
        inforce = acc.vintage_in_force(FAMILY)
        if inforce is None or inforce["vintage"] != vintage:
            continue  # a later-listed issue with an earlier instant: the accessor's answer wins
        y, m = (int(x) for x in vintage.split("-"))
        o = Origin(vintage, rel, avail, y * 12 + m - 1)
        o.info = _series(acc.latest(family=FAMILY, variable=variables).rows)
        o.steo = _series(acc.forecasts(family=FAMILY, variable=variables).rows.filter(pl.col("vintage") == vintage))
        for code, s in o.info.items():
            # the information set ends with the last month before the issue month that this issue publishes
            own = [i for i in s if i < o.issue_idx]
            if own:
                o.last[code] = max(own)
        out.append(o)
    return out


def outcomes_from_frame(frame: pl.DataFrame, codes: Sequence[str] = em.TARGET_CODES) -> dict[str, dict[int, float]]:
    """Monthly observed values of the current STEO bulk table (``STEO.<code>.M``; declared history only)."""
    want = {f"STEO.{c}.M": c for c in codes}
    f = frame.filter(pl.col("indicator").is_in(list(want)) & pl.col("value").is_not_null())
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for ind, ps, v, fl in f.select("indicator", "period_start", "value", "flags").iter_rows():
        if json.loads(fl or "{}").get("estimate_kind") in ("projection", "unknown_boundary"):
            continue
        out[want[ind]][month_idx(ps)] = float(v)
    return dict(out)


# --------------------------------------------------------------------------- statistics (pure)


def dm_hln(d: Sequence[float], k: int) -> dict[str, Any]:
    """Diebold-Mariano test with the Harvey-Leybourne-Newbold correction on loss differentials ``d`` (challenger
    minus benchmark; negative = challenger better). ``k`` = forecast overlap in origin steps (k-1 HAC lags)."""
    from scipy import stats  # type: ignore[import-untyped]

    n = len(d)
    if n < 8:
        return {"n": n, "status": "too_few_pairs"}
    mean = statistics.fmean(d)
    dev = [x - mean for x in d]
    gam = [sum(dev[i] * dev[i - j] for i in range(j, n)) / n for j in range(0, min(k, n - 1))]
    var = gam[0] + 2 * sum(gam[1:])
    kernel = "rectangular"
    if var <= 0:
        var = gam[0] + 2 * sum((1 - j / k) * g for j, g in enumerate(gam[1:], start=1))
        kernel = "bartlett"
    if var <= 0:
        return {"n": n, "status": "zero_variance", "mean_diff": mean}
    dm = mean / math.sqrt(var / n)
    corr = math.sqrt(max((n + 1 - 2 * k + k * (k - 1) / n) / n, 1e-12))
    t = dm * corr
    return {
        "n": n,
        "mean_diff": mean,
        "dm": dm,
        "hln": t,
        "p_challenger_better": float(stats.t.cdf(t, df=n - 1)),
        "p_two_sided": float(2 * stats.t.sf(abs(t), df=n - 1)),
        "hac_lags": k - 1,
        "kernel": kernel,
    }


def holm(pvals: Mapping[Any, float], alpha: float = ALPHA) -> dict[Any, bool]:
    order = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(order)
    out: dict[Any, bool] = {k: False for k in pvals}
    for i, (key, p) in enumerate(order):
        if p <= alpha / (m - i):
            out[key] = True
        else:
            break
    return out


def overlap_steps(h: int, step: int = ORIGIN_STEP_MONTHS) -> int:
    return max(1, math.ceil(h / step))


# --------------------------------------------------------------------------- backtest (pure)


def backtest(
    origins: Sequence[Origin],
    outcomes: Mapping[str, Mapping[int, float]],
    *,
    codes: Sequence[str] = em.TARGET_CODES,
    horizons: Sequence[int] = HORIZONS,
) -> dict[str, Any]:
    """Every forecaster at every origin, variable and horizon; scores; summaries; DM-HLN tests; winners."""
    records: list[dict[str, Any]] = []
    abstain: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unscored: dict[str, int] = defaultdict(int)
    for o in origins:
        for code in codes:
            last = o.last.get(code)
            if last is None:
                abstain[(code, "*")]["no information set for this variable in the issue"] += 1
                continue
            for h in horizons:
                target = o.issue_idx + h
                y = outcomes.get(code, {}).get(target)
                if y is None:
                    unscored[code] += 1
                    continue
                for m in em.models_for(code):
                    p = em.forecast(m, code, o.info, o.steo, last, target)
                    if isinstance(p, em.Abstain):
                        abstain[(code, m)][p.reason] += 1
                        continue
                    ls = em.log_score(p, y)
                    pt = em.pit(p, y)
                    records.append(
                        {
                            "code": code,
                            "model": m,
                            "origin": o.vintage,
                            "h": h,
                            "target": label(target),
                            "k": target - last,
                            "point": p.point,
                            "outcome": y,
                            "crps": em.crps(p, y),
                            "se": (p.point - y) ** 2,
                            "ae": abs(p.point - y),
                            "log_score": ls,
                            "pit": pt,
                            "dist": p.dist,
                        }
                    )
    summary, tests, winners = _summaries(records, codes, horizons)
    return {
        "label": LABEL,
        "honesty": HONESTY,
        "outcome_basis": OUTCOME_BASIS,
        "origins": [
            {"vintage": o.vintage, "release_date": o.release_text, "available_at": o.available_at.isoformat()}
            for o in origins
        ],
        "summary": summary,
        "tests": tests,
        "winners": winners,
        "abstentions": {f"{c}|{m}": dict(v) for (c, m), v in sorted(abstain.items())},
        "unscored_no_outcome_yet": dict(unscored),
        "n_records": len(records),
        "records": records,
    }


def _summaries(
    records: list[dict[str, Any]], codes: Sequence[str], horizons: Sequence[int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by: dict[tuple[str, int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in records:
        by[(r["code"], r["h"], r["model"])][r["origin"]] = r
    summary: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    winners: list[dict[str, Any]] = []
    for code in codes:
        cand_p: dict[tuple[str, int], float] = {}
        for h in horizons:
            bench = by.get((code, h, em.BENCHMARK), {})
            for m in em.models_for(code):
                rs = list(by.get((code, h, m), {}).values())
                if not rs:
                    continue
                common = [o for o in by[(code, h, m)] if o in bench]
                row = {
                    "code": code,
                    "h": h,
                    "model": m,
                    "n": len(rs),
                    "mean_crps": statistics.fmean(r["crps"] for r in rs),
                    "rmse": math.sqrt(statistics.fmean(r["se"] for r in rs)),
                    "mae": statistics.fmean(r["ae"] for r in rs),
                }
                lss = [r["log_score"] for r in rs if r["log_score"] is not None]
                row["mean_log_score"] = statistics.fmean(lss) if lss else None
                pits = [r["pit"] for r in rs if r["pit"] is not None]
                row["coverage_80"] = sum(1 for p in pits if 0.1 <= p <= 0.9) / len(pits) if pits else None
                if common and m != em.BENCHMARK:
                    c_m = statistics.fmean(by[(code, h, m)][o]["crps"] for o in common)
                    c_b = statistics.fmean(bench[o]["crps"] for o in common)
                    row["crps_ratio_vs_rw"] = c_m / c_b if c_b > 0 else None
                    s_m = statistics.fmean(by[(code, h, m)][o]["se"] for o in common)
                    s_b = statistics.fmean(bench[o]["se"] for o in common)
                    row["mse_ratio_vs_rw"] = s_m / s_b if s_b > 0 else None
                summary.append(row)
            for m in em.models_for(code):
                if m == em.BENCHMARK:
                    continue
                for bname in (em.BENCHMARK, em.REFERENCE):
                    if m == bname:
                        continue
                    b = by.get((code, h, bname), {})
                    mine = by.get((code, h, m), {})
                    common = sorted(o for o in mine if o in b)
                    for loss in ("crps", "se"):
                        d = [mine[o][loss] - b[o][loss] for o in common]
                        t = dm_hln(d, overlap_steps(h))
                        tests.append({"code": code, "h": h, "challenger": m, "benchmark": bname, "loss": loss, **t})
                        if (
                            bname == em.BENCHMARK
                            and loss == "crps"
                            and m not in (em.REFERENCE,)
                            and "p_challenger_better" in t
                        ):
                            cand_p[(m, h)] = t["p_challenger_better"]
        sig = holm(cand_p)
        for (m, h), ok in sorted(sig.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            if ok:
                winners.append(
                    {
                        "code": code,
                        "h": h,
                        "model": m,
                        "model_key": em.MODEL_KEYS[m],
                        "p_challenger_better": cand_p[(m, h)],
                        "holm_family_size": len(cand_p),
                        "loss": "crps",
                        "benchmark": em.BENCHMARK,
                    }
                )
    return summary, tests, winners


def result_digest(res: Mapping[str, Any]) -> str:
    slim = {k: v for k, v in res.items() if k != "records"}
    blob = json.dumps(slim, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- database entry point


def load_and_run(conn: Any, store: Any, *, origin: str = "live") -> dict[str, Any]:
    """Read the STEO archive vintages and the current STEO observed table, run the backtest."""
    from reva.atlas.tables import read_atlas_table
    from reva.evaluation.registry.data import latest_version
    from reva.evaluation.vintages import load_vintage_cells

    now = datetime.now(UTC)
    cells = load_vintage_cells(conn, store, origin=origin, dataset_keys=[VINTAGE_TABLE])
    if cells.height == 0:
        raise LookupError(f"{VINTAGE_TABLE} is not registered (origin {origin})")
    pin = latest_version(conn, OUTCOME_TABLE, origin=origin, cutoff=now)
    if pin is None:
        raise LookupError(f"{OUTCOME_TABLE} is not registered (origin {origin})")
    outcomes = outcomes_from_frame(read_atlas_table(store, pin["artifact_sha256"]))
    origins = origins_from_cells(cells)
    res = backtest(origins, outcomes)
    res["inputs"] = {
        "vintage_table": VINTAGE_TABLE,
        "vintage_table_recorded_at": str(cells["table_recorded_at"].max()),
        "vintage_issues": len(origins),
        "outcome_table": {k: pin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")},
        "run_at": now.isoformat(),
    }
    res["digest"] = result_digest(res)
    return res


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="EIA STEO vintage backtest (PRED-ENERGY)")
    ap.add_argument("--origin", default="live")
    ap.add_argument("--out", required=True, help="JSON result path (records included)")
    ap.add_argument("--winners", help="write the winners YAML here (registry/forecasts/energy_winners.yaml)")
    a = ap.parse_args(argv)
    eng = make_engine("api", get_settings())
    with eng.connect() as conn:
        res = load_and_run(conn, get_store(), origin=a.origin)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, default=str)
    if a.winners:
        write_winners(res, a.winners)
    print(json.dumps({"digest": res["digest"], "n_records": res["n_records"], "winners": res["winners"]}, indent=1))
    return 0


def write_winners(res: Mapping[str, Any], path: str) -> None:
    import yaml

    doc = {
        "schema": "reva-energy-winners/1",
        "backtest_label": LABEL,
        "backtest_digest": res["digest"],
        "rule": (
            f"REVA candidate whose CRPS beats the random walk (DM-HLN one-sided p, Holm-Bonferroni over every "
            f"(candidate, horizon) of the variable, alpha {ALPHA}) on the STEO archive vintage backtest"
        ),
        "origins": [o["vintage"] for o in res["origins"]],
        "winners": [
            {k: w[k] for k in ("code", "h", "model", "model_key", "p_challenger_better", "holm_family_size")}
            for w in res["winners"]
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Generated by `python -m reva.evaluation.energy_backtests` (PRED-ENERGY). Do not edit by hand.\n")
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
