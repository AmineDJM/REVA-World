"""Loader and validator of ``registry/scoreboard/tracks.yaml`` (the V8 scoreboard registry).

The validator is the guard against a scoreboard that claims more than the evidence: a row can say ``bat`` only
when its source file exists, the pointed record carries a true win flag, the ratio is below 1 and the
evaluation is not a development stage; a row can *downgrade* a source's win only with a verbatim quote from the
report; quotes must appear verbatim (whitespace-normalised) in their file; numbers must equal the source's; a
computed Holm p must agree with the source's own Holm decision. ``validate`` returns the list of problems (empty
= valid); ``load`` refuses an invalid file.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.v8.scores import catalog as C

REPO = Path(__file__).resolve().parents[4]
PATH = "registry/scoreboard/tracks.yaml"
REQUIRED = ("id", "track", "target", "verdict", "evaluation", "benchmark_kind", "candidate_kind", "metric", "source")
TOL = 1e-6


class ScoreboardInvalid(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} problem(s) in {PATH}: " + "; ".join(problems[:5]))
        self.problems = problems


def _norm(s: str) -> str:
    return " ".join(s.split())


def _pointer(doc: Any, pointer: str) -> Any:
    cur = doc
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(key)]
        elif isinstance(cur, Mapping):
            cur = cur[key]
        else:
            raise KeyError(pointer)
    return cur


def _field(rec: Any, path: str) -> Any:
    cur = rec
    for k in path.split("/"):
        cur = cur.get(k) if isinstance(cur, Mapping) else None
    return cur


class _Files:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._text: dict[str, str | None] = {}
        self._doc: dict[str, Any] = {}

    def text(self, rel: str) -> str | None:
        if rel not in self._text:
            p = self.root / rel
            self._text[rel] = p.read_text(encoding="utf-8") if p.is_file() else None
        return self._text[rel]

    def doc(self, rel: str) -> Any:
        if rel not in self._doc:
            t = self.text(rel)
            self._doc[rel] = None if t is None else (json.loads(t) if rel.endswith(".json") else yaml.safe_load(t))
        return self._doc[rel]

    def sha(self, rel: str) -> str | None:
        p = self.root / rel
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None

    def has_quote(self, rel: str, text: str) -> bool:
        t = self.text(rel)
        return t is not None and _norm(text) in _norm(t)


def _close(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= TOL * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return False


def validate(doc: Mapping[str, Any], root: Path = REPO) -> list[str]:
    """Every problem found (empty list = valid)."""
    out: list[str] = []
    if doc.get("schema") != C.SCHEMA:
        out.append(f"schema must be {C.SCHEMA}")
    f = _Files(root)
    fids = {x["id"] for x in doc.get("forecasters", [])}
    targets = {x["id"] for x in doc.get("targets", [])}
    tracks = {x["id"] for x in doc.get("tracks", [])}
    caveats = set(doc.get("caveats", {}))
    seen: set[str] = set()
    for r in doc.get("rows", []):
        rid = r.get("id", "?")
        miss = [k for k in REQUIRED if r.get(k) in (None, "")]
        if miss:
            out.append(f"{rid}: missing {miss}")
            continue
        if rid in seen:
            out.append(f"{rid}: duplicate id")
        seen.add(rid)
        v, ev, bk = r["verdict"], r["evaluation"], r["benchmark_kind"]
        if v not in C.VERDICTS:
            out.append(f"{rid}: unknown verdict {v}")
        if ev not in C.EVALUATIONS:
            out.append(f"{rid}: unknown evaluation {ev}")
        if bk not in C.BENCHMARK_KINDS:
            out.append(f"{rid}: unknown benchmark kind {bk}")
        if r["candidate_kind"] not in C.CANDIDATE_KINDS:
            out.append(f"{rid}: unknown candidate kind")
        if r["metric"] not in C.METRICS:
            out.append(f"{rid}: unknown metric {r['metric']}")
        if r["target"] not in targets:
            out.append(f"{rid}: unknown target {r['target']}")
        if r["track"] not in tracks:
            out.append(f"{rid}: unknown track {r['track']}")
        fc = r.get("forecaster")
        if bk != "naive" and fc not in fids:
            out.append(f"{rid}: comparison with an official forecaster needs a known forecaster (got {fc})")
        for c in r.get("caveats") or []:
            if c not in caveats:
                out.append(f"{rid}: unknown caveat {c}")
        out += _check_source(r, f)
        if v == "en_attente":
            res = r.get("resolution") or {}
            if not (res.get("date") or res.get("month") or res.get("not_before") or res.get("note_fr")):
                out.append(f"{rid}: pending without a resolution date or rule")
            q = res.get("quote")
            if q and not f.has_quote(q["file"], q["text"]):
                out.append(f"{rid}: resolution quote not found verbatim in {q['file']}")
            if r.get("ratio") is not None:
                out.append(f"{rid}: pending row cannot carry a ratio")
        if v == "bat":
            if ev == "development":
                out.append(f"{rid}: a development result cannot be a win (selection data, not evidence)")
            if r.get("ratio") is None or float(r["ratio"]) >= 1:
                out.append(f"{rid}: a win needs a ratio < 1")
            if "quote" in r["source"]:
                out.append(f"{rid}: a win needs a structured result file, not a quote")
        if v == "egalite" and not r.get("verdict_quote"):
            out.append(f"{rid}: 'égalité' must be quoted from the report")
        vq = r.get("verdict_quote")
        if vq and not f.has_quote(vq["file"], vq["text"]):
            out.append(f"{rid}: verdict quote not found verbatim in {vq['file']}")
        for k in ("report",):
            if r.get(k) and f.text(r[k]) is None:
                out.append(f"{rid}: {k} file {r[k]} missing")
        proto = r.get("protocol")
        if proto and f.text(proto["file"]) is None:
            out.append(f"{rid}: protocol file {proto['file']} missing")
    for n in doc.get("no_track", []):
        q = n.get("quote")
        if q and not f.has_quote(q["file"], q["text"]):
            out.append(f"no_track {n.get('target')}: quote not found in {q['file']}")
    for e in doc.get("events", []):
        q = e.get("quote")
        if not q or not f.has_quote(q["file"], q["text"]):
            out.append(f"event {e.get('id')}: quote not found")
    return out


def _check_source(r: Mapping[str, Any], f: _Files) -> list[str]:
    rid, s, v = r["id"], r["source"], r["verdict"]
    rel = s.get("file")
    if not rel or f.text(rel) is None:
        return [f"{rid}: source file {rel} missing — no verdict without its source"]
    if "quote" in s:
        if not f.has_quote(rel, s["quote"]):
            return [f"{rid}: source quote not found verbatim in {rel}"]
        if v == "bat":
            return [f"{rid}: a quote cannot support a win"]
        return []
    doc = f.doc(rel)
    try:
        rec = _pointer(doc, s["pointer"])
    except (KeyError, IndexError, ValueError, TypeError):
        return [f"{rid}: pointer {s.get('pointer')} not found in {rel}"]
    out: list[str] = []
    fields = s.get("fields") or {}
    if "ratio" in fields and not _close(r.get("ratio"), _field(rec, fields["ratio"])):
        out.append(f"{rid}: ratio {r.get('ratio')} differs from source {_field(rec, fields['ratio'])}")
    if "p" in fields and not _close(r.get("p_raw"), _field(rec, fields["p"])):
        out.append(f"{rid}: p {r.get('p_raw')} differs from source")
    if "win" in fields:
        won = bool(_field(rec, fields["win"]))
        if v == "bat" and not won:
            out.append(f"{rid}: claims a win the source does not record ({fields['win']} is false)")
        if won and v != "bat" and not r.get("verdict_quote"):
            out.append(f"{rid}: source records a win; a downgrade needs a verbatim report quote")
    elif v == "bat":
        out.append(f"{rid}: a win needs a source win flag")
    if r.get("p_holm_basis") == "computed" and r.get("p_holm") is not None and r.get("holm_rejected") is not None:
        if (float(r["p_holm"]) < 0.05) != bool(r["holm_rejected"]):
            out.append(f"{rid}: computed Holm p {r['p_holm']} disagrees with the source's Holm decision")
    return out


# --------------------------------------------------------------------------------------------- read side
def counts_in_headline(r: Mapping[str, Any]) -> bool:
    return r.get("benchmark_kind") in C.HEADLINE_BENCHMARKS


def headline(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Counts of judged comparisons against official forecasters (weak references and naive excluded)."""
    c = Counter(r["verdict"] for r in rows if counts_in_headline(r))
    return {k: c.get(k, 0) for k in C.VERDICTS}


def cell_key(r: Mapping[str, Any]) -> str:
    return f"{r['target']}~{r.get('forecaster') or 'naive'}"


def cells(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Matrix cells (target × forecaster) over every row; counts per verdict, ratio range, evaluation kinds."""
    by: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by[cell_key(r)].append(r)
    out: dict[str, dict[str, Any]] = {}
    for k, rs in by.items():
        head = [r for r in rs if counts_in_headline(r)]
        ratios = [float(r["ratio"]) for r in head if r.get("ratio") is not None]
        ratios += [float(x) for r in head for x in (r.get("ratio_range") or [])]
        cnt = Counter(r["verdict"] for r in head)
        weak = Counter(r["verdict"] for r in rs if r.get("benchmark_kind") == "weak_reference")
        out[k] = {
            "key": k,
            "target": rs[0]["target"],
            "forecaster": rs[0].get("forecaster"),
            "counts": {v: cnt.get(v, 0) for v in C.VERDICTS},
            "weak_reference": {v: weak.get(v, 0) for v in C.VERDICTS if weak.get(v)},
            "n_rows": len(rs),
            "ratio_min": min(ratios) if ratios else None,
            "ratio_max": max(ratios) if ratios else None,
            "evaluations": sorted({r["evaluation"] for r in head}),
            "tracks": sorted({r["track"] for r in rs}),
            "status": _status(cnt, bool(weak)),
            "wins": [r["id"] for r in head if r["verdict"] == "bat"],
        }
    return out


def _status(cnt: Counter[str], weak_only: bool) -> str:
    if sum(cnt.values()) == 0:
        return "weak_only" if weak_only else "none"
    if cnt.get("bat"):
        return "bat"
    if cnt.get("en_attente") and not (cnt.get("ne_bat_pas") or cnt.get("egalite")):
        return "en_attente"
    if cnt.get("ne_bat_pas"):
        return "ne_bat_pas"
    if cnt.get("egalite"):
        return "egalite"
    return "en_attente"


def stale_sources(doc: Mapping[str, Any], root: Path = REPO) -> list[dict[str, Any]]:
    """Source files whose bytes changed since the build (the rows may no longer match: rebuild)."""
    f = _Files(root)
    out = []
    for s in doc.get("sources", []):
        now = f.sha(s["file"])
        if now != s["sha256"]:
            out.append({"file": s["file"], "built_sha256": s["sha256"], "current_sha256": now})
    return out


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float) -> dict[str, Any]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    problems = validate(doc, REPO)
    if problems:
        raise ScoreboardInvalid(problems)
    return dict(doc)


def load(root: Path = REPO) -> dict[str, Any]:
    """The validated scoreboard (cached until the file changes). Raises ScoreboardInvalid."""
    p = root / PATH
    return _load_cached(str(p), p.stat().st_mtime)
