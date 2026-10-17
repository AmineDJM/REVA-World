"""Second pass: combinations with the official forecast, conformal densities and the fair density benchmark.

Everything here uses only errors whose outcome was published at the origin (``outcome_known_at <= origin``), in
the same **bucket** (variable x calendar month of the origin: the official forecast's staleness and the number of
published months depend on it), from the last ``WINDOW`` past origins of that bucket.

* ``forecast.bm_stack`` / ``forecast.bm_stack_oil`` — **uses the official forecast as an input**:
  ``official + lambda * (bridge - official)``; ``lambda`` = least squares of past (outcome - official) on past
  (bridge - official) (both clipped to +-10 for estimation), clipped to [0, 1], shrunk toward the equal weight
  0.5 with a prior worth ``SHRINK_N0`` origins: ``(n * lambda_hat + SHRINK_N0 * 0.5) / (n + SHRINK_N0)``,
  n = number of past origins; needs n >= ``MIN_ORIGINS``.
* ``forecast.bm_bridge`` / ``forecast.bm_bridge_oil`` / stacks as densities — point + series scale x the past
  normalised errors of the same model in the bucket (empirical, at most ``MAX_SAMPLE`` order statistics); with
  too few past errors the model is a point forecast (scored on AE only).
* ``benchmark.<official>_errfan`` — **the fair density benchmark**: N(official point, series scale x RMS of the
  official forecaster's own past normalised errors in the same bucket and window) — the Gaussian twin of the
  conformal density around the official point. ``..._errfan_own``: the series' own past official errors (>= 8),
  descriptive.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from reva.evaluation.pred_models.realtime import Case
from reva.models.forecasting.registry_baselines import Abstain, Predictive

SHRINK_N0 = 3.0
WINDOW = 10
MAX_SAMPLE = 400
MIN_OWN = 8
CLIP = 10.0
BASES = {"stack": "bridge", "stack_oil": "bridge_oil"}
MODELS = ("bridge", "bridge_oil", "stack", "stack_oil")


def key(name: str) -> str:
    return f"forecast.bm_{name}"


@dataclass(frozen=True)
class Rules:
    official: str
    min_origins: int = 3
    min_errors: int = 15


def fan_key(official: str) -> str:
    return f"{official}_errfan"


def own_fan_key(official: str) -> str:
    return f"{official}_errfan_own"


@dataclass
class _Book:
    """Entries (known_at, origin_ts, value) kept sorted by known_at."""

    known: list[float] = field(default_factory=list)
    rows: list[tuple[float, object]] = field(default_factory=list)

    def add(self, known_at: float, origin_ts: float, value: object) -> None:
        i = bisect.bisect_right(self.known, known_at)
        self.known.insert(i, known_at)
        self.rows.insert(i, (origin_ts, value))

    def window(self, t: float, n_origins: int) -> tuple[list[object], int]:
        """Values known at t from the last ``n_origins`` distinct past origins; and that number of origins."""
        avail = self.rows[: bisect.bisect_right(self.known, t)]
        origins = sorted({o for o, _ in avail})[-n_origins:]
        keep = set(origins)
        return [v for o, v in avail if o in keep], len(origins)


def shrunk_lambda(pairs: Sequence[tuple[float, float]], n_origins: int) -> float | None:
    sxy = math.fsum(max(-CLIP, min(CLIP, dy)) * max(-CLIP, min(CLIP, db)) for dy, db in pairs)
    sxx = math.fsum(max(-CLIP, min(CLIP, db)) ** 2 for _, db in pairs)
    if sxx <= 0:
        return None
    lam = min(1.0, max(0.0, sxy / sxx))
    return (n_origins * lam + SHRINK_N0 * 0.5) / (n_origins + SHRINK_N0)


def _thin(xs: list[float]) -> tuple[float, ...]:
    s = sorted(xs)
    if len(s) <= MAX_SAMPLE:
        return tuple(s)
    n = len(s)
    return tuple(s[int((i + 0.5) * n / MAX_SAMPLE)] for i in range(MAX_SAMPLE))


def bucket(c: Case) -> tuple[str, int]:
    return (c.variable, c.origin.month)


def add_second_pass(cases: Sequence[Case], rules: Rules) -> None:
    """Adds stacks, conformal densities and fair fans in place (origins processed in time order)."""
    off = rules.official
    by_origin: dict[datetime, list[Case]] = defaultdict(list)
    for c in cases:
        by_origin[c.origin].append(c)
    pairs: dict[tuple[object, ...], _Book] = defaultdict(_Book)
    errs: dict[tuple[object, ...], _Book] = defaultdict(_Book)
    own: dict[tuple[object, ...], _Book] = defaultdict(_Book)
    for t in sorted(by_origin):
        ts = t.timestamp()
        batch = by_origin[t]
        for c in batch:
            b = bucket(c)
            raw: dict[str, float] = c.info.setdefault("raw", {})
            wp = c.preds.get(off)
            w = wp.point if isinstance(wp, Predictive) else None
            for st, base in BASES.items():
                if w is None or base not in raw:
                    continue
                pv, n = pairs[(*b, base)].window(ts, WINDOW)
                if n < rules.min_origins:
                    continue
                lam = shrunk_lambda(pv, n)  # type: ignore[arg-type]
                if lam is None:
                    continue
                raw[st] = w + lam * (raw[base] - w)
                c.info[f"{st}_lambda"] = round(lam, 4)
            for m in MODELS:
                if m not in raw:
                    continue
                ev, n = errs[(*b, m)].window(ts, WINDOW)
                if c.scale and n >= rules.min_origins and len(ev) >= rules.min_errors:
                    sample = _thin([raw[m] + c.scale * float(e) for e in ev])  # type: ignore[arg-type]
                    c.preds[key(m)] = Predictive("empirical", raw[m], sample=sample)
                else:
                    c.preds[key(m)] = Predictive("point", raw[m])
            if w is None:
                continue
            ev, n = errs[(*b, "official")].window(ts, WINDOW)
            if c.scale and n >= rules.min_origins and len(ev) >= rules.min_errors:
                rms = math.sqrt(math.fsum(float(e) ** 2 for e in ev) / len(ev)) * c.scale  # type: ignore[arg-type]
                c.preds[fan_key(off)] = Predictive("normal", w, mu=w, sigma=rms) if rms > 0 else Abstain("zero")
            else:
                c.preds[fan_key(off)] = Abstain("too few past official errors known at the origin")
            ov, _ = own[(*b, c.geo)].window(ts, 10_000)
            if len(ov) >= MIN_OWN:
                rms = math.sqrt(math.fsum(float(e) ** 2 for e in ov) / len(ov))
                if rms > 0:
                    c.preds[own_fan_key(off)] = Predictive("normal", w, mu=w, sigma=rms)
        for c in batch:  # this origin's errors become usable once the outcome is published
            if c.outcome is None or c.outcome_known_at is None:
                continue
            k = c.outcome_known_at.timestamp()
            b = bucket(c)
            raw = c.info.get("raw", {})
            wp = c.preds.get(off)
            w = wp.point if isinstance(wp, Predictive) else None
            if w is not None:
                own[(*b, c.geo)].add(k, ts, c.outcome - w)
                if c.scale:
                    errs[(*b, "official")].add(k, ts, (c.outcome - w) / c.scale)
                for base in set(BASES.values()):
                    if base in raw:
                        pairs[(*b, base)].add(k, ts, (c.outcome - w, raw[base] - w))
            if c.scale:
                for m in MODELS:
                    if m in raw:
                        errs[(*b, m)].add(k, ts, (c.outcome - raw[m]) / c.scale)


__all__ = ["BASES", "MODELS", "Rules", "add_second_pass", "bucket", "fan_key", "key", "own_fan_key", "shrunk_lambda"]
