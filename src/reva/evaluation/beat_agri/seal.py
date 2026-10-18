"""BEAT-AGRI prospective batch for marketing year 2026/27 (pure: builds the sealed manifest; no DB, no store).

One quantity question per (series, marketing year) inside the series' pre-registered window on the WASDE release
in force at sealing; truth rule declared in the question (``service.truth_rule_text``). Forecasts: the WASDE point
(attributed reference), the fair WASDE fan (benchmark) and REVA's bc / rev / wx (point + Gaussian density), each
labelled with its holdout status. The database entry point is ``reva.cli.beat_agri seal`` (sealed like a registry
batch: content-addressed manifest + ``model.evaluation`` rows, ``metrics.kind = registry_issue``, task scope kind
``beat_agri`` so the generic registry resolver leaves it to ``reva.cli.beat_agri resolve``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from reva.evaluation.beat_agri import cases as C
from reva.evaluation.beat_agri import service as S

SCOPE_KIND = "beat_agri"
BATCH_SCHEMA = "reva-forecast-registry-batch/1"
FORECAST_SCHEMA = "reva-beat-agri-forecast/1"
MODELS: dict[str, tuple[str, str]] = {
    "forecast.reference_usda_wasde": ("1.0.0", "wasde"),
    "forecast.wasde_fair_fan": ("1.0.0", "wasde_fan"),
    "forecast.beat_agri_bc": ("1.0.0", "bc"),
    "forecast.beat_agri_rev": ("1.0.0", "rev"),
    "forecast.beat_agri_wx": ("1.0.0", "wx"),
}
#: cases.compute_candidates also fills a wx point for other corn/soybean series (it keys on the crop only); those
#: are outside every pre-registered wx cell (never scored) and are not issued here.
WX_SERIES = ("us.corn.yield", "us.corn.production", "us.soybeans.yield", "us.soybeans.production")
QUANTILES = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)


def _q(z: float) -> float:
    from statistics import NormalDist

    return NormalDist().inv_cdf(z)


def question(rec: Mapping[str, Any]) -> dict[str, Any]:
    my = int(rec["marketing_year"])
    doc = {
        "family": f"beat_agri.{rec['series']}",
        "series": rec["series"],
        "type": "quantity",
        "frequency": "A",
        "reference_period": f"{my}/{(my + 1) % 100:02d}",
        "marketing_year_first_year": my,
        "unit": "as WASDE prints it (see atlas.usda_wasde_esmis unit)",
        "truth_rule": rec["truth_rule"],
        "resolve_from": f"{my + 1}-{C.TRUTH_MONTH[rec['series']]:02d}-01",
        "origin_release": rec["release"],
        "report_month_offset_k": rec["k"],
        "window": rec["window"],
        "question_key": f"beat_agri|{rec['series']}|{my}|{rec['release']}",
    }
    doc["question_id"] = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
    return doc


def _forecast(q: Mapping[str, Any], model: str, point: float | None, sd: float | None, label: str) -> dict[str, Any]:
    if point is None:
        return {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": model, "status": "abstained",
                "reason": "the pre-registered rule issues no forecast here (too few past years / no usable weather)"}  # fmt: skip
    out: dict[str, Any] = {"schema": FORECAST_SCHEMA, "question_id": q["question_id"], "model": model,
                           "status": "issued", "point": f"{point:.10g}", "label": label}  # fmt: skip
    if sd is not None and sd > 0:
        out["predictive"] = {"kind": "gaussian", "mean": f"{point:.10g}", "sd": f"{sd:.10g}"}
        out["quantiles"] = {f"{z:g}": f"{point + _q(z) * sd:.10g}" for z in QUANTILES}
        out["interval80"] = [f"{point - C.Z80 * sd:.10g}", f"{point + C.Z80 * sd:.10g}"]
    return out


def build_batch(
    prosp: Mapping[str, Any], *, now: datetime, confirmed: Sequence[str], pins: Mapping[str, Any]
) -> dict[str, Any]:
    qs: list[dict[str, Any]] = []
    fs: list[dict[str, Any]] = []
    for rec in prosp["records"]:
        q = question(rec)
        qs.append(q)
        for mk, (_v, m) in MODELS.items():
            if m == "wasde":
                f = _forecast(q, mk, rec["wasde_point"], None, "USDA WASDE projection (attributed reference)")
                f["attribution"] = f"USDA, World Agricultural Supply and Demand Estimates, release {rec['release']}"
            elif m == "wasde_fan":
                f = _forecast(q, mk, rec["wasde_point"], rec["sds"].get("wasde"),
                              "BENCHMARK: N(WASDE point, RMS of WASDE's own past relative errors at this series and k)")  # fmt: skip
            else:
                keys = [k for k in confirmed if k.split("|")[2] == m and q["series"] in C.GROUPS[k.split("|")[0]]
                        and k.split("|")[1] == rec["window"]]  # fmt: skip
                status = ("confirmed on the holdout: " + ", ".join(keys)) if keys else "NOT confirmed on the holdout"
                point, sd = rec["points"].get(m), rec["sds"].get(m)
                if m == "wx" and q["series"] not in WX_SERIES:
                    point = sd = None  # protocol: wx is defined for U.S. corn/soybeans yield and production only
                f = _forecast(q, mk, point, sd,
                              f"REVA correction of the WASDE projection (uses USDA's number); {status}")  # fmt: skip
            fs.append(f)
    return {
        "schema": BATCH_SCHEMA,
        "label": f"V7 BEAT-AGRI marketing year {S.PROSPECTIVE_MY}/{(S.PROSPECTIVE_MY + 1) % 100:02d} on WASDE "
        f"{prosp['release_in_force']}",
        "origin": "live",
        "cycle": f"beat_agri:{prosp['release_in_force']}",
        "issued_at": now.isoformat(),
        "replay_mode": "operational_as_known",
        "statement": "Sealed prospective forecasts of USDA's later estimate (truth rule in each question) for the "
        "2026/27 marketing year on the WASDE release in force; protocol registry/forecasts/beat_agri/protocol.yaml.",
        "protocol": "registry/forecasts/beat_agri/protocol.yaml",
        "code_sha256": S.code_sha256(),
        "data": dict(pins),
        "models": [{"model_key": k, "version": v} for k, (v, _m) in MODELS.items()],
        "questions": qs,
        "forecasts": fs,
    }


__all__ = ["MODELS", "SCOPE_KIND", "build_batch", "question"]
