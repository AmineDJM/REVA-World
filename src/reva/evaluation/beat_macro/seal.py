"""Prospective issuance of the confirmed BEAT-MACRO scope(s) as supplementary sealed registry batches.

Only scopes confirmed on the holdout (``holdout_ledger.yaml`` -> ``confirmed``) issue, and only on registry
question families asking exactly that variable for the current year:

* ``g7rt | unemployment_rate | cy | forecast.bm_stack`` -> family ``weo.unemployment``, cycle horizon 0, the G7
  questions of the current cycle.

The forecast is computed exactly as in the evaluation (``cases.g7_cases`` + ``combine.add_second_pass``) with the
information published at ``issued_at``: the OECD MEI edition in force and the IMF WEO vintage in force (vintage
accessor), and the past errors of the same bucket whose outcome was published by ``issued_at``. **Bucket**: the
information state at issue (a fresh October WEO, the MEI August edition) is that of the 1 November origins, so the
case is placed in the November bucket; the module asserts that no outcome was published between ``issued_at`` and
1 November (so the errors used are identical). The forecast **uses the IMF WEO projection as an input**
(attributed); its claim is worded accordingly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reva.evaluation.beat_macro import cases as C
from reva.evaluation.beat_macro import combine as K
from reva.evaluation.beat_macro import service as S
from reva.models.forecasting.registry_baselines import Abstain, Predictive

#: confirmed scope key -> (registry family, cycle horizon, model key)
SCOPES: dict[str, tuple[str, int, str]] = {
    "g7rt|unemployment_rate|cy|forecast.bm_stack|benchmark.imf_weo|ae": ("weo.unemployment", 0, K.key("stack")),
}
CLAIM = (
    "REVA combination of the IMF WEO unemployment projection with REVA's nowcast from the OECD MEI real-time monthly "
    "unemployment rates (weight from past errors, shrunk toward 1/2) - USES THE IMF FORECAST AS AN INPUT. Holdout "
    "(2019-2025, 70 between-release monthly origins, G7): AE x0.63 vs the WEO in force on the same date, Holm p "
    "0.0044 (family of 7). Its density under-covered on the holdout (80 % interval covered 56 %): treat the interval "
    "as too narrow."
)


def confirmed_scopes() -> list[str]:
    led = S.load_yaml(S.REGISTRY_DIR / "holdout_ledger.yaml")
    out: list[str] = []
    for o in led.get("openings") or []:
        out += [k for k in o.get("confirmed") or [] if k in SCOPES]
    return sorted(set(out))


def issue_time_cases(inp: S.Inputs, issued_at: datetime) -> list[Any]:
    """G7 cases at ``issued_at`` placed in the November bucket, preceded by every dev/holdout origin (whose
    errors feed the second pass); returns only the issue-time cases."""
    nov = datetime(issued_at.year, 11, 1, tzinfo=UTC)
    if not (issued_at.month == 10 and issued_at < nov):
        raise ValueError("this issuance maps the October-WEO state to the November bucket: issue in October")
    past = S.origins("g7rt", "holdout", last=datetime(issued_at.year - 1, 12, 1).date())
    hist = C.g7_cases(inp.cells, inp.oil, past)
    late = [c for c in hist if c.outcome_known_at is not None and issued_at < c.outcome_known_at <= nov]
    if late:
        raise RuntimeError("an outcome was published between issue and 1 November: the bucket mapping is invalid")
    now = [c for c in C.g7_cases(inp.cells, inp.oil, [issued_at]) if c.variable == "unemployment_rate"]
    for c in now:
        c.info["issued_at"] = issued_at.isoformat()
        c.info["bucket_origin"] = nov.isoformat()
        c.origin = nov
    K.add_second_pass([*hist, *now], S.RULES["g7rt"])
    return now


def supplement(original: Mapping[str, Any], now_cases: Sequence[Any], *, issued_at: datetime,
               model_key: str, family: str, horizon: int) -> dict[str, Any] | None:  # fmt: skip
    """Pure: the supplementary batch for one original registry batch (same questions and pins)."""
    from reva.evaluation.registry.forecasts import _record, abstention
    from reva.evaluation.registry.service import BATCH_SCHEMA, LABEL

    by_geo = {c.geo: c for c in now_cases}
    questions: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    for q in original["questions"]:
        if q["family"] != family or int(q.get("cycle_horizon", -1)) != horizon or q["type"] != "quantity":
            continue
        geo = str(q.get("iso3") or q["geo"])
        if geo not in C.G7:
            continue
        c = by_geo.get(geo)
        p = c.preds.get(model_key) if c is not None else None
        if c is None or c.target != str(q["reference_period"]):
            rec = abstention(q, model_key, "no issue-time case for this country / target year")
        elif not isinstance(p, Predictive):
            rec = abstention(q, model_key, p.reason if isinstance(p, Abstain) else "no stack (missing input)")
        else:
            rec = _record(q, model_key, p, ())
            w = c.preds[C.WEO]
            rec["inputs"] = {
                "imf_weo_vintage_in_force": c.info.get("vintage_in_force"),
                "imf_weo_projection": w.point,
                "reva_nowcast_bridge": c.info["raw"].get("bridge"),
                "bridge_detail": c.info.get("bridge"),
                "stack_lambda": c.info.get("stack_lambda"),
                "oecd_mei_state": c.info.get("mei_state"),
                "bucket_origin": c.info.get("bucket_origin"),
            }
            rec["attribution"] = (
                "combination input: International Monetary Fund, World Economic Outlook (projection, vintage in force "
                "at issue); OECD MEI real-time editions (unemployment rate)"
            )
            rec["claim"] = CLAIM
        forecasts.append(rec)
        questions.append(q)
    if not forecasts:
        return None
    return {
        "schema": BATCH_SCHEMA,
        "label": LABEL,
        "origin": original["origin"],
        "cycle": original["cycle"],
        "issued_at": issued_at.isoformat(),
        "replay_mode": "operational_as_known",
        "supplement_of": original.get("_sha256"),
        "statement": "Supplementary sealed batch (agent BEAT-MACRO): forecasts of a scope confirmed on the "
        "pre-registered BEAT-MACRO holdout (registry/forecasts/beat_macro/holdout_ledger.yaml), on the same questions "
        "and pinned table versions as the original batch, frozen at issued_at.",
        "specs": original["specs"],
        "families": {k: v for k, v in original["families"].items() if k == family},
        "models": [{"model_key": model_key, "version": S.MODEL_VERSION, "description": CLAIM}],
        "data": original["data"],
        "evidence": {
            "protocol": "registry/forecasts/beat_macro/protocol.yaml",
            "hypotheses": "registry/forecasts/beat_macro/hypotheses.yaml",
            "holdout": "registry/forecasts/beat_macro/holdout.json",
            "code_sha256": S.code_sha256(),
            "confirmed_scopes": confirmed_scopes(),
            "resolution_note": "the registry resolves on the first WEO vintage publishing the year as an actual "
            "(normally April of the next year); the BEAT-MACRO holdout used the first vintage on/after 1 October "
            "of the next year",
        },
        "code_digest": original.get("code_digest"),
        "questions": questions,
        "forecasts": forecasts,
        "generation": {"skipped": [], "pending_families": [], "notes": []},
    }


__all__ = ["CLAIM", "SCOPES", "confirmed_scopes", "issue_time_cases", "supplement"]
