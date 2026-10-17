"""CLI of the finance back-test (V6, PRED-FIN): ``python -m reva.cli.fin_backtest --out result.json [--record]``.

Kept out of :mod:`reva.evaluation.finance_backtest` so the evaluation package never reaches the object-store factory
(invariant 11). The computation is read-only; ``--record`` then takes the dev-database write lock
(``/tmp/reva-devdb.lock``, same file as ``flock``) only for the short write of the manifest and evaluation rows.
Research evaluation only: not investment advice, not a trading signal.
"""

from __future__ import annotations

import json
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import fcntl

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.evaluation import finance_backtest as B
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="Finance back-test (PRED-FIN)")
    ap.add_argument("--origin", default="live")
    ap.add_argument("--start-year", type=int, default=2000)
    ap.add_argument("--family", action="append", default=[], help="limit to these families (repeatable)")
    ap.add_argument("--out", required=True, help="JSON result path")
    ap.add_argument("--record", action="store_true", help="store the manifest and evaluation rows (worker)")
    ap.add_argument("--lock", default="/tmp/reva-devdb.lock")  # noqa: S108  (the shared dev-DB lock file)
    a = ap.parse_args(argv)
    eng = make_engine("worker", get_settings())
    store = get_store()
    m = B.compute(eng, store, origin=a.origin, start_year=a.start_year, families=a.family or None)
    if a.record:
        with open(a.lock, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            m = B.record(eng, store, m, origin=a.origin)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1, default=str)
    print(json.dumps({"backtest_sha256": m.get("backtest_sha256"), "level_replays": m["n_level_replays"],
                      "meeting_replays": m["n_meeting_replays"], "winners": m["winners"]}, indent=1))  # fmt: skip
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
