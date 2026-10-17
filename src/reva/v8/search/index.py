"""Query side of the V8 search index (read-only SQLite + an in-memory layer for popular records).

Tiers (best first): ``id`` exact identifier/code, ``exact`` whole label/alias, ``prefix`` a label starts with the
query, ``word`` a word of a label starts with the query (or every query word does), ``trigram`` substring or fuzzy
trigram similarity (fuzzy on popular records only). Tiers are ordered strictly; within a tier the record's prior
(kind weight, Wikidata sitelinks) and then the shorter label win. Lower tiers are only read when the better ones
hold fewer than ``limit`` hits. One hit per record; nothing is merged by name.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from reva.v8.search import hrefs
from reva.v8.search.build import GROUP_ORDER, GROUPS, ORIGINS, SEARCH_KINDS, index_path, kind_fr
from reva.v8.search.normalize import norm_code, norm_text, trigrams

TIER_BASE = {"id": 0.85, "exact": 0.75, "prefix": 0.6, "word": 0.48, "trigram": 0.2}
TIER_RANK = {"id": 0, "exact": 1, "prefix": 2, "word": 3, "trigram": 4}
PRIOR_WEIGHT = 0.09
SQL_PREFIX_LIMIT = 400
SQL_FTS_LIMIT = 1500
FUZZY_MIN = 0.42
MAX_LIMIT = 100


class IndexMissing(Exception):
    """The index file does not exist (run ``uv run reva v8 index build``)."""


@dataclass
class Hit:
    rid: int
    tier: str
    matched: str
    sim: float = 1.0
    score: float = 0.0


def score_of(tier: str, prior: float, name_len: int, q_len: int, sim: float = 1.0) -> float:
    base = TIER_BASE[tier] + (0.25 * sim if tier == "trigram" else 0.0)
    length_pen = 0.0 if tier in ("id", "exact") else 0.03 * min(1.0, max(0, name_len - q_len) / 40)
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
        self.strs: dict[int, str] = dict(db.execute("SELECT sid, s FROM strs").fetchall())
        pop = dict(db.execute("SELECT k, v FROM pop").fetchall())
        self.pop_rid = np.frombuffer(pop["rid"], dtype=np.int64)
        self.pop_prior = np.frombuffer(pop["prior"], dtype=np.float32)
        self.pop_kind = np.frombuffer(pop["kind"], dtype=np.int8)
        self.ntri = np.frombuffer(pop["ntri"], dtype=np.int32)
        self.pop_norm = pop["norm"].decode().split("\n") if self.pop_rid.size else []
        self.pop_raw = pop["raw"].decode().split("\n") if self.pop_rid.size else []
        self.postings = {t: np.frombuffer(b, dtype=np.int32) for t, b in db.execute("SELECT tri, idx FROM pop_tri")}

    # ------------------------------------------------------------------ plumbing
    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            db.execute("PRAGMA mmap_size = 536870912")
            db.execute("PRAGMA cache_size = -32768")
            self._local.db = db
        return db

    def status(self) -> dict[str, Any]:
        return {
            "built": True,
            "built_at": self.meta.get("built_at"),
            "version": self.meta.get("version"),
            "documents": self.meta.get("documents"),
            "names": self.meta.get("names"),
            "codes": self.meta.get("codes"),
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
        limit = max(1, min(int(limit), MAX_LIMIT))
        qn = norm_text(q)
        qc = norm_code(q)
        kindset = {k for k in (kinds or ()) if k in SEARCH_KINDS} or None
        found: dict[int, Hit] = {}
        if not qn and not qc:
            return self._result(q, qn, [], kindset, limit, lang, origin)
        db = self._db()
        kind_ids = {SEARCH_KINDS.index(k) for k in kindset} if kindset else None

        def put(rid: int, tier: str, matched: str, sim: float = 1.0) -> None:
            h = found.get(rid)
            if h is None or TIER_RANK[tier] < TIER_RANK[h.tier] or (tier == h.tier == "trigram" and sim > h.sim):
                found[rid] = Hit(rid, tier, matched, sim)

        def enough(tier: str) -> bool:
            # filtered queries cannot count SQL hits before reading their docs: read every tier
            if kindset or origin:
                return False
            r = TIER_RANK[tier]
            return sum(1 for h in found.values() if TIER_RANK[h.tier] <= r) >= limit

        for key in {qc, qn.replace(" ", "")} - {""}:
            for rid, raw in db.execute(
                "SELECT rid, COALESCE(raw, upper(norm)) FROM code WHERE norm = ? LIMIT 100", (key,)
            ):
                put(rid, "id", raw)
        if qn:
            for rid, raw in db.execute("SELECT rid, COALESCE(raw, norm) FROM name WHERE norm = ? LIMIT 300", (qn,)):
                put(rid, "exact", raw)
            self._pop_prefix(qn, put, kind_ids)
            if not enough("prefix"):
                for rid, raw in db.execute(
                    "SELECT rid, COALESCE(raw, norm) FROM name WHERE norm > ? AND norm < ? LIMIT ?",
                    (qn, qn + "\U0010ffff", SQL_PREFIX_LIMIT),
                ):
                    put(rid, "prefix", raw)
            qwords = qn.split(" ")
            if not enough("prefix"):
                self._pop_contains(qn, qwords, put, kind_ids)
            fts_words = [w for w in qwords if len(w) >= 3]
            if fts_words and not enough("word"):
                expr = " AND ".join('"' + w.replace('"', "") + '"' for w in fts_words)
                for rid, norm, raw in db.execute(
                    "SELECT n.rid, n.norm, COALESCE(n.raw, n.norm) FROM name_fts f JOIN name n ON n.nid = f.rowid "
                    "WHERE name_fts MATCH ? LIMIT ?",
                    (expr, SQL_FTS_LIMIT),
                ):
                    tier = contained_tier(qn, qwords, norm)
                    if tier:
                        put(rid, tier, raw)
            if len(qn) >= 4 and not enough("word"):
                self._fuzzy(qn, put, kind_ids)
        return self._result(q, qn, list(found.values()), kindset, limit, lang, origin)

    def _pop_ok(self, i: int, kind_ids: set[int] | None) -> bool:
        return kind_ids is None or int(self.pop_kind[i]) in kind_ids

    def _pop_prefix(self, qn: str, put: Callable[..., None], kind_ids: set[int] | None) -> None:
        lo = bisect.bisect_left(self.pop_norm, qn)
        hi = bisect.bisect_left(self.pop_norm, qn + "\U0010ffff")
        for i in range(lo, min(hi, lo + 5000)):
            if self._pop_ok(i, kind_ids):
                put(int(self.pop_rid[i]), "exact" if self.pop_norm[i] == qn else "prefix", self.pop_raw[i])

    def _pop_contains(self, qn: str, qwords: list[str], put: Callable[..., None], kind_ids: set[int] | None) -> None:
        """Word / substring matches among popular names: candidates hold every trigram of the query words."""
        tg = sorted({t for w in qwords for t in trigrams(w) if " " not in t})
        if not tg:
            return
        lists = [self.postings.get(t) for t in tg]
        arrs = sorted((x for x in lists if x is not None), key=len)
        if len(arrs) < len(lists):
            return
        cand = arrs[0]
        for arr in arrs[1:6]:
            cand = np.intersect1d(cand, arr, assume_unique=True)
            if cand.size == 0:
                return
        for c in cand[:5000]:
            i = int(c)
            if not self._pop_ok(i, kind_ids):
                continue
            tier = contained_tier(qn, qwords, self.pop_norm[i])
            if tier:
                put(int(self.pop_rid[i]), tier, self.pop_raw[i])

    def _fuzzy(self, qn: str, put: Callable[..., None], kind_ids: set[int] | None) -> None:
        tg = list(trigrams(qn))
        arrs = [self.postings[t] for t in tg if t in self.postings]
        if not arrs:
            return
        idx, overlap = np.unique(np.concatenate(arrs), return_counts=True)
        sim = overlap / (len(tg) + self.ntri[idx] - overlap)
        keep = np.nonzero(sim >= FUZZY_MIN)[0]
        if keep.size > 300:
            keep = keep[np.argsort(-sim[keep])[:300]]
        for k in keep:
            i = int(idx[k])
            if self._pop_ok(i, kind_ids):
                put(int(self.pop_rid[i]), "trigram", self.pop_raw[i], float(sim[k]))

    # ------------------------------------------------------------------ assembly
    def _docs(self, rids: list[int]) -> dict[int, tuple[Any, ...]]:
        db = self._db()
        docs: dict[int, tuple[Any, ...]] = {}
        for i in range(0, len(rids), 900):
            chunk = rids[i : i + 900]
            for row in db.execute(
                "SELECT rid, kind, id, label_fr, label_en, sublabel, href, ek, src, origin, prior, extra "
                f"FROM doc WHERE rid IN ({','.join('?' * len(chunk))})",
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
        return docs

    def _result(
        self,
        q: str,
        qn: str,
        hits: list[Hit],
        kindset: set[str] | None,
        limit: int,
        lang: str,
        origin: str | None,
    ) -> dict[str, Any]:
        # tiers are strict: once ``limit`` hits are reached at some tier, lower tiers cannot enter the page
        hits.sort(key=lambda h: TIER_RANK[h.tier])
        more = False
        if not kindset and not origin and len(hits) > limit:
            cut = TIER_RANK[hits[limit - 1].tier]
            keep = [h for h in hits if TIER_RANK[h.tier] <= cut]
            more = True
            hits = keep
        docs = self._docs([h.rid for h in hits])
        out = []
        for h in hits:
            d = docs.get(h.rid)
            if d is None or (kindset and d[1] not in kindset) or (origin and d[9] != origin):
                continue
            label = (d[3] if lang == "fr" else d[4]) or d[4] or d[3] or d[2]
            h.score = score_of(h.tier, d[10], len(h.matched), len(qn), h.sim)
            out.append((h, d, label))
        out.sort(key=lambda x: (-x[0].score, len(x[2]), x[1][2]))
        page = [_hit_json(h, d, label) for h, d, label in out[:limit]]
        counts: dict[str, int] = {}
        for x in page:
            counts[x["group"]] = counts.get(x["group"], 0) + 1
        return {
            "query": q,
            "normalized": qn,
            "hits": page,
            "groups": [{"group": g, "count": counts[g]} for g in GROUP_ORDER if g in counts],
            "truncated": more or len(out) > limit,
        }


def contained_tier(qn: str, qwords: list[str], norm: str) -> str | None:
    """How a (normalised) name contains the (normalised) query: exact, prefix, word, trigram (substring) or not."""
    if norm == qn:
        return "exact"
    if norm.startswith(qn):
        return "prefix"
    padded = " " + norm
    if (" " + qn) in padded or all((" " + w) in padded for w in qwords):
        return "word"
    if qn in norm or all(w in norm for w in qwords):
        return "trigram"
    return None


def _hit_json(h: Hit, d: tuple[Any, ...], label: str) -> dict[str, Any]:
    _rid, kind, id_, _lf, _le, sub, href, ek, source, origin, _prior, extra = d
    if href is None:  # compact KG record: sublabel and href rebuilt here
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
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            _CACHE.pop(key, None)
            raise IndexMissing(key) from None
        if cur is not None and cur.mtime == mtime:
            return cur
        idx = SearchIndex(p)
        _CACHE[key] = idx
        return idx
