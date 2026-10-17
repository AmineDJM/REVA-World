"""Forecasting registry questions with the baselines (pure).

A frozen forecast record stores everything needed to score it later without refitting: the predictive
distribution (``normal`` mu/sigma, the sorted ``empirical`` sample, or ``point`` only), the point, the central
interval at the question's level, the probability for binary questions, the horizon in periods from the last
observation used, and a digest of the training data. Numbers are stored as decimal strings with 10 significant
digits (``num``): that string *is* the forecast; scores parse it back.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from reva.evaluation.registry import periods as P
from reva.kernel.ids import logical_hash
from reva.models.forecasting.registry_baselines import BASELINES, Abstain, Predictive, run_baseline

FORECAST_SCHEMA = "reva-forecast-record/1"
REFERENCE_WEO = "forecast.reference_imf_weo"
REFERENCE_MODELS: dict[str, tuple[str, str]] = {
    REFERENCE_WEO: (
        "1.0.0",
        "IMF World Economic Outlook projection of the pinned (latest) WEO vintage, attributed to the IMF; point only",
    )
}
QUANTILE_LEVELS = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)


def num(x: float) -> str:
    return format(float(x), ".10g")


def parse_num(s: str) -> float:
    return float(s)


def train_digest(history: Sequence[tuple[int, float]]) -> str:
    return logical_hash([[i, num(v)] for i, v in history])


def predictive_record(p: Predictive) -> dict[str, Any]:
    if p.family == "normal":
        assert p.mu is not None and p.sigma is not None
        return {"family": "normal", "mu": num(p.mu), "sigma": num(p.sigma)}
    if p.family == "empirical":
        return {"family": "empirical", "sample": [num(v) for v in p.sample]}
    return {"family": "point"}


def predictive_from_record(point: str, rec: Mapping[str, Any] | None) -> Predictive:
    pt = parse_num(point)
    if not rec or rec.get("family") == "point":
        return Predictive("point", pt)
    if rec["family"] == "normal":
        return Predictive("normal", pt, mu=parse_num(rec["mu"]), sigma=parse_num(rec["sigma"]))
    if rec["family"] == "empirical":
        return Predictive("empirical", pt, sample=tuple(sorted(parse_num(v) for v in rec["sample"])))
    raise ValueError(f"unknown predictive family {rec.get('family')!r}")


def _record(
    q: Mapping[str, Any], model_key: str, p: Predictive, history: Sequence[tuple[int, float]]
) -> dict[str, Any]:
    freq = str(q["frequency"])
    target = P.parse_label(freq, str(q["reference_period"]))
    out: dict[str, Any] = {
        "schema": FORECAST_SCHEMA,
        "question_id": q["question_id"],
        "model": model_key,
        "status": "issued",
        "point": num(p.point),
        "predictive": predictive_record(p),
        "detail": dict(p.detail),
    }
    if history:
        out["origin_period"] = P.label_of(freq, history[-1][0])
        out["horizon_periods"] = target - history[-1][0]
        out["n_train"] = len(history)
        out["train_digest"] = train_digest(history)
    if p.distributional:
        out["quantiles"] = {f"{lv:g}": num(v) for lv in QUANTILE_LEVELS if (v := p.quantile(lv)) is not None}
    if q["type"] == "interval":
        iv = p.interval(float(q["interval_level"]))
        out["interval"] = [num(iv[0]), num(iv[1])] if iv else None
    if q["type"] == "binary":
        pr = p.prob_above(float(Decimal(str(q["threshold"]))))
        out["prob_above"] = num(pr) if pr is not None else None
    return out


def abstention(q: Mapping[str, Any], model_key: str, reason: str) -> dict[str, Any]:
    return {
        "schema": FORECAST_SCHEMA,
        "question_id": q["question_id"],
        "model": model_key,
        "status": "abstained",
        "reason": reason,
    }


def forecast_baseline(q: Mapping[str, Any], model_key: str, history: Sequence[tuple[int, float]]) -> dict[str, Any]:
    freq = str(q["frequency"])
    target = P.parse_label(freq, str(q["reference_period"]))
    res = run_baseline(model_key, history, target, P.season_of(freq))
    if isinstance(res, Abstain):
        return abstention(q, model_key, res.reason)
    return _record(q, model_key, res, history)


def forecast_reference(
    q: Mapping[str, Any], model_key: str, projection: tuple[str, Mapping[str, Any]] | None
) -> dict[str, Any]:
    """External reference forecast (point only): the projection cell of the pinned table version."""
    if q["type"] != "quantity":
        return abstention(q, model_key, "point-only external projection: no distribution for interval/binary questions")
    if projection is None:
        return abstention(q, model_key, "no projection cell for this period in the pinned table version")
    value, ref = projection
    out = _record(q, model_key, Predictive("point", float(Decimal(value))), ())
    out["source_cell"] = dict(ref)
    out["attribution"] = "International Monetary Fund, World Economic Outlook (projection), as pinned at issue"
    return out


def finance_models() -> dict[str, tuple[str, str]]:
    """Finance benchmarks and candidates (V6 PRED-FIN); every one is registered so that a batch or a resolution can
    record a row for it, issued or not."""
    from reva.models.forecasting.finance_models import FIN_MODELS

    return {k: (v, f"{kind}: {d}") for k, (v, kind, d) in FIN_MODELS.items()}


def conflict_models() -> dict[str, tuple[str, str]]:
    """Conflict-risk baselines, candidate and the attributed VIEWS reference (V6 PRED-CONFLICT,
    ``reva.evaluation.conflict``); registered like the finance models."""
    from reva.evaluation.conflict.models import CONFLICT_MODELS

    return {k: (v, f"{kind}: {d}") for k, (v, kind, d) in CONFLICT_MODELS.items()}


def energy_models() -> dict[str, tuple[str, str]]:
    """Energy forecasters vs the EIA STEO (V6 PRED-ENERGY): random walk, attributed STEO reference, candidates."""
    from reva.evaluation.registry.energy import energy_models as _em

    return _em()


def pred_models() -> dict[str, tuple[str, str]]:
    """Forecasters registered on the locked real-time holdout (V6 PRED-MODELS) for a registry family; they issue
    through supplementary sealed batches (``reva.evaluation.pred_models.issue``)."""
    from reva.evaluation.pred_models.issue import registry_models

    return registry_models()


def ensemble_models() -> dict[str, tuple[str, str]]:
    """The REVA ensemble (V7 BEAT-ENSEMBLE, :mod:`reva.evaluation.registry.ensemble`)."""
    from reva.evaluation.registry.ensemble import model_entry

    return model_entry()


def all_models() -> dict[str, tuple[str, str]]:
    return {
        **BASELINES,
        **REFERENCE_MODELS,
        **finance_models(),
        **conflict_models(),
        **energy_models(),
        **pred_models(),
        **ensemble_models(),
    }


__all__ = [
    "FORECAST_SCHEMA",
    "QUANTILE_LEVELS",
    "REFERENCE_MODELS",
    "REFERENCE_WEO",
    "abstention",
    "all_models",
    "conflict_models",
    "ensemble_models",
    "finance_models",
    "forecast_baseline",
    "forecast_reference",
    "num",
    "parse_num",
    "predictive_from_record",
    "predictive_record",
    "train_digest",
]
