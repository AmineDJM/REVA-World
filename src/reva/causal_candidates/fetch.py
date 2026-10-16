"""Budget-metered MediaWiki API access for the causal-candidate extractor (agent CAUSCAND).

Source key ``wikipedia.causal_text`` (``registry/sources/wikipedia_causal_text.yaml``), hosts
``en.wikipedia.org`` and ``fr.wikipedia.org``, action API, GET only, no key, no personal data, generic
project User-Agent. Every request goes through ``reva.operations.budgets``: ``reserve_ingest`` before the
first request (a run that does not fit is refused, nothing fetched), ``metered`` per request (top-up or
refusal before the transport is called), ``settle_metered`` with the actual usage whatever happened
(invariant 30). The ledger lives in a throwaway ``reva_*`` database (never the dev database ``reva``).

Politeness: one request at a time, >= 10 s between requests to the same host (the rate limiter of the
client), ``maxlag=5``. A 429/503 is waited out once (Retry-After <= 120 s, counted as a new request);
a second one ends the run: what was done is checkpointed and a later run resumes.

Storage (CC BY-SA 4.0 text): full articles are **never** stored. Each response is hashed (``raw_sha256``,
recorded with the revision ids in ``fetch_log.jsonl``) and reduced in memory to the sentences that match the
connective prefilter (:func:`reva.causal_candidates.extract.sentence_records`). Any revision can be
re-read by its id (``revids=``), so the pin is exact without archiving the article.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from reva.causal_candidates.extract import (
    EXTRACTOR_VERSION,
    SOURCE_KEY,
    Resolution,
    SentenceRecord,
    record_to_dict,
    sentence_records,
)
from reva.kernel.errors import SourceResponseError

log = logging.getLogger("reva.causal_candidates")

HOSTS = {"en": "en.wikipedia.org", "fr": "fr.wikipedia.org"}
MIN_INTERVAL_SECONDS = 10.0
MAX_RESPONSE_BYTES = 12_000_000
CONTENT_BATCH = 4
RESOLVE_BATCH = 50
PROJECT_URL = "https://github.com/AmineDJM/REVA-World"
USER_AGENT = (
    f"REVA-World/0.3 (research world knowledge graph; {PROJECT_URL}) reva-causal-candidates/{EXTRACTOR_VERSION}"
)
REPO_ROOT = Path(__file__).resolve().parents[3]
BUDGET_REQUEST = REPO_ROOT / "registry" / "atlas" / "budget_requests" / "causcand.yaml"
DEFAULT_DB = "reva_causcand"


class RateLimitedStop(SourceResponseError):
    """Wikimedia throttled us twice in a row: the run stops (resumable)."""


def api_url(lang: str, params: Mapping[str, str]) -> str:
    base = {"action": "query", "format": "json", "formatversion": "2", "maxlag": "5"}
    return f"https://{HOSTS[lang]}/w/api.php?{urlencode({**base, **params})}"


def content_url(lang: str, titles: Sequence[str]) -> str:
    if not 0 < len(titles) <= 50:
        raise ValueError("1..50 titles per request")
    return api_url(
        lang,
        {
            "prop": "revisions|pageprops",
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "ppprop": "wikibase_item",
            "redirects": "1",
            "titles": "|".join(titles),
        },
    )


def revisions_url(lang: str, revids: Sequence[int]) -> str:
    """Re-read pinned revisions (reproduction of a stored candidate)."""
    return api_url(
        lang,
        {
            "prop": "revisions|pageprops",
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "ppprop": "wikibase_item",
            "revids": "|".join(str(int(r)) for r in revids),
        },
    )


def resolve_url(lang: str, titles: Sequence[str]) -> str:
    if not 0 < len(titles) <= 50:
        raise ValueError("1..50 titles per request")
    return api_url(
        lang,
        {"prop": "pageprops", "ppprop": "wikibase_item|disambiguation", "redirects": "1", "titles": "|".join(titles)},
    )


# ----------------------------------------------------------------------------- parsing


@dataclass
class Page:
    requested: str
    title: str
    page_id: int
    qid: str | None
    revision_id: int
    revision_timestamp: str
    wikitext: str


def _json(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise SourceResponseError("MediaWiki API answer is not JSON", retryable=False) from e
    if not isinstance(doc, dict):
        raise SourceResponseError("MediaWiki API answer is not an object", retryable=False)
    if "error" in doc:
        err = doc["error"] or {}
        code = str(err.get("code", ""))
        raise SourceResponseError(
            f"MediaWiki API error {code}: {str(err.get('info', ''))[:200]}",
            retryable=code in ("maxlag", "ratelimited"),
            kind=code or "api_error",
        )
    return doc


def _title_map(q: Mapping[str, Any]) -> dict[str, str]:
    """requested title -> final title (normalisation then redirects)."""
    norm = {d["from"]: d["to"] for d in q.get("normalized", []) or []}
    redir = {d["from"]: d["to"] for d in q.get("redirects", []) or []}

    def final(t: str) -> str:
        t = norm.get(t, t)
        seen = 0
        while t in redir and seen < 5:
            t = redir[t]
            seen += 1
        return t

    return {k: final(k) for k in (*norm, *redir)}


def parse_content(body: bytes, requested: Sequence[str]) -> tuple[list[Page], list[str]]:
    """Pages with their main-slot wikitext; returns (pages, requested titles without content)."""
    doc = _json(body)
    q = doc.get("query") or {}
    tm = _title_map(q)
    by_title = {p.get("title"): p for p in q.get("pages", []) or []}
    pages, missing = [], []
    for t in requested:
        p = by_title.get(tm.get(t, t))
        revs = (p or {}).get("revisions") or []
        content = ((revs[0].get("slots") or {}).get("main") or {}).get("content") if revs else None
        if not p or p.get("missing") or content is None:
            missing.append(t)
            continue
        pages.append(
            Page(
                requested=t,
                title=p["title"],
                page_id=int(p["pageid"]),
                qid=(p.get("pageprops") or {}).get("wikibase_item"),
                revision_id=int(revs[0]["revid"]),
                revision_timestamp=str(revs[0].get("timestamp", "")),
                wikitext=content,
            )
        )
    return pages, missing


def parse_resolve(body: bytes, requested: Sequence[str]) -> dict[str, Resolution]:
    doc = _json(body)
    q = doc.get("query") or {}
    tm = _title_map(q)
    redirected = {d["from"] for d in q.get("redirects", []) or []}
    norm = {d["from"]: d["to"] for d in q.get("normalized", []) or []}
    by_title = {p.get("title"): p for p in q.get("pages", []) or []}
    out: dict[str, Resolution] = {}
    for t in requested:
        final = tm.get(t, t)
        p = by_title.get(final)
        if p is None or p.get("missing") or p.get("invalid"):
            out[t] = Resolution(t, None, None)
            continue
        pp = p.get("pageprops") or {}
        out[t] = Resolution(
            target=t,
            title=p.get("title"),
            qid=pp.get("wikibase_item"),
            disambiguation="disambiguation" in pp,
            namespace=int(p.get("ns", 0)),
            redirected=norm.get(t, t) in redirected or t in redirected,
        )
    return out


# ----------------------------------------------------------------------------- budgeted session


def requested_budget_overlay() -> Callable[[Any], Any]:
    """``load_budget`` with this agent's *requested* per-source limit added (only for keys the pinned policy
    lacks). Used until ATLASCORE merges ``registry/atlas/budget_requests/causcand.yaml``; the pinned file is
    not modified and the overlay is visible in the ledger's policy version (``+causcand_request``)."""
    import yaml

    from reva.operations import budgets

    pinned = budgets.load_budget
    req = {r["source_key"]: int(r["daily_requests"]) for r in yaml.safe_load(BUDGET_REQUEST.read_text())["requests"]}

    def overlay(policy: Any = None) -> Any:
        b = pinned(policy)
        per = dict(b.per_source_requests)
        added = {k: v for k, v in req.items() if k not in per}
        if not added:
            return b
        per.update(added)
        return replace(b, per_source_requests=per, policy_version=b.policy_version + "+causcand_request")

    return overlay


@dataclass
class Session:
    http: Any  # MeteredHttp (live) or a fixture/fake client with ``async get``
    requests: int = 0
    nbytes: int = 0
    throttled: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    async def get(self, url: str, *, key: str) -> tuple[bytes, str, str]:
        """GET one API URL; returns (body, raw sha256, retrieved_at ISO)."""
        for attempt in (1, 2, 3):
            r = await self.http.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                max_bytes=MAX_RESPONSE_BYTES,
                accept_status=(200, 429, 503),
            )
            self.requests += 1
            self.nbytes += len(r.body)
            if r.status in (429, 503):
                self.throttled += 1
                ra = (getattr(r, "headers", {}) or {}).get("retry-after", "")
                wait = float(ra) if str(ra).isdigit() else 60.0
                self.log.append({"key": key, "status": r.status, "retry_after": ra, "attempt": attempt})
                if attempt == 3 or wait > 120:
                    raise RateLimitedStop(
                        f"HTTP {r.status} from Wikimedia (twice or long Retry-After)", retryable=True, status=r.status
                    )
                await asyncio.sleep(wait)
                continue
            sha = hashlib.sha256(r.body).hexdigest()
            when = r.retrieved_at.isoformat() if getattr(r, "retrieved_at", None) else datetime.now(UTC).isoformat()
            try:
                _json(r.body)
            except SourceResponseError as e:
                if e.details.get("kind") == "maxlag" and attempt == 1:
                    self.throttled += 1
                    await asyncio.sleep(30)
                    continue
                raise
            self.log.append(
                {"key": key, "status": r.status, "bytes": len(r.body), "raw_sha256": sha, "retrieved_at": when}
            )
            return r.body, sha, when
        raise RateLimitedStop("unreachable", retryable=True)  # pragma: no cover


def _live_inner(settings: Any) -> Any:
    """The SafeHttpClient restricted to the two Wikipedia hosts, 10 s per host, one at a time.

    Built by the atlas ingest's builder (``reva.atlas.ingest._live_client``); every caller here wraps it in
    the budget meter (:func:`budgeted_session`)."""
    from reva.atlas.ingest import _live_client

    spec = SimpleNamespace(
        allowed_hosts=tuple(HOSTS.values()),
        min_interval_seconds=MIN_INTERVAL_SECONDS,
        max_response_bytes=MAX_RESPONSE_BYTES,
    )
    return _live_client(spec, settings)  # type: ignore[arg-type]


class budgeted_session:  # used as ``async with budgeted_session(...)``
    """reserve_ingest -> metered(live client) -> settle_metered, ledger in a throwaway ``reva_*`` database."""

    def __init__(
        self,
        *,
        db_name: str = DEFAULT_DB,
        planned_requests: int,
        use_requested_budget: bool = False,
        http_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.db_name = db_name
        self.planned = planned_requests
        self.use_requested = use_requested_budget
        self.http_factory = http_factory
        self.usage: dict[str, Any] | None = None

    async def __aenter__(self) -> Session:
        from reva.adapters.postgres.engine import make_engine
        from reva.kg.harvest import ensure_database
        from reva.operations.budgets import load_budget, metered, reserve_ingest

        settings = ensure_database(self.db_name)  # refuses the dev database and non reva_* names
        self.engine = make_engine("worker", settings)
        budget = (requested_budget_overlay() if self.use_requested else load_budget)(None)
        run_id = f"causcand-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.reservation = reserve_ingest(
            self.engine, job_id=run_id, attempt=1, source_key=SOURCE_KEY, planned_requests=self.planned, budget=budget
        )
        self.inner = (self.http_factory or _live_inner)(settings)
        self.http = metered(self.inner, self.engine, self.reservation, budget=budget)
        return Session(self.http)

    async def __aexit__(self, *exc: Any) -> None:
        from reva.operations.budgets import settle_metered

        try:
            self.usage = settle_metered(self.engine, self.http)
        finally:
            await self.inner.aclose()
            self.engine.dispose()


# ----------------------------------------------------------------------------- harvest steps


def _done_titles(log_path: Path) -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            d = json.loads(line)
            if d.get("kind") == "article":
                done.add((d["lang"], d["requested"]))
    return done


async def harvest_sentences(
    session: Session, articles: Iterable[tuple[str, str]], out_dir: Path, *, batch: int = CONTENT_BATCH
) -> dict[str, Any]:
    """Fetch (lang, title) articles, keep prefiltered sentences only (``sentences.jsonl``), log each article
    (revision, raw sha256) in ``fetch_log.jsonl``. Resumable: titles already logged are skipped."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path, sent_path = out_dir / "fetch_log.jsonl", out_dir / "sentences.jsonl"
    done = _done_titles(log_path)
    todo: dict[str, list[str]] = {}
    for lang, title in articles:
        if (lang, title) not in done:
            todo.setdefault(lang, []).append(title)
    stats: dict[str, Any] = {"articles": 0, "sentences": 0, "missing": [], "bytes": 0}
    for lang, titles in todo.items():
        queue = [titles[i : i + batch] for i in range(0, len(titles), batch)]
        while queue:
            chunk = queue.pop(0)
            body, sha, when = await session.get(content_url(lang, chunk), key=f"content:{lang}:{chunk[0]}")
            stats["bytes"] += len(body)
            pages, missing = parse_content(body, chunk)
            if missing and len(chunk) > 1:  # response size limit: retry the missing ones one by one
                queue = [[t] for t in missing] + queue
                missing = []
            with sent_path.open("a") as fs, log_path.open("a") as fl:
                for p in pages:
                    recs = sentence_records(
                        lang=lang,
                        title=p.title,
                        page_id=p.page_id,
                        article_qid=p.qid,
                        revision_id=p.revision_id,
                        revision_timestamp=p.revision_timestamp,
                        wikitext=p.wikitext,
                        raw_sha256=sha,
                        retrieved_at=when,
                    )
                    for r in recs:
                        fs.write(json.dumps(record_to_dict(r), ensure_ascii=False) + "\n")
                    fl.write(
                        json.dumps(
                            {
                                "kind": "article",
                                "lang": lang,
                                "requested": p.requested,
                                "title": p.title,
                                "page_id": p.page_id,
                                "qid": p.qid,
                                "revision_id": p.revision_id,
                                "revision_timestamp": p.revision_timestamp,
                                "wikitext_chars": len(p.wikitext),
                                "wikitext_sha256": hashlib.sha256(p.wikitext.encode()).hexdigest(),
                                "sentences_kept": len(recs),
                                "raw_sha256": sha,
                                "retrieved_at": when,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    stats["articles"] += 1
                    stats["sentences"] += len(recs)
                for t in missing:
                    fl.write(
                        json.dumps(
                            {
                                "kind": "article",
                                "lang": lang,
                                "requested": t,
                                "missing": True,
                                "raw_sha256": sha,
                                "retrieved_at": when,
                            }
                        )
                        + "\n"
                    )
                    stats["missing"].append(f"{lang}:{t}")
            log.info("%s: %d articles, %d sentences so far", lang, stats["articles"], stats["sentences"])
    return stats


def load_records(out_dir: Path) -> list[SentenceRecord]:
    from reva.causal_candidates.extract import record_from_dict

    p = out_dir / "sentences.jsonl"
    return [record_from_dict(json.loads(line)) for line in p.read_text().splitlines()] if p.exists() else []


def load_resolutions(path: Path) -> dict[str, dict[str, Resolution]]:
    out: dict[str, dict[str, Resolution]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            d = json.loads(line)
            lang = d.pop("lang")
            d.pop("raw_sha256", None)
            d.pop("retrieved_at", None)
            out.setdefault(lang, {})[d["target"]] = Resolution(**d)
    return out


async def resolve_targets(session: Session, needed: Mapping[str, set[str]], cache: Path) -> dict[str, int]:
    """Resolve link targets not yet in ``cache`` (``resolutions.jsonl``), 50 per request."""
    have = load_resolutions(cache)
    stats: dict[str, int] = {}
    for lang, targets in needed.items():
        todo = sorted(t for t in targets if t not in have.get(lang, {}))
        stats[lang] = len(todo)
        for i in range(0, len(todo), RESOLVE_BATCH):
            chunk = todo[i : i + RESOLVE_BATCH]
            body, sha, when = await session.get(resolve_url(lang, chunk), key=f"resolve:{lang}:{i}")
            res = parse_resolve(body, chunk)
            with cache.open("a") as f:
                for t in chunk:
                    r = res[t]
                    f.write(
                        json.dumps(
                            {"lang": lang, **r.__dict__, "raw_sha256": sha, "retrieved_at": when}, ensure_ascii=False
                        )
                        + "\n"
                    )
    return stats
