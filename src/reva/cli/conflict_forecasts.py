"""CLI of the conflict-risk forecasts (V6, PRED-CONFLICT): ``python -m reva.cli.conflict_forecasts backtest|issue``.

Kept out of :mod:`reva.evaluation.conflict` so the evaluation package never reaches the object-store factory
(invariant 11). Writes go through worker credentials; run under ``flock /tmp/reva-devdb.lock`` on the dev DB.
"""

from __future__ import annotations

import json
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation.conflict.service import issue_batch, record_backtest
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="conflict-risk forecasts (PRED-CONFLICT)")
    ap.add_argument("action", choices=("backtest", "issue"))
    ap.add_argument("--origin", default="live")
    a = ap.parse_args(argv)
    eng = make_engine("worker", get_settings())
    fn = record_backtest if a.action == "backtest" else issue_batch
    print(json.dumps(fn(eng, get_store(), origin=a.origin), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
