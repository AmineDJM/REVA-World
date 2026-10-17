"""Rolling pseudo-real-time back-test of the finance families (V6, agent PRED-FIN).

**What it is.** For every finance family of ``registry/forecasts/question_specs.yaml`` (``finance`` block), every
month-end origin (last published day of a month) from ``start_year`` on, every horizon ``h`` of the family (months),
each benchmark and candidate model forecasts the family's target (month-end value or monthly mean of month
``origin + h``) **using only data published at the origin**: the series itself up to the origin day, auxiliary
daily inputs lagged by their declared ``lag_days`` (BIS bulk files: 7), monthly CPI with its publication lag.
Values are the *final vintage* (today's table versions): daily market data are rarely revised, but this is still
labelled ``final-vintage pseudo-real-time``, never real-time skill.

**Scores.** CRPS (primary), absolute and squared error, 80 % interval score and coverage, Brier score of the
direction question ("above the latest monthly value at the origin"), and for policy meetings Brier and log score
of "the rate in force ``window_days`` after the origin is higher / lower than at the origin".

**Tests.** Diebold-Mariano with the Harvey-Leybourne-Newbold correction (:func:`reva.models.tournament.dm_hln_test`,
lag ``h - 1`` for ``h``-month forecasts made monthly; 1 for the 42-day meeting windows) on the time series of loss
differentials *model − reference* (reference: ``fin.rw_histvol``, the random walk, for levels;
``fin.meeting_base_rate`` for meetings). One-sided p-values; Holm step-down across all (family, horizon) cells of
one model on its primary metric at 5 %. A candidate is a **significant winner** on a cell only if Holm rejects
there; only such cells may add the candidate to a family's sealed forecasters. DM is conservative for nested
comparisons (the random walk is nested in the drift models; Clark & West 2007) — stated in the report.

Research evaluation only: not investment advice, not a trading signal (:data:`DISCLAIMER`).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from reva.evaluation.registry import finance as FN
from reva.evaluation.registry import periods as P
from reva.evaluation.registry.scores import crps as crps_of
from reva.evaluation.registry.specs import Family, SpecSet
from reva.models.forecasting import finance_models as F
from reva.models.forecasting.registry_baselines import Abstain, Predictive
from reva.models.metrics import interval_covers, interval_score
from reva.models.tournament import dm_hln_test, holm_rejections

SCHEMA = "reva-finance-backtest/1"
LABEL = (
    "final-vintage pseudo-real-time back-test: each forecast uses only values dated (and, for inputs, published) "
    "up to its origin, but read from today's table versions; never real-time skill"
)
DISCLAIMER = F.DISCLAIMER
LEVEL_REF = "fin.rw_histvol"
MEETING_REF = "fin.meeting_base_rate"
MEETING_WINDOW_DAYS = 42
ALPHA = 0.05
INTERVAL = 0.8


@dataclass
class LevelCase:
    family: str
    fin: Mapping[str, Any]
    target_kind: str  # month_end | month_mean
    series: list[tuple[date, float]]
    horizons: tuple[int, ...]
    models: tuple[str, ...]
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeetingCase:
    family: str
    fin: Mapping[str, Any]
    history: list[tuple[date, float]]
    inflation: dict[int, float]
    models: tuple[str, ...]


# --------------------------------------------------------------------------- case building


def _models(fam: Family) -> tuple[str, ...]:
    assert fam.finance is not None
    ms = list(fam.finance.get("forecasters") or []) + list(fam.finance.get("candidates") or [])
    return tuple(dict.fromkeys(ms))


def build_cases(specs: SpecSet, frames: Mapping[str, pl.DataFrame]) -> tuple[list[LevelCase], list[MeetingCase]]:
    """Cases from the finance families of the specs (``frames``: dataset_key -> pinned frame)."""
    from reva.evaluation.registry.questions import family_daily_cells

    levels: list[LevelCase] = []
    meetings: list[MeetingCase] = []
    for fam in specs.families:
        if fam.finance is None or fam.dataset_key is None or fam.dataset_key not in frames:
            continue
        inputs = FN.load_inputs(fam.finance, frames, _models(fam))
        if fam.finance["kind"] == "meeting":
            meetings.append(
                MeetingCase(
                    fam.key, fam.finance, inputs.get("history") or [], inputs.get("inflation") or {}, _models(fam)
                )
            )
            continue
        g = fam.geos[0]
        cells = family_daily_cells(frames[fam.dataset_key], fam, g.provider_geo)
        kind = "month_mean" if fam.value_rule == "monthly_mean_of_daily" else "month_end"
        hs = tuple(sorted({h for r in fam.questions for h in r.horizons}))
        levels.append(LevelCase(fam.key, fam.finance, kind, FN.to_floats(cells), hs, _models(fam), inputs))
    return levels, meetings


# --------------------------------------------------------------------------- level families


def _month_groups(series: Sequence[tuple[date, float]]) -> dict[int, list[int]]:
    """month index -> positions in ``series`` of its published days."""
    g: dict[int, list[int]] = defaultdict(list)
    for i, (d, _) in enumerate(series):
        g[P.index_of("M", d)].append(i)
    return g


def _value(series: Sequence[tuple[date, float]], pos: Sequence[int], kind: str) -> float:
    if kind == "month_end":
        return series[pos[-1]][1]
    return math.fsum(series[i][1] for i in pos) / len(pos)


@dataclass
class _Ctx:
    """Per-case precomputation shared by every origin."""

    month_curves: list[tuple[int, dict[float, float]]] = field(default_factory=list)


def _prepare(case: LevelCase) -> _Ctx:
    ctx = _Ctx()
    curves: dict[date, dict[float, float]] = case.inputs.get("curve") or {}
    if curves:
        last: dict[int, date] = {}
        for d in sorted(curves):
            last[P.index_of("M", d)] = d
        ctx.month_curves = [(m, curves[last[m]]) for m in sorted(last)]
    return ctx


def _forecast(
    model: str,
    case: LevelCase,
    ctx: _Ctx,
    hist: Sequence[tuple[date, float]],
    target: F.Target,
    garch: F.Garch | Abstain | None,
) -> Predictive | Abstain:
    fin = case.fin
    origin = hist[-1][0]
    if model == "fin.garch11":
        if garch is None or isinstance(garch, Abstain):
            return garch or Abstain("no GARCH fit")
        ks = F.steps(origin, target)
        if isinstance(ks, Abstain):
            return ks
        var = F.target_variance(garch.step_vars(max(ks)), ks)
        return F.predictive(hist[-1][1], 0.0, var, fin["scale"], {"alpha": f"{garch.alpha:.4f}"})
    if model == "fin.ns_dl":
        curves = case.inputs.get("curve") or {}
        cur = curves.get(origin)
        if cur is None:
            return Abstain("no par curve on the origin day")
        om = P.index_of("M", origin)
        hist_c = [c for m, c in ctx.month_curves if m <= om]
        mat = float(fin["maturity"])
        minus = float(fin["minus_maturity"]) if fin.get("minus_maturity") is not None else None
        return F.ns_dl_forecast(hist_c, cur, hist, target, mat, minus)
    return FN.run_model(model, fin, hist, target, case.inputs)


def _scores(p: Predictive, y: float, threshold: float) -> dict[str, float | None]:
    c = crps_of(p, y)
    iv = p.interval(INTERVAL)
    pr = p.prob_above(threshold)
    o = 1 if y > threshold else 0
    return {
        "crps": c,
        "ae": abs(p.point - y),
        "se": (p.point - y) ** 2,
        "is": interval_score(iv, y, INTERVAL) if iv else None,
        "cov": (1.0 if interval_covers(iv, y) else 0.0) if iv else None,
        "brier": (pr - o) ** 2 if pr is not None else None,
        "prob": pr,
        "event": float(o),
    }


def run_level_case(case: LevelCase, start_year: int = 2000) -> list[dict[str, Any]]:
    """Scored replays ``{family, model, h, origin (month index), metrics...}`` of one level case."""
    s = case.series
    groups = _month_groups(s)
    months = sorted(groups)
    if len(months) < 3:
        return []
    last_complete = months[-2]  # the latest month may be incomplete
    ctx = _prepare(case)
    rows: list[dict[str, Any]] = []
    scale = case.fin["scale"]
    for om in months:
        if om < start_year * 12 or om >= last_complete:
            continue
        pos = groups[om]
        cut = pos[-1]
        hist = s[: cut + 1]
        threshold = _value(s, pos, case.target_kind)
        tasks = [(h, om + h) for h in case.horizons if om + h <= last_complete and (om + h) in groups]
        if not tasks:
            continue
        garch = None
        if "fin.garch11" in case.models:
            garch = F.garch11_fit(F.daily_changes(hist, scale, F.GARCH_WINDOW))
        for h, tm in tasks:
            y = _value(s, groups[tm], case.target_kind)
            target = F.Target(case.target_kind, tm // 12, tm % 12 + 1)  # type: ignore[arg-type]
            for mk in case.models:
                p = _forecast(mk, case, ctx, hist, target, garch)
                if isinstance(p, Abstain):
                    continue
                rows.append({"family": case.family, "pool": f"{case.fin['kind']}_{case.target_kind}", "model": mk,
                             "h": h, "origin": om, "y": y, **_scores(p, y, threshold)})  # fmt: skip
    return rows


# --------------------------------------------------------------------------- meeting families


def run_meeting_case(case: MeetingCase, start_year: int = 2000, window_days: int = MEETING_WINDOW_DAYS
                     ) -> list[dict[str, Any]]:  # fmt: skip
    """Monthly origins (last weekday of each month): P(level in force ``window_days`` later moved in the family's
    direction), every model trained only on windows that ended by the origin."""
    hist = case.history
    if not hist:
        return []
    days = [d for d, _ in hist]
    direction = case.fin["direction"]
    lag = int(((case.fin.get("inputs") or {}).get("inflation") or {}).get("lag_days", 45))
    feat = FN.taylor_feature(case.inflation, lag)
    allw = F.all_windows(hist, window_days, days[-1], feat)
    rows: list[dict[str, Any]] = []
    y, m = start_year, 1
    while True:
        o = F.last_weekday(y, m)
        end = o + timedelta(days=window_days)
        if end > days[-1]:
            break
        if o >= days[0]:
            known = F.windows_known_at(allw, o)
            a, b = F.level_on(hist, o, days), F.level_on(hist, end, days)
            if a is not None and b is not None:
                ev = 1 if ((b > a + 1e-9) if direction == "up" else (b < a - 1e-9)) else 0
                state = F.last_move(hist, o, days=days)
                for mk in case.models:
                    if mk == "fin.meeting_base_rate":
                        pr = F.meeting_base_rate(known, direction)
                    elif mk == "fin.meeting_momentum":
                        pr = F.meeting_momentum(known, state, direction)
                    elif mk == "fin.taylor_logit":
                        pr = F.taylor_logit(known, feat(o, a), state, direction)
                    else:
                        continue
                    if isinstance(pr, Abstain):
                        continue
                    q = pr if ev else 1.0 - pr
                    rows.append({"family": case.family, "pool": f"meeting_{direction}", "model": mk, "h": 1,
                                 "origin": P.index_of("M", o),
                                 "event": float(ev), "prob": pr, "brier": (pr - ev) ** 2,
                                 "logs": -math.log(q) if q > 0 else None})  # fmt: skip
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return rows


# --------------------------------------------------------------------------- comparison


def _mean(xs: Sequence[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return math.fsum(v) / len(v) if v else None


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(rows)}
    for k in ("crps", "ae", "se", "is", "cov", "brier", "logs", "event", "prob"):
        out[k] = _mean([r.get(k) for r in rows])
    if out.get("se") is not None:
        out["rmse"] = math.sqrt(out["se"])
    return out


def compare(
    rows: Sequence[Mapping[str, Any]], ref: str, metric: str, *, dm_horizon: int | None = None
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """DM-HLN of ``metric`` loss differentials model − ``ref`` per (family, model, h) on common origins."""
    by: dict[tuple[str, str, int], dict[int, float]] = defaultdict(dict)
    for r in rows:
        v = r.get(metric)
        if v is not None:
            by[(r["family"], r["model"], int(r["h"]))][int(r["origin"])] = float(v)
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for (fam, mk, h), vals in sorted(by.items()):
        if mk == ref:
            continue
        base = by.get((fam, ref, h), {})
        common = sorted(set(vals) & set(base))
        if len(common) < 12:
            continue
        d = [vals[o] - base[o] for o in common]
        mm, mb = math.fsum(vals[o] for o in common) / len(common), math.fsum(base[o] for o in common) / len(common)
        t = dm_hln_test(d, dm_horizon if dm_horizon is not None else h)
        out[(fam, mk, h)] = {
            "family": fam,
            "model": mk,
            "h": h,
            "metric": metric,
            "reference": ref,
            "n": len(common),
            "first_origin": P.label_of("M", common[0]),
            "last_origin": P.label_of("M", common[-1]),
            "mean_model": mm,
            "mean_reference": mb,
            "ratio": mm / mb if mb > 0 else None,
            "dm_stat": t.statistic,
            "p_better": t.p_less,
            "p_worse": t.p_greater,
        }
    return out


def compare_pooled(
    rows: Sequence[Mapping[str, Any]], ref: str, metric: str, *, dm_horizon: int | None = None
) -> list[dict[str, Any]]:
    """Informational panel test per (pool, model, h): at each origin, the mean over the pool's families of the
    loss differential model − ``ref`` scaled by the reference's mean loss in that (family, h) (scale-free), then
    DM-HLN on that series. Never used to admit a candidate (winners are per family cell)."""
    by: dict[tuple[str, str, str, int], dict[int, float]] = defaultdict(dict)
    for r in rows:
        v = r.get(metric)
        if v is not None:
            by[(str(r.get("pool")), r["family"], r["model"], int(r["h"]))][int(r["origin"])] = float(v)
    scale: dict[tuple[str, int], float] = {}
    for (_pool, fam, mk, h), vals in by.items():
        if mk == ref and vals:
            scale[(fam, h)] = math.fsum(vals.values()) / len(vals)
    acc: dict[tuple[str, str, int], dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    fams: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for (pool, fam, mk, h), vals in by.items():
        if mk == ref or not scale.get((fam, h)):
            continue
        base = by.get((pool, fam, ref, h), {})
        for o in set(vals) & set(base):
            acc[(pool, mk, h)][o].append((vals[o] - base[o]) / scale[(fam, h)])
        fams[(pool, mk, h)].add(fam)
    out: list[dict[str, Any]] = []
    for (pool, mk, h), per in sorted(acc.items()):
        origins = sorted(per)
        if len(origins) < 12:
            continue
        d = [math.fsum(per[o]) / len(per[o]) for o in origins]
        t = dm_hln_test(d, dm_horizon if dm_horizon is not None else h)
        out.append({"pool": pool, "model": mk, "h": h, "metric": metric, "reference": ref,
                    "families": sorted(fams[(pool, mk, h)]), "n_origins": len(origins),
                    "mean_scaled_diff": math.fsum(d) / len(d), "dm_stat": t.statistic, "p_better": t.p_less,
                    "p_worse": t.p_greater})  # fmt: skip
    return out


def holm_by_model(tests: Mapping[tuple[str, str, int], dict[str, Any]], alpha: float = ALPHA) -> None:
    """Holm step-down per model across its (family, horizon) cells, both directions (adds ``holm_better`` /
    ``holm_worse`` booleans in place)."""
    models = sorted({k[1] for k in tests})
    for mk in models:
        keys = [k for k in tests if k[1] == mk]
        for side, flag in (("p_better", "holm_better"), ("p_worse", "holm_worse")):
            pv = {i: float(tests[k][side]) for i, k in enumerate(keys)}
            rej = holm_rejections(pv, alpha)
            for i, k in enumerate(keys):
                tests[k][flag] = i in rej


def _benchmarks_in(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    bench = {k for k, (_, kind, _) in F.FIN_MODELS.items() if kind == "benchmark"}
    return sorted({str(r["model"]) for r in rows} & bench)


def evaluate(level_rows: Sequence[Mapping[str, Any]], meeting_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summaries per (family, model, h); DM-HLN tests with Holm against the reference (random walk / base rate) and
    against every other benchmark; significant winners.

    A candidate wins a (family, horizon) cell only if it is significantly better (one-sided DM-HLN, Holm per model
    and benchmark across cells, 5 %) than **every** benchmark scored on that cell (intersection-union rule: no
    further multiplicity correction needed, and a candidate cannot win by beating only the weakest benchmark)."""
    summary: list[dict[str, Any]] = []
    groups: dict[tuple[str, str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for r in [*level_rows, *meeting_rows]:
        groups[(r["family"], r["model"], int(r["h"]))].append(r)
    for (fam, mk, h), rs in sorted(groups.items()):
        summary.append({"family": fam, "model": mk, "h": h, **summarise(rs)})
    vs: dict[str, dict[tuple[str, str, int], dict[str, Any]]] = {}
    for ref in _benchmarks_in(level_rows):
        t = compare(level_rows, ref, "crps")
        holm_by_model(t)
        vs[ref] = t
    for ref in _benchmarks_in(meeting_rows):
        t = compare(meeting_rows, ref, "brier", dm_horizon=2)
        holm_by_model(t)
        vs[ref] = t
    primary = {**vs.get(LEVEL_REF, {}), **vs.get(MEETING_REF, {})}
    secondary: dict[str, list[dict[str, Any]]] = {}
    for metric in ("se", "brier", "is"):
        t = compare(level_rows, LEVEL_REF, metric)
        holm_by_model(t)
        secondary[metric] = list(t.values())
    t = compare(meeting_rows, MEETING_REF, "logs", dm_horizon=2)
    holm_by_model(t)
    secondary["meeting_log_score"] = list(t.values())
    others = [t for ref, tt in sorted(vs.items()) if ref not in (LEVEL_REF, MEETING_REF) for t in tt.values()]
    candidates = {k for k, (_, kind, _) in F.FIN_MODELS.items() if kind == "candidate"}
    winners: list[dict[str, Any]] = []
    for key, v in sorted(primary.items()):
        if v["model"] not in candidates:
            continue
        refs = {ref: tt[key] for ref, tt in vs.items() if key in tt}
        if refs and all(x.get("holm_better") for x in refs.values()):
            winners.append(
                {
                    "family": v["family"],
                    "model": v["model"],
                    "h": v["h"],
                    "metric": v["metric"],
                    "vs": {
                        ref: {"ratio": x["ratio"], "p_better": x["p_better"], "n": x["n"]} for ref, x in refs.items()
                    },
                }
            )
    pooled = compare_pooled(level_rows, LEVEL_REF, "crps") + compare_pooled(
        meeting_rows, MEETING_REF, "brier", dm_horizon=2
    )
    return {
        "summary": summary,
        "pooled_informational": pooled,
        "tests_primary": list(primary.values()),
        "tests_vs_other_benchmarks": others,
        "tests_secondary": secondary,
        "winners": winners,
    }


def run(
    specs: SpecSet, frames: Mapping[str, pl.DataFrame], *, start_year: int = 2000, families: Sequence[str] | None = None
) -> dict[str, Any]:
    """Pure: the whole back-test from pinned frames."""
    levels, meetings = build_cases(specs, frames)
    if families:
        levels = [c for c in levels if c.family in families]
        meetings = [c for c in meetings if c.family in families]
    lrows: list[dict[str, Any]] = []
    for c in levels:
        lrows.extend(run_level_case(c, start_year))
    mrows: list[dict[str, Any]] = []
    for mc in meetings:
        mrows.extend(run_meeting_case(mc, start_year))
    ev = evaluate(lrows, mrows)
    return {
        "schema": SCHEMA,
        "label": LABEL,
        "disclaimer": DISCLAIMER,
        "vintage_mode": "final_vintage_pseudo_real_time",
        "start_year": start_year,
        "reference": {"levels": LEVEL_REF, "meetings": MEETING_REF},
        "tests": "Diebold-Mariano with HLN correction, one-sided, Holm step-down per model and benchmark across "
        f"(family, horizon) cells at {ALPHA}; primary metric CRPS (levels) / Brier (meetings); a candidate wins a "
        "cell only if significantly better than every benchmark scored on it",
        "meeting_window_days": MEETING_WINDOW_DAYS,
        "families": {
            c.family: {
                "models": list(c.models),
                "horizons": list(c.horizons),
                "target": c.target_kind,
                "first_day": c.series[0][0].isoformat() if c.series else None,
                "last_day": c.series[-1][0].isoformat() if c.series else None,
            }
            for c in levels
        }
        | {
            c.family: {
                "models": list(c.models),
                "direction": c.fin["direction"],
                "first_day": c.history[0][0].isoformat() if c.history else None,
                "last_day": c.history[-1][0].isoformat() if c.history else None,
            }
            for c in meetings
        },
        "n_level_replays": len(lrows),
        "n_meeting_replays": len(mrows),
        **ev,
    }


__all__ = [
    "DISCLAIMER",
    "LABEL",
    "LevelCase",
    "MeetingCase",
    "build_cases",
    "compare",
    "evaluate",
    "holm_by_model",
    "run",
    "run_level_case",
    "run_meeting_case",
    "summarise",
]


# --------------------------------------------------------------------------- database runner


def input_dataset_keys(specs: SpecSet, families: Sequence[str] | None = None) -> list[str]:
    """Every table a finance family reads (its own table and its inputs)."""
    keys: set[str] = set()
    for fam in specs.families:
        if fam.finance is None or fam.dataset_key is None or (families and fam.key not in families):
            continue
        keys.add(fam.dataset_key)
        for inp in FN.used_inputs(fam.finance, _models(fam)).values():
            keys.add(inp["dataset_key"])
    return sorted(keys)


def run_recorded(
    engine: Any,
    store: Any,
    *,
    origin: str = "live",
    start_year: int = 2000,
    families: Sequence[str] | None = None,
    record: bool = True,
    actor: str = "worker:forecast_registry",
) -> dict[str, Any]:
    """Pin the latest version of every input table, run the back-test and (``record``) store the manifest
    content-addressed with a provenance activity and one ``model.evaluation`` row per finance model
    (``metrics.kind = 'finance_backtest'``, replay mode ``revised_data_retrospective``)."""
    from datetime import datetime

    from sqlalchemy import text

    from reva.evaluation.registry.data import FrameCache, latest_version
    from reva.evaluation.registry.service import _code_digest, _insert_eval, _short_pin, model_version_ids
    from reva.evaluation.registry.specs import load_specs

    specs = load_specs()
    with engine.connect() as conn:
        cutoff: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        pins = {k: latest_version(conn, k, origin=origin, cutoff=cutoff) for k in input_dataset_keys(specs, families)}
    cache = FrameCache(store, size=len(pins) + 1)
    frames = {k: cache.get(p["artifact_sha256"]) for k, p in pins.items() if p}
    manifest = run(specs, frames, start_year=start_year, families=families)
    manifest["origin"] = origin
    manifest["run_at"] = cutoff.isoformat()
    manifest["data"] = {"tables": {k: _short_pin(p) for k, p in pins.items() if p},
                        "missing_tables": sorted(k for k, p in pins.items() if not p)}  # fmt: skip
    manifest["specs"] = {"version": specs.version, "sha256": specs.sha256}
    manifest["code_digest"] = _code_digest()
    if not record:
        return manifest
    from reva.adapters.postgres import repositories as repo

    with engine.connect() as conn:
        mv = model_version_ids(conn)
    with engine.begin() as conn:
        sha = repo.store_json_artifact(conn, store, manifest, "evaluation", schema_version="finance-backtest/1")
        act = repo.simple_activity(
            "evaluation.finance_backtest",
            "evaluation",
            actor,
            {"label": LABEL, "sha256": sha, "disclaimer": DISCLAIMER},
        )
        repo.record_activity(
            conn,
            act,
            inputs=[
                (t["artifact_sha256"], {"role": "atlas_table", "dataset_key": k})
                for k, t in sorted(manifest["data"]["tables"].items())
            ],
            outputs=[sha],
        )
        ctx = repo.create_context(conn, mode="evaluation", origin=origin, manifest_sha256=sha, knowledge_cutoff=cutoff)
        scope = {"kind": "finance_backtest", "origin": origin, "vintage_mode": "final_vintage_pseudo_real_time",
                 "families": sorted(manifest["families"])}  # fmt: skip
        for mk, (ver, kind, _d) in sorted(F.FIN_MODELS.items()):
            per = [r for r in manifest["summary"] if r["model"] == mk]
            tests = [t for t in manifest["tests_primary"] if t["model"] == mk]
            _insert_eval(
                conn, mv[mk], ctx, sha, "revised_data_retrospective", scope,
                {"kind": "finance_backtest", "label": LABEL, "disclaimer": DISCLAIMER, "model": f"{mk}@{ver}",
                 "model_role": kind, "backtest_sha256": sha, "summary": per, "tests": tests,
                 "winner_cells": [w for w in manifest["winners"] if w["model"] == mk]},
                act.id,
            )  # fmt: skip
    return {**manifest, "backtest_sha256": sha}
