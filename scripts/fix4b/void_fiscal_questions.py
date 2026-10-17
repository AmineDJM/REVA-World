"""FIX4B (audit AUDIT3 M7): close the 5 sealed WEO questions worded as calendar years whose resolution cell is a
fiscal-year value, as ``void`` with a reason (append-only: ``reva.evaluation.registry.service.void_questions``;
the sealed manifests are never edited or re-sealed). Usage:
``flock /tmp/reva-devdb.lock uv run python scripts/fix4b/void_fiscal_questions.py [--write]``.
"""

from __future__ import annotations

import json
import sys

from reva.adapters.objects.factory import get_store
from reva.adapters.postgres.engine import make_engine
from reva.evaluation.ledger import load_frozen_manifest
from reva.evaluation.registry import service as svc

QIDS = (
    "fq_ce2ce06b59d841b5091a9d62",  # IND CPI 2026
    "fq_2cd61181ed8f5ad5b27bad82",  # IND CPI 2027
    "fq_c1f49d578e1d3333dacee22e",  # EGY CPI 2027
    "fq_7f4930b1540b2285e6aae2a0",  # EGY unemployment 2026
    "fq_bd1b7d6205294f4f2ab21f02",  # EGY unemployment 2027
)
REPORTING = {"IND": "April/March", "EGY": "July/June"}


def main(write: bool) -> int:
    eng, store = make_engine("worker"), get_store()
    with eng.connect() as c:
        batches = svc._batches(c, "live")
    voids: dict[str, dict[str, object]] = {}
    for b in batches:
        m = load_frozen_manifest(store, b["sha"])
        for q in m["questions"]:
            if q["question_id"] not in QIDS:
                continue
            geo = q.get("geo") or q.get("iso3")
            print(
                b["sha"][:12],
                json.dumps({k: q.get(k) for k in ("question_id", "family", "geo", "reference_period", "type")}),
            )
            voids[q["question_id"]] = {
                "reason": "fiscal_year_basis_mismatch",
                "detail": (
                    f"The question is worded for calendar year {q.get('reference_period')} ({q.get('family')}, {geo}); the IMF WEO "
                    f"series it resolves on is on a fiscal-year basis for {geo} (LATEST_ACTUAL_ANNUAL_DATA 'FY…', "
                    f"reporting year {REPORTING.get(str(geo), 'non-calendar')}), so the value published under that "
                    "year label is a fiscal-year value. It cannot answer the question as worded; void by registry "
                    "rule (audit AUDIT3 M7, agent FIX4B), never scored."
                ),
                "audit": "docs/dev/review_v4_v5_audit.md M7",
            }
    missing = sorted(set(QIDS) - set(voids))
    if missing:
        print("not found:", missing)
        return 1
    if write:
        print(json.dumps(svc.void_questions(eng, store, voids, origin="live"), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main("--write" in sys.argv))
