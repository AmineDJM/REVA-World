"""Energy question families of the forecast registry (V6, agent PRED-ENERGY): STEO reference and winners (pure).

An energy family declares an ``energy`` block in ``registry/forecasts/question_specs.yaml``::

    energy:
      code: BREPUUS                                   # EIA STEO series code (reva.models.forecasting.energy_models)
      vintage_table: atlas.vintage_steo.steo_archive  # STEO archive issues (information set + EIA's projection)
      winners: forecasts/energy_winners.yaml          # registry-relative; written by reva.evaluation.energy_backtests

At issue the vintage table is pinned like the family's own table (latest version at ``issued_at``, listed in the
sealed manifest) and read through the vintage accessor in ``as_known`` mode at ``issued_at``: the **STEO issue in
force** gives (a) the information set (history as published in that issue) and (b) EIA's projections. Every energy
question then gets, besides the registry's generic baselines:

* ``forecast.energy_rw`` — the backtest's random-walk benchmark on that information set (a baseline);
* ``forecast.reference_eia_steo`` — EIA's projection for the reference month, **attributed to EIA**, point only
  (interval and binary questions: abstains, like the IMF WEO reference);
* only the REVA candidates listed as **winners** for this series and horizon in the winners file (significant
  CRPS gain over the random walk in the STEO archive backtest, Holm-corrected); none otherwise.

The horizon of a question for the winners rule is the number of months between the STEO issue month in force and
the reference month (the backtest's ``h``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from typing import Any

import polars as pl
import yaml

from reva.evaluation.cutoff import CutoffMode
from reva.evaluation.registry import periods as P
from reva.models.forecasting import energy_models as em
from reva.models.forecasting.registry_baselines import Predictive
from reva.ontology.registry import REGISTRY_ROOT

STEO_KEY = "forecast.reference_eia_steo"
RW_KEY = "forecast.energy_rw"
EMPIRICAL_N = 200
ATTRIBUTION = (
    "U.S. Energy Information Administration, Short-Term Energy Outlook {issue} (projection), as pinned at issue"
)


def block(fam: Any) -> Mapping[str, Any] | None:
    raw = getattr(fam, "raw", None) or {}
    b = raw.get("energy")
    return b if isinstance(b, Mapping) else None


def input_keys(fam: Any) -> list[str]:
    """Tables an energy family reads besides its own (the STEO archive vintage table)."""
    b = block(fam)
    return [str(b["vintage_table"])] if b and b.get("vintage_table") else []


@lru_cache(maxsize=4)
def _winners_file(rel: str) -> tuple[str | None, frozenset[tuple[str, int, str]], str | None]:
    path = REGISTRY_ROOT / rel
    if not path.exists():
        return None, frozenset(), None
    data = path.read_bytes()
    doc = yaml.safe_load(data) or {}
    if doc.get("schema") != "reva-energy-winners/1":
        raise ValueError(f"{rel}: unknown winners schema")
    ws = frozenset(
        (str(w["code"]), int(w["h"]), str(w["model"]))
        for w in doc.get("winners") or []
        if str(w["model"]) in em.MODEL_KEYS and str(w["model"]) not in (em.BENCHMARK, em.REFERENCE)
    )
    return hashlib.sha256(data).hexdigest(), ws, doc.get("backtest_digest")


def winners(rel: str) -> tuple[str | None, frozenset[tuple[str, int, str]], str | None]:
    return _winners_file(rel)


def energy_context(
    fam: Any,
    pins: Mapping[str, Mapping[str, Any] | None],
    frames: Mapping[str, pl.DataFrame],
    extra_pins: dict[str, dict[str, Any]],
    issued_at: datetime,
) -> dict[str, Any]:
    """The STEO issue in force at ``issued_at`` (information set + projections) from the pinned vintage table."""
    from reva.evaluation.vintages import VintageAccessor, cells_from_atlas_frame

    b = block(fam)
    assert b is not None
    key = str(b["vintage_table"])
    pin = pins.get(key)
    ctx: dict[str, Any] = {"code": str(b["code"]), "issue": None, "reason": None}
    sha, ws, digest = winners(str(b.get("winners") or "forecasts/energy_winners.yaml"))
    ctx.update(winners=ws, winners_sha256=sha, backtest_digest=digest)
    if pin is None:
        ctx["reason"] = f"vintage table {key} not registered at issue"
        return ctx
    extra_pins[key] = dict(pin)
    rec = datetime.fromisoformat(str(pin["recorded_at"]))
    cells = cells_from_atlas_frame(frames[pin["artifact_sha256"]], dataset_key=key, table_recorded_at=rec)
    variables = [f"eia_steo:{c}" for c in em.INPUT_CODES]
    cells = cells.filter((pl.col("family") == "eia_steo") & pl.col("variable").is_in(variables))
    acc = VintageAccessor(cells, issued_at, CutoffMode.as_known)
    inforce = acc.vintage_in_force("eia_steo")
    if inforce is None:
        ctx["reason"] = "no STEO issue available at issue time"
        return ctx
    from reva.evaluation.energy_backtests import _series

    vintage = str(inforce["vintage"])
    y, m = (int(x) for x in vintage.split("-"))
    issue_idx = y * 12 + m - 1
    info = _series(acc.latest(family="eia_steo", variable=variables).rows)
    proj = acc.forecasts(family="eia_steo", variable=variables).rows.filter(pl.col("vintage") == vintage)
    ctx.update(
        issue=vintage,
        issue_label=f"{P.label_of('M', issue_idx)}",
        release_date=inforce.get("vintage_date_text"),
        available_at=inforce.get("available_at"),
        issue_idx=issue_idx,
        info=info,
        steo=_series(proj),
        steo_refs={
            (str(r["variable"]).split(":", 1)[1], r["period_start"].year * 12 + r["period_start"].month - 1): {
                "raw_sha256": r["raw_sha256"],
                "selector": r["selector"],
                "indicator": r["indicator"],
                "value": r["value"],
            }
            for r in proj.select("variable", "period_start", "raw_sha256", "selector", "indicator", "value").iter_rows(
                named=True
            )
        },
        vintage_pin={k: pin[k] for k in ("dataset_key", "artifact_sha256", "recorded_at")},
    )
    return ctx


def to_predictive(p: em.Pred, detail: tuple[tuple[str, str], ...]) -> Predictive:
    """Registry predictive of an energy forecast: Gaussian as is; log-normal as a 200-quantile empirical sample."""
    if p.dist == "normal" and p.mu is not None and p.sigma is not None:
        return Predictive("normal", p.point, mu=p.mu, sigma=p.sigma, detail=detail)
    if p.dist == "lognormal":
        qs = tuple(sorted(float(p.quantile((i + 0.5) / EMPIRICAL_N) or 0.0) for i in range(EMPIRICAL_N)))
        return Predictive("empirical", p.point, sample=qs, detail=(*detail, ("predictive", "lognormal quantiles")))
    return Predictive("point", p.point, detail=detail)


def energy_forecasts(q: Mapping[str, Any], fam: Any, ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Frozen records of the energy forecasters for one question (see the module docstring)."""
    from reva.evaluation.registry.forecasts import _record, abstention

    code = str(ctx["code"])
    keys = [RW_KEY, STEO_KEY, *sorted({em.MODEL_KEYS[m] for (c, _h, m) in ctx.get("winners", ()) if c == code})]
    if ctx.get("issue") is None:
        return [abstention(q, k, str(ctx.get("reason") or "no STEO issue")) for k in (RW_KEY, STEO_KEY)]
    target = P.parse_label("M", str(q["reference_period"]))
    h = target - int(ctx["issue_idx"])
    info: dict[str, dict[int, float]] = ctx["info"]
    own = [i for i in info.get(code, {}) if i < int(ctx["issue_idx"])]
    out: list[dict[str, Any]] = []
    if not own:
        return [abstention(q, k, f"series {code} not in the STEO issue in force") for k in (RW_KEY, STEO_KEY)]
    last = max(own)
    hist = sorted((i, v) for i, v in info[code].items() if i <= last)
    detail = (
        ("steo_issue", str(ctx["issue"])),
        ("steo_release_date", str(ctx.get("release_date"))),
        ("information_set", "STEO issue in force (vintage accessor, as_known at issue)"),
        ("horizon_months_after_issue", str(h)),
    )
    win = {m for (c, hh, m) in ctx.get("winners", ()) if c == code and hh == h}
    for m in [em.BENCHMARK, em.REFERENCE, *sorted(win)]:
        mk = em.MODEL_KEYS[m]
        if m == em.REFERENCE and q["type"] != "quantity":
            out.append(abstention(q, mk, "point-only external projection: no distribution for interval/binary"))
            continue
        p = em.forecast(m, code, info, ctx["steo"], last, target)
        if isinstance(p, em.Abstain):
            out.append(abstention(q, mk, p.reason))
            continue
        d = detail
        if m not in (em.BENCHMARK, em.REFERENCE):
            d = (
                *detail,
                ("winner_rule", "significant CRPS gain vs random walk (DM-HLN, Holm), STEO archive backtest"),
                ("winners_sha256", str(ctx.get("winners_sha256"))),
                ("backtest_digest", str(ctx.get("backtest_digest"))),
            )
        rec = _record(q, mk, to_predictive(p, d), hist)
        if m == em.REFERENCE:
            rec["source_cell"] = {**ctx["steo_refs"].get((code, target), {}), "table": ctx.get("vintage_pin")}
            rec["attribution"] = ATTRIBUTION.format(issue=ctx["issue"])
        if m == "combo_steo_rw":
            rec["attribution"] = "REVA equal-weight combination of the EIA STEO projection and a random walk"
        out.append(rec)
    missing = [k for k in keys if k not in {r["model"] for r in out}]
    for k in missing:
        out.append(abstention(q, k, f"not a significant winner for {code} at horizon {h} (STEO archive backtest)"))
    return out


def energy_models() -> dict[str, tuple[str, str]]:
    """Energy forecasters (V6 PRED-ENERGY) for :func:`reva.evaluation.registry.forecasts.all_models`."""
    return {k: (v, f"{kind}: {d}") for k, (v, kind, d) in em.ENERGY_MODELS.items()}


__all__ = [
    "RW_KEY",
    "STEO_KEY",
    "block",
    "energy_context",
    "energy_forecasts",
    "energy_models",
    "input_keys",
    "to_predictive",
    "winners",
]
