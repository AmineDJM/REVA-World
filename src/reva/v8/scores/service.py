"""Page bundles of the V8 scoreboard (pure functions over the validated registry + optional live state)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reva.v8.scores import catalog as C
from reva.v8.scores.registry import REPO, _Files, cell_key, cells, counts_in_headline, headline, stale_sources

TRACK_FILES_FOR_CHARTS = {
    "beat_macro": "registry/forecasts/beat_macro/holdout.json",
    "beat_energy": "docs/dev/v7_beat_energy_result.json",
}


def _sealed(row: Mapping[str, Any], sealed: Mapping[str, int] | None) -> bool | None:
    m = row.get("registry_model")
    if not m or sealed is None:
        return None
    return sealed.get(m, 0) > 0


def _calendar(doc: Mapping[str, Any], sealed: Mapping[str, int] | None) -> list[dict[str, Any]]:
    """Pending scoreboard rows grouped by (track, resolution), plus dated events quoted from reports."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in doc["rows"]:
        if r["verdict"] != "en_attente":
            continue
        res = r.get("resolution") or {}
        when = res.get("date") or res.get("not_before") or res.get("month")
        k = (r["track"], f"{when}|{res.get('note_fr')}")
        g = groups.setdefault(k, {
            "kind": "track", "track": r["track"], "date": res.get("date"), "not_before": res.get("not_before"),
            "month": res.get("month"), "sort": when or "9999", "note_fr": res.get("note_fr"), "quote": res.get("quote"),
            "rows": [], "forecasters": set(), "sealed": None,
        })  # fmt: skip
        g["rows"].append(r["id"])
        if r.get("forecaster"):
            g["forecasters"].add(r["forecaster"])
        s = _sealed(r, sealed)
        if s is not None:
            g["sealed"] = bool(g["sealed"]) or s
    out = []
    for g in groups.values():
        g["forecasters"] = sorted(g["forecasters"])
        g["n_rows"] = len(g["rows"])
        out.append(g)
    for e in doc.get("events", []):
        out.append({"kind": "event", "track": None, "date": e.get("date"), "month": e.get("month"), "not_before": None,
                    "sort": e.get("date") or e.get("month"), "note_fr": e["label_fr"], "quote": e["quote"],
                    "rows": [], "n_rows": 0, "forecasters": [], "sealed": None, "id": e["id"]})  # fmt: skip
    return sorted(out, key=lambda g: (str(g["sort"]), str(g["track"])))


def page(doc: Mapping[str, Any], *, sealed: Mapping[str, int] | None = None, root: Path = REPO) -> dict[str, Any]:
    rows = doc["rows"]
    cs = cells(rows)
    tgt_order = [t["id"] for t in doc["targets"]]
    used_targets = {c["target"] for c in cs.values()} | {n["target"] for n in doc["no_track"]}
    official = [f for f in doc["forecasters"]]
    used_f = {c["forecaster"] for c in cs.values() if c["forecaster"]} | {
        n["forecaster"] for n in doc["no_track"] if n["forecaster"]
    }
    no_track = {f"{n['target']}~{n['forecaster'] or 'none'}": n for n in doc["no_track"]}
    matrix_rows = []
    for d in doc["domains"]:
        trs = []
        for t in doc["targets"]:
            if t["domain"] != d["id"] or t["id"] not in used_targets:
                continue
            row_cells = {}
            for f in official:
                k = f"{t['id']}~{f['id']}"
                if k in cs and sum(cs[k]["counts"].values()) + sum(cs[k]["weak_reference"].values()) > 0:
                    row_cells[f["id"]] = cs[k]
                elif k in no_track:
                    row_cells[f["id"]] = {"key": k, "status": "no_track", "reason_fr": no_track[k]["reason_fr"],
                                          "quote": no_track[k]["quote"], "partial": no_track[k].get("partial", False)}  # fmt: skip
            naive = cs.get(f"{t['id']}~naive")
            nt = no_track.get(f"{t['id']}~none")
            trs.append({"target": t["id"], "label_fr": t["label_fr"], "cells": row_cells,
                        "naive": naive, "no_track": nt})  # fmt: skip
        trs.sort(key=lambda x: tgt_order.index(x["target"]))
        matrix_rows.append({"domain": d["id"], "label_fr": d["label_fr"], "targets": trs})
    h = headline(rows)
    weak = Counter(r["verdict"] for r in rows if r["benchmark_kind"] == "weak_reference")
    naive = Counter(r["verdict"] for r in rows if r["benchmark_kind"] == "naive")
    by_eval: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        if counts_in_headline(r):
            by_eval[r["evaluation"]][r["verdict"]] += 1
    track_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        if counts_in_headline(r):
            track_counts[r["track"]][r["verdict"]] += 1
    tracks = [{**{k: t[k] for k in ("id", "label_fr", "agent", "version", "report", "summary_fr")},
               "protocol": t.get("protocol"), "counts": {v: track_counts[t["id"]].get(v, 0) for v in C.VERDICTS},
               "n_rows": sum(1 for r in rows if r["track"] == t["id"])} for t in doc["tracks"]]  # fmt: skip
    wins = [_brief(r) for r in rows if r["verdict"] == "bat" and counts_in_headline(r)]
    return {
        "schema": doc["schema"],
        "headline": h,
        "headline_rule_fr": "Comparaisons jugées contre un prévisionniste officiel (point publié, densité équitable autour "
        "de son point, ou ensemble officiel). Les repères faibles et les références naïves sont comptés à part.",
        "by_evaluation": {k: {v: c.get(v, 0) for v in C.VERDICTS} for k, c in by_eval.items()},
        "weak_reference": {v: weak.get(v, 0) for v in C.VERDICTS},
        "naive": {v: naive.get(v, 0) for v in C.VERDICTS},
        "cells_summary": dict(Counter(c["status"] for c in cs.values() if c["forecaster"])),
        "n_rows": len(rows),
        "wins": wins,
        "forecasters": [f for f in official if f["id"] in used_f],
        "matrix": matrix_rows,
        "tracks": tracks,
        "calendar": _calendar(doc, sealed),
        "vocab": {
            k: doc[k] for k in ("verdicts", "evaluations", "benchmark_kinds", "candidate_kinds", "metrics", "caveats")
        },
        "sources": doc["sources"],
        "stale_sources": stale_sources(doc, root),
        "sealed_models": dict(sealed) if sealed is not None else None,
    }


def _brief(r: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("id", "track", "target", "forecaster", "candidate_label_fr", "benchmark_label_fr", "candidate_kind",
            "evaluation", "metric", "ratio", "p_holm", "horizon_label_fr", "caveats")  # fmt: skip
    return {k: r.get(k) for k in keys}


def cell_detail(doc: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    rows = [r for r in doc["rows"] if cell_key(r) == key]
    if not rows:
        return None
    return {"key": key, "cell": cells(rows)[key], "rows": rows}


def track_detail(doc: Mapping[str, Any], tid: str, *, sealed: Mapping[str, int] | None = None,
                 root: Path = REPO) -> dict[str, Any] | None:  # fmt: skip
    meta = next((t for t in doc["tracks"] if t["id"] == tid), None)
    if meta is None:
        return None
    rows = [{**r, "sealed": _sealed(r, sealed)} for r in doc["rows"] if r["track"] == tid]
    fams: dict[tuple[Any, Any], list[str]] = defaultdict(list)
    for r in rows:
        fams[(r.get("window"), r.get("holm_family_size"))].append(r["id"])
    return {
        "track": meta,
        "counts": {v: sum(1 for r in rows if r["verdict"] == v and counts_in_headline(r)) for v in C.VERDICTS},
        "rows": rows,
        "holm_families": [{"window": w, "size": n, "rows": len(ids)} for (w, n), ids in fams.items()],
        "charts": charts(tid, rows, root),
        "calendar": [g for g in _calendar(doc, sealed) if g["track"] == tid],
    }


def charts(tid: str, rows: Sequence[Mapping[str, Any]], root: Path = REPO) -> list[dict[str, Any]]:
    """Chart-ready series copied from the track's result files (nothing computed beyond selection)."""
    f = _Files(root)
    out: list[dict[str, Any]] = []
    if tid == "beat_macro":
        rel = TRACK_FILES_FOR_CHARTS[tid]
        d = f.doc(rel)
        if d:
            desc = d.get("descriptive", {})
            hyp = {(r["source"]["pointer"], r["id"]) for r in rows if r.get("window") == "holdout"}
            for i, r in enumerate(d.get("results", [])):
                rid = next((x for p, x in hyp if p == f"/results/{i}"), None)
                bm = next((b for b in desc.get("by_month", []) if b.get("task") == r["task"]
                           and b["candidate"] == r["candidate"] and b["benchmark"] == r["benchmark"]
                           and b["loss"] == r["loss"] and any(x["variable"] == r["variable"] for x in b["rows"])), None)  # fmt: skip
                if bm:
                    out.append({
                        "kind": "ratio_by", "row": rid, "x_label_fr": "mois de l'origine (1 = janvier)",
                        "title_fr": f"Ratio selon le mois d'origine — {r['candidate']} vs {r['benchmark']} ({r['loss']})",
                        "points": [{"x": x["month"], "y": x["relative"], "n": x["n"]} for x in bm["rows"]
                                   if x["variable"] == r["variable"]],
                        "source": {"file": rel, "pointer": "/descriptive/by_month"},
                    })  # fmt: skip
            for s in desc.get("summaries", []):
                if s.get("pit_hist10"):
                    out.append({
                        "kind": "pit", "title_fr": f"PIT holdout — {s['model']} ({s['task']}, {s['variable']})",
                        "model": s["model"], "task": s["task"], "variable": s["variable"], "counts": s["pit_hist10"],
                        "coverage80": s.get("coverage80"), "n": s.get("n"),
                        "source": {"file": rel, "pointer": "/descriptive/summaries"},
                    })  # fmt: skip
    if tid == "beat_energy":
        rel = TRACK_FILES_FOR_CHARTS[tid]
        d = f.doc(rel)
        if d:
            by: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
            for x in d.get("informational_w1_per_offset", []):
                by[(x["code"], int(x["h"]))].append({"x": x["offset"], "y": x["ratio"], "n": x["n"]})
            for (code, h), pts in sorted(by.items()):
                out.append({
                    "kind": "ratio_by", "row": None, "x_label_fr": "jours après la publication du STEO",
                    "title_fr": f"{code} h{h} — nowcast quotidien / STEO (MAE) selon le délai (informatif, hors famille)",
                    "points": sorted(pts, key=lambda p: p["x"]),
                    "source": {"file": rel, "pointer": "/informational_w1_per_offset"},
                })  # fmt: skip
    if tid == "beat_health":
        for r in rows:
            if r.get("by_horizon"):
                out.append({
                    "kind": "ratio_by", "row": r["id"], "x_label_fr": "horizon (semaines)",
                    "title_fr": f"{r['candidate_label_fr']} — WIS relatif par horizon ({r['target']})",
                    "points": [{"x": int(k), "y": v, "n": None} for k, v in sorted(r["by_horizon"].items(), key=lambda kv: int(kv[0]))],
                    "source": r["source"],
                })  # fmt: skip
    cov = [r for r in rows if r.get("coverage")]
    if cov:
        out.append({
            "kind": "coverage", "title_fr": "Couverture des intervalles : observée vs nominale",
            "points": [{"row": r["id"], "label": r["candidate_label_fr"], "nominal": float(k), "observed": v,
                        "benchmark_observed": (r.get("benchmark_coverage") or {}).get(k)}
                       for r in cov for k, v in (r["coverage"] or {}).items() if v is not None],
        })  # fmt: skip
    return out


def report_text(doc: Mapping[str, Any], rel: str, root: Path = REPO) -> dict[str, Any] | None:
    """Text of a report/protocol referenced by the registry (whitelist: never an arbitrary path)."""
    allowed = {t.get("report") for t in doc["tracks"]} | {(t.get("protocol") or {}).get("file") for t in doc["tracks"]}
    allowed |= {r.get("report") for r in doc["rows"]} | {(r.get("protocol") or {}).get("file") for r in doc["rows"]}
    allowed |= {(r.get("verdict_quote") or {}).get("file") for r in doc["rows"]}
    allowed |= {n["quote"]["file"] for n in doc["no_track"]}
    allowed.discard(None)
    if rel not in allowed or not (rel.endswith(".md") or rel.endswith(".yaml")):
        return None
    f = _Files(root)
    t = f.text(rel)
    if t is None:
        return None
    return {"path": rel, "sha256": f.sha(rel), "text": t, "format": "markdown" if rel.endswith(".md") else "yaml"}
