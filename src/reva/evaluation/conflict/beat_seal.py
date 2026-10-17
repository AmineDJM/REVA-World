"""V7 BEAT-CONFLICT: sealed prospective country-month RISK ESTIMATES for the admitted families only (pure).

Admission (``registry/forecasts/beat_conflict/protocol.yaml``; run ``docs/dev/v7_beat_conflict_result.json``): the
W1 final-vintage backtest admitted ``forecast.cm_hurdle`` for both country-month families, hence also
``forecast.cm_stack_views`` (the combination with VIEWS). W3 freezes them at origin 2026-08 (GED 26.1 final to
2025-12 + UCDP Candidate 2026-01..08) for target months 2026-11..2027-04 (steps 3-8) with the two pre-registered
benchmarks: persistence (fair density from its own past errors) and the VIEWS run in force (attributed).

Questions resolve on ``atlas.ucdp_ged.cand_country_month`` (UCDP Candidate, real-time preliminary vintage) by the
registry's generic resolver, with the FIX6 rule ``zero_within_coverage`` (an absent country-month within the
resolving version's covered months = 0 recorded, labelled). Risk estimates from aggregate counts: never a
prediction of a specific attack, actor or person.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np

from reva.evaluation.conflict import beat as B
from reva.evaluation.conflict.families import Q_BEST, Q_BRD, Q_STATE_BASED, RISK_NOTE
from reva.evaluation.registry.absence import ABSENCE_RULE_TEXT, ZERO_WITHIN_COVERAGE
from reva.evaluation.registry.forecasts import FORECAST_SCHEMA, QUANTILE_LEVELS, num
from reva.evaluation.registry.questions import (
    QUESTION_SCHEMA,
    RESOLUTION_RULE,
    country_label,
    issue_timing,
    question_id,
)

PROTOCOL_PATH = "registry/forecasts/beat_conflict/protocol.yaml"
DATASET_KEY = "atlas.ucdp_ged.cand_country_month"
INDICATOR = "deaths_best|tov=sb"
UNIT = "deaths (UCDP GED best estimate, state-based)"
OWNER = "BEAT-CONFLICT"
PUBLICATION_LAG_DAYS = 45  # UCDP Candidate monthly releases, roughly within the following month or quarter
VOID_AFTER_DAYS = 400  # one missed annual GED cycle tolerated
DEFINITIONS_SOURCE = (
    "UCDP definitions page (uu.se, quoted verbatim in V6 PRED-CONFLICT); UCDP Candidate Events Dataset codebook "
    "1.5 (archived by V6CONFLICT, sha256 0107eac7...)"
)
MODELS: dict[str, tuple[str, str]] = {
    B.CAND: (
        "1.0.0",
        "W1-admitted REVA candidate: country-month ridge hurdle on GED dynamics, spread, neighbours, IMF shocks",
    ),
    B.STACK: ("1.0.0", "combination with VIEWS: linear pool, weight on VIEWS from past pairs only"),
    B.PERS: ("1.0.0", "BENCHMARK: persistence, fair density from its own past same-bin changes"),
    B.VIEWS: ("1.0.0", "external reference, attributed to VIEWS (main_mean / main_dich of the run in force)"),
}
FAMILIES: dict[str, dict[str, Any]] = {
    "ucdp.sb_deaths_cm": {
        "kind": "quantity",
        "threshold": None,
        "fr": "Morts au combat, conflits armés étatiques (mois-pays, UCDP Candidate)",
        "en": "State-based battle-related deaths (country-month, UCDP Candidate)",
        "rule": "Value: sum of the UCDP best estimates of state-based events (type_of_violence 1) whose date_start "
        "falls in the calendar month, located in the country (UCDP Candidate Events, REVA aggregate "
        "deaths_best|tov=sb).",
    },
    "ucdp.sb_conflict25_cm": {
        "kind": "binary",
        "threshold": 24,
        "fr": "Au moins 25 morts au combat (conflits étatiques) dans le mois",
        "en": "At least 25 state-based battle-related deaths in the country-month",
        "rule": "Event: the country-month value deaths_best|tov=sb is at least 25 (VIEWS' own target applied to "
        "UCDP Candidate; REVA rule).",
    },
}


def definition(key: str) -> str:
    f = FAMILIES[key]
    return f'{f["rule"]} UCDP: "{Q_STATE_BASED}" "{Q_BRD}" "{Q_BEST}" Absence: {ABSENCE_RULE_TEXT}. {RISK_NOTE}'


def family_doc(key: str, countries: Sequence[str]) -> dict[str, Any]:
    f = FAMILIES[key]
    q: dict[str, Any] = {"type": f["kind"], "horizons": list(range(3, 9))}
    doc: dict[str, Any] = {
        "key": key,
        "concept_id": None,
        "labels": {"fr": f["fr"], "en": f["en"]},
        "definition": definition(key),
        "definition_source": DEFINITIONS_SOURCE,
        "dataset_key": DATASET_KEY,
        "indicator": INDICATOR,
        "frequency": "M",
        "value_rule": "cell",
        "unit": UNIT,
        "countries": sorted(countries),
        "publication_lag_days": PUBLICATION_LAG_DAYS,
        "void_after_days": VOID_AFTER_DAYS,
        "backtest_lag_periods": 1,
        "absence_rule": ZERO_WITHIN_COVERAGE,
        "resolution_note": "First atlas.ucdp_ged.cand_country_month version registered after the seal whose coverage "
        "includes the month (UCDP Candidate, preliminary real-time vintage); zero_within_coverage. " + RISK_NOTE,
        "owner_agent": OWNER,
        "beat_conflict": {"protocol": PROTOCOL_PATH, "window": "W3"},
    }
    if f["kind"] == "binary":
        q["threshold_rule"] = "above_fixed"
        doc["threshold"] = str(f["threshold"])
    doc["questions"] = [q]
    return doc


def _texts(key: str, fr: str, en: str, iso: str, month: str, last: Mapping[str, str]) -> tuple[str, str]:
    lp, lv = last["period"], last["value"]
    if FAMILIES[key]["kind"] == "quantity":
        return (
            f"Estimation de risque : combien de morts dans des conflits armés étatiques (UCDP Candidate, estimation "
            f"« best ») seront recensés pour {fr} ({iso}) en {month} ? (dernier mois connu : {lv} en {lp})",
            f"Risk estimate: how many deaths in state-based armed conflict (UCDP Candidate best estimate) will be "
            f"recorded for {en} ({iso}) in {month}? (latest known month: {lv} in {lp})",
        )
    return (
        f"Estimation de risque : UCDP Candidate recensera-t-il au moins 25 morts dans des conflits armés étatiques "
        f"pour {fr} ({iso}) en {month} ?",
        f"Risk estimate: will UCDP Candidate record at least 25 deaths in state-based armed conflict for {en} "
        f"({iso}) in {month}?",
    )


def question(
    key: str,
    iso: str,
    gw: str,
    target: int,
    origin: int,
    last: float,
    cycle: str,
    pin: Mapping[str, Any],
    issued_on: date,
) -> dict[str, Any]:
    f = FAMILIES[key]
    en, fr = country_label(iso)
    ty, tm = target // 12, target % 12 + 1
    start = date(ty, tm, 1)
    end = date(ty + (tm == 12), tm % 12 + 1, 1)
    month = B.mlabel(target)
    lastd = {"period": B.mlabel(origin), "value": num(last)}
    q: dict[str, Any] = {
        "schema": QUESTION_SCHEMA,
        "question_key": f"{cycle}|{key}|{iso}|{month}|{f['kind']}",
        "cycle": cycle,
        "family": key,
        "concept_id": None,
        "label_fr": f["fr"],
        "label_en": f["en"],
        "definition": definition(key),
        "definition_source": DEFINITIONS_SOURCE,
        "geo": iso,
        "iso3": iso,
        "geo_label_en": en,
        "geo_label_fr": fr,
        "provider_geo": gw,
        "frequency": "M",
        "reference_period": month,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "cycle_horizon": target - origin,
        "type": f["kind"],
        "unit": UNIT,
        "interval_level": None,
        "threshold": str(f["threshold"]) if f["threshold"] is not None else None,
        "threshold_rule": "above_fixed" if f["kind"] == "binary" else None,
        "threshold_basis": (
            {"period": "fixed", "value": str(f["threshold"]), "meaning": "at least 25 deaths"}
            if f["threshold"] is not None
            else None
        ),
        "condition": None,
        "last_observed": lastd,
        "risk_note": RISK_NOTE,
        "timing": issue_timing(start, end, issued_on),
        "resolution": {
            "source": DATASET_KEY,
            "indicator": INDICATOR,
            "indicator_parts": None,
            "geo_match": "iso3",
            "provider_geo": None,
            "value_rule": "cell",
            "rule": RESOLUTION_RULE,
            "absence_rule": ZERO_WITHIN_COVERAGE,
            "absence_rule_text": ABSENCE_RULE_TEXT,
            "expected_date": (end + timedelta(days=PUBLICATION_LAG_DAYS)).isoformat(),
            "void_after": (end + timedelta(days=VOID_AFTER_DAYS)).isoformat(),
            "note": "First UCDP Candidate country-month table version registered after the seal covering the month.",
            "issue_table": dict(pin),
        },
    }
    q["text_fr"], q["text_en"] = _texts(key, fr, en, iso, month, lastd)
    q["question_id"] = question_id(q)
    return q


def record(q: Mapping[str, Any], mk: str, fc: B.Fc | None, origin: int, detail: Mapping[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": mk}
    if fc is None:
        return {**base, "status": "abstained", "reason": "no forecast for this country-month (not in the run / no fit)"}
    out: dict[str, Any] = {
        **base,
        "status": "issued",
        "origin_period": B.mlabel(origin),
        "horizon_periods": int(q["cycle_horizon"]),
        "detail": {**detail, "point_log1p": num(fc.point_l)},
    }
    if q["type"] == "binary":
        out["point"] = None
        out["prob_above"] = num(min(max(fc.prob, 0.0), 1.0))
        return out
    out["point"] = num(max(math.expm1(fc.point_l), 0.0))
    if fc.q is not None:
        s = np.sort(np.expm1(fc.q))
        out["predictive"] = {"family": "empirical", "sample": [num(float(v)) for v in s]}
        out["quantiles"] = {f"{lv:g}": num(float(np.quantile(s, lv))) for lv in QUANTILE_LEVELS}
    else:
        out["predictive"] = {"family": "point"}
    return out


__all__ = ["DATASET_KEY", "FAMILIES", "MODELS", "PROTOCOL_PATH", "family_doc", "question", "record"]
