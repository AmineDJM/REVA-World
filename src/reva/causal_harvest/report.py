"""Consistency report over the harvested Wikidata causal statements (agent CAUSWD).

Reads the current view of the CAUSWD throwaway KG database. REVA asserts nothing: every edge is "Wikidata
asserts …" with its statement id and entity revision; precedence/cycles are *checks* on those assertions.
The evidence grades here are a provisional reading of ``docs/roadmap/v3_causality.md`` (T3 referenced /
T4 unreferenced, E when time-inconsistent) until CAUSCORE's grader (``registry/causal/grading.yaml``) exists.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from datetime import date
from typing import Any

from reva.causal_harvest import core

START_PIDS = ("P580", "P585", "P571")


def load_graph(engine: Any) -> dict[str, Any]:
    from sqlalchemy import text

    with engine.connect() as c:
        rows = (
            c.execute(
                text(
                    "SELECT subject_qid, property_pid, object_qid, value, statement_id, rank, refs, qualifiers, source_key,"
                    " source_revision FROM kg.statement_current WHERE property_pid = ANY(:p)"
                ),
                {"p": sorted(core.CAUSAL_PIDS)},
            )
            .mappings()
            .all()
        )
        times = c.execute(
            text(
                "SELECT subject_qid, property_pid, value_time_lo, value_time_hi FROM kg.statement_current "
                "WHERE property_pid = ANY(:p) AND value_time_lo IS NOT NULL"
            ),
            {"p": list(START_PIDS)},
        ).all()
        labels = dict(
            c.execute(
                text(
                    "SELECT DISTINCT ON (qid) qid, label FROM kg.entity_label WHERE NOT is_alias AND lang IN ('en','fr') "
                    "ORDER BY qid, (lang = 'en') DESC, entity_id DESC"
                )
            ).all()
        )
        ents = {
            r[0]: {"sitelinks": r[1], "revision": r[2]}
            for r in c.execute(text("SELECT qid, sitelinks, source_revision FROM kg.entity_current")).all()
        }
    start: dict[str, tuple[date, date, str]] = {}
    for q, p, lo, hi in times:
        cur = start.get(q)
        rank = START_PIDS.index(p)
        if cur is None or (rank, lo) < (START_PIDS.index(cur[2]), cur[0]):
            start[q] = (lo, hi, p)
    return {"rows": rows, "start": start, "labels": labels, "entities": ents}


def analyse(g: dict[str, Any], *, top: int = 25) -> dict[str, Any]:
    rows, start, labels, ents = g["rows"], g["start"], g["labels"], g["entities"]

    def lab(q: str) -> str:
        return f"{labels.get(q, '?')} ({q})"

    by_pid: Counter[str] = Counter()
    ref_classes: Counter[str] = Counter()
    temporal: Counter[str] = Counter()
    grades: Counter[str] = Counter()
    non_entity = 0
    edges: list[dict[str, Any]] = []
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_pid[r["property_pid"]] += 1
        e = core.causal_edge(r["subject_qid"], r["property_pid"], r["object_qid"], r["statement_id"])
        wdqs_row = r["source_key"] == "wikidata.wdqs"
        rc = core.reference_class(r["refs"] or [], wdqs_row=wdqs_row)
        ref_classes[rc] += 1
        if e is None:
            non_entity += 1
            continue
        cs, es = start.get(e.cause), start.get(e.effect)
        tc = core.temporal_check((cs[0], cs[1]) if cs else None, (es[0], es[1]) if es else None)
        temporal[tc] += 1
        tier = {
            "scholarly": "T3+scholarly_ref",
            "referenced": "T3",
            "wikimedia_import_only": "T4",
            "none": "T4",
            "unknown": "T3/T4?",
        }[rc]
        edge = {
            "cause": e.cause,
            "effect": e.effect,
            "relation": e.relation,
            "pid": e.pid,
            "statement_id": e.statement_id,
            "asserted_on": e.subject,
            "revision": r["source_revision"],
            "rank": r["rank"],
            "references": r["refs"],
            "ref_class": rc,
            "tier": tier,
            "temporal": tc,
            "qualifiers": {k: v for k, v in (r["qualifiers"] or {}).items() if not k.startswith("_")},
        }
        edges.append(edge)
        pairs[e.key].append(edge)
    for edge in edges:
        corroborated = len({x["statement_id"] for x in pairs[(edge["cause"], edge["effect"])]}) > 1
        edge["mirrored"] = corroborated  # asserted on both items (one source: Wikidata — not independent)
        if edge["temporal"] == "inconsistent":
            g_ = "E"
        elif edge["tier"].startswith("T3") and edge["tier"] != "T3/T4?":
            g_ = "C"
        elif edge["tier"] == "T4":
            g_ = "D"
        else:
            g_ = "C/D?"
        edge["provisional_grade"] = g_
        grades[g_] += 1
    succ: dict[str, list[str]] = defaultdict(list)
    for c_, e_ in pairs:
        if c_ != e_:
            succ[c_].append(e_)
    self_loops = [lab(c_) for (c_, e_) in pairs if c_ == e_]
    nodes = sorted({q for k in pairs for q in k}, key=lambda q: int(q[1:]))
    sccs = core.strongly_connected(nodes, succ)
    deg: Counter[str] = Counter()
    for c_, e_ in pairs:
        deg[c_] += 1
        deg[e_] += 1
    indeg = Counter(e_ for (_, e_) in pairs)
    outdeg = Counter(c_ for (c_, _) in pairs)
    inconsistent = [
        {
            "cause": lab(x["cause"]),
            "effect": lab(x["effect"]),
            "pid": x["pid"],
            "statement_id": x["statement_id"],
            "revision": x["revision"],
            "cause_start": [str(d) for d in start[x["cause"]][:2]] + [start[x["cause"]][2]],
            "effect_start": [str(d) for d in start[x["effect"]][:2]] + [start[x["effect"]][2]],
        }
        for x in edges
        if x["temporal"] == "inconsistent"
    ]
    cycles = []
    for comp in sccs:
        cs_ = set(comp)
        cyc_edges = [
            {"cause": lab(c_), "effect": lab(e_), "statements": [x["statement_id"] for x in pairs[(c_, e_)]]}
            for (c_, e_) in pairs
            if c_ in cs_ and e_ in cs_ and c_ != e_
        ]
        cycles.append({"size": len(comp), "nodes": [lab(q) for q in comp], "edges": cyc_edges[:20]})
    return {
        "statements": len(rows),
        "by_property": dict(by_pid),
        "non_entity_objects": non_entity,
        "edges": len(edges),
        "distinct_pairs": len(pairs),
        "mirrored_pairs": sum(1 for v in pairs.values() if len({x["statement_id"] for x in v}) > 1),
        "nodes": len(nodes),
        "nodes_with_snapshot": sum(1 for q in nodes if q in ents),
        "nodes_with_start": sum(1 for q in nodes if q in start),
        "reference_classes": dict(ref_classes),
        "temporal": dict(temporal),
        "provisional_grades": dict(grades),
        "inconsistent": inconsistent,
        "self_loops": self_loops,
        "cycles": cycles,
        "top_connected": [
            {
                "node": lab(q),
                "degree": n,
                "in(effect of)": indeg[q],
                "out(cause of)": outdeg[q],
                "sitelinks": (ents.get(q) or {}).get("sitelinks"),
            }
            for q, n in deg.most_common(top)
        ],
        "_edges": edges,
        "_succ": succ,
    }


def chain_exhibits(
    a: dict[str, Any], g: dict[str, Any], *, starts: list[str] | None = None, limit: int = 12, min_hops: int = 2
) -> list[dict[str, Any]]:
    """Longest sourced chains (every hop = one or more Wikidata statements with revision and references)."""
    labels, ents = g["labels"], g["entities"]
    succ = a["_succ"]
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in a["_edges"]:
        by_pair[(e["cause"], e["effect"])].append(e)
    if starts is None:
        roots = [q for q in succ if succ[q]]
        roots.sort(key=lambda q: -int((ents.get(q) or {}).get("sitelinks") or 0))
        starts = roots[:200]
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for s in starts:
        for path in core.chains(s, succ, max_hops=6, limit=3):
            if len(path) - 1 < min_hops or tuple(path) in seen:
                continue
            seen.add(tuple(path))
            hops = []
            for c_, e_ in itertools.pairwise(path):
                hops.append(
                    {
                        "cause": f"{labels.get(c_, '?')} ({c_})",
                        "effect": f"{labels.get(e_, '?')} ({e_})",
                        "asserted_by": [
                            {
                                "statement_id": x["statement_id"],
                                "pid": x["pid"],
                                "on": x["asserted_on"],
                                "revision": x["revision"],
                                "ref_class": x["ref_class"],
                                "temporal": x["temporal"],
                                "references": x["references"][:3],
                            }
                            for x in by_pair[(c_, e_)]
                        ],
                    }
                )
            out.append(
                {
                    "hops": len(hops),
                    "path": [f"{labels.get(q, '?')} ({q})" for q in path],
                    "chain_grade_weakest_hop": max(
                        min(x["provisional_grade"] for x in by_pair[(c_, e_)]) for c_, e_ in itertools.pairwise(path)
                    ),
                    "hops_detail": hops,
                }
            )
            if len(out) >= limit:
                return out
    return out
