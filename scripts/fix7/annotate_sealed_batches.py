"""FIX7 (QA-PRED review V6): append annotations to three sealed registry batches (never edits them).

* PRED-ENERGY ``6d76937c…``: its ``forecast.energy_combo_steo_rw`` forecasts were issued under the per-variable
  Holm rule; the combination is EIA-derived and worse than STEO alone on points -> excluded from future issuance.
* PRED-CONFLICT ``ef57141a…``: its 2026 country-year questions were sealed on 2026-10-17 (~80 % of the year
  elapsed) -> near-nowcasts, scored separately from 1-year-ahead forecasts.
* PRED-HEALTH ``0ab8fcc2…``: FluNet / WHO GHE candidates were admitted on final-vintage back-tests.

Usage: ``flock /tmp/reva-devdb.lock uv run python scripts/fix7/annotate_sealed_batches.py [--write]``
(dry run by default: prints the annotations and counts).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from typing import Any

ENERGY = "6d76937c0e6d123a7f56cd0454c681d57f7ead3c56bac7540095b3d9d3802164"
CONFLICT = "ef57141ac8e02d9f3e6e9535577ee8a20634cdcad8acc501cc4a0d7cccb2c937"
HEALTH = "0ab8fcc25d7710d21aef988d138e902f1d1117be948a47e4337f806cbecfd383"
REVIEW = "docs/dev/review_v6_predictions.md"


def build(store: Any) -> dict[str, dict[str, Any]]:
    from reva.evaluation.ledger import load_frozen_manifest
    from reva.evaluation.registry.questions import issue_timing

    out: dict[str, dict[str, Any]] = {}
    m = load_frozen_manifest(store, ENERGY)
    combo = [f for f in m["forecasts"] if f["model"] == "forecast.energy_combo_steo_rw" and f["status"] == "issued"]
    out[ENERGY] = {
        "kind": "eia_derived_winner_excluded",
        "source": REVIEW + " (QA-PRED X3, §3.D, §6)",
        "note": "The forecast.energy_combo_steo_rw forecasts of this batch were issued as 'winners vs the random walk' "
        "under a per-variable Holm family. The combination is half EIA's own STEO projection and is worse than STEO "
        "alone on point loss (U.S. crude h4 MAE 0.445 vs 0.337); under one Holm family over the run only 4 "
        "(series, horizon) winners remain, none of them this combination. These forecasts stay sealed and will be "
        "scored, but they are EIA-derived and are not evidence of REVA skill; the model is excluded from future "
        "issuance (energy_models.NOT_ISSUABLE, FIX7).",
        "model": "forecast.energy_combo_steo_rw",
        "n_issued_in_batch": len(combo),
        "question_ids": sorted({f["question_id"] for f in combo}),
    }
    m = load_frozen_manifest(store, CONFLICT)
    issued_on = date.fromisoformat(m["issued_at"][:10])
    kinds: Counter[str] = Counter()
    near = []
    for q in m["questions"]:
        t = issue_timing(date.fromisoformat(q["period_start"]), date.fromisoformat(q["period_end"]), issued_on)
        kinds[t["kind"]] += 1
        if t["kind"] in ("near_nowcast", "nowcast"):
            near.append(q["question_id"])
    out[CONFLICT] = {
        "kind": "near_nowcast_questions",
        "source": REVIEW + " (§3.F, §7)",
        "note": f"Questions on the current year {issued_on.year} were sealed on {issued_on.isoformat()} with about "
        "80 % of the year elapsed: they are near-nowcasts, not 1-year-ahead forecasts (the back-test's h = 1 assumed "
        "an end-of-year origin). Score and report them separately from the forecasts of later years. New conflict "
        "questions carry this in their own metadata (question.timing, FIX7). Inputs note: the KG candidate's V-Dem "
        "v16 / current WDI covariates are final-vintage (revised back in time); baselines use UCDP history only.",
        "timing_counts": dict(kinds),
        "elapsed_fraction_at_issue": issue_timing(date(issued_on.year, 1, 1), date(issued_on.year + 1, 1, 1),
                                                  issued_on)["elapsed_fraction_at_issue"],  # fmt: skip
        "question_ids": sorted(near),
    }
    m = load_frozen_manifest(store, HEALTH)
    fams = {k: v for k, v in (m.get("families") or {}).items() if v.get("winners")}
    cand = Counter(f["model"] for f in m["forecasts"] if f["status"] == "issued" and any(
        f["model"] in v["winners"] for v in fams.values()))  # fmt: skip
    out[HEALTH] = {
        "kind": "final_vintage_admission",
        "source": REVIEW + " (§7, X9)",
        "note": "The candidates issued in this batch (FluNet blend, WHO GHE log-drift / blend) were admitted on "
        "FINAL-VINTAGE back-tests (revised / backfilled data; GHE = modelled estimates WHO re-estimates in every "
        "release, 11 annual origins). Their scores are final-vintage-admitted results, not evidence of real-time "
        "skill; GHE questions forecast the next WHO GHE release (a model estimate), not registered deaths. New "
        "health batches carry this in question.candidate_admission (FIX7).",
        "families": {k: {"winners": v["winners"], "backtest": (v.get("backtest") or {}).get("kind")} for k, v in fams.items()},
        "issued_candidate_forecasts": dict(cand),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.registry.service import annotate_batch
    from reva.settings import get_settings

    store = get_store()
    ann = build(store)
    res = {}
    for sha, doc in ann.items():
        summary = {k: v for k, v in doc.items() if k != "question_ids"} | {"n_question_ids": len(doc.get("question_ids") or [])}
        res[sha] = summary
        if a.write:
            res[sha]["annotation_sha256"] = annotate_batch(make_engine("worker", get_settings()), store, sha, doc,
                                                           actor="agent:fix7")  # fmt: skip
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
