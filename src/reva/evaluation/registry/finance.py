"""Finance question families of the forecast registry (V6, agent PRED-FIN): daily inputs and forecasters (pure).

A finance family declares a ``finance`` block in ``registry/forecasts/question_specs.yaml``::

    finance:
      kind: fx | yield | spread | oil | meeting
      scale: log | level
      forecasters: [fin.rw_histvol, ...]      # keys of reva.models.forecasting.finance_models.FIN_MODELS
      generic_baselines: true | false         # also run the registry's monthly baselines (persistence, AR, ...)
      inputs: {name: {dataset_key, indicator | indicator_prefix, provider_geo, lag_days, frequency: D | M}}
      maturity / minus_maturity              # yield and spread families (years)
      direction: up | down                    # meeting families
      calendar: {dataset_key, indicator, provider_geo}, resolution_offset_days, meetings_ahead

Every input table is pinned at issue (latest version at ``issued_at``, same rule as the family's own table) and
listed in the sealed manifest. Inputs are read *as published at the origin*: a daily input with ``lag_days`` = 7
(BIS bulk files, weekly) uses the latest value dated at least 7 days before the origin; a monthly input (CPI) uses
months whose end is at least ``lag_days`` before the origin.

Every finance question carries :data:`~reva.models.forecasting.finance_models.DISCLAIMER` (research forecast, not
advice, not a trading signal).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from reva.evaluation.registry import periods as P
from reva.models.forecasting import finance_models as F
from reva.models.forecasting.registry_baselines import Abstain, Predictive

FIN_KINDS = ("fx", "yield", "spread", "oil", "meeting")
DAILY_RULES = ("monthly_mean_of_daily", "month_end_of_daily", "month_end_spread_of_daily", "meeting_change")

Ref = dict[str, Any]


# --------------------------------------------------------------------------- frame reads


def _json(raw: str | None) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def daily_cells(frame: pl.DataFrame, indicator: str, provider_geo: str) -> list[tuple[date, str, Ref]]:
    """Published day cells of one series, sorted by day: ``(day, value text, {raw_sha256, selector, flags})``."""
    f = frame.filter(
        (pl.col("indicator") == indicator)
        & (pl.col("provider_geo") == provider_geo)
        & pl.col("value").is_not_null()
        & ((pl.col("period_end") - pl.col("period_start")).dt.total_days() == 1)
    ).sort("period_start")
    out = []
    for r in f.select("period_start", "value", "raw_sha256", "selector", "flags").iter_rows(named=True):
        out.append(
            (
                r["period_start"],
                str(r["value"]),
                {"raw_sha256": r["raw_sha256"], "selector": _json(r["selector"]), "flags": _json(r["flags"])},
            )
        )
    days = [c[0] for c in out]
    if len(set(days)) != len(days):
        raise ValueError(f"several cells per day for {indicator} / {provider_geo}")
    return out


def to_floats(cells: Sequence[tuple[date, str, Ref]]) -> list[tuple[date, float]]:
    return [(d, float(Decimal(v))) for d, v, _ in cells]


def spread_cells(
    a: Sequence[tuple[date, str, Ref]], b: Sequence[tuple[date, str, Ref]]
) -> list[tuple[date, str, Ref]]:
    """Day-by-day ``a - b`` (decimal arithmetic) on the days both are published; refs of both cells kept."""
    bb = {d: (v, r) for d, v, r in b}
    out = []
    for d, v, r in a:
        if d in bb:
            w, r2 = bb[d]
            out.append((d, format(Decimal(v) - Decimal(w), "f"), {"cells": [r, r2]}))
    return out


def curve_frame(frame: pl.DataFrame, prefix: str, provider_geo: str) -> dict[date, dict[float, float]]:
    """Daily par curves ``{day: {maturity_years: yield}}`` from the ``<prefix><tenor>`` indicators."""
    f = frame.filter(
        pl.col("indicator").str.starts_with(prefix) & (pl.col("provider_geo") == provider_geo) & pl.col("value").is_not_null()
    )
    out: dict[date, dict[float, float]] = {}
    for ind, d, v in f.select("indicator", "period_start", "value").iter_rows():
        m = F.UST_TENORS.get(ind[len(prefix) :])
        if m is None:
            continue
        out.setdefault(d, {})[m] = float(Decimal(v))
    return out


def monthly_values(frame: pl.DataFrame, indicator: str, provider_geo: str) -> dict[int, float]:
    """Calendar-month cells of one series: ``{month index: value}``."""
    f = frame.filter(
        (pl.col("indicator") == indicator) & (pl.col("provider_geo") == provider_geo) & pl.col("value").is_not_null()
    )
    out: dict[int, float] = {}
    for s, e, v in f.select("period_start", "period_end", "value").iter_rows():
        cl = P.classify(s, e)
        if cl is not None and cl[0] == "M":
            out[cl[1]] = float(Decimal(v))
    return out


def published_months(values: Mapping[int, float], origin: date, lag_days: int) -> dict[int, float]:
    """Months whose end is at least ``lag_days`` before ``origin`` (pseudo-real-time availability)."""
    return {m: v for m, v in values.items() if P.end_of("M", m) + timedelta(days=lag_days) <= origin}


def as_of(series: Sequence[tuple[date, float]], origin: date, lag_days: int = 0) -> list[tuple[date, float]]:
    cut = origin - timedelta(days=lag_days)
    return [x for x in series if x[0] <= cut]


def latest(series: Sequence[tuple[date, float]], origin: date, lag_days: int = 0, max_age_days: int = 45
           ) -> float | None:  # fmt: skip
    s = as_of(series, origin, lag_days)
    if not s or (origin - s[-1][0]).days > lag_days + max_age_days:
        return None
    return s[-1][1]


def month_end_series(daily: Sequence[tuple[date, float]]) -> list[tuple[int, float]]:
    """Month-end values (last published day of each month, the last month excluded: it may be incomplete)."""
    by: dict[int, float] = {}
    for d, v in daily:
        by[P.index_of("M", d)] = v
    if by:
        by.pop(max(by))
    return sorted(by.items())


# --------------------------------------------------------------------------- inputs at issue


def load_inputs(fin: Mapping[str, Any], frames: Mapping[str, pl.DataFrame]) -> dict[str, Any]:
    """Raw input series of a finance family from the pinned frames (``frames``: dataset_key -> frame)."""
    out: dict[str, Any] = {}
    for name, spec in (fin.get("inputs") or {}).items():
        frame = frames.get(spec["dataset_key"])
        if frame is None:
            continue
        if spec.get("indicator_prefix"):
            out[name] = curve_frame(frame, spec["indicator_prefix"], spec["provider_geo"])
        elif spec.get("frequency", "D") == "M":
            out[name] = monthly_values(frame, spec["indicator"], spec["provider_geo"])
        else:
            out[name] = to_floats(daily_cells(frame, spec["indicator"], spec["provider_geo"]))
    return out


def target_of(q: Mapping[str, Any]) -> F.Target:
    idx = P.parse_label("M", str(q["reference_period"]))
    kind = "month_mean" if q["resolution"]["value_rule"] == "monthly_mean_of_daily" else "month_end"
    return F.Target(kind, idx // 12, idx % 12 + 1)


def _lag(fin: Mapping[str, Any], name: str) -> int:
    return int(((fin.get("inputs") or {}).get(name) or {}).get("lag_days", 0))


def run_model(
    model: str,
    fin: Mapping[str, Any],
    series: Sequence[tuple[date, float]],
    target: F.Target,
    inputs: Mapping[str, Any],
) -> Predictive | Abstain:
    """One finance forecaster for a month-end / month-mean target, as of the last day of ``series``."""
    if not series:
        return Abstain("no usable history")
    scale = fin["scale"]
    origin = series[-1][0]
    if model == "fin.rw_histvol":
        return F.rw_histvol(series, target, scale)
    if model == "fin.garch11":
        return F.garch_forecast(series, target, scale)
    if model == "fin.fx_forward_cip":
        iq = latest(inputs.get("rate_quote") or [], origin, _lag(fin, "rate_quote"))
        ib = latest(inputs.get("rate_base") or [], origin, _lag(fin, "rate_base"))
        return F.fx_forward_cip(series, target, iq, ib)
    if model in ("fin.ust_eh_forward", "fin.ns_dl"):
        curves: Mapping[date, Mapping[float, float]] = inputs.get("curve") or {}
        cur = curves.get(origin)
        mat = float(fin["maturity"])
        minus = float(fin["minus_maturity"]) if fin.get("minus_maturity") is not None else None
        if model == "fin.ust_eh_forward":
            return F.ust_eh_forward(series, target, cur, mat, minus)
        if cur is None:
            return Abstain("no par curve published on the origin day")
        month_days = month_end_series([(d, 0.0) for d in sorted(curves) if d <= origin])
        hist = [curves[_last_day_of(curves, m)] for m, _ in month_days]
        return F.ns_dl_forecast(hist, cur, series, target, mat, minus)
    if model == "fin.ppp_reversion":
        cb = published_months(inputs.get("cpi_base") or {}, origin, _lag(fin, "cpi_base"))
        cq = published_months(inputs.get("cpi_quote") or {}, origin, _lag(fin, "cpi_quote"))
        return F.ppp_reversion(series, target, month_end_series(series), cb, cq)
    return Abstain(f"model {model} does not forecast {fin['kind']} levels")


def _last_day_of(curves: Mapping[date, Any], month_idx: int) -> date:
    return max(d for d in curves if P.index_of("M", d) == month_idx)


# --------------------------------------------------------------------------- policy meetings


def taylor_feature(inflation: Mapping[int, float], lag_days: int):  # type: ignore[no-untyped-def]
    """``x = 1.5 * pi - i`` with the latest inflation (y/y, %) published ``lag_days`` after its month end."""

    def feat(d: date, level: float) -> float | None:
        pub = published_months(inflation, d, lag_days)
        if not pub:
            return None
        m = max(pub)
        if P.index_of("M", d) - m > 4:
            return None
        return 1.5 * pub[m] - level

    return feat


def meeting_prob(
    model: str,
    fin: Mapping[str, Any],
    history: Sequence[tuple[date, float]],
    length_days: int,
    origin: date,
    inputs: Mapping[str, Any],
) -> float | Abstain:
    """P(the level in force ``length_days`` after the last published day moved in ``fin.direction``)."""
    direction = fin["direction"]
    if not history:
        return Abstain("no usable history")
    if length_days < 1:
        return Abstain("resolution day not after the last published day")
    infl = inputs.get("inflation") or {}
    feat = taylor_feature(infl, _lag(fin, "inflation")) if model == "fin.taylor_logit" else None
    ws = F.windows(history, length_days, origin, feature=feat)
    state = F.last_move(history, history[-1][0])
    if model == "fin.meeting_base_rate":
        return F.meeting_base_rate(ws, direction)
    if model == "fin.meeting_momentum":
        return F.meeting_momentum(ws, state, direction)
    if model == "fin.taylor_logit":
        x_now = taylor_feature(infl, _lag(fin, "inflation"))(origin, history[-1][1])
        return F.taylor_logit(ws, x_now, state, direction)
    return Abstain(f"model {model} does not forecast policy meetings")


def meeting_days(frame: pl.DataFrame, cal: Mapping[str, Any]) -> list[tuple[date, str, Ref]]:
    """Decision days of one body from the calendar table: ``(day, verbatim text, ref)``."""
    f = frame.filter(
        (pl.col("indicator") == cal["indicator"]) & (pl.col("provider_geo") == cal["provider_geo"])
    ).sort("period_start")
    return [
        (s, str(t or ""), {"raw_sha256": h, "selector": _json(sel)})
        for s, t, h, sel in f.select("period_start", "value_text", "raw_sha256", "selector").iter_rows()
    ]


def level_in_force(cells: Sequence[tuple[date, str, Ref]], day: date) -> tuple[str, Ref] | None:
    """Value of the latest published cell on or before ``day``, provided a cell on or after ``day`` is published
    (so no change between them can be missing); None otherwise."""
    before = [c for c in cells if c[0] <= day]
    if not before or not any(c[0] >= day for c in cells):
        return None
    return before[-1][1], before[-1][2]


# --------------------------------------------------------------------------- frozen forecast records


def finance_forecasts(
    q: Mapping[str, Any],
    fin: Mapping[str, Any],
    own: Sequence[tuple[date, float]],
    inputs: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Frozen records of every declared finance forecaster for one question (abstentions with their reason)."""
    from reva.evaluation.registry.forecasts import FORECAST_SCHEMA, _record, abstention, num, train_digest

    out: list[dict[str, Any]] = []
    meeting = fin["kind"] == "meeting"
    for mk in fin["forecasters"]:
        if meeting:
            hist = inputs.get("history") or []
            if not hist:
                out.append(abstention(q, mk, "history input not published in the pinned table"))
                continue
            origin = hist[-1][0]
            r_day = date.fromisoformat(str(q["resolution"]["level_day"]))
            res = meeting_prob(mk, fin, hist, (r_day - origin).days, origin, inputs)
            if isinstance(res, Abstain):
                out.append(abstention(q, mk, res.reason))
                continue
            out.append(
                {
                    "schema": FORECAST_SCHEMA,
                    "question_id": q["question_id"],
                    "model": mk,
                    "status": "issued",
                    "point": None,
                    "predictive": None,
                    "prob_above": num(res),
                    "detail": {"window_days": str((r_day - origin).days), "direction": fin["direction"]},
                    "origin_period": origin.isoformat(),
                    "n_train": len(hist),
                    "train_digest": train_digest([(d.toordinal(), v) for d, v in hist]),
                }
            )
            continue
        target = target_of(q)
        p = run_model(mk, fin, own, target, inputs)
        if isinstance(p, Abstain):
            out.append(abstention(q, mk, p.reason))
            continue
        hist_m = [(P.index_of("M", d), v) for d, v in own[-1:]]
        rec = _record(q, mk, p, hist_m)
        rec["origin_day"] = own[-1][0].isoformat()
        rec["n_train"] = len(own)  # published days used (daily series)
        rec["train_digest"] = train_digest([(d.toordinal(), v) for d, v in own])
        out.append(rec)
    return out


__all__ = [
    "DAILY_RULES",
    "FIN_KINDS",
    "as_of",
    "curve_frame",
    "daily_cells",
    "finance_forecasts",
    "latest",
    "level_in_force",
    "load_inputs",
    "meeting_days",
    "meeting_prob",
    "month_end_series",
    "monthly_values",
    "published_months",
    "run_model",
    "spread_cells",
    "target_of",
    "taylor_feature",
    "to_floats",
]
