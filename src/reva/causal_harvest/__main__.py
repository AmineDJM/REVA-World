"""CLI: ``python -m reva.causal_harvest {seeds,run,status,report}`` (see docs/dev/v3_causwd.md)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from reva.adapters.connectors.wikidata import REPO_ROOT
from reva.causal_harvest import live
from reva.operations.budgets import BudgetExhausted

REPORT_PATH = REPO_ROOT / "var" / "kg" / "reports" / "causwd_report.json"


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reva.causal_harvest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seeds", help="seed counts from recorded snapshots and specs (no network, no DB)")
    sub.add_parser("status", help="harvest state summary")
    r = sub.add_parser("run", help="one budgeted session (snapshots, discovery, hourly WDQS attempt)")
    r.add_argument("--db", default=live.DEFAULT_DB)
    r.add_argument("--ledger-db", default=live.LEDGER_DB)
    r.add_argument("--cap-wbapi", type=int, default=None, help="upper bound; 30%% of today's remaining always applies")
    r.add_argument("--cap-wdqs", type=int, default=0)
    r.add_argument("--discover", default="", help="pid=pages,... CirrusSearch discovery pages (500 hits each)")
    r.add_argument("--minutes", type=float, default=None)
    r.add_argument("--max-entities", type=int, default=live.MAX_ENTITIES)
    rp = sub.add_parser("report", help="consistency report over the loaded causal statements")
    rp.add_argument("--db", default=live.DEFAULT_DB)
    rp.add_argument("--chains", type=int, default=12)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.cmd == "seeds":
        st = live.State(live.STATE_PATH.with_name("causal.seeds_dryrun.json"))
        _print(live.initial_seeds(st, kg_engine=None) | {"frontier": len(st.frontier)})
        return 0
    if args.cmd == "status":
        st = live.State()
        fr = st.frontier
        _print(
            {
                "fetched": len(st.fetched),
                "frontier": len(fr),
                "missing": len(st.data["missing"]),
                "frontier_by_tier": {
                    t: sum(1 for v in fr.values() if v["tier"] == t) for t in sorted({v["tier"] for v in fr.values()})
                },
                "causal_statements_seen": sum(int(v.get("causal", 0)) for v in st.fetched.values()),
                "discovery": {
                    k: {x: v.get(x) for x in ("pages", "hits", "total", "query")}
                    for k, v in st.data["discovery"].items()
                },
                "wdqs": {
                    k: {x: v.get(x) for x in ("status", "last_error")} | {"chunks": len(v.get("chunks", {}))}
                    for k, v in st.data["wdqs"].items()
                },
                "wdqs_last_attempt": st.data.get("wdqs_last_attempt"),
                "sessions": st.data["sessions"][-5:],
            }
        )
        return 0
    if args.cmd == "run":
        pages = {}
        for part in filter(None, args.discover.split(",")):
            k, _, v = part.partition("=")
            pages[k.strip()] = int(v or 1)
        until = time.time() + args.minutes * 60 if args.minutes else None
        try:
            out = asyncio.run(
                live.run(
                    db=args.db,
                    ledger_db=args.ledger_db,
                    cap_wbapi=args.cap_wbapi,
                    cap_wdqs=args.cap_wdqs,
                    discover_pages=pages,
                    until=until,
                    max_entities=args.max_entities,
                )
            )
        except BudgetExhausted as e:
            _print({"status": "budget_exhausted", "message": e.message, **e.details})
            return 3
        _print(out)
        return 0
    if args.cmd == "report":
        from reva.adapters.postgres.engine import make_engine
        from reva.causal_harvest.report import analyse, chain_exhibits, load_graph
        from reva.kg.harvest import harvest_settings

        eng = make_engine("worker", harvest_settings(args.db))
        try:
            g = load_graph(eng)
        finally:
            eng.dispose()
        a = analyse(g)
        a["chains"] = chain_exhibits(a, g, limit=args.chains)
        a["generated_at"] = datetime.now(UTC).isoformat()
        a.pop("_succ")
        edges = a.pop("_edges")
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(a | {"edges_sample": edges[:200]}, indent=1, ensure_ascii=False, default=str))
        _print(
            {k: v for k, v in a.items() if k not in ("chains", "cycles", "inconsistent")}
            | {"cycles": len(a["cycles"]), "inconsistent": len(a["inconsistent"]), "report": str(REPORT_PATH)}
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
