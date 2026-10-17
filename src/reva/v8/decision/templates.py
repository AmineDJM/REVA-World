"""Templates of the decision composer: the five reference decisions, the instruments and the schema limits.

The composer validates a free decision against exactly what ``reva.predict.decision.Decision`` and the instrument
registry accept (patterns, limits, units, which geography an instrument needs), so an invalid decision is refused
before any request. The comparison presets are *variants* of reference decisions (one field changed), labelled as
such; they are inputs, never results.
"""

from __future__ import annotations

from typing import Any

from reva.predict import engine as pe
from reva.predict.decision import (
    ENTITY_ID_RE,
    ISO3_RE,
    MAX_ANCHORS,
    MAX_COUNTRIES,
    MAX_HORIZON_QUARTERS,
    Decision,
    instruments,
    reference_decisions,
)

NOT_ADVICE_FR = "Recherche, pas un conseil financier ni une recommandation."
#: Instruments whose first link is a market price (oil, rates): the studio repeats the not-advice notice there.
MARKET_INSTRUMENTS = frozenset({"oil_production_quota_change", "oil_price_assumption", "policy_rate_change"})

UNIT_LABEL_FR: dict[str, str] = {
    "mb/d": "mb/j",
    "million barrels per day": "millions de barils/jour",
    "kb/d": "kb/j",
    "thousand barrels per day": "milliers de barils/jour",
    "percent": "%",
    "%": "%",
    "share_of_exports_banned": "part des exportations interdite (1 = totale)",
    "share_of_imports_banned": "part des importations interdite (1 = totale)",
    "bp": "points de base",
    "basis points": "points de base",
    "percentage_points": "points de %",
    "pp": "points de %",
    "years": "ans",
    "months": "mois",
}


def _needs(ins: Any) -> dict[str, bool]:
    geos = {str(t["geo"]) for t in ins.templates}
    timings = {str(t.get("timing", "step")) for t in ins.templates}
    transforms = {str(t["transform"]) for t in ins.templates}
    return {
        "actor_geo": "ACTOR" in geos,
        "target_geo": "TARGET" in geos,
        "countries": "EACH" in geos,
        "occurrence": "occurrence" in timings,  # magnitude must be exactly 1 (complete measure)
        "level": "level" in transforms,  # target level (e.g. an age), not a change
    }


def summary_fr(d: Decision) -> str:
    ins = instruments().get(d.instrument)
    unit = UNIT_LABEL_FR.get(d.magnitude.unit, d.magnitude.unit)
    dur = "maintenue sur tout l'horizon" if d.duration_quarters is None else f"pendant {d.duration_quarters} trim."
    return (
        f"{d.actor.label_fr} · {ins.label_fr if ins else d.instrument} · {d.magnitude.value} {unit} · "
        f"dès {d.start}, {dur} · horizon {d.horizon_quarters} trim."
    )


def variant(d: Decision, **over: Any) -> Decision:
    """A variant of a reference decision with some fields changed (labelled; its id says it is a variant)."""
    doc = d.model_dump(mode="json")
    mag = over.pop("magnitude_value", None)
    if mag is not None:
        doc["magnitude"] = {**doc["magnitude"], "value": str(mag)}
    doc.update(over)
    return Decision.model_validate(doc)


def compare_presets() -> list[dict[str, Any]]:
    refs = reference_decisions()
    out: list[dict[str, Any]] = []
    ecb = refs.get("ecb_plus_50bp_2027q1")
    if ecb is not None:
        a = variant(
            ecb,
            id="ecb_plus_25bp_2027q1.variant",
            label_fr="La BCE relève ses taux directeurs de 25 points de base au 1er trimestre 2027 (variante de la "
            "décision de référence)",
            label_en=None,
            magnitude_value="25",
        )
        out.append(
            {
                "id": "ecb_25_vs_50",
                "label_fr": "BCE +25 pb contre +50 pb",
                "a": a.model_dump(mode="json"),
                "b_reference_id": ecb.id,
                "note_fr": "A = variante (+25 pb) de la décision de référence ; B = la référence (+50 pb).",
            }
        )
    opec = refs.get("opec_plus_cut_2mbd_2027q1")
    if opec is not None:
        a = variant(
            opec,
            id="opec_plus_cut_1mbd_2027q1.variant",
            label_fr="OPEP+ réduit sa production de 1 mb/j à partir du 1er trimestre 2027 (variante de la décision de "
            "référence)",
            label_en=None,
            magnitude_value="-1",
        )
        out.append(
            {
                "id": "opec_1_vs_2",
                "label_fr": "OPEP+ −1 mb/j contre −2 mb/j",
                "a": a.model_dump(mode="json"),
                "b_reference_id": opec.id,
                "note_fr": "A = variante (−1 mb/j) de la décision de référence ; B = la référence (−2 mb/j).",
            }
        )
    return out


def templates() -> dict[str, Any]:
    refs = reference_decisions()
    ins = instruments()
    return {
        "label": pe.LABEL_UNVALIDATED,
        "disclaimers_fr": list(pe.DISCLAIMERS_FR),
        "not_advice_fr": NOT_ADVICE_FR,
        "references": [
            {
                "id": d.id,
                "decision": d.model_dump(mode="json"),
                "summary_fr": summary_fr(d),
                "instrument_label_fr": ins[d.instrument].label_fr if d.instrument in ins else d.instrument,
                "market": d.instrument in MARKET_INSTRUMENTS,
            }
            for d in refs.values()
        ],
        "instruments": [
            {
                **i.as_json(),
                "needs": _needs(i),
                "unit_labels_fr": {u: UNIT_LABEL_FR.get(u, u) for u in sorted(i.magnitude_units)},
                "market": i.id in MARKET_INSTRUMENTS,
            }
            for i in ins.values()
        ],
        "schema": {
            "id_pattern": r"^[a-z0-9][a-z0-9_.-]{0,99}$",
            "start_pattern": r"^\d{4}Q[1-4]$",
            "iso3_pattern": ISO3_RE,
            "entity_id_pattern": ENTITY_ID_RE,
            "label_max": 300,
            "party_label_max": 200,
            "magnitude_pattern": r"^[+-]?(\d+(\.\d*)?|\.\d+)$",
            "horizon_min": 1,
            "horizon_max": MAX_HORIZON_QUARTERS,
            "duration_min": 1,
            "duration_max": 40,
            "countries_max": MAX_COUNTRIES,
            "anchors_max": MAX_ANCHORS,
            "draws_min": 100,
            "draws_max": 5000,
        },
        "compare_presets": compare_presets(),
    }
