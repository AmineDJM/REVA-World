"""Query side of the V8 search index (read-only SQLite + a small in-memory layer for popular records).

Tiers (best first): ``id`` exact identifier/code, ``exact`` whole label/alias, ``prefix`` label starts with the
query, ``word`` a word of the label starts with the query (or every query word does), ``trigram`` substring or
fuzzy trigram similarity (popular records only). Within a tier the record's prior (kind, Wikidata sitelinks)
orders hits. One hit per record; nothing is merged by name.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from reva.v8.search import hrefs
from reva.v8.search.build import GROUP_ORDER, GROUPS, ORIGINS, SEARCH_KINDS, index_path, kind_fr
from reva.v8.search.normalize import norm_code, norm_text, trigrams

TIER_BASE = {"id": 0.85, "exact": 0.75, "prefix": 0.55, "word": 0.45, "trigram": 0.25}
TIER_RANK = {"id": 0, "exact": 1, "prefix": 2, "word": 3, "trigram": 4}
PRIOR_WEIGHT = 0.14
SQL_PREFIX_LIMIT = 400
SQL_FTS_LIMIT = 1500
FUZZY_MIN = 0.42


class IndexMissing(Exception):
    """The index file does not exist (run ``uv run reva v8 index build``)."""


@dataclass
class Hit:
    rid: int
    tier: str
    matched: str
    score: float = 0.0


def _score(tier: str, prior: float, name_len: int, q_len: int, sim: float = 1.0) -> float:
    base = TIER_BASE[tier]
    if tier == "trigram":
        base = 0.12 + 0.18 * sim
    length_pen = 0.0 if tier in ("id", "exact") else 0.06 * min(1.0, max(0, name_len - q_len) / 40)
    return round(min(0.999, base + PRIOR_WEIGHT * prior - length_pen), 4)


class SearchIndex:
    """Thread-safe reader. Each thread gets its own read-only SQLite connection."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise IndexMissing(str(path))
        self.path = path
        self.mtime = path.stat().st_mtime
        self._local = threading.local()
        db = self._db()
        self.meta = {k: json.loads(v) for k, v in db.execute("SELECT k, v FROM meta")}
        self.strs = dict(db.execute("SELECT sid, s FROM strs").fetchall())
        # popular names in memory: sorted norms (prefix), trigram postings (fuzzy / substring candidates)
        rows = db.execute(
            "SELECT n.nid, n.rid, n.norm, n.raw, d.prior, d.kind FROM name n JOIN doc d ON d.rid = n.rid "
            "WHERE n.pop = 1"
        ).fetchall()
        self.pop_rid = np.array([r[1] for r in rows], dtype=np.int64)
        self.pop_norm = [r[2] for r in rows]
        self.pop_raw = [r[3] for r in rows]
        self.pop_prior = np.array([r[4] for r in rows], dtype=np.float32)
        order = sorted(range(len(rows)), key=lambda i: self.pop_norm[i])
        self.sorted_norms = [self.pop_norm[i] for i in order]
        self.sorted_idx = order
        post: dict[str, list[int]] = {}
        ntri = np.zeros(len(rows), dtype=np.int32)
        for i, n in enumerate(self.pop_norm):
            tg = trigrams(n)
            ntri[i] = len(tg)
            for t in tg:
                post.setdefault(t, []).append(i)
        self.postings = {t: np.array(v, dtype=np.int32) for t, v in post.items()}
        self.ntri = ntri

    # ------------------------------------------------------------------ plumbing
    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            db.execute("PRAGMA mmap_size = 536870912")
            db.execute("PRAGMA cache_size = -65536")
            self._local.db = db
        return db

    def status(self) -> dict[str, Any]:
        return {
            "built": True,
            "built_at": self.meta.get("built_at"),
            "version": self.meta.get("version"),
            "documents": self.meta.get("documents"),
            "names": self.meta.get("names"),
            "kinds": self.meta.get("kinds"),
            "errors": self.meta.get("errors") or [],
            "origin": self.meta.get("origin"),
            "path_bytes": self.path.stat().st_size,
            "popular_names_in_memory": len(self.pop_norm),
        }

    # ------------------------------------------------------------------ search
    def search(
        self,
        q: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
        lang: str = "fr",
        origin: str | None = None,
    ) -> dict[str, Any]:
        qn = norm_text(q)
        qc = norm_code(q)
        kindset = {k for k in (kinds or ()) if k in SEARCH_KINDS} or None
        found: dict[int, Hit] = {}
        if not qn and not qc:
            return self._result(q, qn, [], kindset, limit, lang, origin, total=0)
        db = self._db()

        def put(rid: int, tier: str, matched: str, score_sim: float = 1.0) -> None:
            h = found.get(rid)
            if (
                h is None
                or TIER_RANK[tier] < TIER_RANK[h.tier]
                or (tier == h.tier == "trigram" and score_sim > h.score)
            ):
                found[rid] = Hit(rid, tier, matched, score_sim if tier == "trigram" else 0.0)

        # 1. identifiers / codes, 2. exact labels
        for key in {qc, qn.replace(" ", "")} - {""}:
            for rid, raw in db.execute(
                "SELECT rid, COALESCE(raw, upper(norm)) FROM code WHERE norm = ? LIMIT 100", (key,)
            ):
                put(rid, "id", raw)
        if qn:
            for rid, raw in db.execute("SELECT rid, COALESCE(raw, norm) FROM name WHERE norm = ? LIMIT 300", (qn,)):
                put(rid, "exact", raw)
        if qn:
            self._pop_prefix_word(qn, put)
            # long tail: alphabetical prefix range + FTS substring
            hi = qn + "\U0010ffff"
            for rid, raw in db.execute(
                "SELECT rid, COALESCE(raw, norm) FROM name WHERE norm > ? AND norm < ? LIMIT ?",
                (qn, hi, SQL_PREFIX_LIMIT),
            ):
                put(rid, "prefix", raw)
            qwords = qn.split(" ")
            fts_words = [w for w in qwords if len(w) >= 3]
            if fts_words:
                expr = " AND ".join('"' + w.replace('"', "") + '"' for w in fts_words)
                for rid, norm, raw in db.execute(
                    "SELECT n.rid, n.norm, COALESCE(n.raw, n.norm) FROM name_fts f JOIN name n ON n.nid = f.rowid "
                    "WHERE name_fts MATCH ? LIMIT ?",
                    (expr, SQL_FTS_LIMIT),
                ):
                    tier = self._contained_tier(qn, qwords, norm)
                    if tier:
                        put(rid, tier, raw)
            strong = sum(1 for h in found.values() if TIER_RANK[h.tier] <= TIER_RANK["word"])
            if len(qn) >= 4 and strong < limit:
                self._fuzzy(qn, put)
        return self._result(q, qn, list(found.values()), kindset, limit, lang, origin, total=len(found))

    @staticmethod
    def _contained_tier(qn: str, qwords: list[str], norm: str) -> str | None:
        if norm.startswith(qn):
            return "prefix"
        padded = " " + norm
        if (" " + qn) in padded:
            return "word"
        if all((" " + w) in padded for w in qwords):
            return "word"
        if qn in norm or all(w in norm for w in qwords):
            return "trigram"
        return None

    def _pop_prefix_word(self, qn: str, put: Any) -> None:
        lo = bisect.bisect_left(self.sorted_norms, qn)
        hi = bisect.bisect_left(self.sorted_norms, qn + "\U0010ffff")
        for j in range(lo, min(hi, lo + 5000)):
            i = self.sorted_idx[j]
            put(int(self.pop_rid[i]), "exact" if self.pop_norm[i] == qn else "prefix", self.pop_raw[i])
        # word / substring among popular names: candidates hold every trigram of the query
        tg = [t for t in trigrams(qn) if t.strip()]
        lists = [self.postings.get(t) for t in tg]
        if not tg or any(x is None for x in lists):
            return
        lists.sort(key=len)  # type: ignore[arg-type]
        cand = lists[0]
        for arr in lists[1:4]:
            cand = np.intersect1d(cand, arr, assume_unique=True)  # type: ignore[arg-type]
            if len(cand) == 0:
                return
        qwords = qn.split(" ")
        for i in cand[:5000]:
            tier = self._contained_tier(qn, qwords, self.pop_norm[i])
            if tier:
                put(int(self.pop_rid[i]), tier, self.pop_raw[i])

    def _fuzzy(self, qn: str, put: Any) -> None:
        tg = list(trigrams(qn))
        arrs = [self.postings[t] for t in tg if t in self.postings]
        if not arrs:
            return
        allp = np.concatenate(arrs)
        idx, overlap = np.unique(allp, return_counts=True)
        sim = overlap / (len(tg) + self.ntri[idx] - overlap)
        keep = np.nonzero(sim >= FUZZY_MIN)[0]
        if len(keep) > 300:
            keep = keep[np.argsort(-sim[keep])[:300]]
        for k in keep:
            i = int(idx[k])
            put(int(self.pop_rid[i]), "trigram", self.pop_raw[i], float(sim[k]))

    # ------------------------------------------------------------------ assembly
    def _result(
        self,
        q: str,
        qn: str,
        hits: list[Hit],
        kindset: set[str] | None,
        limit: int,
        lang: str,
        origin: str | None,
        total: int,
    ) -> dict[str, Any]:
        docs: dict[int, tuple[Any, ...]] = {}
        db = self._db()
        rids = [h.rid for h in hits]
        for i in range(0, len(rids), 900):
            chunk = rids[i : i + 900]
            for row in db.execute(
                f"SELECT rid, kind, id, label_fr, label_en, sublabel, href, ek, src, origin, prior, "
                f"extra FROM doc WHERE rid IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                docs[row[0]] = (
                    row[0],
                    SEARCH_KINDS[row[1]],
                    *row[2:7],
                    self.strs.get(row[7]),
                    self.strs.get(row[8]),
                    ORIGINS[row[9]],
                    row[10],
                    row[11],
                )
        out = []
        for h in hits:
            d = docs.get(h.rid)
            if d is None:
                continue
            if kindset and d[1] not in kindset:
                continue
            if origin and d[9] != origin:
                continue
            label = (d[3] if lang == "fr" else d[4]) or d[4] or d[3] or d[2]
            h.score = _score(h.tier, d[10], len(norm_text(h.matched)), len(qn), h.score if h.tier == "trigram" else 1.0)
            out.append((h, d, label))
        out.sort(key=lambda x: (-x[0].score, TIER_RANK[x[0].tier], len(x[2]), x[1][2]))
        n_match = len(out)
        out = out[:limit]
        result_hits = [_hit_json(h, d, label) for h, d, label in out]
        counts: dict[str, int] = {}
        for x in result_hits:
            counts[x["group"]] = counts.get(x["group"], 0) + 1
        groups = [{"group": g, "count": counts[g]} for g in GROUP_ORDER if g in counts]
        return {
            "query": q,
            "normalized": qn,
            "hits": result_hits,
            "groups": groups,
            "truncated": n_match > limit,
            "matches": n_match,
        }


def _hit_json(h: Hit, d: tuple[Any, ...], label: str) -> dict[str, Any]:
    _rid, kind, id_, _lf, _le, sub, href, ek, source, origin, _prior, extra = d
    if href is None:  # compact KG record
        sub = " · ".join(x for x in (kind_fr(ek), id_, sub) if x)
        href = hrefs.entity(ek or "entity", id_)
    return {
        "id": id_,
        "kind": kind,
        "label": label,
        "sublabel": sub or "",
        "href": href,
        "score": h.score,
        "group": GROUPS[kind],
        "match": h.tier,
        "matched": h.matched,
        "entity_kind": ek,
        "source": source,
        "origin": origin,
        "extra": json.loads(extra) if extra else {},
    }


_LOCK = threading.Lock()
_CACHE: dict[str, SearchIndex] = {}


def get_index(path: Path | None = None) -> SearchIndex:
    """Process-wide index, reopened when the file is rebuilt (mtime change)."""
    p = path or index_path()
    key = str(p)
    with _LOCK:
        cur = _CACHE.get(key)
        if cur is not None:
            try:
                if p.stat().st_mtime == cur.mtime:
                    return cur
            except FileNotFoundError:
                _CACHE.pop(key, None)
                raise IndexMissing(key) from None
        idx = SearchIndex(p)
        _CACHE[key] = idx
        return idx
