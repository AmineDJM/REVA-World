"""Database checks: KG graph sanity, KG time sanity, licence and provenance of KG statements / causal claims /
effect estimates, object-store integrity, causal sanity. Read-only SQL (see :mod:`reva.qa.backend`).

The heavy reads are two aggregate passes (statements and claims grouped by predicate / source / raw object) and
one edge read for the part_of / is_a / located_in families; everything else is derived from them in Python.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from reva.atlas.snapshot import normalise_rights
from reva.qa.backend import QaBackend, lit, lits
from reva.qa.config import REPO_ROOT, QaConfig
from reva.qa.report import CheckResult, status_for

EX = 8
CAUSAL_PIDS = ("P828", "P1542", "P1478", "P1479", "P1536", "P1537")


def _prefix(x: str | None) -> str:
    if not x:
        return "(null)"
    if re.match(r"^Q[0-9]+$", x):
        return "wikidata"
    return x.split(":", 1)[0] if ":" in x else "(no prefix)"


# ---------------------------------------------------------------------------------------------- KG aggregate pass
def kg_aggregate(be: QaBackend, origin: str, today: str) -> list[dict[str, Any]]:
    o = lit(origin)
    return be.rows(
        "SELECT s.property_pid, s.source_key, s.raw_sha256, COUNT(*) AS n, "
        "SUM(CASE WHEN s.object_qid IS NOT NULL THEN 1 ELSE 0 END) AS with_object, "
        "SUM(CASE WHEN s.object_qid IS NOT NULL AND eo.qid IS NULL THEN 1 ELSE 0 END) AS dangling_object, "
        "MIN(CASE WHEN s.object_qid IS NOT NULL AND eo.qid IS NULL THEN s.object_qid END) AS dangling_object_ex, "
        "MIN(CASE WHEN s.object_qid IS NOT NULL AND eo.qid IS NULL THEN s.id END) AS dangling_row_ex, "
        "SUM(CASE WHEN es.qid IS NULL THEN 1 ELSE 0 END) AS dangling_subject, "
        "MIN(CASE WHEN es.qid IS NULL THEN s.subject_qid END) AS dangling_subject_ex, "
        f"SUM(CASE WHEN s.valid_lo > DATE {lit(today)} OR s.value_time_lo > DATE {lit(today)} THEN 1 ELSE 0 END) AS future, "
        f"MIN(CASE WHEN s.valid_lo > DATE {lit(today)} OR s.value_time_lo > DATE {lit(today)} THEN s.id END) AS future_ex, "
        "MAX(CASE WHEN a.sha256 IS NOT NULL THEN 1 ELSE 0 END) AS has_artifact, "
        "MAX(CAST(a.rights AS TEXT)) AS rights "
        "FROM kg.statement_current s "
        f"LEFT JOIN (SELECT DISTINCT qid FROM kg.entity WHERE origin = {o}) eo ON eo.qid = s.object_qid "
        f"LEFT JOIN (SELECT DISTINCT qid FROM kg.entity WHERE origin = {o}) es ON es.qid = s.subject_qid "
        "LEFT JOIN meta.artifact a ON a.sha256 = s.raw_sha256 "
        f"WHERE s.origin = {o} GROUP BY s.property_pid, s.source_key, s.raw_sha256"
    )


def causal_aggregate(be: QaBackend, origin: str) -> list[dict[str, Any]]:
    o = lit(origin)
    return be.rows(
        "SELECT c.source_key, c.relation, c.raw_sha256, COUNT(*) AS n, "
        "SUM(CASE WHEN g.claim_row_id IS NULL THEN 1 ELSE 0 END) AS ungraded, "
        "SUM(CASE WHEN g.claim_row_id IS NULL AND gk.claim_key IS NOT NULL THEN 1 ELSE 0 END) AS stale_grade, "
        "MIN(CASE WHEN g.claim_row_id IS NULL THEN c.claim_key END) AS ungraded_ex, "
        "SUM(CASE WHEN ec.qid IS NULL THEN 1 ELSE 0 END) AS cause_missing, "
        "MIN(CASE WHEN ec.qid IS NULL THEN c.cause_id END) AS cause_missing_ex, "
        "SUM(CASE WHEN ee.qid IS NULL THEN 1 ELSE 0 END) AS effect_missing, "
        "MIN(CASE WHEN ee.qid IS NULL THEN c.effect_id END) AS effect_missing_ex, "
        "SUM(CASE WHEN c.cause_id = c.effect_id THEN 1 ELSE 0 END) AS self_loop, "
        "MAX(CASE WHEN a.sha256 IS NOT NULL THEN 1 ELSE 0 END) AS has_artifact, "
        "MAX(CAST(a.rights AS TEXT)) AS rights "
        "FROM kg.causal_claim_current c "
        f"LEFT JOIN (SELECT DISTINCT claim_row_id FROM kg.causal_grade_current WHERE origin = {o}) g ON g.claim_row_id = c.id "
        f"LEFT JOIN (SELECT DISTINCT claim_key FROM kg.causal_grade_current WHERE origin = {o}) gk ON gk.claim_key = c.claim_key "
        f"LEFT JOIN (SELECT DISTINCT qid FROM kg.entity WHERE origin = {o}) ec ON ec.qid = c.cause_id "
        f"LEFT JOIN (SELECT DISTINCT qid FROM kg.entity WHERE origin = {o}) ee ON ee.qid = c.effect_id "
        "LEFT JOIN meta.artifact a ON a.sha256 = c.raw_sha256 "
        f"WHERE c.origin = {o} GROUP BY c.source_key, c.relation, c.raw_sha256"
    )


# ---------------------------------------------------------------------------------------------- graph
def _edge_specs(families: Iterable[str], exclude: set[str]) -> dict[str, list[tuple[str, bool]]]:
    """family -> [(pid, forward)]: forward = edge subject→object reads "subject <family> object"."""
    from reva.kg.families import load_registry

    reg = load_registry()
    out: dict[str, list[tuple[str, bool]]] = {}
    for f in families:
        fam = reg.families[f]
        specs = []
        for pid, m in reg.mappings.items():
            if pid in exclude:
                continue
            if m.family == f:
                specs.append((pid, m.direction == "forward"))
            elif fam.inverse_family and m.family == fam.inverse_family:
                specs.append((pid, m.direction != "forward"))
        out[f] = sorted(specs)
    return out


def _sccs(adj: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with > 1 node or a self loop (iterative Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    i = 0
    for root in list(adj):
        if root in index:
            continue
        work = [(root, iter(sorted(adj.get(root, ()))))]
        index[root] = low[root] = i
        i += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = i
                    i += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(adj.get(nxt, ())))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in adj.get(v, ()):
                    out.append(sorted(comp))
    return out


def country_nodes() -> set[str]:
    d = json.loads((REPO_ROOT / "registry" / "entities" / "country_qids.json").read_text())
    nodes = {v["qid"] for v in d["countries"].values() if v.get("qid")}
    nodes |= {f"iso3:{k}" for k in d["countries"]}
    return nodes


def graph_checks(be: QaBackend, cfg: QaConfig, origin: str, agg: list[dict[str, Any]]) -> list[CheckResult]:
    g = cfg.section("graph")
    nex = int(g.get("examples") or EX)
    out: list[CheckResult] = []
    # dangling references by predicate
    by: dict[str, dict[str, Any]] = {}
    for r in agg:
        b = by.setdefault(
            r["property_pid"],
            {
                "predicate": r["property_pid"],
                "statements": 0,
                "with_object": 0,
                "dangling_object": 0,
                "dangling_subject": 0,
                "example": None,
            },
        )
        b["statements"] += int(r["n"])
        b["with_object"] += int(r["with_object"] or 0)
        b["dangling_object"] += int(r["dangling_object"] or 0)
        b["dangling_subject"] += int(r["dangling_subject"] or 0)
        if r["dangling_object"] and b["example"] is None:
            b["example"] = f"row {r['dangling_row_ex']} → {r['dangling_object_ex']}"
        if r["dangling_subject"] and not b.get("subject_example"):
            b["subject_example"] = r["dangling_subject_ex"]
    rows = sorted([b for b in by.values() if b["dangling_object"]], key=lambda b: -b["dangling_object"])
    for b in rows:
        b["share"] = round(b["dangling_object"] / max(b["with_object"], 1), 4)
    tot = sum(b["dangling_object"] for b in rows)
    out.append(
        CheckResult(
            "graph.dangling_object",
            "graph",
            "edges whose object is not a KG entity (by predicate)",
            status_for(tot, "warn"),
            tot,
            sum(b["with_object"] for b in by.values()),
            [{"predicate": b["predicate"], "example": b["example"]} for b in rows[:nex]],
            rows,
            note="Object id has no kg.entity row in this origin (unlabeled node in family pages).",
        )
    )
    srows = sorted(
        [
            {
                "predicate": b["predicate"],
                "statements": b["statements"],
                "dangling_subject": b["dangling_subject"],
                "example": b.get("subject_example"),
            }
            for b in by.values()
            if b["dangling_subject"]
        ],
        key=lambda b: -b["dangling_subject"],
    )
    stot = sum(b["dangling_subject"] for b in srows)
    out.append(
        CheckResult(
            "graph.dangling_subject",
            "graph",
            "statements whose subject is not a KG entity",
            status_for(stot, "warn"),
            stot,
            sum(b["statements"] for b in by.values()),
            [],
            srows,
        )
    )

    # edges for cycles + located_in
    fams = list(g.get("cycle_families") or [])
    li = g.get("located_in") or {}
    specs = _edge_specs(fams, set(g.get("cycle_exclude_pids") or []))
    li_specs = _edge_specs([li.get("family", "located_in")], set(li.get("exclude_pids") or []))
    pids = {p for v in [*specs.values(), *li_specs.values()] for p, _ in v}
    present = {r["property_pid"] for r in agg}
    pids &= present
    edges = (
        be.rows(
            "SELECT subject_qid, property_pid, object_qid FROM kg.statement_current "
            f"WHERE origin = {lit(origin)} AND object_qid IS NOT NULL AND property_pid IN {lits(pids)}"
        )
        if pids
        else []
    )
    by_pid: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        by_pid[e["property_pid"]].append((e["subject_qid"], e["object_qid"]))
    for fam, sp in specs.items():
        adj: dict[str, set[str]] = defaultdict(set)
        label: dict[tuple[str, str], str] = {}
        n_edges = 0
        for pid, fwd in sp:
            for s, o in by_pid.get(pid, ()):
                u, v = (s, o) if fwd else (o, s)
                adj[u].add(v)
                label.setdefault((u, v), pid)
                n_edges += 1
        comps = _sccs(adj)
        nodes = sum(len(c) for c in comps)
        ex = []
        for c in sorted(comps, key=len)[:nex]:
            cs = set(c)
            ex.append(
                {
                    "nodes": c[:8],
                    "size": len(c),
                    "edges": [f"{u} -{label[(u, v)]}-> {v}" for u in c[:8] for v in sorted(adj[u] & cs)][:8],
                }
            )
        out.append(
            CheckResult(
                f"graph.cycles.{fam}",
                "graph",
                f"cycles in {fam} (child → parent edges)",
                status_for(len(comps), "warn"),
                len(comps),
                n_edges,
                ex,
                [
                    {
                        "cycles": len(comps),
                        "nodes_in_cycles": nodes,
                        "edges": n_edges,
                        "predicates": ", ".join(p for p, _ in sp if p in by_pid),
                    }
                ],
            )
        )
    # located_in chains reaching a country
    countries = country_nodes()
    up: dict[str, set[str]] = defaultdict(set)
    first_pid: dict[str, str] = {}
    for sp in li_specs.values():
        for pid, fwd in sp:
            for s, o in by_pid.get(pid, ()):
                u, v = (s, o) if fwd else (o, s)
                up[u].add(v)
                first_pid.setdefault(u, pid)
    down: dict[str, set[str]] = defaultdict(set)
    for u, vs in up.items():
        for v in vs:
            down[v].add(u)
    reach = set(countries)
    frontier = list(countries)
    while frontier:
        n = frontier.pop()
        for a in down.get(n, ()):
            if a not in reach:
                reach.add(a)
                frontier.append(a)
    subj = [a for a in up if a not in countries]
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ex = []
    for a in subj:
        p = first_pid[a]
        per[p][0] += 1
        if a not in reach:
            per[p][1] += 1
            if len(ex) < nex:
                chain = [a]
                while len(chain) < 6 and up.get(chain[-1]):
                    nxt = sorted(up[chain[-1]])[0]
                    if nxt in chain:
                        chain.append(nxt + " (loop)")
                        break
                    chain.append(nxt)
                ex.append({"subject": a, "predicate": p, "chain": " → ".join(chain)})
    bad = sum(v[1] for v in per.values())
    out.append(
        CheckResult(
            "graph.located_in_no_country",
            "graph",
            "located_in chains that never reach a country",
            status_for(bad, "warn"),
            bad,
            len(subj),
            ex,
            sorted(
                [
                    {"predicate": k, "subjects": v[0], "not_reaching": v[1], "share": round(v[1] / max(v[0], 1), 4)}
                    for k, v in per.items()
                ],
                key=lambda r: -r["not_reaching"],
            ),
            note=f"Country = one of {len(countries)} crosswalk QIDs / iso3: nodes; anatomical/genomic located_in excluded.",
        )
    )
    return out


# ---------------------------------------------------------------------------------------------- KG time
def kg_time_check(cfg: QaConfig, agg: list[dict[str, Any]]) -> CheckResult:
    ok = set(cfg.section("time").get("kg_future_ok_predicates") or [])
    per: Counter[str] = Counter()
    exr: dict[str, Any] = {}
    allowed = 0
    for r in agg:
        n = int(r["future"] or 0)
        if not n:
            continue
        if r["property_pid"] in ok:
            allowed += n
            continue
        per[r["property_pid"]] += n
        exr.setdefault(r["property_pid"], r["future_ex"])
    tot = sum(per.values())
    return CheckResult(
        "time.kg_future_statement",
        "time",
        "KG statements dated after today",
        status_for(tot, "warn"),
        tot,
        sum(int(r["n"]) for r in agg),
        [{"predicate": k, "row": exr[k]} for k, _ in per.most_common(EX)],
        [{"predicate": k, "statements": v} for k, v in per.most_common()],
        note=f"{allowed} more on predicates declared future-valid (kg_future_ok_predicates).",
    )


# ---------------------------------------------------------------------------------------------- licence + provenance
def _licence_rows(rows: list[dict[str, Any]], key: str, cfg: QaConfig) -> tuple[list[dict[str, Any]], int, int]:
    pend_vals = cfg.section("licence").get("pending_review_values") or []
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = per.setdefault(
            r[key],
            {
                key: r[key],
                "rows": 0,
                "no_raw": 0,
                "raw_not_archived": 0,
                "no_licence": 0,
                "restricted": 0,
                "review_pending": 0,
            },
        )
        n = int(r["n"])
        b["rows"] += n
        if not r["raw_sha256"]:
            b["no_raw"] += n
            continue
        if not r["has_artifact"]:
            b["raw_not_archived"] += n
            continue
        rights = json.loads(r["rights"]) if r["rights"] else None
        nr = normalise_rights(rights, basis="raw_artifact")
        if nr is None:
            b["no_licence"] += n
        elif nr["restricted"]:
            b["restricted"] += n
        txt = (r["rights"] or "").lower()
        if any(v in txt for v in pend_vals):
            b["review_pending"] += n
    out = sorted(per.values(), key=lambda b: -(b["no_licence"] + b["no_raw"] + b["raw_not_archived"]))
    return out, sum(b["no_licence"] for b in out), sum(b["rows"] for b in out)


def licence_provenance_checks(
    cfg: QaConfig,
    kg: list[dict[str, Any]] | None,
    causal: list[dict[str, Any]] | None,
    effects: list[dict[str, Any]] | None,
    domains: set[str],
) -> list[CheckResult]:
    out: list[CheckResult] = []
    for name, rows, key in (
        ("kg", kg, "source_key"),
        ("causal", causal, "source_key"),
        ("effects", effects, "source_key"),
    ):
        if rows is None:
            continue
        per, no_lic, total = _licence_rows(rows, key, cfg)
        if "licence" in domains:
            out.append(
                CheckResult(
                    f"licence.{name}_raw",
                    "licence",
                    f"{name} rows whose raw object carries no licence",
                    status_for(no_lic, "warn"),
                    no_lic,
                    total,
                    [{"source_key": b[key], "rows": b["no_licence"]} for b in per if b["no_licence"]][:EX],
                    [
                        {k: b[k] for k in (key, "rows", "no_licence", "restricted", "review_pending")}
                        for b in per
                        if b["no_licence"] or b["restricted"] or b["review_pending"]
                    ],
                    note="Licence read from meta.artifact.rights of the cited raw object (as the families API does).",
                )
            )
        if "provenance" in domains:
            nr = sum(b["no_raw"] for b in per)
            na = sum(b["raw_not_archived"] for b in per)
            out.append(
                CheckResult(
                    f"provenance.{name}_no_raw",
                    "provenance",
                    f"{name} rows without a raw object (null or not archived)",
                    status_for(nr + na, "fail" if na else "warn"),
                    nr + na,
                    total,
                    [
                        {"source_key": b[key], "no_raw": b["no_raw"], "raw_not_archived": b["raw_not_archived"]}
                        for b in per
                        if b["no_raw"] or b["raw_not_archived"]
                    ][:EX],
                    [
                        {k: b[k] for k in (key, "rows", "no_raw", "raw_not_archived")}
                        for b in per
                        if b["no_raw"] or b["raw_not_archived"]
                    ],
                )
            )
    return out


def object_checks(
    be: QaBackend, cfg: QaConfig, referenced: dict[str, str], *, verify_all: bool, log: Any = None
) -> list[CheckResult]:
    """Every archived object exists (store head); a seeded sample (or all) is decoded and re-hashed; every raw
    sha256 cited by data rows has an archived location."""
    p = cfg.section("provenance")
    locs = be.rows("SELECT artifact_sha256, namespace, verified_byte_size FROM meta.artifact_location")
    inline = {r["artifact_sha256"] for r in be.rows("SELECT artifact_sha256 FROM meta.artifact_inline")}
    located = {r["artifact_sha256"] for r in locs}
    seed = str(p.get("verify_seed") or "QA")
    order = sorted(locs, key=lambda r: hashlib.sha256((r["artifact_sha256"] + seed).encode()).hexdigest())
    n_verify = len(order) if verify_all else int(p.get("verify_sample") or 0)
    budget = None if verify_all else int(p.get("verify_max_bytes") or 0)
    states: Counter[str] = Counter()
    bad: list[dict[str, Any]] = []
    verified = vbytes = 0
    for i, r in enumerate(order):
        size = int(r["verified_byte_size"] or 0)
        verify = verified < n_verify and (budget is None or vbytes + size <= budget)
        st = be.object_state(r["namespace"], r["artifact_sha256"], verify=verify)
        if verify:
            verified += 1
            vbytes += size
        states[st] += 1
        if st != "ok":
            bad.append({"namespace": r["namespace"], "sha256": r["artifact_sha256"], "state": st})
        if log and i and i % 2000 == 0:
            log(f"[qa objects] {i}/{len(order)} checked, {verified} verified")
    missing_loc = sorted(s for s in referenced if s not in located and s not in inline)
    return [
        CheckResult(
            "provenance.objects",
            "provenance",
            "archived objects missing or corrupt (store API, decoded)",
            status_for(len(bad), "fail"),
            len(bad),
            len(order),
            bad[:EX],
            [{"state": k, "objects": v} for k, v in states.items()]
            + [{"state": "fully re-hashed", "objects": verified, "bytes": vbytes}],
            note=(
                "Existence of every meta.artifact_location object via the store (zstd-transparent); a "
                "seeded sample is decoded and re-hashed (--verify-all re-hashes all)."
            ),
        ),
        CheckResult(
            "provenance.referenced_not_archived",
            "provenance",
            "raw sha256 cited by atlas cells / tables without an archived location",
            status_for(len(missing_loc), "fail"),
            len(missing_loc),
            len(referenced),
            [{"sha256": s, "cited_by": referenced[s]} for s in missing_loc[:EX]],
        ),
    ]


# ---------------------------------------------------------------------------------------------- causal
def causal_checks(
    be: QaBackend, cfg: QaConfig, origin: str, agg: list[dict[str, Any]], kg_pids: dict[str, int]
) -> list[CheckResult]:
    c = cfg.section("causal")
    out: list[CheckResult] = []
    per: dict[str, Counter[str]] = defaultdict(Counter)
    ex_ungraded: list[dict[str, Any]] = []
    ends: Counter[tuple[str, str]] = Counter()
    ends_ex: dict[tuple[str, str], str] = {}
    total = 0
    for r in agg:
        n = int(r["n"])
        total += n
        s = per[r["source_key"]]
        s["claims"] += n
        s["ungraded"] += int(r["ungraded"] or 0)
        s["stale_grade"] += int(r["stale_grade"] or 0)
        s["self_loop"] += int(r["self_loop"] or 0)
        if r["ungraded"] and len(ex_ungraded) < EX and r["ungraded_ex"]:
            ex_ungraded.append({"source_key": r["source_key"], "claim_key": r["ungraded_ex"]})
        for side in ("cause", "effect"):
            nm = int(r[f"{side}_missing"] or 0)
            if nm:
                k = (r["source_key"], side)
                ends[k] += nm
                ends_ex.setdefault(k, r[f"{side}_missing_ex"])
    ung = sum(s["ungraded"] for s in per.values())
    out.append(
        CheckResult(
            "causal.ungraded",
            "causal",
            "current causal claims without a grade for their row",
            status_for(ung, "warn"),
            ung,
            total,
            ex_ungraded,
            sorted(
                [
                    {"source_key": k, "claims": v["claims"], "ungraded": v["ungraded"], "stale_grade": v["stale_grade"]}
                    for k, v in per.items()
                    if v["ungraded"]
                ],
                key=lambda r: -r["ungraded"],
            ),
            note="stale_grade = a grade exists for the claim key but for an older row (re-delivered claim).",
        )
    )
    miss = sum(ends.values())
    out.append(
        CheckResult(
            "causal.missing_endpoint",
            "causal",
            "claim endpoints (cause / effect) that are not KG entities",
            status_for(miss, "warn"),
            miss,
            2 * total,
            [{"source_key": k[0], "side": k[1], "example": ends_ex[k]} for k, _ in ends.most_common(EX)],
            [
                {"source_key": k[0], "side": k[1], "claims": v, "id_prefix": _prefix(ends_ex[k])}
                for k, v in ends.most_common()
            ],
            note="Shown without a label (label_loaded false) until the endpoint is loaded; not a wrong claim by itself.",
        )
    )
    sl = sum(s["self_loop"] for s in per.values())
    out.append(
        CheckResult(
            "causal.self_loop",
            "causal",
            "claims whose cause is their effect",
            status_for(sl, "warn"),
            sl,
            total,
            [],
            [{"source_key": k, "claims": v["self_loop"]} for k, v in per.items() if v["self_loop"]],
        )
    )

    # wording leaks: causal properties / causal-worded predicates read as a non-causal family
    from reva.kg.families import load_registry

    reg = load_registry()
    rx = re.compile(c.get("wording_regex") or r"(?i)\bcaus")
    neg = re.compile(c["negation_regex"]) if c.get("negation_regex") else None

    def worded(txt: str) -> bool:
        return bool(rx.search(txt)) and not (neg and neg.search(txt))

    leaks: list[dict[str, Any]] = []
    for pid, n in sorted(kg_pids.items()):
        m = reg.mappings.get(pid)
        pid_worded = pid in CAUSAL_PIDS or bool(rx.search(pid.split(":", 1)[-1].replace("_", " ")))
        if pid_worded:
            leaks.append(
                {
                    "predicate": pid,
                    "statements": n,
                    "family": m.family if m else None,
                    "status": "LEAK: read as a family" if m else "unmapped (not traversed)",
                }
            )
    for pid in CAUSAL_PIDS:
        m = reg.mappings.get(pid)
        if m and pid not in kg_pids:
            leaks.append({"predicate": pid, "statements": 0, "family": m.family, "status": "LEAK: mapped"})
    labels: list[dict[str, Any]] = []
    for fid, fam in reg.families.items():
        if getattr(fam, "causal", False):
            continue
        for lang, txt in [*fam.label.items(), *fam.inverse_label.items(), *fam.description.items()]:
            if worded(txt or ""):
                labels.append({"family": fid, "lang": lang, "text": txt[:160]})
    lt = REPO_ROOT / "registry" / "links" / "link_types.yaml"
    if lt.exists():
        import yaml

        doc = yaml.safe_load(lt.read_text()) or {}
        for t in [*(doc.get("derived_links") or []), *(doc.get("statement_edges") or [])]:
            if not isinstance(t, dict) or t.get("causal"):
                continue
            for field_ in ("labels", "honesty"):
                for lang, txt in (t.get(field_) or {}).items():
                    if isinstance(txt, str) and worded(txt):
                        labels.append({"link_type": t.get("id"), "field": field_, "lang": lang, "text": txt[:160]})
    mapped_leaks = [x for x in leaks if x["family"]]
    out.append(
        CheckResult(
            "causal.wording_leak_predicates",
            "causal",
            "causal-worded KG predicates read as a non-causal family",
            status_for(len(mapped_leaks), "warn"),
            len(mapped_leaks),
            len(kg_pids),
            mapped_leaks[:EX],
            leaks,
            note="Causal statements must be read through the causal loader as attributed claims; unmapped rows are listed "
            "for review only.",
        )
    )
    out.append(
        CheckResult(
            "causal.wording_leak_labels",
            "causal",
            "non-causal family / link-type labels worded causally",
            status_for(len(labels), "warn"),
            len(labels),
            None,
            labels[:EX],
            labels,
        )
    )
    rels = list(c.get("source_relations_review") or [])
    if rels:
        conds = " OR ".join(f"c.claim_key LIKE {lit('%|' + r + '|%')}" for r in rels)
        rows = be.rows(
            "SELECT c.source_key, COUNT(*) AS n, MIN(c.claim_key) AS ex FROM kg.causal_claim_current c "
            f"WHERE c.origin = {lit(origin)} AND ({conds}) AND CAST(c.qualifiers AS TEXT) NOT LIKE '%source_relation%' "
            # FIX4A: the filler replaced by a REVA-made altered-state node (reva_state:<state>:<filler>, verbatim
            # source relation + label in the selector) is the fix, not an "X causes" claim
            "AND c.cause_id NOT LIKE 'reva_state:%' AND c.effect_id NOT LIKE 'reva_state:%' "
            "GROUP BY c.source_key"
        )
        n = sum(int(r["n"]) for r in rows)
        out.append(
            CheckResult(
                "causal.disruption_axioms_unqualified",
                "causal",
                "'disruption / dysfunction / alteration of X' axioms stored as 'X causes' without the source relation",
                status_for(n, "warn"),
                n,
                total,
                [{"source_key": r["source_key"], "claim_key": r["ex"]} for r in rows],
                [{"source_key": r["source_key"], "claims": int(r["n"])} for r in rows],
                note="AUDIT3 H1: relations "
                + ", ".join(rels)
                + "; fixed when qualifiers carry source_relation or the filler "
                "is replaced by a REVA-made reva_state: node (FIX4A).",
            )
        )
    return out


def effects_aggregate(be: QaBackend, origin: str) -> list[dict[str, Any]]:
    return be.rows(
        "SELECT e.source_key, e.raw_sha256, COUNT(*) AS n, MAX(CASE WHEN a.sha256 IS NOT NULL THEN 1 ELSE 0 END) "
        "AS has_artifact, MAX(CAST(a.rights AS TEXT)) AS rights FROM kg.effect_estimate_current e "
        f"LEFT JOIN meta.artifact a ON a.sha256 = e.raw_sha256 WHERE e.origin = {lit(origin)} "
        "GROUP BY e.source_key, e.raw_sha256"
    )
