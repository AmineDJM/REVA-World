"""Run decisions against a database, read-only (CLI and API share this entry point).

``uv run python -m reva.predict.run --reference all --out <file.json>`` runs the reference decisions on the
configured database with the ``reva_sim`` role inside a READ ONLY transaction (nothing is ever written).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from sqlalchemy import Connection, text

from reva.predict import db as pdb
from reva.predict.decision import Decision, reference_decisions
from reva.predict.engine import DEFAULT_DRAWS, predict


def run_on_connection(
    conn: Connection,
    store: Any,
    decision: Decision,
    *,
    draws: int = DEFAULT_DRAWS,
    seed: int = 0,
    as_of_max: str | None = None,
    origin: str = "any",
    with_io: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    effects = pdb.DbEffectSource(conn, origin=origin, as_of_max=as_of_max)
    claims = pdb.DbClaimSource(conn, origin=None if origin == "any" else origin)
    baseline, baseline_gap = pdb.atlas_baseline(conn, store, effects)
    data = pdb.plugin_data(conn, store, decision, with_io=with_io)
    from reva.predict.context import NoBaseline

    out = predict(
        decision,
        effects,
        claims,
        baseline if baseline is not None else NoBaseline(),
        data,
        draws=draws,
        seed=seed,
        as_of_max=as_of_max,
    )
    if baseline_gap:
        out["gaps"].append({"code": "baseline_unavailable", "message": baseline_gap})
    out["provenance"] = {
        "database_read_only": True,
        "release": data.crude_provenance or None,
        "io": data.io_provenance or None,
        "atlas_snapshot": getattr(baseline, "snapshot", None),
        "compute_seconds": round(time.perf_counter() - t0, 3),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference", default="all", help="reference decision id or 'all'")
    ap.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="-")
    args = ap.parse_args(argv)
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    settings = get_settings()
    store = build_store(settings)
    refs = reference_decisions()
    ids = sorted(refs) if args.reference == "all" else [args.reference]
    results = {}
    eng = make_engine("sim", settings)
    for i in ids:
        with eng.connect() as conn, conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            results[i] = run_on_connection(conn, store, refs[i], draws=args.draws, seed=args.seed)
    doc = json.dumps(results, indent=1, ensure_ascii=False, default=str)
    if args.out == "-":
        sys.stdout.write(doc + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(doc + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
