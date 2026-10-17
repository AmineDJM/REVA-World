"""Series extraction from atlas frames and question generation (pure: no database).

A question is fully defined at issue time: variable (family / concept), geography, reference period, type
(``quantity`` / ``interval`` / ``binary`` with its threshold), unit, resolution source and rule, expected
resolution date and void date. Its id is a hash of that definition; its ``question_key`` (cycle, family, geo,
period, type) is what the scheduled regeneration de-duplicates on within a quarterly cycle.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

import polars as pl

from reva.evaluation.registry import periods as P
from reva.evaluation.registry.specs import Family
from reva.kernel.ids import logical_hash

QUESTION_SCHEMA = "reva-forecast-question/1"
RESOLUTION_RULE = "first_observed_release_after_seal"
MAX_STALE = {"M": 12, "Q": 6, "A": 2}
PROJECTION_KINDS = ("projection", "unknown_boundary")
DERIVED_QUANT = Decimal("1e-8")


@dataclass(frozen=True)
class Cell:
    idx: int
    value: str
    refs: tuple[dict[str, Any], ...]  # [{raw_sha256, selector, flags}] of the source cell(s)


@dataclass
class Series:
    geo: str
    iso3: str | None
    label_en: str
    label_fr: str
    observed: list[Cell] = field(default_factory=list)
    projections: dict[int, Cell] = field(default_factory=dict)

    def history(self) -> list[tuple[int, float]]:
        return [(c.idx, float(Decimal(c.value))) for c in self.observed]

    def observed_at(self, idx: int) -> Cell | None:
        for c in self.observed:
            if c.idx == idx:
                return c
        return None


def _flags(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        return {"unparsed_flags": raw[:200]}
    return v if isinstance(v, dict) else {}


def _country_labels() -> dict[str, tuple[str, str]]:
    """Country labels from the reviewed crosswalk file (read directly: no connector import, invariant 11)."""
    from reva.ontology.registry import REGISTRY_ROOT

    doc = json.loads((REGISTRY_ROOT / "entities" / "country_crosswalk.json").read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for e in doc.get("entities", []):
        key = e.get("key")
        if key:
            out[str(key)] = (str(e.get("label") or key), str(e.get("label_fr") or e.get("label") or key))
    return out


_LABELS: dict[str, tuple[str, str]] | None = None


def country_label(iso3: str) -> tuple[str, str]:
    global _LABELS
    if _LABELS is None:
        try:
            _LABELS = _country_labels()
        except Exception:  # crosswalk unavailable: the code itself is the label
            _LABELS = {}
    return _LABELS.get(iso3, (iso3, iso3))


def extract_series(frame: pl.DataFrame, family: Family) -> tuple[dict[str, Series], list[dict[str, Any]]]:
    """Per-geography observed series (and projection cells, kept apart) of one family in one atlas table."""
    notes: list[dict[str, Any]] = []
    if family.indicator is not None:
        f = frame.filter(pl.col("indicator") == family.indicator)
    else:
        f = frame.filter(pl.col("indicator").str.starts_with(family.indicator_parts[0]))
        f = f.filter(pl.col("indicator").map_elements(family.matches_indicator, return_dtype=pl.Boolean))
    if family.geos:
        by_provider = {g.provider_geo: g for g in family.geos}
        f = f.filter(pl.col("provider_geo").is_in(list(by_provider)))
    else:
        f = f.filter(pl.col("iso3").is_not_null())
        if family.countries:
            f = f.filter(pl.col("iso3").is_in(list(family.countries)))
    f = f.filter(pl.col("value").is_not_null())
    cells: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    want = "D" if family.value_rule == "monthly_mean_of_daily" else family.frequency
    dropped = 0
    for r in f.select(
        "iso3", "provider_geo", "indicator", "period_start", "period_end", "value", "flags", "raw_sha256", "selector"
    ).iter_rows(named=True):
        start, end = r["period_start"], r["period_end"]
        if want == "D":
            if (end - start).days != 1:
                dropped += 1
                continue
            idx = P.index_of("M", start)
            key_idx = start.toordinal()
        else:
            cl = P.classify(start, end)
            if cl is None or cl[0] != want:
                dropped += 1
                continue
            idx = key_idx = cl[1]
        geo = r["provider_geo"] if family.geos else r["iso3"]
        cells[geo][key_idx].append(
            {
                "idx": idx,
                "indicator": r["indicator"],
                "value": r["value"],
                "flags": _flags(r["flags"]),
                "raw_sha256": r["raw_sha256"],
                "selector": _flags(r["selector"]),
            }
        )
    if dropped:
        notes.append({"family": family.key, "reason": "cells at another frequency / non-calendar window", "n": dropped})
    out: dict[str, Series] = {}
    for geo, by_idx in sorted(cells.items()):
        ambiguous = [k for k, v in by_idx.items() if len({c["indicator"] for c in v}) > 1 or len(v) > 1]
        if ambiguous:
            notes.append({"family": family.key, "geo": geo, "reason": "several series match one period; skipped"})
            continue
        if family.geos:
            g = next(x for x in family.geos if x.provider_geo == geo)
            s = Series(g.key, None, g.label_en, g.label_fr)
        else:
            en, fr = country_label(geo)
            s = Series(geo, geo, en, fr)
        raw_cells = sorted((v[0] for v in by_idx.values()), key=lambda c: c["idx"])
        _fill(s, raw_cells, family, by_idx)
        if s.observed or s.projections:
            out[s.geo] = s
    return out, notes


def _ref(c: Mapping[str, Any]) -> dict[str, Any]:
    return {"raw_sha256": c["raw_sha256"], "selector": c["selector"], "flags": c["flags"]}


def _is_projection(c: Mapping[str, Any]) -> bool:
    return c["flags"].get("estimate_kind") in PROJECTION_KINDS


def _fill(s: Series, raw_cells: list[dict[str, Any]], family: Family, by_idx: Any) -> None:
    if family.value_rule == "cell":
        for c in raw_cells:
            cell = Cell(c["idx"], str(c["value"]), (_ref(c),))
            if _is_projection(c):
                s.projections[c["idx"]] = cell
            else:
                s.observed.append(cell)
        return
    obs = [c for c in raw_cells if not _is_projection(c)]
    if family.value_rule == "yoy_pct_from_index":
        lag = P.season_of(family.frequency)
        by = {c["idx"]: c for c in obs}
        for c in obs:
            prev = by.get(c["idx"] - lag)
            if prev is None:
                continue
            v = yoy_pct(str(c["value"]), str(prev["value"]))
            if v is not None:
                s.observed.append(Cell(c["idx"], v, (_ref(c), _ref(prev))))
        return
    if family.value_rule == "monthly_mean_of_daily":
        months: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for c in obs:
            months[c["idx"]].append(c)
        if not months:
            return
        last_month = max(months)
        for m in sorted(months):
            if m == last_month:  # the latest month may still be incomplete: never an observation
                continue
            vals = [Decimal(str(c["value"])) for c in months[m]]
            mean = (sum(vals, Decimal(0)) / len(vals)).quantize(DERIVED_QUANT)
            s.observed.append(Cell(m, format(mean, "f"), tuple(_ref(c) for c in months[m][:3])))


def yoy_pct(current: str, previous: str) -> str | None:
    """100 x (current / previous - 1) in decimal arithmetic, quantised to 1e-8 (None when previous is 0)."""
    try:
        a, b = Decimal(current), Decimal(previous)
    except InvalidOperation:
        return None
    if b == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 34
        v = ((a / b - 1) * 100).quantize(DERIVED_QUANT)
    return format(v, "f")


# --------------------------------------------------------------------------- generation


def _fmt_threshold(v: str) -> str:
    try:
        d = Decimal(v)
    except InvalidOperation:
        return v
    return format(d.normalize(), "f") if math.isfinite(float(d)) else v


def question_text(q: Mapping[str, Any]) -> tuple[str, str]:
    var_fr, var_en = q["label_fr"], q["label_en"]
    geo_fr, geo_en, per = q["geo_label_fr"], q["geo_label_en"], q["reference_period"]
    unit = q["unit"]
    if q["type"] == "quantity":
        return (
            f"Quelle sera la valeur publiée de « {var_fr} » pour {geo_fr} ({q['geo']}), période {per} ? ({unit})",
            f"What will the published value of '{var_en}' be for {geo_en} ({q['geo']}), period {per}? ({unit})",
        )
    if q["type"] == "interval":
        lvl = round(100 * float(q["interval_level"]))
        return (
            f"Intervalle central à {lvl} % de « {var_fr} » pour {geo_fr} ({q['geo']}), période {per} ({unit})",
            f"Central {lvl}% interval of '{var_en}' for {geo_en} ({q['geo']}), period {per} ({unit})",
        )
    t = _fmt_threshold(str(q["threshold"]))
    b = q["threshold_basis"]
    return (
        f"« {var_fr} » pour {geo_fr} ({q['geo']}), période {per}, sera-t-elle strictement supérieure à {t} "
        f"({unit}, dernière valeur publiée au moment de la question, période {b['period']}) ?",
        f"Will '{var_en}' for {geo_en} ({q['geo']}), period {per}, be strictly above {t} "
        f"({unit}, latest published value when asked, period {b['period']})?",
    )


def question_id(q: Mapping[str, Any]) -> str:
    return "fq_" + logical_hash({k: v for k, v in q.items() if k != "question_id"})[:24]


def generate_questions(
    family: Family,
    series: Mapping[str, Series],
    *,
    cycle: str,
    interval_level: float,
    source_pin: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Questions of one family for one quarterly cycle; skipped (geo, reason) listed."""
    freq = family.frequency
    anchor = P.cycle_anchor(cycle)
    anchor_idx = P.index_of(freq, anchor)
    questions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    geos = [g.key for g in family.geos] if family.geos else list(family.countries or sorted(series))
    for geo in geos:
        s = series.get(geo)
        if s is None or not s.observed:
            skipped.append({"family": family.key, "geo": geo, "reason": "no observed value in the pinned table"})
            continue
        last = s.observed[-1]
        if anchor_idx - last.idx > MAX_STALE[freq]:
            skipped.append(
                {
                    "family": family.key,
                    "geo": geo,
                    "reason": f"stale series: last observed period {P.label_of(freq, last.idx)}",
                }
            )
            continue
        for rule in family.questions:
            for h in rule.horizons:
                target = anchor_idx + h
                if target <= last.idx:
                    skipped.append(
                        {
                            "family": family.key,
                            "geo": geo,
                            "period": P.label_of(freq, target),
                            "reason": "reference period already observed at issue",
                        }
                    )
                    continue
                pend = P.end_of(freq, target)
                q: dict[str, Any] = {
                    "schema": QUESTION_SCHEMA,
                    "question_key": f"{cycle}|{family.key}|{geo}|{P.label_of(freq, target)}|{rule.type}",
                    "cycle": cycle,
                    "family": family.key,
                    "concept_id": family.concept_id,
                    "label_fr": family.label_fr,
                    "label_en": family.label_en,
                    "definition": family.definition,
                    "definition_source": family.definition_source,
                    "geo": geo,
                    "iso3": s.iso3,
                    "geo_label_en": s.label_en,
                    "geo_label_fr": s.label_fr,
                    "frequency": freq,
                    "reference_period": P.label_of(freq, target),
                    "period_start": P.start_of(freq, target).isoformat(),
                    "period_end": pend.isoformat(),
                    "cycle_horizon": h,
                    "type": rule.type,
                    "unit": family.unit,
                    "interval_level": interval_level if rule.type == "interval" else None,
                    "threshold": last.value if rule.type == "binary" else None,
                    "threshold_rule": rule.threshold_rule,
                    "threshold_basis": (
                        {"period": P.label_of(freq, last.idx), "value": last.value} if rule.type == "binary" else None
                    ),
                    "last_observed": {"period": P.label_of(freq, last.idx), "value": last.value},
                    "resolution": {
                        "source": family.dataset_key,
                        "indicator": family.indicator,
                        "indicator_parts": list(family.indicator_parts) or None,
                        "geo_match": "provider_geo" if family.geos else "iso3",
                        "provider_geo": next((g.provider_geo for g in family.geos if g.key == geo), None),
                        "value_rule": family.value_rule,
                        "rule": RESOLUTION_RULE,
                        "expected_date": (pend + timedelta(days=family.publication_lag_days)).isoformat(),
                        "void_after": (pend + timedelta(days=family.void_after_days)).isoformat(),
                        "note": family.resolution_note,
                        "issue_table": dict(source_pin),
                    },
                }
                q["text_fr"], q["text_en"] = question_text(q)
                q["question_id"] = question_id(q)
                questions.append(q)
    return questions, skipped


def resolution_value(q: Mapping[str, Any], series: Mapping[str, Series]) -> Cell | None:
    """The observed cell answering ``q`` in one table version's extracted series (None when absent)."""
    s = series.get(str(q["geo"]))
    if s is None:
        return None
    return s.observed_at(P.parse_label(str(q["frequency"]), str(q["reference_period"])))


def binary_outcome(q: Mapping[str, Any], value: float) -> int:
    return 1 if value > float(Decimal(str(q["threshold"]))) else 0


def period_ended(q: Mapping[str, Any], today: date) -> bool:
    return date.fromisoformat(str(q["period_end"])) <= today


def history_until(s: Series, max_idx: int) -> list[tuple[int, float]]:
    return [(c.idx, float(Decimal(c.value))) for c in s.observed if c.idx <= max_idx]


__all__ = [
    "QUESTION_SCHEMA",
    "RESOLUTION_RULE",
    "Cell",
    "Series",
    "binary_outcome",
    "extract_series",
    "generate_questions",
    "history_until",
    "period_ended",
    "question_id",
    "question_text",
    "resolution_value",
    "yoy_pct",
]
