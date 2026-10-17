"""Effect estimates as propagation hops (V5, agent PRED-ENGINE).

Reads PRED-EFFECT's estimate JSON (``docs/dev/v5_effect_interface.md``, "Estimate JSON") and turns one estimate
into a *hop*: a linear map ``Δeffect = β · Δcause`` with

* the change unit the cause must be stated in (``in_unit``) and the one the effect comes out in (``out_unit``):
  ``percent``, ``log_points``, ``pp``, ``pp_gdp``, ``occurrence``, ``std_dev`` or ``level:<unit text>``;
* β (``point``, or ``point / shock size`` for a ``response`` — linear scaling of a published response is a
  stated assumption) and its uncertainty from the estimate's own SE / CI / range only (``normal_approx``; no
  uncertainty is ever invented: an estimate without one is a *scenario point* and every distribution it enters
  is flagged incomplete);
* a time profile on the engine's quarterly grid (lag profile ``irf`` or a dated single horizon) or ``undated``
  (cumulative / peak / average / long-run / unspecified horizons).

Also: applicability of an estimate's scope to a geography and period, and the deterministic choice among
several estimates of the same edge (never summed, never averaged).

Pure module (standard library and numpy only).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import NormalDist
from typing import Any

from reva.predict.decision import VarRef

# ------------------------------------------------------------------------------------------- unit vocabulary
#: Exact definitional unit factors for stated shock units: (canonical unit, factor) with
#: ``1 <alias> = factor <canonical>``. Nothing else is ever converted.
UNIT_ALIASES: dict[str, tuple[str, Decimal]] = {
    "percent": ("percent", Decimal(1)),
    "%": ("percent", Decimal(1)),
    "percentage_points": ("pp", Decimal(1)),
    "percentage points": ("pp", Decimal(1)),
    "percentage point": ("pp", Decimal(1)),
    "pp": ("pp", Decimal(1)),
    "bp": ("pp", Decimal("0.01")),
    "basis points": ("pp", Decimal("0.01")),
    "basis_points": ("pp", Decimal("0.01")),
    "log_points": ("log_points", Decimal(1)),
    "std_dev": ("std_dev", Decimal(1)),
    "occurrence": ("occurrence", Decimal(1)),
    "percent_of_gdp": ("pp_gdp", Decimal(1)),
    "mb/d": ("level:thousand barrels per day", Decimal(1000)),
    "million barrels per day": ("level:thousand barrels per day", Decimal(1000)),
    "kb/d": ("level:thousand barrels per day", Decimal(1)),
    "thousand barrels per day": ("level:thousand barrels per day", Decimal(1)),
    "months": ("level:months", Decimal(1)),
    "years": ("level:months", Decimal(12)),
}

TRANSFORM_UNIT: dict[str, str] = {
    "percent_change": "percent",
    "log_change": "log_points",
    "log_level": "log_points",
    "pp_change": "pp",
    "growth_rate": "pp",
    "gap": "pp",
    "share_of_gdp_change": "pp_gdp",
    "std_dev_shock": "std_dev",
    "occurrence": "occurrence",
}
LEVEL_TRANSFORMS = ("level_change", "level")
APPROX_PAIR = frozenset({"percent", "log_points"})
UNDATED_KINDS = ("cumulative", "peak", "average", "long_run", "unspecified")
UNDATED_LABEL_FR = {
    "cumulative": "effet cumulé (date non précisée)",
    "peak": "pic de l'effet (date non précisée)",
    "average": "effet moyen sur la période de l'estimation",
    "long_run": "effet de long terme (non daté)",
    "unspecified": "horizon non précisé par la source",
}

#: Ranking of identification families for the deterministic choice among estimates of one edge (higher first).
METHOD_RANK: dict[str, int] = {
    "accounting_identity": 9,
    "physical_law": 9,
    "meta_analysis": 8,
    "rct": 7,
    "iv": 7,
    "did": 7,
    "rdd": 7,
    "synthetic_control": 7,
    "local_projection": 6,
    "svar": 6,
    "event_study": 6,
    "var": 5,
    "bvar": 5,
    "panel_regression": 4,
    "time_series_regression": 4,
    "gravity": 4,
    "cross_section_regression": 3,
    "input_output": 3,
    "structural_model": 3,
    "dsge": 3,
    "calibration": 2,
    "other": 1,
}
CHOICE_RULE = (
    "one estimate per edge, never summed or averaged: (1) scope applies exactly to the geography > world panel > "
    "country group or unstated scope; (2) published uncertainty > none; (3) dated lag profile > dated single "
    "horizon > undated; (4) identification family (identity > meta-analysis > experimental/quasi-experimental > "
    "LP/SVAR/event study > VAR > regressions > structural/IO > calibration); (5) most recent as-of; (6) key"
)
OLD_ESTIMATE_YEARS = 20


def canonical_unit(text: str | None) -> tuple[str | None, Decimal]:
    if text is None:
        return None, Decimal(1)
    t = str(text).strip()
    hit = UNIT_ALIASES.get(t) or UNIT_ALIASES.get(t.lower())
    if hit:
        return hit
    return f"level:{t}", Decimal(1)


def units_compatible(node_unit: str, hop_unit: str) -> tuple[bool, str | None]:
    """Same change unit, or percent vs log points (small-change approximation, stated)."""
    if node_unit == hop_unit:
        return True, None
    if {node_unit, hop_unit} == APPROX_PAIR:
        return True, "approximation: log points ≈ percent (valid for small changes only)"
    return False, None


# ------------------------------------------------------------------------------------------- uncertainty
def _z(level: float) -> float:
    return NormalDist().inv_cdf(0.5 + level / 2)


def normal_approx(est: Mapping[str, Any]) -> dict[str, Any]:
    """{mean, sd, basis} as PRED-EFFECT's ``reva.effects.normal_approx`` (used when that package is absent).

    basis ``se``: sd = SE; ``ci``: sd = half-width / z(level) (symmetric interval assumed, stated);
    ``heuristic_range``: partial uncertainty, sd = half-range / 2 (labelled heuristic); ``None``: no uncertainty
    was published, the estimate is a scenario point (sd None) and no distribution may be invented.
    """
    try:  # PRED-EFFECT's implementation when present, for one shared definition
        from reva.effects import normal_approx as _na  # type: ignore[import-not-found,unused-ignore]

        out = dict(_na(est))
        if out.get("basis") == "range":
            out["basis"] = "heuristic_range"
        return out
    except ImportError:
        pass
    u = est.get("uncertainty") or {}
    mean = _f(est.get("point"))
    se, lo, hi, lvl = _f(u.get("se")), _f(u.get("ci_low")), _f(u.get("ci_high")), _f(u.get("ci_level"))
    kind = u.get("interval_kind")
    if se is not None and se > 0:
        return {"mean": mean, "sd": se, "basis": "se"}
    if lo is not None and hi is not None and hi > lo:
        if kind in (None, "confidence", "credible") and lvl is not None and 0 < lvl < 1:
            return {"mean": mean, "sd": (hi - lo) / (2 * _z(lvl)), "basis": "ci"}
        return {"mean": mean, "sd": (hi - lo) / 4, "basis": "heuristic_range"}
    return {"mean": mean, "sd": None, "basis": None}


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


# ------------------------------------------------------------------------------------------- hop model
@dataclass(frozen=True)
class ProfilePoint:
    coef: float
    sd: float | None


@dataclass
class Hop:
    """One effect estimate normalised for propagation."""

    estimate_key: str
    cause: VarRef
    effect: VarRef
    in_unit: str | None
    out_unit: str | None
    coef: float | None
    sd: float | None
    basis: str | None
    dated: dict[int, ProfilePoint] | None  # quarter offset -> β (step response)
    undated_kind: str | None
    sign: str | None
    scope: Mapping[str, Any]
    as_of: date | None
    method: str | None
    raw: Mapping[str, Any]
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    shock_canonical: float | None = None  # published shock size in the cause's canonical unit (response measure)

    @property
    def quantified(self) -> bool:
        return self.coef is not None

    @property
    def has_uncertainty(self) -> bool:
        if self.dated is not None:
            return all(p.sd is not None for p in self.dated.values())
        return self.sd is not None

    def provenance(self) -> dict[str, Any]:
        r = self.raw
        src = r.get("source") or {}
        return {
            "estimate_key": self.estimate_key,
            "source_kind": src.get("kind"),
            "source_key": src.get("source_key"),
            "citation": (src.get("citation") or {}).get("text") if isinstance(src.get("citation"), Mapping) else None,
            "doi": (src.get("citation") or {}).get("doi") if isinstance(src.get("citation"), Mapping) else None,
            "url": (src.get("citation") or {}).get("url") if isinstance(src.get("citation"), Mapping) else None,
            "locator": src.get("locator"),
            "quote": src.get("quote"),
            "raw_sha256": src.get("raw_sha256"),
            "run": src.get("run"),
            "identity": src.get("identity"),
            "transcription": src.get("transcription"),
            "rights": src.get("rights"),
            "as_of": r.get("as_of"),
            "method": r.get("method"),
            "identification": r.get("identification"),
            "applicability_note": r.get("applicability"),
            "source_caveats": r.get("source_caveats"),
            "causal_claim_keys": list(r.get("causal_claim_keys") or []),
            "curation": r.get("curation"),
            "wording": r.get("wording"),
            "wording_fr": r.get("wording_fr"),
            "origin": r.get("origin"),
        }


def _ref(side: Mapping[str, Any] | None) -> VarRef:
    side = side or {}
    return VarRef(str(side.get("kind")), str(side.get("id")))


def _level_unit(ref: VarRef, unit_of: Callable[[VarRef], str | None]) -> str | None:
    u = unit_of(ref)
    return canonical_unit(u)[0] if u else None


def _side_unit(transform: str | None, ref: VarRef, unit_of: Callable[[VarRef], str | None]) -> str | None:
    if transform in TRANSFORM_UNIT:
        return TRANSFORM_UNIT[transform]
    if transform in LEVEL_TRANSFORMS:
        return _level_unit(ref, unit_of)
    return None


def _quarter_profile(irf: Sequence[Mapping[str, Any]], ci_level: float | None, notes: list[str]) -> dict[int, Any]:
    by_unit: dict[str, list[Mapping[str, Any]]] = {}
    for p in irf:
        by_unit.setdefault(str(p.get("unit")), []).append(p)
    out: dict[int, ProfilePoint] = {}

    def pt(p: Mapping[str, Any]) -> ProfilePoint | None:
        c = _f(p.get("point"))
        if c is None:
            return None
        se = _f(p.get("se"))
        lo, hi = _f(p.get("ci_low")), _f(p.get("ci_high"))
        sd = se if se and se > 0 else None
        if sd is None and lo is not None and hi is not None and hi > lo and ci_level:
            sd = (hi - lo) / (2 * _z(ci_level))
        return ProfilePoint(c, sd)

    for p in by_unit.get("quarter", []):
        q = pt(p)
        if q is not None:
            out[int(p["h"])] = q
    months = by_unit.get("month", [])
    if months:
        groups: dict[int, list[ProfilePoint]] = {}
        for p in months:
            q = pt(p)
            if q is not None:
                groups.setdefault(int(p["h"]) // 3, []).append(q)
        for qi, pts in groups.items():
            if qi in out:
                continue
            sds = [x.sd for x in pts]
            out[qi] = ProfilePoint(
                sum(x.coef for x in pts) / len(pts),
                None if any(s is None for s in sds) else sum(s for s in sds if s is not None) / len(sds),
            )
        notes.append("monthly lag profile read on quarters: mean of the published months of each quarter")
    years = by_unit.get("year", [])
    if years:
        for p in years:
            q = pt(p)
            if q is None:
                continue
            for k in range(4):
                out.setdefault(4 * int(p["h"]) + k, q)
        notes.append("annual lag profile read on quarters: the response of year h is applied to its four quarters")
    if by_unit.get("day"):
        notes.append("daily lag-profile points are not read on the quarterly grid")
    return out


def hop_from_estimate(est: Mapping[str, Any], unit_of: Callable[[VarRef], str | None]) -> Hop:
    """Normalise one estimate JSON into a hop (``problems`` non-empty = cannot propagate a number)."""
    cause, effect = _ref(est.get("cause")), _ref(est.get("effect"))
    c_tr = (est.get("cause") or {}).get("transform")
    e_tr = (est.get("effect") or {}).get("transform")
    measure, unit = est.get("measure"), est.get("unit")
    notes: list[str] = []
    problems: list[str] = []
    in_unit = _side_unit(c_tr, cause, unit_of)
    out_unit = _side_unit(e_tr, effect, unit_of)
    scale = 1.0
    shock_canonical: float | None = None
    if measure == "response":
        shock = est.get("shock") or {}
        size = _f(shock.get("size"))
        s_unit, factor = canonical_unit(shock.get("unit"))
        if size is None or size == 0 or s_unit is None:
            problems.append("response_without_shock_size")
        else:
            in_unit = s_unit
            scale = 1.0 / (size * float(factor))
            shock_canonical = abs(size * float(factor))
            if size != 1 or factor != 1:
                notes.append(
                    f"published response to a shock of {shock.get('size')} {shock.get('unit')} scaled linearly "
                    "to the decision's change (linearity assumed)"
                )
        out_unit = canonical_unit(unit)[0] if unit in ("percent", "percentage_points", "log_points") else out_unit
    elif measure in ("elasticity", "pass_through"):
        in_unit = "log_points" if c_tr == "log_change" else "percent"
        out_unit = "log_points" if e_tr in ("log_change", "log_level") else "percent"
    elif measure == "semi_elasticity":
        in_unit = "pp" if unit == "percent_per_pp" else _side_unit(c_tr, cause, unit_of)
        out_unit = "percent"
    elif measure == "slope":
        if unit == "pp_per_pp":
            in_unit, out_unit = "pp", "pp"
        elif unit == "pp_per_percent":
            in_unit, out_unit = "percent", "pp"
    if in_unit is None:
        problems.append("cause_unit_undetermined")
    if out_unit is None:
        problems.append("effect_unit_undetermined")
    if in_unit == "std_dev":
        problems.append("std_dev_shock_needs_cause_standard_deviation")
    point = _f(est.get("point"))
    na = normal_approx(est)
    sd = na.get("sd")
    basis = na.get("basis")
    if basis == "heuristic_range":
        notes.append("uncertainty from a published range only: sd = half-range / 2 (heuristic, labelled)")
    elif basis == "ci":
        notes.append("sd derived from the published interval assuming symmetry (normal approximation)")
    elif basis == "dispersion_across_estimates":
        notes.append("sd = dispersion across the published estimates summarised (heterogeneity, not sampling error)")
    coef = None if point is None else point * scale
    sd_s = None if sd is None else abs(float(sd) * scale)
    unc = est.get("uncertainty") or {}
    hz = est.get("horizon") or {}
    irf = list(est.get("irf") or [])
    dated: dict[int, ProfilePoint] | None = None
    undated_kind: str | None = None
    if irf:
        prof = _quarter_profile(irf, _f(unc.get("ci_level")), notes)
        if prof:
            dated = {
                q: ProfilePoint(p.coef * scale, None if p.sd is None else abs(p.sd * scale)) for q, p in prof.items()
            }
            if any(p.sd is None for p in dated.values()) and sd_s is not None:
                notes.append(
                    "some lag-profile points have no published uncertainty: those horizons are scenario points"
                )
        else:
            undated_kind = str(hz.get("kind") or "unspecified")
    elif hz.get("kind") in ("impact", "at_horizon") and coef is not None:
        u = str(hz.get("unit") or "quarter")
        v = 0 if hz.get("kind") == "impact" else _f(hz.get("value"))
        if v is None or u not in ("quarter", "month", "year"):
            undated_kind = "unspecified"
            notes.append(f"horizon {hz.get('value')!r} {u!r} is not read on the quarterly grid")
        elif u == "year":
            # an annual response (e.g. inflation in the year of the shock) covers the four quarters of that year
            dated = {4 * int(v) + k: ProfilePoint(coef, sd_s) for k in range(4)}
            notes.append(
                f"annual response of year {int(v)} after the shock applied to the four quarters of that year; "
                "other horizons are not computed (never interpolated)"
            )
        else:
            q = int(v) if u == "quarter" else int(v) // 3
            dated = {q: ProfilePoint(coef, sd_s)}
            notes.append("single published horizon: values at other horizons are not computed (never interpolated)")
            if u == "month":
                notes.append(f"horizon of {int(v)} months read as quarter offset {q}")
    else:
        undated_kind = str(hz.get("kind") or "unspecified")
        if undated_kind not in UNDATED_KINDS:
            undated_kind = "unspecified"
    as_of = None
    try:
        as_of = date.fromisoformat(str(est.get("as_of"))[:10]) if est.get("as_of") else None
    except ValueError:
        as_of = None
    return Hop(
        estimate_key=str(est.get("estimate_key")),
        cause=cause,
        effect=effect,
        in_unit=in_unit,
        out_unit=out_unit,
        coef=coef,
        sd=sd_s,
        basis=basis if point is not None else None,
        dated=dated,
        undated_kind=undated_kind if dated is None else None,
        sign=est.get("sign"),
        scope=est.get("scope") or {},
        as_of=as_of,
        method=est.get("method"),
        raw=est,
        notes=notes,
        problems=problems,
        shock_canonical=shock_canonical,
    )


# ------------------------------------------------------------------------------------------- applicability
APPLICABILITY_RANK = {"exact": 3, "world_panel": 2, "group_unverified": 1, "scope_unstated": 1}


def applicability(scope: Mapping[str, Any], geo: str, start_year: int) -> tuple[str, list[str]]:
    """Scope of an estimate vs a geography and the decision's start year.

    ``exact`` (geo listed), ``world_panel`` (scope lists WLD: a transported average), ``group_unverified``
    (only a verbatim country group: membership is not machine-checked), ``scope_unstated``, or
    ``out_of_scope`` (countries listed, geo not among them: the estimate is not used for that geography).
    """
    notes: list[str] = []
    countries = [str(c) for c in (scope.get("countries") or [])]
    group = scope.get("country_group")
    if geo in countries:
        status = "exact"
    elif "WLD" in countries:
        status = "world_panel"
        if geo != "WLD":
            notes.append(f"estimate from a world-level scope applied to {geo} (transported average)")
    elif countries:
        return "out_of_scope", [f"estimate scope {countries} does not include {geo}"]
    elif group:
        status = "group_unverified"
        notes.append(f"estimate scope is the group '{group}' (verbatim); membership of {geo} is not machine-checked")
    else:
        status = "scope_unstated"
        notes.append("the estimate states no geographic scope")
    period = scope.get("period") or {}
    to = str(period.get("to") or "")[:4]
    if to.isdigit() and start_year - int(to) > OLD_ESTIMATE_YEARS:
        notes.append(
            f"estimated on data up to {to}; the decision starts in {start_year} (more than {OLD_ESTIMATE_YEARS} years "
            "later: structural change is not accounted for)"
        )
    elif to.isdigit() and int(to) < start_year:
        notes.append(f"estimated on data up to {to}; applied to {start_year} (out-of-sample period)")
    return status, notes


def choice_key(h: Hop, app_status: str) -> tuple[Any, ...]:
    dated_rank = 2 if (h.dated is not None and len(h.dated) > 1) else 1 if h.dated is not None else 0
    as_of = h.as_of.toordinal() if h.as_of else 0
    return (
        -APPLICABILITY_RANK.get(app_status, 0),
        0 if h.has_uncertainty else 1,
        -dated_rank,
        -METHOD_RANK.get(str(h.method), 0),
        -as_of,
        h.estimate_key,
    )
