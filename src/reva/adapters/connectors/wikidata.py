"""Wikidata access for the REVA knowledge graph (agent WDHARVEST).

Two endpoints, both CC0:

* **WDQS** SPARQL — ``https://query.wikidata.org/sparql`` (GET, ``format=json``), for selecting items and
  explicit statement rows. Queries are killed by WDQS at 60 s; long queries are split by the harvester.
* **Wikibase API** ``wbgetentities`` — ``https://www.wikidata.org/w/api.php`` (≤ 50 ids per call,
  ``props=claims|labels|descriptions|aliases|sitelinks``, languages fr|en|es|pt|ar|zh|ru|de, ``maxlag=5``),
  for versioned entity snapshots (``lastrevid``) and statements with ids, ranks, qualifiers and references.

Policy compliance (https://meta.wikimedia.org/wiki/User-Agent_policy, WDQS usage policy, API etiquette):

* descriptive ``User-Agent`` with contact (``REVA_CONTACT_EMAIL`` / ``REVA_HTTP_CONTACT_EMAIL`` when set, else
  the project URL);
* **one request at a time machine-wide**: every request holds an exclusive ``fcntl`` lock on
  ``var/locks/wikidata.lock`` (all agents/processes on this machine share it) and the shared state file next
  to it enforces ≥ ``min_interval`` seconds between the end of one request and the start of the next and any
  cool-down set by a 429/503 ``Retry-After`` (honoured by every process, not only the one that got it);
* back-off on 429/503 (Retry-After, else exponential from 5 s), ``maxlag`` errors honoured;
* WDQS query time-outs are *not* retried (the query must be split) — a typed :class:`QueryTimeout`.

Every successful raw response body is archived content-addressed (``ArtifactStore`` namespace ``raw``) and
indexed in ``var/kg/archive_index.jsonl``; every request goes through the caller's HTTP object, which for
live runs is the budget-metered client (:class:`reva.operations.budgets.MeteredHttp`) — one per source key
(:data:`SOURCE_WDQS`, :data:`SOURCE_WBAPI`).

Extraction (:func:`extract_entity`) keeps only claims whose property is allow-listed in
``registry/kg/properties.yaml``, drops deprecated-rank claims, keeps preferred/normal rank, temporal
qualifiers (P580/P582/P585 + earliest/latest), the other allow-listed qualifiers, and compact references
(P248 stated in, P854 reference URL, P813 retrieved, P143 imported from). Wikidata statements are
community-edited: they are labelled ``community_edited`` and never ``verified``.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import fcntl
import hashlib
import json
import logging
import os
import random
import re
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import yaml

from reva.kernel.errors import RevaError, SourceResponseError, UnsafeRequest

log = logging.getLogger("reva.wikidata")

REPO_ROOT = Path(__file__).resolve().parents[4]
WDQS_URL = "https://query.wikidata.org/sparql"
WBAPI_URL = "https://www.wikidata.org/w/api.php"
ALLOWED_HOSTS: tuple[str, ...] = ("query.wikidata.org", "www.wikidata.org")
SOURCE_WDQS = "wikidata.wdqs"
SOURCE_WBAPI = "wikidata.wbapi"
LANGUAGES: tuple[str, ...] = ("fr", "en", "es", "pt", "ar", "zh", "ru", "de")
ENTITY_PROPS = ("claims", "labels", "descriptions", "aliases", "sitelinks")
LABEL_PROPS = ("labels", "descriptions", "aliases", "sitelinks")
MAX_IDS_PER_CALL = 50
MAX_GET_URL = 7500  # WDQS/varnish accept longer GETs, but chunks this large mean the spec should split
ENTITY_PREFIX = "http://www.wikidata.org/entity/"
PROPERTIES_FILE = REPO_ROOT / "registry" / "kg" / "properties.yaml"
VERSION = "0.3"
#: Seconds between the end of one Wikidata request and the start of the next, machine-wide. 2 s (not 1 s):
#: on 2026-10-15 the API answered 429 "You are making too many requests" at ~1 req/s aggregate.
DEFAULT_MIN_INTERVAL = 2.0
PROJECT_URL = "https://github.com/AmineDJM/REVA-World"
TEMPORAL_QUALIFIERS = {"P580": "start", "P582": "end", "P585": "point_in_time"}
SITELINKS_KEPT = ("enwiki", "frwiki", "eswiki", "ptwiki", "arwiki", "zhwiki", "ruwiki", "dewiki")


class WikidataError(RevaError):
    code = "wikidata_error"
    http_status = 502


class QueryTimeout(WikidataError):
    """WDQS stopped the query at its 60 s limit: split it (never retried as is)."""

    code = "wdqs_timeout"


class SparqlSyntaxError(WikidataError):
    code = "wdqs_bad_query"
    http_status = 400


class RateLimited(WikidataError):
    """Still throttled (429/503/maxlag) after the allowed retries."""

    code = "wikidata_rate_limited"
    http_status = 429


# --------------------------------------------------------------------------- user agent
def user_agent(contact: str | None = None) -> str:
    contact = contact or os.environ.get("REVA_CONTACT_EMAIL") or os.environ.get("REVA_HTTP_CONTACT_EMAIL") or ""
    who = f"mailto:{contact.strip()}" if contact.strip() else PROJECT_URL
    return f"REVA-World/{VERSION} (research world knowledge graph; {who}) reva-kg-harvest/{VERSION}"


# --------------------------------------------------------------------------- machine-wide lock
def default_lock_dir() -> Path:
    return Path(os.environ.get("REVA_WIKIDATA_LOCK_DIR") or (REPO_ROOT / "var" / "locks"))


class MachineLock:
    """Exclusive cross-process slot for one Wikidata request + shared pacing state.

    ``fcntl.flock`` on ``<dir>/wikidata.lock``; the JSON state file ``<dir>/wikidata.state.json`` holds
    ``last_end`` (wall clock of the end of the last request, any process) and ``cooldown_until``.
    """

    def __init__(self, directory: Path | None = None, *, min_interval: float = DEFAULT_MIN_INTERVAL,
                 clock: Any = time.time, sleep: Any = asyncio.sleep) -> None:
        self.dir = Path(directory) if directory else default_lock_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.dir / "wikidata.lock"
        self.state_path = self.dir / "wikidata.state.json"
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._cooldown: float | None = None
        self.waited_seconds = 0.0

    def _read_state(self) -> dict[str, float]:
        try:
            data = json.loads(self.state_path.read_text())
            return {k: float(v) for k, v in data.items() if isinstance(v, int | float)}
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: Mapping[str, float]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(state)))
        os.replace(tmp, self.state_path)

    def cooldown(self, seconds: float) -> None:
        """Ask every process to wait ``seconds`` before its next request (429/503/maxlag)."""
        self._cooldown = max(self._cooldown or 0.0, seconds)

    @contextlib.asynccontextmanager
    async def slot(self, endpoint: str = "any") -> AsyncIterator[MachineLock]:
        """Hold the machine-wide lock for one request to ``endpoint`` ('wdqs' | 'wbapi').

        Pacing (``min_interval``) is global; cool-downs are per endpoint (WDQS and the Wikibase API throttle
        independently), stored as ``cooldown_until:<endpoint>``.
        """
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        ckey = f"cooldown_until:{endpoint}"
        try:
            while True:
                await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
                state = self._read_state()
                now = self._clock()
                cool = state.get(ckey, 0.0) - now
                if cool > self.min_interval:
                    # A long cool-down is waited *without* the lock, so other endpoints keep flowing.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    if cool > 30:
                        log.warning("wikidata %s cool-down: %.0f s left (429/503 Retry-After)", endpoint, cool)
                    step = min(cool, 60.0)
                    self.waited_seconds += step
                    await self._sleep(step)
                    continue
                wait = max(state.get("last_end", 0.0) + self.min_interval, state.get(ckey, 0.0)) - now
                if wait > 0:  # pacing: at most min_interval, under the lock
                    self.waited_seconds += wait
                    await self._sleep(wait)
                break
            try:
                self._cooldown = None
                try:
                    yield self
                finally:
                    end = self._clock()
                    state = self._read_state()
                    new = {k: v for k, v in state.items() if k.startswith("cooldown_until:") and v > end}
                    new["last_end"] = end
                    new["pid"] = float(os.getpid())
                    if self._cooldown:
                        new[ckey] = max(new.get(ckey, 0.0), end + self._cooldown)
                    self._write_state(new)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------- archive
@dataclass
class Archive:
    """Content-addressed archive of raw responses (and the exact query texts).

    With ``compress`` (default) the stored object is the response body gzip-encoded deterministically
    (``mtime=0``), media type ``application/gzip``: its sha256 is the artifact address statements cite
    (``raw_sha256``); the index also records ``body_sha256`` (sha256 of the provider bytes as received).
    """

    store: Any | None
    index_path: Path | None = None
    compress: bool = True

    def put(self, body: bytes, media_type: str, meta: Mapping[str, Any]) -> str:
        import gzip

        body_sha = hashlib.sha256(body).hexdigest()
        data = gzip.compress(body, compresslevel=6, mtime=0) if self.compress else body
        stored_type = "application/gzip" if self.compress else media_type
        sha = hashlib.sha256(data).hexdigest()
        if self.store is not None:
            obj = self.store.put_bytes(data, "raw", stored_type)
            if obj.sha256 != sha:  # pragma: no cover - store verifies the bytes itself
                raise WikidataError("archive hash mismatch")
        if self.index_path is not None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("a") as f:
                f.write(json.dumps({"sha256": sha, "body_sha256": body_sha, "bytes": len(body),
                                    "stored_bytes": len(data), "media_type": media_type,
                                    "encoding": "gzip" if self.compress else "identity", **meta},
                                   sort_keys=True, default=str) + "\n")
        return sha


# --------------------------------------------------------------------------- results
@dataclass(frozen=True)
class SparqlResult:
    variables: tuple[str, ...]
    bindings: list[dict[str, dict[str, str]]]
    query_sha256: str
    raw_sha256: str
    bytes: int
    retrieved_at: datetime
    elapsed_seconds: float

    def column(self, var: str) -> list[str | None]:
        return [b.get(var, {}).get("value") for b in self.bindings]


@dataclass(frozen=True)
class EntitiesResult:
    entities: dict[str, dict[str, Any]]  # id -> raw entity JSON (redirect targets keyed by the target id)
    missing: tuple[str, ...]
    redirects: dict[str, str]  # requested id -> target id
    raw_sha256s: tuple[str, ...]
    raw_sha_by_id: dict[str, str]
    bytes: int
    requests: int
    retrieved_at: datetime


@dataclass
class ClientStats:
    requests: int = 0
    bytes: int = 0
    retries: int = 0
    throttled: int = 0
    timeouts: int = 0
    maxlag_relaxed: int = 0
    lock_wait_seconds: float = 0.0
    by_endpoint: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "bytes": self.bytes, "retries": self.retries,
                "throttled": self.throttled, "timeouts": self.timeouts, "maxlag_relaxed": self.maxlag_relaxed,
                "lock_wait_seconds": round(self.lock_wait_seconds, 2), "by_endpoint": dict(self.by_endpoint)}


def qid_from_uri(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(ENTITY_PREFIX):
        tail = value[len(ENTITY_PREFIX):]
        return tail if tail[:1] in ("Q", "P", "L") and tail[1:].isdigit() else None
    return value if value[:1] in ("Q", "P") and value[1:].isdigit() else None


def property_from_uri(value: str | None) -> str | None:
    """``wdt:P31``/``p:P31``/``ps:P31``/``wd:P31`` IRIs (or a bare id) -> ``P31``."""
    if not value:
        return None
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail[:1] == "P" and tail[1:].isdigit() else None


def statement_id_from_uri(value: str | None) -> str | None:
    """``http://www.wikidata.org/entity/statement/Q142-ABC...`` -> ``Q142$ABC...`` (wbgetentities form)."""
    if not value:
        return None
    tail = value.rsplit("/", 1)[-1]
    head, sep, rest = tail.partition("-")
    return f"{head}${rest}" if sep and head[:1] == "Q" else tail


# --------------------------------------------------------------------------- client
class WikidataClient:
    """Locked, paced, archived access to WDQS and wbgetentities.

    ``wdqs_http`` / ``wbapi_http``: objects with ``async get(url, *, headers=, max_bytes=, accept_status=)``
    returning :class:`reva.adapters.http.safe_client.HttpResult` (a ``SafeHttpClient``, the budget-metered
    wrapper of one, ``FixtureHttp`` or ``RecordingHttp``). Pass the same object twice when no metering split
    is needed.
    """

    def __init__(
        self,
        wdqs_http: Any,
        wbapi_http: Any | None = None,
        *,
        archive: Archive | None = None,
        lock: MachineLock | None = None,
        contact: str | None = None,
        max_retries: int = 12,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.wdqs_http = wdqs_http
        self.wbapi_http = wbapi_http or wdqs_http
        self.archive = archive or Archive(None)
        self.lock = lock or MachineLock()
        self.ua = user_agent(contact)
        self.max_retries = max_retries
        self._sleep = sleep
        self.stats = ClientStats()

    # -- core request loop -------------------------------------------------------------------------
    async def _request(self, http: Any, url: str, *, endpoint: str, accept: str, meta: Mapping[str, Any],
                       max_bytes: int | None = None) -> tuple[Any, str]:
        if len(url) > MAX_GET_URL and endpoint == "wdqs":
            raise WikidataError(f"query URL too long ({len(url)} chars): reduce the VALUES chunk size",
                                reason="url_too_long")
        headers = {"User-Agent": self.ua, "Accept": accept}
        attempt = 0
        qs_lag = 0  # consecutive maxlag errors caused by query-service (WDQS updater) lag
        while True:
            attempt += 1
            kw: dict[str, Any] = {"headers": headers, "accept_status": (200, 400, 403, 429, 500, 502, 503, 504)}
            if max_bytes:
                kw["max_bytes"] = max_bytes
            async with self.lock.slot(endpoint) as slot:
                result = await http.get(url, **kw)
                self.stats.lock_wait_seconds = slot.waited_seconds
                status = int(result.status)
                self.stats.requests += 1
                self.stats.bytes += len(result.body)
                self.stats.by_endpoint[endpoint] = self.stats.by_endpoint.get(endpoint, 0) + 1
                retry_after = _retry_after_seconds(result.headers.get("retry-after"))
                maxlag = endpoint == "wbapi" and status == 200 and _is_maxlag(result.body)
                if status in (429, 503) or maxlag:
                    self.stats.throttled += 1
                    delay = retry_after if retry_after is not None else min(300.0, 5.0 * 2 ** (attempt - 1))
                    slot.cooldown(delay)  # every process waits, not only this one
            if status == 200 and not maxlag:
                sha = self.archive.put(result.body, result.media_type or "application/json",
                                       {**meta, "endpoint": endpoint,
                                        "retrieved_at": result.retrieved_at.isoformat(), "status": status})
                return result, sha
            if maxlag and _maxlag_type(result.body) == "wikibase-queryservice":
                # Wikidata folds the WDQS *updater* lag into maxlag to slow down edits; it says nothing
                # about the replica serving this read. After 3 such errors in a row the read is sent
                # without maxlag (the Retry-After pause is still honoured).
                qs_lag += 1
                if qs_lag >= 3 and "&maxlag=" in url:
                    url = re.sub(r"&maxlag=\d+", "", url)
                    self.stats.maxlag_relaxed += 1
                    log.warning("wbapi: sustained query-service lag; sending this read without maxlag")
            if status in (429, 503) or maxlag:
                if attempt > self.max_retries:
                    raise RateLimited(f"{endpoint}: still throttled after {attempt} attempts", status=status)
                self.stats.retries += 1
                log.warning("%s throttled (HTTP %s%s, retry-after=%s): %s", endpoint, status,
                            " maxlag" if maxlag else "", result.headers.get("retry-after"),
                            _first_line(result.body[:600].decode("utf-8", "replace"))[:200])
                continue  # the shared cool-down is applied when the next slot is taken
            body_head = result.body[:4000].decode("utf-8", "replace")
            timed_out = "TimeoutException" in body_head or (status in (500, 504) and "timeout" in body_head.lower())
            if endpoint == "wdqs" and timed_out:
                self.stats.timeouts += 1
                raise QueryTimeout("WDQS query exceeded its time limit; split the spec", status=status)
            if status == 400:
                raise SparqlSyntaxError(f"{endpoint}: HTTP 400: {_first_line(body_head)}", status=status)
            if status == 403:
                raise WikidataError(f"{endpoint}: HTTP 403 (User-Agent policy or ban)", status=status,
                                    retryable=False)
            if attempt > 3:
                raise WikidataError(f"{endpoint}: HTTP {status} after {attempt} attempts", status=status)
            self.stats.retries += 1
            await self._sleep(min(120.0, 5.0 * 2 ** (attempt - 1)) + random.uniform(0, 1))

    # -- WDQS ----------------------------------------------------------------------------------------
    async def sparql(self, query: str, *, meta: Mapping[str, Any] | None = None) -> SparqlResult:
        q = query.strip()
        qbytes = q.encode()
        query_sha = hashlib.sha256(qbytes).hexdigest()
        self.archive.put(qbytes, "application/sparql-query", {"kind": "sparql_query", **(meta or {})})
        url = f"{WDQS_URL}?{urlencode({'query': q, 'format': 'json'})}"
        t0 = time.monotonic()
        result, sha = await self._request(self.wdqs_http, url, endpoint="wdqs",
                                          accept="application/sparql-results+json",
                                          meta={"kind": "sparql_results", "query_sha256": query_sha, **(meta or {})})
        doc = _json(result.body)
        try:
            variables = tuple(doc["head"]["vars"])
            bindings = list(doc["results"]["bindings"])
        except (KeyError, TypeError) as e:
            raise SourceResponseError("WDQS response is not SPARQL JSON results", retryable=False) from e
        return SparqlResult(variables, bindings, query_sha, sha, len(result.body), result.retrieved_at,
                            time.monotonic() - t0)

    # -- wbgetentities -----------------------------------------------------------------------------
    @staticmethod
    def entities_url(ids: Sequence[str], props: Sequence[str] = ENTITY_PROPS,
                     languages: Sequence[str] = LANGUAGES) -> str:
        params = {"action": "wbgetentities", "ids": "|".join(ids), "props": "|".join(props),
                  "languages": "|".join(languages), "format": "json", "maxlag": "5"}
        return f"{WBAPI_URL}?{urlencode(params, safe='|')}"

    async def get_entities(self, ids: Iterable[str], *, props: Sequence[str] = ENTITY_PROPS,
                           languages: Sequence[str] = LANGUAGES, batch_size: int = MAX_IDS_PER_CALL,
                           meta: Mapping[str, Any] | None = None) -> EntitiesResult:
        """Snapshots of ``ids`` (≤ 50 per call). Oversized responses are retried in halves."""
        todo = sorted({i for i in ids if i}, key=_id_sort_key)
        entities: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        redirects: dict[str, str] = {}
        shas: list[str] = []
        sha_by_id: dict[str, str] = {}
        nbytes = 0
        requests = 0
        retrieved = datetime.now(UTC)
        queue: list[list[str]] = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
        while queue:
            chunk = queue.pop(0)
            url = self.entities_url(chunk, props, languages)
            try:
                result, sha = await self._request(self.wbapi_http, url, endpoint="wbapi", accept="application/json",
                                                  meta={"kind": "wbgetentities", "ids": chunk,
                                                        "props": list(props), **(meta or {})})
            except UnsafeRequest as e:
                if "exceeds limit" in e.message and len(chunk) > 1:
                    half = len(chunk) // 2
                    queue[:0] = [chunk[:half], chunk[half:]]
                    continue
                raise
            requests += 1
            nbytes += len(result.body)
            shas.append(sha)
            retrieved = result.retrieved_at
            doc = _json(result.body)
            if "error" in doc:
                err = doc["error"]
                raise WikidataError(f"wbgetentities error {err.get('code')}: {str(err.get('info'))[:200]}",
                                    api_code=err.get("code"))
            for key, ent in (doc.get("entities") or {}).items():
                if "missing" in ent:
                    missing.append(key)
                    continue
                eid = ent.get("id", key)
                if eid != key:
                    redirects[key] = eid
                entities[eid] = ent
                sha_by_id[eid] = sha
            for key, eid in (doc.get("redirects") or {}).items() if isinstance(doc.get("redirects"), dict) else []:
                redirects.setdefault(key, eid)
        return EntitiesResult(entities, tuple(missing), redirects, tuple(shas), sha_by_id, nbytes, requests, retrieved)


def _id_sort_key(i: str) -> tuple[str, int]:
    return (i[:1], int(i[1:]) if i[1:].isdigit() else 0)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("at ", "\tat ")):
            return line[:300]
    return text[:300]


def _json(body: bytes) -> Any:
    head = body[:256].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html")):
        raise SourceResponseError("HTML page received where JSON was expected", retryable=True, kind="html_body")
    try:
        return json.loads(body)
    except ValueError as e:
        raise SourceResponseError("malformed JSON from Wikidata", retryable=False, kind="malformed_json") from e


def _maxlag_type(body: bytes) -> str | None:
    try:
        return str((json.loads(body).get("error") or {}).get("type") or "") or None
    except ValueError:
        return None


def _is_maxlag(body: bytes) -> bool:
    if b'"maxlag"' not in body[:2000]:
        return False
    try:
        return (json.loads(body).get("error") or {}).get("code") == "maxlag"
    except ValueError:
        return False


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(900.0, float(value))
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(900.0, (dt - datetime.now(UTC)).total_seconds()))


# --------------------------------------------------------------------------- property allow-list
@dataclass(frozen=True)
class PropertyDef:
    property: str
    reva_predicate: str
    inverse: str | None
    kind: str
    group: str
    label_en: str | None
    label_fr: str | None
    symmetric: bool = False


@dataclass(frozen=True)
class AllowList:
    properties: dict[str, PropertyDef]
    qualifiers: frozenset[str]
    references: frozenset[str]
    sha256: str

    def __contains__(self, pid: object) -> bool:
        return pid in self.properties


def load_allowlist(path: Path | None = None) -> AllowList:
    raw = (path or PROPERTIES_FILE).read_bytes()
    doc = yaml.safe_load(raw)
    props: dict[str, PropertyDef] = {}
    for p in doc["properties"]:
        pid = p["property"]
        if pid in props:
            raise ValueError(f"duplicate property {pid} in allow-list")
        props[pid] = PropertyDef(pid, p["reva_predicate"], p.get("inverse"), p["kind"], p.get("group", ""),
                                 p.get("label_en"), p.get("label_fr"), bool(p.get("symmetric", False)))
    quals = frozenset(q["property"] for q in doc.get("qualifiers") or [])
    refs = frozenset(r["property"] for r in doc.get("references") or [])
    return AllowList(props, quals | frozenset(TEMPORAL_QUALIFIERS), refs, hashlib.sha256(raw).hexdigest())


# --------------------------------------------------------------------------- extraction
def datavalue_to_value(snak: Mapping[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    """A Wikibase snak -> (value_kind, object_qid, typed literal dict).

    value kinds: entity | time | quantity | monolingualtext | string | coordinate | somevalue | novalue | other.
    Times keep Wikibase precision (9 = year, 10 = month, 11 = day, 8 = decade, 7 = century, 6 = millennium) and
    calendar model; quantities keep the decimal amount *as a string* with unit and bounds (no float).
    """
    snaktype = snak.get("snaktype", "value")
    if snaktype != "value":
        return snaktype, None, {"snaktype": snaktype}
    dv = snak.get("datavalue") or {}
    t = dv.get("type")
    v = dv.get("value")
    datatype = snak.get("datatype")
    if t == "wikibase-entityid" and isinstance(v, dict):
        eid = v.get("id") or (f"Q{v['numeric-id']}" if v.get("entity-type") == "item" else None)
        return "entity", eid, {"id": eid}
    if t == "time" and isinstance(v, dict):
        return "time", None, {"time": v.get("time"), "precision": v.get("precision"),
                              "calendar": qid_from_uri(v.get("calendarmodel")) or v.get("calendarmodel")}
    if t == "quantity" and isinstance(v, dict):
        unit = v.get("unit")
        out = {"amount": v.get("amount"), "unit": None if unit in (None, "1") else (qid_from_uri(unit) or unit)}
        for b in ("lowerBound", "upperBound"):
            if v.get(b) is not None:
                out["lower" if b == "lowerBound" else "upper"] = v[b]
        return "quantity", None, out
    if t == "monolingualtext" and isinstance(v, dict):
        return "monolingualtext", None, {"text": v.get("text"), "language": v.get("language")}
    if t == "globecoordinate" and isinstance(v, dict):
        return "coordinate", None, {"lat": v.get("latitude"), "lon": v.get("longitude"),
                                    "precision": v.get("precision"), "globe": qid_from_uri(v.get("globe"))}
    if t == "string":
        kind = {"external-id": "external_id", "url": "url"}.get(str(datatype), "string")
        return kind, None, {"value": v}
    return "other", None, {"type": t, "value": v}


def _compact_snak_value(snak: Mapping[str, Any]) -> Any:
    kind, qid, value = datavalue_to_value(snak)
    if kind == "entity":
        return qid
    if kind == "time":
        return {"time": value["time"], "precision": value["precision"], "calendar": value["calendar"]}
    if kind in ("string", "external_id", "url"):
        return value["value"]
    if kind in ("somevalue", "novalue"):
        return {"snaktype": kind}
    return value


@dataclass(frozen=True)
class WdStatement:
    """One extracted statement (wbgetentities claim or WDQS row), ready for KG loading."""

    subject: str
    property: str
    rank: str  # preferred | normal
    value_kind: str
    object_qid: str | None
    value: dict[str, Any]
    statement_id: str | None
    start: dict[str, Any] | None
    end: dict[str, Any] | None
    point_in_time: dict[str, Any] | None
    qualifiers: dict[str, list[Any]]
    references: list[dict[str, list[Any]]]
    source_key: str
    source_revision: int | None
    raw_sha256: str
    retrieved_at: datetime
    selector: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["retrieved_at"] = self.retrieved_at.isoformat()
        return d


@dataclass(frozen=True)
class WdEntity:
    qid: str
    kind_hint: str | None
    labels: dict[str, str]
    descriptions: dict[str, str]
    aliases: dict[str, list[str]]
    sitelinks_count: int
    sitelinks: dict[str, str]  # selected wikis -> title
    lastrevid: int | None
    modified: str | None
    instance_of: tuple[str, ...]
    statements: tuple[WdStatement, ...]
    raw_sha256: str
    retrieved_at: datetime
    claims_fetched: bool
    dropped: dict[str, int]  # counts: deprecated, not_allowlisted


def _time_of(snaks: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    for s in snaks:
        kind, _, value = datavalue_to_value(s)
        if kind == "time":
            return value
        if kind in ("somevalue", "novalue"):
            return {"snaktype": kind}
    return None


def extract_entity(raw: Mapping[str, Any], allow: AllowList, *, raw_sha256: str, retrieved_at: datetime,
                   kind_hint: str | None = None, properties: Iterable[str] | None = None,
                   source_key: str = SOURCE_WBAPI) -> WdEntity:
    """wbgetentities entity JSON -> :class:`WdEntity` with allow-listed, non-deprecated statements."""
    qid = str(raw["id"])
    rev = raw.get("lastrevid")
    keep = set(properties) & set(allow.properties) if properties else set(allow.properties)
    claims = raw.get("claims")
    claims_fetched = isinstance(claims, dict)
    statements: list[WdStatement] = []
    dropped = {"deprecated": 0, "not_allowlisted": 0}
    for pid, claim_list in (claims or {}).items() if claims_fetched else []:
        if pid not in keep:
            dropped["not_allowlisted"] += len(claim_list)
            continue
        for c in claim_list:
            rank = c.get("rank", "normal")
            if rank == "deprecated":
                dropped["deprecated"] += 1
                continue
            kind, obj, value = datavalue_to_value(c.get("mainsnak") or {})
            quals_raw: Mapping[str, list[Any]] = c.get("qualifiers") or {}
            temporal = {name: _time_of(quals_raw.get(p, [])) for p, name in TEMPORAL_QUALIFIERS.items()}
            qualifiers = {
                p: [_compact_snak_value(s) for s in snaks]
                for p, snaks in quals_raw.items()
                if p in allow.qualifiers and p not in TEMPORAL_QUALIFIERS
            }
            refs: list[dict[str, list[Any]]] = []
            for ref in c.get("references") or []:
                compact = {p: [_compact_snak_value(s) for s in snaks]
                           for p, snaks in (ref.get("snaks") or {}).items() if p in allow.references}
                if compact:
                    refs.append(compact)
            sid = c.get("id")
            statements.append(WdStatement(
                subject=qid, property=pid, rank=rank, value_kind=kind, object_qid=obj, value=value,
                statement_id=sid, start=temporal["start"], end=temporal["end"],
                point_in_time=temporal["point_in_time"], qualifiers=qualifiers, references=refs,
                source_key=source_key, source_revision=int(rev) if rev is not None else None,
                raw_sha256=raw_sha256, retrieved_at=retrieved_at,
                selector={"entity": qid, "statement_id": sid, "lastrevid": rev},
            ))
    labels = {k: v["value"] for k, v in (raw.get("labels") or {}).items() if isinstance(v, dict)}
    descriptions = {k: v["value"] for k, v in (raw.get("descriptions") or {}).items() if isinstance(v, dict)}
    aliases = {k: [a["value"] for a in v] for k, v in (raw.get("aliases") or {}).items() if isinstance(v, list)}
    sitelinks_raw = raw.get("sitelinks") or {}
    sitelinks = {str(k): str(v.get("title")) for k, v in sitelinks_raw.items()
                 if k in SITELINKS_KEPT and isinstance(v, dict) and v.get("title")}
    p31 = tuple(s.object_qid for s in statements if s.property == "P31" and s.object_qid)
    if not p31 and claims_fetched:
        p31 = tuple(
            datavalue_to_value(c.get("mainsnak") or {})[1] or ""
            for c in (claims or {}).get("P31", []) if c.get("rank") != "deprecated"
        )
        p31 = tuple(x for x in p31 if x)
    return WdEntity(
        qid=qid, kind_hint=kind_hint, labels=labels, descriptions=descriptions, aliases=aliases,
        sitelinks_count=len(sitelinks_raw), sitelinks=sitelinks,
        lastrevid=int(rev) if rev is not None else None, modified=raw.get("modified"),
        instance_of=p31, statements=tuple(statements), raw_sha256=raw_sha256, retrieved_at=retrieved_at,
        claims_fetched=claims_fetched, dropped=dropped,
    )


# --------------------------------------------------------------------------- SPARQL literal values
XSD = "http://www.w3.org/2001/XMLSchema#"


def binding_to_value(b: Mapping[str, str] | None) -> tuple[str, str | None, dict[str, Any]]:
    """A SPARQL JSON binding -> (value_kind, object_qid, typed literal), mirroring :func:`datavalue_to_value`.

    WDQS gives dates as ``xsd:dateTime`` without precision: precision is recorded as ``None`` (unknown) —
    never guessed. ``wikibase:`` blank nodes / ``.well-known/genid`` IRIs are ``somevalue``.
    """
    if not b:
        return "novalue", None, {"snaktype": "absent"}
    t, v = b.get("type"), b.get("value", "")
    if t == "uri":
        qid = qid_from_uri(v)
        if qid:
            return "entity", qid, {"id": qid}
        if "/.well-known/genid/" in v:
            return "somevalue", None, {"snaktype": "somevalue"}
        return "url", None, {"value": v}
    if t == "bnode":
        return "somevalue", None, {"snaktype": "somevalue"}
    dt = b.get("datatype", "")
    if dt == XSD + "dateTime":
        tv = v if v.startswith(("+", "-")) else f"+{v}"
        # WDQS: astronomical year numbering, proleptic Gregorian (julian values are converted by WDQS)
        return "time", None, {"time": tv, "precision": None, "calendar": None, "astronomical": True}
    if dt in (XSD + "decimal", XSD + "integer", XSD + "double", XSD + "float"):
        return "quantity", None, {"amount": v, "unit": None}
    if dt == "http://www.opengis.net/ont/geosparql#wktLiteral":
        return "coordinate", None, {"wkt": v}
    if b.get("xml:lang"):
        return "monolingualtext", None, {"text": v, "language": b["xml:lang"]}
    return "string", None, {"value": v}


def time_value_from_binding(b: Mapping[str, str] | None) -> dict[str, Any] | None:
    if not b:
        return None
    kind, _, value = binding_to_value(b)
    return value if kind in ("time", "somevalue") else None


# --------------------------------------------------------------------------- live HTTP
def make_live_http(*, max_bytes: int = 52_428_800, timeout_seconds: float = 75.0, proxy_mode: str | None = None,
                   contact: str | None = None) -> Any:
    """A SafeHttpClient restricted to the two Wikidata hosts.

    ``max_attempts=1``: retries/back-off are done by :class:`WikidataClient` (which must distinguish WDQS
    time-outs, 429/503 and maxlag, and share cool-downs machine-wide). The in-process rate limiter is a second
    guard (1 request/s/host, concurrency 1) behind the machine-wide lock.
    """
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
    from reva.adapters.http.safe_client import SafeHttpClient

    policies = {h: HostPolicy(min_interval_seconds=1.0, max_concurrency=1, max_retry_after_seconds=900.0)
                for h in ALLOWED_HOSTS}
    return SafeHttpClient(allowed_hosts=ALLOWED_HOSTS, timeout_seconds=timeout_seconds, max_bytes=max_bytes,
                          max_attempts=1, rate_limiter=RateLimiter(policies), proxy_mode=proxy_mode,  # type: ignore[arg-type]
                          contact_email=contact)
