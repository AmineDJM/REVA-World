"""Health forecast registry, pure part: specs, periods, series extraction, questions, forecasts and back-tests.

Nothing here touches the database. Series come from atlas frames (``reva.atlas.tables`` long schema) of pinned table
versions; projections (``flags.estimate_kind`` = projection) are never read as observations. Forecasts are quantile
sets at :data:`reva.models.forecasting.health_quantile.HUB_LEVELS`.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from reva.atlas.projection import PROJECTION_KINDS as PROJECTION_KINDS  # FIX5: one vocabulary (re-export)
from reva.atlas.projection import is_projection
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.models.forecasting import health_quantile as HQ
from reva.ontology.registry import REGISTRY_ROOT

SPECS_PATH = REGISTRY_ROOT / "forecasts" / "health_question_specs.yaml"
QUESTION_SCHEMA = "reva-health-question/1"
FORECAST_SCHEMA = "reva-health-forecast/1"
RESOLUTION_RULE = "first_observed_release_after_seal"
LEVELS = HQ.HUB_LEVELS
STALE_WEEKS = 26
STALE_YEARS = 5
DM_LAG = 3

#: model key -> (version, role, description). Roles: baseline (always issued), candidate (issued only as a winner).
MODELS: dict[str, tuple[str, str, str]] = {
    "health.persistence": ("1.0.0", "baseline", "last value + symmetrised empirical h-step changes (trailing window)"),
    "health.climatology": (
        "1.0.0",
        "baseline",
        "empirical quantiles of the same season week (+/- 2) in earlier seasons",
    ),
    "health.log_ar": ("1.0.0", "candidate", "AR(2) on weekly log(y+1) differences, residual bootstrap"),
    "health.log_drift": ("1.0.0", "candidate", "random walk with drift on log values (annual)"),
    "health.blend": ("1.0.0", "candidate", "quantile average of the other models that did not abstain"),
}
REFERENCE_MODELS: dict[str, tuple[str, str]] = {
    "forecast.reference_flusight_ensemble": ("1.0.0", "CDC FluSight-ensemble, attributed; quantiles as published"),
    "forecast.reference_covidhub_ensemble": ("1.0.0", "CDC CovidHub-ensemble, attributed; quantiles as published"),
}
WEEKLY_MODELS = ("health.persistence", "health.climatology", "health.log_ar", "health.blend")
ANNUAL_MODELS = ("health.persistence", "health.log_drift", "health.blend")


# --------------------------------------------------------------------------- specs


@dataclass(frozen=True)
class Family:
    key: str
    label_fr: str
    label_en: str
    definition: str
    definition_source: str
    dataset_key: str
    value_rule: str
    frequency: str
    week_start: str | None
    unit: str
    lower: float | None
    upper: float | None
    geos: tuple[str, ...] | None  # None = every geography of the table
    horizons: tuple[int, ...]
    void_after_days: int
    backtest: Mapping[str, Any]
    indicators: tuple[tuple[str, str | None], ...]  # (indicator, GHE cause or None)
    reference: Mapping[str, Any] | None = None
    hub: str | None = None
    min_specimens: int = 0
    disease_link: Any = None
    disease_link_reason: str | None = None
    licence_review: bool = False
    resolution_note: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def models(self) -> tuple[str, ...]:
        return ANNUAL_MODELS if self.frequency == "A" else WEEKLY_MODELS

    @property
    def baselines(self) -> tuple[str, ...]:
        return tuple(m for m in self.models if MODELS[m][1] == "baseline")

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(m for m in self.models if MODELS[m][1] == "candidate")


@dataclass(frozen=True)
class SpecSet:
    version: str
    alpha: float
    families: tuple[Family, ...]
    sha256: str

    def family(self, key: str) -> Family:
        for f in self.families:
            if f.key == key:
                return f
        raise KeyError(key)


def _fail(msg: str, **ctx: Any) -> None:
    raise ValidationProblem(f"health_question_specs: {msg}", **ctx)


def _family(doc: Mapping[str, Any], panels: Mapping[str, Sequence[str]]) -> Family:
    key = str(doc.get("key") or "")
    freq = doc.get("frequency")
    if freq not in ("W", "A"):
        _fail("frequency must be W or A", family=key)
    ws = doc.get("week_start")
    if freq == "W" and ws not in ("monday", "sunday"):
        _fail("weekly families need week_start monday|sunday", family=key)
    rule = doc.get("value_rule")
    if rule not in ("cell", "flunet_positivity"):
        _fail("unknown value_rule", family=key)
    if doc.get("indicator_template"):
        causes = dict(doc.get("causes") or {})
        if not causes:
            _fail("indicator_template needs causes", family=key)
        inds: tuple[tuple[str, str | None], ...] = tuple(
            (str(doc["indicator_template"]).format(cause=c), str(c)) for c in causes
        )
    elif rule == "flunet_positivity":
        inds = (("flunet.*", None),)
    else:
        inds = ((str(doc["indicator"]), None),)
    if doc.get("panel"):
        geos: tuple[str, ...] | None = tuple(panels[doc["panel"]])
    elif doc.get("geos") == "all":
        geos = None
    else:
        geos = tuple(doc.get("geos") or ())
    lo, hi = ([*list(doc.get("bounds") or [0, None]), None, None])[:2]
    hz = tuple(int(h) for h in doc.get("horizons") or ())
    if not hz:
        _fail("family without horizons", family=key)
    labels = doc.get("labels") or {}
    return Family(
        key=key,
        label_fr=str(labels.get("fr") or key),
        label_en=str(labels.get("en") or key),
        definition=str(doc.get("definition") or ""),
        definition_source=str(doc.get("definition_source") or ""),
        dataset_key=str(doc["dataset_key"]),
        value_rule=str(rule),
        frequency=str(freq),
        week_start=ws,
        unit=str(doc.get("unit") or ""),
        lower=None if lo is None else float(lo),
        upper=None if hi is None else float(hi),
        geos=geos,
        horizons=hz,
        void_after_days=int(doc["void_after_days"]),
        backtest=dict(doc.get("backtest") or {}),
        indicators=inds,
        reference=dict(doc["reference_forecaster"]) if doc.get("reference_forecaster") else None,
        hub=doc.get("hub"),
        min_specimens=int(doc.get("min_specimens") or 0),
        disease_link=doc.get("disease_link"),
        disease_link_reason=doc.get("disease_link_reason"),
        licence_review=bool(doc.get("licence_review")),
        resolution_note=doc.get("resolution_note"),
        raw=dict(doc),
    )


def parse_specs(doc: Mapping[str, Any]) -> SpecSet:
    if doc.get("schema") != "reva-health-question-specs/1":
        _fail("unknown schema")
    panels = {str(k): [str(c) for c in v] for k, v in (doc.get("panels") or {}).items()}
    fams = tuple(_family(f, panels) for f in doc.get("families") or [])
    if len({f.key for f in fams}) != len(fams):
        _fail("duplicate family keys")
    alpha = float((doc.get("winner_rule") or {}).get("alpha", 0.05))
    return SpecSet(str(doc.get("version")), alpha, fams, logical_hash(dict(doc)))


def load_specs(path: Path = SPECS_PATH) -> SpecSet:
    return parse_specs(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def cached_specs() -> SpecSet:
    return load_specs()


# --------------------------------------------------------------------------- periods


def week_idx(start: date, week_start: str) -> int:
    o = start.toordinal()
    if week_start == "monday":
        if start.weekday() != 0:
            raise ValueError(f"{start} is not a Monday")
        return (o - 1) // 7
    if start.weekday() != 6:
        raise ValueError(f"{start} is not a Sunday")
    return o // 7


def week_start_of(idx: int, week_start: str) -> date:
    return date.fromordinal(idx * 7 + 1) if week_start == "monday" else date.fromordinal(idx * 7)


def period_of(fam: Family, idx: int) -> tuple[date, date]:
    if fam.frequency == "A":
        return date(idx, 1, 1), date(idx + 1, 1, 1)
    s = week_start_of(idx, str(fam.week_start))
    return s, s + timedelta(days=7)


def idx_of_cell(fam: Family, start: date, end: date) -> int | None:
    if fam.frequency == "A":
        if start.month == 1 and start.day == 1 and end == date(start.year + 1, 1, 1):
            return start.year
        return None
    if (end - start).days != 7:
        return None
    try:
        return week_idx(start, str(fam.week_start))
    except ValueError:
        return None


def label_of(fam: Family, idx: int) -> str:
    if fam.frequency == "A":
        return f"{idx:04d}"
    s = week_start_of(idx, str(fam.week_start))
    if fam.week_start == "monday":
        y, w, _ = s.isocalendar()
        return f"{y:04d}-W{w:02d}"
    return f"EW-{(s + timedelta(days=6)).isoformat()}"


def season_week(fam: Family, idx: int) -> int:
    return (week_start_of(idx, str(fam.week_start)) + timedelta(days=3)).isocalendar()[1]


def season_year(fam: Family, idx: int) -> int:
    return (week_start_of(idx, str(fam.week_start)) + timedelta(days=3)).isocalendar()[0]


def round_of(fam: Family, today: date) -> tuple[str, int]:
    """(round label, anchor index): annual = (year, year); ISO-week = (ISO week label, week index of the issue week);
    hub = (reference date, index of the epiweek ending on the reference date)."""
    if fam.frequency == "A":
        return f"{today.year:04d}", today.year
    if fam.week_start == "sunday":
        ref = today + timedelta(days=(5 - today.weekday()) % 7)
        return ref.isoformat(), week_idx(ref - timedelta(days=6), "sunday")
    monday = today - timedelta(days=today.weekday())
    i = week_idx(monday, "monday")
    return label_of(fam, i), i


# --------------------------------------------------------------------------- series


@dataclass(frozen=True)
class Cell:
    value: str
    refs: tuple[Mapping[str, Any], ...]


Series = dict[str, dict[int, Cell]]  # series key (geo or geo|cause) -> period index -> cell


def series_key(geo: str, cause: str | None) -> str:
    return geo if cause is None else f"{geo}|cause={cause}"


def _json(s: str | None) -> dict[str, Any]:
    if not s:
        return {}
    try:
        v = json.loads(s)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _geo_ok(fam: Family, geo: str) -> bool:
    return fam.geos is None or geo in fam.geos


def extract_cells(frame: pl.DataFrame, fam: Family) -> tuple[Series, dict[str, str], list[dict[str, Any]]]:
    """Observed cells of a ``cell`` family: (series, labels, notes). Projections are skipped."""
    out: Series = {}
    labels: dict[str, str] = {}
    notes: list[dict[str, Any]] = []
    want = {ind: cause for ind, cause in fam.indicators}
    f = frame.filter(pl.col("indicator").is_in(list(want)) & pl.col("value").is_not_null())
    dup: set[str] = set()
    for r in f.select(
        "indicator", "provider_geo", "period_start", "period_end", "value", "flags", "raw_sha256", "selector"
    ).iter_rows(named=True):
        geo = str(r["provider_geo"])
        if not _geo_ok(fam, geo):
            continue
        flags = _json(r["flags"])
        if is_projection(flags, period_start=r["period_start"]):
            continue
        idx = idx_of_cell(fam, r["period_start"], r["period_end"])
        if idx is None:
            continue
        k = series_key(geo, want[r["indicator"]])
        cells = out.setdefault(k, {})
        if idx in cells:
            dup.add(k)
            continue
        cells[idx] = Cell(str(r["value"]), ({"raw_sha256": r["raw_sha256"], "selector": _json(r["selector"])},))
        if "location_name" in flags:
            labels[geo] = str(flags["location_name"])
    for k in sorted(dup):
        notes.append({"family": fam.key, "series": k, "reason": "two cells for one period; series skipped"})
        out.pop(k, None)
    return out, labels, notes


_FLU_COLS = ("spec_processed_nb", "inf_all", "inf_negative")


def positivity(
    rows: Mapping[str, Mapping[str, Decimal | None]], min_specimens: int
) -> tuple[str | None, dict[str, Any]]:
    """FluNet positivity of one country-week from its origin rows ``{origin: {column: value}}`` (see the family
    definition). Returns (value or None, detail)."""
    pos = tot = Decimal(0)
    used, derived_zero = [], []
    for origin, cols in sorted(rows.items()):
        p, a, n = cols.get("spec_processed_nb"), cols.get("inf_all"), cols.get("inf_negative")
        if p is None or p <= 0:
            continue
        if a is None:
            if n is not None and n == p:
                a = Decimal(0)
                derived_zero.append(origin)
            else:
                continue
        if a > p:
            continue
        pos += a
        tot += p
        used.append(origin)
    detail = {
        "origins": used,
        "specimens": str(tot),
        "positives": str(pos),
        "inf_all_zero_from_negatives": derived_zero,
    }
    if tot < min_specimens or tot == 0:
        return None, {**detail, "reason": f"fewer than {min_specimens} processed specimens"}
    v = (100 * pos / tot).quantize(Decimal("1e-6"))
    return format(v, "f"), detail


def extract_flunet(frame: pl.DataFrame, fam: Family) -> tuple[Series, dict[str, str], list[dict[str, Any]]]:
    f = frame.filter(pl.col("indicator").str.starts_with("flunet."))
    acc: dict[tuple[str, int], dict[str, dict[str, Decimal | None]]] = defaultdict(lambda: defaultdict(dict))
    refs: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for r in f.select(
        "indicator", "provider_geo", "period_start", "period_end", "value", "raw_sha256", "selector"
    ).iter_rows(named=True):
        geo = str(r["provider_geo"])
        if not _geo_ok(fam, geo):
            continue
        col, _, origin = str(r["indicator"]).removeprefix("flunet.").partition("|origin=")
        if col not in _FLU_COLS:
            continue
        idx = idx_of_cell(fam, r["period_start"], r["period_end"])
        if idx is None:
            continue
        acc[(geo, idx)][origin][col] = None if r["value"] is None else Decimal(str(r["value"]))
        if col == "spec_processed_nb" and len(refs[(geo, idx)]) < 3:
            refs[(geo, idx)].append({"raw_sha256": r["raw_sha256"], "selector": _json(r["selector"])})
    out: Series = {}
    for (geo, idx), rows in acc.items():
        v, detail = positivity(rows, fam.min_specimens)
        if v is not None:
            out.setdefault(geo, {})[idx] = Cell(v, tuple({**x, "derivation": detail} for x in refs[(geo, idx)][:1]))
    return out, {}, []


def extract_series(frame: pl.DataFrame, fam: Family) -> tuple[Series, dict[str, str], list[dict[str, Any]]]:
    if fam.value_rule == "flunet_positivity":
        return extract_flunet(frame, fam)
    return extract_cells(frame, fam)


def as_floats(cells: Mapping[int, Cell], until: int | None = None) -> list[tuple[int, float]]:
    return [(i, float(Decimal(c.value))) for i, c in sorted(cells.items()) if until is None or i <= until]


def extract_vintages(frame: pl.DataFrame, fam: Family) -> dict[date, dict[str, dict[int, float]]]:
    """``{vintage date: {geo: {epiweek index: value}}}`` of a hub vintages table."""
    ind = fam.indicators[0][0]
    f = frame.filter(pl.col("indicator").str.starts_with(ind + "|vintage=") & pl.col("value").is_not_null())
    out: dict[date, dict[str, dict[int, float]]] = defaultdict(lambda: defaultdict(dict))
    for r in f.select("indicator", "provider_geo", "period_start", "period_end", "value").iter_rows(named=True):
        geo = str(r["provider_geo"])
        if not _geo_ok(fam, geo):
            continue
        idx = idx_of_cell(fam, r["period_start"], r["period_end"])
        if idx is None:
            continue
        v = date.fromisoformat(str(r["indicator"]).rsplit("=", 1)[1])
        out[v][geo][idx] = float(Decimal(str(r["value"])))
    return out


def history_as_of(
    vintages: Mapping[date, Mapping[str, Mapping[int, float]]], cutoff: date
) -> dict[str, list[tuple[int, float]]]:
    """Each epiweek's value from the latest vintage <= ``cutoff`` that published it (later vintages override)."""
    merged: dict[str, dict[int, float]] = defaultdict(dict)
    for v in sorted(x for x in vintages if x <= cutoff):
        for geo, cells in vintages[v].items():
            merged[geo].update(cells)
    return {g: sorted(c.items()) for g, c in merged.items()}


def extract_ensemble(
    frame: pl.DataFrame, fam: Family
) -> dict[tuple[str, str, int], tuple[tuple[str, ...], dict[str, Any]]]:
    """``{(reference date, geo, epiweek index): (23 quantile values as published, source ref)}`` (complete sets only)."""
    assert fam.reference is not None
    prefix = f"{fam.reference['model_id']}.{fam.reference['target']}|ref="
    f = frame.filter(pl.col("indicator").str.starts_with(prefix))
    acc: dict[tuple[str, str, int], dict[str, str]] = defaultdict(dict)
    ref0: dict[tuple[str, str, int], dict[str, Any]] = {}
    want = {format(Decimal(str(lv)).normalize(), "f"): lv for lv in LEVELS}
    for r in f.select(
        "indicator", "provider_geo", "period_start", "period_end", "value", "raw_sha256", "selector"
    ).iter_rows(named=True):
        geo = str(r["provider_geo"])
        if not _geo_ok(fam, geo) or r["value"] is None:
            continue
        idx = idx_of_cell(fam, r["period_start"], r["period_end"])
        if idx is None:
            continue
        rest = str(r["indicator"])[len(prefix) :]
        ref, _, q = rest.partition("|q=")
        try:
            qn = format(Decimal(q).normalize(), "f")
        except InvalidOperation:
            continue
        if qn not in want:
            continue
        acc[(ref, geo, idx)][qn] = str(r["value"])
        if qn == "0.5":
            ref0[(ref, geo, idx)] = {
                "raw_sha256": r["raw_sha256"],
                "selector": _json(r["selector"]),
                "indicator": r["indicator"],
            }
    out = {}
    for k, qs in acc.items():
        if len(qs) == len(want):
            out[k] = (tuple(qs[format(Decimal(str(lv)).normalize(), "f")] for lv in LEVELS), ref0.get(k, {}))
    return out


# --------------------------------------------------------------------------- forecasting


def num(x: float) -> str:
    return format(float(x), ".10g")


def run_models(fam: Family, history: Sequence[tuple[int, float]], target: int) -> dict[str, HQ.Result]:
    if not history:
        return {m: HQ.Abstain("no history") for m in fam.models}
    steps = target - history[-1][0]
    if steps < 1:
        return {m: HQ.Abstain("target already observed") for m in fam.models}
    lo, hi = fam.lower, fam.upper
    out: dict[str, HQ.Result] = {}
    if fam.frequency == "A":
        out["health.persistence"] = HQ.persistence(history, steps, freq="A", lower=lo, upper=hi)
        out["health.log_drift"] = HQ.log_drift(history, steps, lower=lo, upper=hi)
        out["health.blend"] = HQ.blend(
            {k: out[k] for k in ("health.persistence", "health.log_drift")}, lower=lo, upper=hi
        )
        return out
    out["health.persistence"] = HQ.persistence(history, steps, freq="W", lower=lo, upper=hi)
    out["health.climatology"] = HQ.climatology(
        history, target, lambda i: season_week(fam, i), lambda i: season_year(fam, i), lower=lo, upper=hi
    )
    out["health.log_ar"] = HQ.log_ar(history, steps, lower=lo, upper=hi)
    out["health.blend"] = HQ.blend(
        {k: out[k] for k in ("health.persistence", "health.climatology", "health.log_ar")}, lower=lo, upper=hi
    )
    return out


def train_digest(history: Sequence[tuple[int, float]]) -> str:
    return logical_hash([[i, num(v)] for i, v in history])


def forecast_record(
    q: Mapping[str, Any], model: str, res: HQ.Result, history: Sequence[tuple[int, float]]
) -> dict[str, Any]:
    base = {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": model}
    if isinstance(res, HQ.Abstain):
        return {**base, "status": "abstained", "reason": res.reason}
    return {
        **base,
        "status": "issued",
        "levels": [num(lv) for lv in res.levels],
        "quantiles": [num(v) for v in res.values],
        "detail": json.loads(json.dumps(dict(res.detail), default=str)),
        "steps_from_last_observed": q["steps_from_last_observed"],
        "train_digest": train_digest(history),
        "n_train": len(history),
    }


def reference_record(
    q: Mapping[str, Any], fam: Family, ens: tuple[tuple[str, ...], Mapping[str, Any]] | None
) -> dict[str, Any]:
    assert fam.reference is not None
    mk = str(fam.reference["model_key"])
    base = {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": mk}
    if ens is None:
        return {
            **base,
            "status": "abstained",
            "reason": "no complete ensemble quantile set for this round in the pinned table",
        }
    values, ref = ens
    return {
        **base,
        "status": "issued",
        "levels": [num(lv) for lv in LEVELS],
        "quantiles": list(values),
        "attribution": f"{fam.reference['model_id']} of the US CDC {fam.hub} forecast hub, as published; not a REVA forecast",
        "source_cell": dict(ref),
    }


# --------------------------------------------------------------------------- questions


def question_id(q: Mapping[str, Any]) -> str:
    return "hq_" + logical_hash({k: v for k, v in q.items() if k != "question_id"})[:24]


def geo_label(geo: str, labels: Mapping[str, str]) -> str:
    if geo in labels:
        return labels[geo]
    if len(geo) == 3 and geo.isalpha() and geo.isupper():
        from reva.evaluation.registry.questions import country_label

        return country_label(geo)[0]
    return geo


def generate_questions(
    fam: Family,
    series: Series,
    labels: Mapping[str, str],
    *,
    today: date,
    source_pin: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rnd, anchor = round_of(fam, today)
    qs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for skey in sorted(series):
        cells = series[skey]
        geo, _, cause = skey.partition("|cause=")
        if not cells:
            continue
        last = max(cells)
        if fam.frequency == "A":
            if anchor - last > STALE_YEARS:
                skipped.append({"family": fam.key, "series": skey, "reason": f"stale: last year {last}"})
                continue
            targets = [(h, last + h) for h in fam.horizons]
        else:
            if anchor - last > STALE_WEEKS:
                skipped.append({"family": fam.key, "series": skey, "reason": f"stale: last week {label_of(fam, last)}"})
                continue
            targets = [(h, anchor + h) for h in fam.horizons]
        for h, t in targets:
            if t <= last:
                skipped.append(
                    {
                        "family": fam.key,
                        "series": skey,
                        "period": label_of(fam, t),
                        "reason": "already observed at issue",
                    }
                )
                continue
            start, end = period_of(fam, t)
            q: dict[str, Any] = {
                "schema": QUESTION_SCHEMA,
                "question_key": f"{rnd}|{fam.key}|{skey}|{label_of(fam, t)}|quantiles",
                "round": rnd,
                "family": fam.key,
                "label_fr": fam.label_fr,
                "label_en": fam.label_en,
                "definition": fam.definition,
                "definition_source": fam.definition_source,
                "geo": geo,
                "geo_label": geo_label(geo, labels),
                "cause": cause or None,
                "cause_title": (fam.raw.get("causes") or {}).get(cause) if cause else None,
                "frequency": fam.frequency,
                "week_start": fam.week_start,
                "reference_period": label_of(fam, t),
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "horizon": h,
                "steps_from_last_observed": t - last,
                "type": "quantiles",
                "levels": [num(lv) for lv in LEVELS],
                "unit": fam.unit,
                "bounds": [fam.lower, fam.upper],
                "last_observed": {"period": label_of(fam, last), "value": cells[last].value},
                "disease_link": fam.disease_link,
                "disease_link_reason": fam.disease_link_reason,
                "licence_review": fam.licence_review,
                "resolution": {
                    "source": fam.dataset_key,
                    "indicator": next(i for i, c in fam.indicators if (c or "") == cause)
                    if fam.value_rule == "cell"
                    else "flunet.* (derived positivity)",
                    "value_rule": fam.value_rule,
                    "rule": RESOLUTION_RULE,
                    "expected_after": end.isoformat(),
                    "void_after": (end + timedelta(days=fam.void_after_days)).isoformat(),
                    "note": fam.resolution_note,
                    "issue_table": dict(source_pin),
                },
            }
            q["text_en"] = (
                f"Quantiles ({len(LEVELS)} levels) of '{fam.label_en}'"
                + (f", cause '{q['cause_title']}'" if cause else "")
                + f" for {q['geo_label']} ({geo}), period {q['reference_period']} ({fam.unit})"
            )
            q["text_fr"] = (
                f"Quantiles ({len(LEVELS)} niveaux) de « {fam.label_fr} »"
                + (f", cause « {q['cause_title']} »" if cause else "")
                + f" pour {q['geo_label']} ({geo}), période {q['reference_period']} ({fam.unit})"
            )
            q["question_id"] = question_id(q)
            qs.append(q)
    return qs, skipped


# --------------------------------------------------------------------------- back-tests


@dataclass(frozen=True)
class Task:
    origin: str
    series: str
    horizon: int
    target: int
    history: Sequence[tuple[int, float]]
    truth: float
    reference: tuple[float, ...] | None = None


def final_vintage_tasks(fam: Family, series: Series) -> Iterable[Task]:
    """Final-vintage pseudo-real-time replays: at origin o, values with index <= o - lag are known; targets o + h
    (weekly) or o + h years (annual). Values are the final (revised) vintage: never real-time skill."""
    bt = fam.backtest
    lag = int(bt.get("publication_lag_weeks", 0))
    stride = int(bt.get("stride_weeks", 1))
    for skey in sorted(series):
        hist = as_floats(series[skey])
        if not hist:
            continue
        by = dict(hist)
        last = hist[-1][0]
        if fam.frequency == "A":
            first, final = int(bt["first_origin"]), int(bt.get("last_origin", last - 1))
            origins: Iterable[int] = range(first, final + 1)
            known_lag = 0
        else:
            first = week_idx(date.fromisoformat(str(bt["first_origin"])), str(fam.week_start))
            origins = range(first, last - min(fam.horizons) + 1, stride)
            known_lag = lag
        for o in origins:
            known = [(i, v) for i, v in hist if i <= o - known_lag]
            if not known:
                continue
            for h in fam.horizons:
                t = (known[-1][0] + h) if fam.frequency == "A" else o + h
                if t in by and t > known[-1][0]:
                    yield Task(label_of(fam, o), skey, h, t, known, by[t])


def as_of_tasks(
    fam: Family,
    truth: Series,
    vintages: Mapping[date, Mapping[str, Mapping[int, float]]],
    ensemble: Mapping[tuple[str, str, int], tuple[tuple[str, ...], Mapping[str, Any]]],
) -> tuple[list[Task], list[dict[str, Any]]]:
    """As-of replays of the hub rounds: for reference date R, the history is the vintage published for that round
    (vintage date = R - ``vintage_to_reference_days``), targets R + 7h, truth = the latest target table, reference =
    the hub ensemble quantiles of round R."""
    lag = int(fam.backtest.get("vintage_to_reference_days", 7))
    refs = sorted({k[0] for k in ensemble})
    tasks: list[Task] = []
    skipped: list[dict[str, Any]] = []
    for ref in refs:
        rd = date.fromisoformat(ref)
        v = rd - timedelta(days=lag)
        if v not in vintages:
            skipped.append({"reference_date": ref, "reason": f"no data vintage dated {v} (the round's data)"})
            continue
        hist = history_as_of(vintages, v)
        r_idx = week_idx(rd - timedelta(days=6), "sunday")
        for geo in sorted(hist):
            known = hist[geo]
            tcells = truth.get(geo, {})
            for h in fam.horizons:
                t = r_idx + h
                cell = tcells.get(t)
                if cell is None or not known or t <= known[-1][0]:
                    continue
                ens = ensemble.get((ref, geo, t))
                tasks.append(
                    Task(
                        ref,
                        geo,
                        h,
                        t,
                        known,
                        float(Decimal(cell.value)),
                        tuple(float(x) for x in ens[0]) if ens else None,
                    )
                )
    return tasks, skipped


def run_backtest(fam: Family, tasks: Iterable[Task], *, alpha: float) -> dict[str, Any]:
    """Score every model (and the reference) on each task; per-origin aggregates, relative WIS, DM tests, winners."""
    ref_key = str(fam.reference["model_key"]) if fam.reference else None
    per_origin: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    pair_origin: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0.0, 0.0]))
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wis": 0.0, "ae": 0.0, "c50": 0, "c90": 0, "by_h": defaultdict(lambda: [0.0, 0])}
    )
    national: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    abst: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    n_tasks = 0
    models = [*fam.models] + ([ref_key] if ref_key else [])
    for t in tasks:
        n_tasks += 1
        res = run_models(fam, t.history, t.target)
        wis: dict[str, float] = {}
        for m, r in res.items():
            if isinstance(r, HQ.Abstain):
                abst[m][r.reason] += 1
                continue
            sc = HQ.score(r.levels, r.values, t.truth)
            wis[m] = sc["wis"]
            _acc(stats[m], sc, t.horizon)
        if ref_key:
            if t.reference is None:
                abst[ref_key]["no ensemble forecast for this task"] += 1
            else:
                sc = HQ.score(LEVELS, t.reference, t.truth)
                wis[ref_key] = sc["wis"]
                _acc(stats[ref_key], sc, t.horizon)
        for m, w in wis.items():
            per_origin[t.origin][m][0] += w
            per_origin[t.origin][m][1] += 1
            if t.series == "US":
                national[m][0] += w
                national[m][1] += 1
        for m in wis:
            for b in wis:
                if m != b:
                    acc = pair_origin[f"{m}|{b}"][t.origin]
                    acc[0] += wis[m] - wis[b]
                    acc[1] += 1
                    acc[2] += wis[m]
                    acc[3] += wis[b]
    summary = {
        m: {
            "n": s["n"],
            "mean_wis": s["wis"] / s["n"],
            "mean_ae_median": s["ae"] / s["n"],
            "coverage50": s["c50"] / s["n"],
            "coverage90": s["c90"] / s["n"],
            "by_horizon": {str(h): v[0] / v[1] for h, v in sorted(s["by_h"].items())},
        }
        for m, s in stats.items()
        if s["n"]
    }
    benchmarks = [*fam.baselines] + ([ref_key] if ref_key else [])
    pairs: dict[str, Any] = {}
    for m in models:
        for b in benchmarks:
            key = f"{m}|{b}"
            if m == b or key not in pair_origin:
                continue
            po = pair_origin[key]
            origins = sorted(po)
            sm, sb, n = sum(po[o][2] for o in origins), sum(po[o][3] for o in origins), sum(po[o][1] for o in origins)
            diffs = [po[o][0] / po[o][1] for o in origins]
            pairs[key] = {
                "n_tasks": int(n),
                "n_origins": len(origins),
                "relative_wis": (sm / sb) if sb > 0 else None,
                "dm": HQ.dm_test(diffs, DM_LAG),
            }
    winners = []
    k = max(1, len(fam.baselines))
    for c in fam.candidates:
        ok = True
        for b in fam.baselines:
            p = pairs.get(f"{c}|{b}")
            dm = (p or {}).get("dm") or {}
            if not p or p["relative_wis"] is None or p["relative_wis"] >= 1 or dm.get("status") != "ok":
                ok = False
                break
            if not (dm["mean_diff"] < 0 and dm["p_value"] < alpha / k):
                ok = False
                break
        if ok:
            winners.append(c)
    return {
        "family": fam.key,
        "n_tasks": n_tasks,
        "models": summary,
        "national_US_mean_wis": {m: v[0] / v[1] for m, v in national.items() if v[1]} or None,
        "pairs": pairs,
        "winners": winners,
        "winner_rule": f"candidate beats every baseline: relative WIS < 1 and DM p < {alpha}/{k} with mean difference < 0",
        "abstentions": {m: dict(v) for m, v in abst.items()},
        "per_origin": {o: {m: [round(v[0], 6), v[1]] for m, v in d.items()} for o, d in sorted(per_origin.items())},
    }


def _acc(s: dict[str, Any], sc: Mapping[str, Any], h: int) -> None:
    s["n"] += 1
    s["wis"] += sc["wis"]
    s["ae"] += sc["ae_median"]
    s["c50"] += int(bool(sc["cover50"]))
    s["c90"] += int(bool(sc["cover90"]))
    s["by_h"][h][0] += sc["wis"]
    s["by_h"][h][1] += 1


def finite(x: Any) -> Any:
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: finite(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [finite(v) for v in x]
    return x


__all__ = [
    "LEVELS",
    "MODELS",
    "REFERENCE_MODELS",
    "Family",
    "SpecSet",
    "Task",
    "as_of_tasks",
    "extract_ensemble",
    "extract_series",
    "extract_vintages",
    "final_vintage_tasks",
    "forecast_record",
    "generate_questions",
    "history_as_of",
    "load_specs",
    "positivity",
    "reference_record",
    "round_of",
    "run_backtest",
    "run_models",
]
