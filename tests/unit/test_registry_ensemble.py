"""REVA ensemble in the forecast registry (V7 BEAT-ENSEMBLE): sealed weights from resolved scores only, labelled
equal weights while history is short, exact re-derivation, scoreboard cells."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from reva.evaluation.registry import ensemble as E
from reva.evaluation.registry import scores as S
from reva.models.forecasting import online_combination as OC

T0 = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)
FAM = "weo.gdp_growth"


def q(i: int, qtype: str = "quantity", end: str = "2027-01-01") -> dict[str, Any]:
    return {
        "question_id": f"fq_{i:024x}",
        "question_key": f"2026-Q4|{FAM}|G{i}|2026|{qtype}",
        "family": FAM,
        "type": qtype,
        "geo": f"G{i}",
        "reference_period": "2026",
        "frequency": "A",
        "period_start": "2026-01-01",
        "period_end": end,
        "interval_level": 0.8,
        "threshold": "2.0" if qtype == "binary" else None,
    }


def fc(qid: str, model: str, point: str | None, pred: dict[str, Any] | None = None, prob: str | None = None) -> dict:
    out: dict[str, Any] = {"question_id": qid, "model": model, "status": "issued"}
    if point is not None:
        out["point"] = point
        out["predictive"] = pred or {"family": "point"}
    if prob is not None:
        out["prob_above"] = prob
    return out


def original() -> dict[str, Any]:
    qs = [q(1), q(2, "interval"), q(3, "binary"), q(4, end="2026-01-01")]
    fcs = []
    for x in qs:
        qid = x["question_id"]
        fcs.append(
            fc(qid, "forecast.registry_persistence", "2.0", {"family": "normal", "mu": "2.0", "sigma": "1.0"}, "0.5")
        )
        fcs.append(
            fc(qid, "forecast.registry_ar", "3.0", {"family": "empirical", "sample": ["2.0", "3.0", "4.0"]}, "0.7")
        )
        if x["type"] == "quantity":
            fcs.append(fc(qid, "forecast.reference_imf_weo", "2.6"))
    fcs.append({"question_id": qs[0]["question_id"], "model": "forecast.registry_seasonal_naive", "status": "abstained",
                "reason": "annual"})  # fmt: skip
    return {
        "_sha256": "o" * 64, "origin": "test_fixture", "cycle": "2026-Q4", "issued_at": T0.isoformat(),
        "specs": {"version": 1}, "families": {FAM: {"dataset_key": "atlas.imf.weo"}},
        "data": {"tables": {}}, "questions": qs, "forecasts": fcs,
    }  # fmt: skip


def comps(m: dict[str, Any]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    return E.components_of(m["_sha256"], [m])


def history(n: int) -> list[dict[str, Any]]:
    """Resolved score records where AR is twice as good as persistence on absolute error (and WEO in between)."""
    out = []
    for i in range(n):
        qid = f"fq_h{i:022x}"
        for model, ae in (("forecast.registry_persistence", 2.0), ("forecast.registry_ar", 1.0),
                          ("forecast.reference_imf_weo", 1.5), (E.ENSEMBLE_KEY, 0.1)):  # fmt: skip
            out.append({"question_id": qid, "model": model, "family": FAM, "type": "quantity", "abs_error": str(ae),
                        "crps": None if model == "forecast.reference_imf_weo" else str(ae / 2),
                        "resolution_sha256": "r" * 64})  # fmt: skip
    return out


def test_equal_weights_labelled_when_no_resolution() -> None:
    m = original()
    b = E.build_ensemble_batch(m, comps(m), [], issued_at=T0, already=set())
    assert b is not None and b["supplement_of"] == "o" * 64 and b["label"] == E.ENSEMBLE_LABEL
    # the question whose period had ended is skipped with a reason, not forecast
    assert [x["question_id"] for x in b["generation"]["skipped"]] == [q(4)["question_id"]]
    assert len(b["forecasts"]) == 3
    tasks = b["ensemble"]["tasks"]
    assert {t["status"] for t in tasks.values()} == {OC.EQUAL}
    loc = tasks[f"{FAM}|quantity|location"]
    assert loc["components"] == ["forecast.reference_imf_weo", "forecast.registry_ar", "forecast.registry_persistence"]
    assert loc["history"] == [] and loc["n_resolved_questions"] == 0
    f1 = b["forecasts"][0]
    assert f1["model"] == E.ENSEMBLE_KEY and f1["status"] == "issued"
    assert float(f1["point"]) == pytest.approx((2.0 + 3.0 + 2.6) / 3)  # equal-weight mean of the three points (WEO included)
    assert f1["predictive"]["family"] == "empirical" and len(f1["predictive"]["sample"]) == 50
    assert f1["ensemble"]["weights"]["shape"] == {"forecast.registry_ar": "0.5", "forecast.registry_persistence": "0.5"}
    assert {c["model"] for c in f1["ensemble"]["components"]} == {
        "forecast.reference_imf_weo", "forecast.registry_ar", "forecast.registry_persistence"}  # fmt: skip
    f2 = b["forecasts"][1]
    lo, hi = (float(x) for x in f2["interval"])
    assert lo < float(f2["point"]) < hi
    f3 = b["forecasts"][2]
    assert f3["prob_above"] == "0.6" and "point" not in f3
    # resolution scores it with the registry's own scorer
    s = S.score_forecast(q(1), f1, "2.5")
    assert s["crps"] is not None and s["abs_error"] is not None


def test_online_weights_from_resolved_scores_only_and_reproducible() -> None:
    m = original()
    hist = history(30)
    b = E.build_ensemble_batch(m, comps(m), hist, issued_at=T0, already=set())
    assert b is not None
    loc = b["ensemble"]["tasks"][f"{FAM}|quantity|location"]
    assert loc["status"] == OC.ONLINE and loc["n_resolved_questions"] == 30
    w = {k: float(v) for k, v in loc["weights"].items()}
    assert w["forecast.registry_ar"] > w["forecast.reference_imf_weo"] > w["forecast.registry_persistence"]
    assert all(r[1] != E.ENSEMBLE_KEY for r in loc["history"])  # its own scores never set its weights
    assert len(loc["history"]) == 90 and loc["resolution_sha256s"] == ["r" * 64]
    shape = b["ensemble"]["tasks"][f"{FAM}|quantity|shape"]
    assert shape["components"] == ["forecast.registry_ar", "forecast.registry_persistence"]  # point-only excluded
    # interval and binary tasks have no history in this family/type: equal, labelled
    assert b["ensemble"]["tasks"][f"{FAM}|binary|probability"]["status"] == OC.EQUAL
    # exact re-derivation from the recorded weights + component batches, and of the weights from the recorded rows
    assert E.rebuild_forecasts(b, comps(m)) == b["forecasts"]
    assert E.recheck_weights(b)
    bad = copy.deepcopy(b)
    bad["ensemble"]["tasks"][f"{FAM}|quantity|location"]["history"][0][2] = "999"
    assert not E.recheck_weights(bad)
    # already-ensembled questions are not issued twice
    done = {x["question_id"] for x in b["questions"]}
    assert E.build_ensemble_batch(m, comps(m), hist, issued_at=T0, already=done) is None


def test_scoreboard_cells_compare_with_the_best_component_only_on_common_questions() -> None:
    ens: dict[str, dict[str, Any]] = {}
    scores: dict[tuple[str, str], dict[str, Any]] = {}
    for i in range(40):
        qid = f"fq_{i:024x}"
        ens[qid] = {"family": FAM, "type": "quantity", "geo": f"G{i % 8}", "reference_period": str(2026 + i % 5),
                    "components": ["a", "b", "w"]}  # fmt: skip
        noise = 0.1 * ((i * 7) % 5)
        scores[(qid, E.ENSEMBLE_KEY)] = {"crps": 1.0 + noise, "abs_error": 1.4 + noise}
        scores[(qid, "a")] = {"crps": 1.2 + noise, "abs_error": 1.5 + noise}
        scores[(qid, "b")] = {"crps": 2.0 + noise, "abs_error": 2.5 + noise}
        if i < 10:  # point-only official forecast scored on few questions: not eligible as "best"
            scores[(qid, "w")] = {"crps": None, "abs_error": 0.5}
    cells = {c["metric"]: c for c in E._cells(ens, scores)}
    crps = cells["crps"]
    assert crps["status"] == "tested" and crps["best_single"]["model"] == "a"
    assert [c["model"] for c in crps["competitors"]] == ["a", "b"]  # w has no CRPS (point only, QA-PRED X2)
    assert crps["test"]["mean_diff"] < 0 and crps["test"]["n"] == 40
    ae = cells["abs_error"]
    assert ae["best_single"]["model"] == "a" and {c["model"] for c in ae["competitors"]} == {"a", "b", "w"}
    few = {k: v for k, v in list(ens.items())[:5]}
    assert {c["status"] for c in E._cells(few, scores)} == {"awaiting_resolutions"}
