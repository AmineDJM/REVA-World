"""Command line for the causal-candidate space (agent CAUSCAND).

``uv run python -m reva.causal_candidates <command>``:

* ``seeds --out seeds.json [--limit 500]`` -- one budgeted WDQS query (event items with most sitelinks).
* ``harvest --seeds seeds.json --top 250 --out DIR [--use-requested-budget]`` -- fetch the seeds' EN + FR
  articles (budget-metered, resumable), keep prefiltered sentences, extract, resolve argument links,
  write ``DIR/candidates.jsonl`` + ``DIR/report.json``.
* ``relink --out DIR [--offline] [--use-requested-budget]`` -- re-run the (re-tuned) extractor on the
  stored sentences; resolve only link targets not yet cached (live, budgeted) unless ``--offline``.
* ``sample --out DIR --n 50 --seed 20261016`` -- a reproducible random sample for hand evaluation.

Live commands write the budget ledger into a throwaway database (``--db``, default ``reva_causcand``;
the dev database ``reva`` is refused).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reva.causal_candidates import extract as X
from reva.causal_candidates import fetch as F


def articles_for(seeds_path: Path, top: int) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    from reva.causal_candidates.seeds import load_seeds, top_seeds

    doc = load_seeds(seeds_path)
    chosen = top_seeds(doc["seeds"], top)
    arts = [(lang, t) for s in chosen for lang, t in (("en", s.enwiki), ("fr", s.frwiki)) if t]
    return arts, [{"qid": s.qid, "sitelinks": s.sitelinks, "enwiki": s.enwiki, "frwiki": s.frwiki} for s in chosen]


def build(out: Path, *, linked_at: str | None = None) -> dict[str, Any]:
    """Extract from stored sentences + link with cached resolutions -> candidates.jsonl, report.json."""
    recs = F.load_records(out)
    cands, misses = X.extract(recs)
    res = F.load_resolutions(out / "resolutions.jsonl")
    docs, dropped = X.link_candidates(cands, res, linked_at=linked_at or datetime.now(UTC).isoformat())
    with (out / "candidates.jsonl").open("w") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    report = {
        "extractor": {"id": X.EXTRACTOR_ID, "version": X.EXTRACTOR_VERSION},
        "sentences_stored": len(recs),
        "sentences_by_lang": dict(Counter(r.lang for r in recs)),
        "raw_candidates": len(cands),
        "connective_matches_without_arguments": len(misses),
        "miss_reasons": dict(Counter(m.get("left") or m.get("right") or "other" for m in misses)),
        "candidates": len(docs),
        "dropped_at_linking": dropped,
        "by_lang": dict(Counter(d["source"]["lang"] for d in docs)),
        "by_connective": dict(Counter(d["connective"]["id"] for d in docs).most_common()),
        "by_flag": dict(Counter(d["negation_hedge_flag"] for d in docs)),
        "with_modifier_head": sum(1 for d in docs if d["cause"]["modifier_head"] or d["effect"]["modifier_head"]),
        "articles_with_candidates": len({(d["source"]["lang"], d["source"]["article_title"]) for d in docs}),
        "distinct_pairs": len({(d["cause_qid"], d["effect_qid"]) for d in docs}),
    }
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report


async def _harvest(args: argparse.Namespace) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    arts, chosen = articles_for(Path(args.seeds), args.top)
    (out / "articles.json").write_text(
        json.dumps(
            {"seeds": args.seeds, "top": args.top, "events": chosen, "articles": arts}, indent=1, ensure_ascii=False
        )
    )
    planned = math.ceil(len(arts) / F.CONTENT_BATCH) + 20
    result: dict[str, Any] = {"articles_requested": len(arts)}
    bs = F.budgeted_session(db_name=args.db, planned_requests=planned, use_requested_budget=args.use_requested_budget)
    try:
        async with bs as sess:
            try:
                result["fetch"] = await F.harvest_sentences(sess, arts, out)
                cands, _ = X.extract(F.load_records(out))
                result["resolve"] = await F.resolve_targets(sess, X.needed_targets(cands), out / "resolutions.jsonl")
            except (F.RateLimitedStop, BudgetExhausted) as e:  # partial run: what was logged stays (resumable)
                result["stopped"] = e.problem()
            finally:
                result["http"] = {"requests": sess.requests, "bytes": sess.nbytes, "throttled": sess.throttled}
                with (out / "http_log.jsonl").open("a") as f:
                    for row in sess.log:
                        f.write(json.dumps(row) + "\n")
    finally:
        result["budget"] = bs.usage
    result["report"] = build(out)
    (out / f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json").write_text(
        json.dumps(result, indent=1, default=str)
    )
    return result


async def _relink(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out)
    result: dict[str, Any] = {}
    if not args.offline:
        cands, _ = X.extract(F.load_records(out))
        need = X.needed_targets(cands)
        have = F.load_resolutions(out / "resolutions.jsonl")
        missing = sum(len([t for t in ts if t not in have.get(lang, {})]) for lang, ts in need.items())
        if missing:
            bs = F.budgeted_session(
                db_name=args.db,
                planned_requests=math.ceil(missing / F.RESOLVE_BATCH) + 2,
                use_requested_budget=args.use_requested_budget,
            )
            async with bs as sess:
                result["resolve"] = await F.resolve_targets(sess, need, out / "resolutions.jsonl")
            result["budget"] = bs.usage
    result["report"] = build(out)
    return result


def sample(out: Path, n: int, seed: int) -> list[dict[str, Any]]:
    docs = [json.loads(line) for line in (out / "candidates.jsonl").read_text().splitlines()]
    rng = random.Random(seed)
    picked = rng.sample(docs, min(n, len(docs)))
    return [
        {
            "candidate_id": d["candidate_id"],
            "lang": d["source"]["lang"],
            "article": d["source"]["article_title"],
            "connective": d["connective"]["id"],
            "flag": d["negation_hedge_flag"],
            "sentence": d["sentence"],
            "cause": f"{d['cause']['anchor']} -> {d['cause']['resolved_title']} ({d['cause_qid']})"
            + (f" [head: {d['cause']['modifier_head']}]" if d["cause"]["modifier_head"] else ""),
            "effect": f"{d['effect']['anchor']} -> {d['effect']['resolved_title']} ({d['effect_qid']})"
            + (f" [head: {d['effect']['modifier_head']}]" if d["effect"]["modifier_head"] else ""),
        }
        for d in picked
    ]


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - operator entry point
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m reva.causal_candidates", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seeds")
    s.add_argument("--out", required=True)
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--db", default=F.DEFAULT_DB)
    h = sub.add_parser("harvest")
    h.add_argument("--seeds", required=True)
    h.add_argument("--top", type=int, default=250)
    h.add_argument("--out", required=True)
    h.add_argument("--db", default=F.DEFAULT_DB)
    h.add_argument("--use-requested-budget", action="store_true")
    r = sub.add_parser("relink")
    r.add_argument("--out", required=True)
    r.add_argument("--offline", action="store_true")
    r.add_argument("--db", default=F.DEFAULT_DB)
    r.add_argument("--use-requested-budget", action="store_true")
    m = sub.add_parser("sample")
    m.add_argument("--out", required=True)
    m.add_argument("--n", type=int, default=50)
    m.add_argument("--seed", type=int, default=20261016)
    args = ap.parse_args(argv)
    if args.cmd == "seeds":
        from reva.causal_candidates.seeds import fetch_seeds

        doc = asyncio.run(fetch_seeds(db_name=args.db, limit=args.limit, out=Path(args.out)))
        print(json.dumps({"items_matching": doc["items_matching"], "seeds": len(doc["seeds"])}))
    elif args.cmd == "harvest":
        print(json.dumps(asyncio.run(_harvest(args)), indent=1, default=str))
    elif args.cmd == "relink":
        print(json.dumps(asyncio.run(_relink(args)), indent=1, default=str))
    else:
        print(json.dumps(sample(Path(args.out), args.n, args.seed), indent=1, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    main()
