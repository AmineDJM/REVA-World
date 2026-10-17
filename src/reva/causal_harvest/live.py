"""Live Wikidata causal harvest (agent CAUSWD): budgeted, locked, resumable.

Two paths, both through :class:`reva.adapters.connectors.wikidata.WikidataClient` (machine-wide lock, ≥ 2 s
pacing, per-endpoint Retry-After cool-downs, raw archive):

1. **WDQS listings** (``registry/kg/queries/causal/*.yaml``, one query per causal property, the two big ones
   split by QID prefix) — at most one attempt per hour, never retried on 429 (``max_retries=0``), skipped while
   another process holds a WDQS cool-down. Rows whose subject has a snapshot are re-read from it; the others
   are loaded as WDQS rows (revision and references explicitly unknown).
2. **Snapshots** (``wbgetentities``, 50 ids per call, ``props=info|claims|labels|descriptions|aliases|sitelinks``)
   of event seeds and of their causal neighbours (one hop), prioritised; optional CirrusSearch discovery
   (``haswbstatement:<causal pid>``, sorted by incoming links) adds the items that carry causal statements.
   Only causal + context properties are loaded (``core.HARVEST_PIDS``); causal statements keep their full
   references and causality qualifiers (``core.enrich_causal_statements``).

Budget (invariant 30): the shared ledger is the throwaway harvest database ``reva_kg_wdharvest`` (all Wikidata
agents reserve there). One reservation **per source key with its own operation id** (``<run>-wdqs`` /
``<run>-wbapi``), each request metered, settled at the end; a per-session hard cap (``CappedHttp``) on top. KG
loads go to the CAUSWD throwaway database (``reva_kg_causwd``); the dev database ``reva`` is refused by name.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from reva.adapters.connectors.wikidata import (
    ENTITY_PROPS,
    REPO_ROOT,
    SOURCE_WBAPI,
    SOURCE_WDQS,
    WBAPI_URL,
    AllowList,
    Archive,
    QueryTimeout,
    RateLimited,
    WdEntity,
    WikidataClient,
    WikidataError,
    _json,
    extract_entity,
    load_allowlist,
)
from reva.causal_harvest import core
from reva.kg.harvest import (
    HARVEST_VERSION,
    OBJECT_ROOT,
    STATE_DIR,
    Batch,
    Harvester,
    KgDbSink,
    discover_specs,
    ensure_database,
    harvest_settings,
    live_http,
    render_chunks,
    with_limit,
)
from reva.operations.budgets import BudgetExhausted

log = logging.getLogger("reva.causal_harvest")

CAUSWD_VERSION = "causwd/1.0.0"
DEFAULT_DB = "reva_kg_causwd"
LEDGER_DB = "reva_kg_wdharvest"
STATE_PATH = STATE_DIR / "causal.causwd.json"
SPEC_DIR = REPO_ROOT / "registry" / "kg" / "queries" / "causal"
ARCHIVE_INDEX = REPO_ROOT / "var" / "kg" / "archive_index.jsonl"
WBAPI_DAILY = 4000
SHARE_OF_REMAINING = 0.30
WDQS_MIN_GAP_SECONDS = 3600.0
MAX_ENTITIES = 20_000
MIN_FREE_BYTES = 5 * 1024**3
MAX_FOOTPRINT_BYTES = 380 * 1024**2
DISCOVERY_ORDER = ("P1478", "P1536", "P1479", "P1537", "P828", "P1542")
#: Seed tiers (lower = earlier). Hop-1 neighbours inherit tier + 1 of the best entity pointing at them.
TIER_RECORDED, TIER_KG_CAUSAL, TIER_KG_EVENTS, TIER_DISCOVERY_SPECIFIC, TIER_DISCOVERY_GENERAL, TIER_WDQS = (
    0,
    1,
    2,
    4,
    6,
    8,
)


# --------------------------------------------------------------------------- budget
class CappedHttp:
    """Hard per-session request cap in front of the budget-metered client (refuses before the transport)."""

    def __init__(self, inner: Any, cap: int, source_key: str) -> None:
        self.inner = inner
        self.cap = cap
        self.source_key = source_key
        self.requests = 0

    @property
    def origin(self) -> str:
        return str(getattr(self.inner, "origin", "live"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def get(self, url: str, **kw: Any) -> Any:
        if self.requests >= self.cap:
            raise BudgetExhausted(
                f"CAUSWD session cap of {self.cap} {self.source_key} requests reached",
                reason="causwd_session_cap",
                source_key=self.source_key,
            )
        self.requests += 1
        return await self.inner.get(url, **kw)


def wikidata_usage_today(index_path: Path = ARCHIVE_INDEX, *, now: datetime | None = None) -> dict[str, int]:
    """Today's (UTC) archived successful Wikidata responses per endpoint, all agents (the ledger books every
    harvester request under one account, see docs/dev/v3_causwd.md; this count is the cross-check)."""
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    out: dict[str, int] = {}
    try:
        with index_path.open() as f:
            for line in f:
                if day not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if str(d.get("retrieved_at", "")).startswith(day) and d.get("endpoint"):
                    out[d["endpoint"]] = out.get(d["endpoint"], 0) + 1
    except OSError:
        pass
    return out


def ledger_usage_today(engine: Any) -> dict[str, int]:
    from reva.operations.budgets import usage_today

    with engine.connect() as conn:
        return {k: usage_today(conn, k)["source_requests"] for k in (SOURCE_WBAPI, SOURCE_WDQS)}


def wbapi_cap(
    ledger: Mapping[str, int],
    archived: Mapping[str, int],
    *,
    daily: int = WBAPI_DAILY,
    share: float = SHARE_OF_REMAINING,
) -> tuple[int, dict[str, int]]:
    """≤ ``share`` of today's remaining Wikibase-host budget. "Used" is the larger of the ledger's wbapi account
    and the archived successful responses on the Wikibase host (429s are not archived: under-count)."""
    host = sum(v for k, v in archived.items() if k != "wdqs")
    used = max(int(ledger.get(SOURCE_WBAPI, 0)), host)
    remaining = max(0, daily - used)
    return int(remaining * share), {
        "used_estimate": used,
        "ledger_wbapi": int(ledger.get(SOURCE_WBAPI, 0)),
        "archived_wikibase_host": host,
        "remaining_estimate": remaining,
    }


def causwd_usage_today(engine: Any) -> int:
    """Wikibase-host requests CAUSWD sessions booked today in the shared ledger (settled or reserved)."""
    from sqlalchemy import text

    from reva.operations.budgets import DAY_START_SQL

    with engine.connect() as c:
        return int(
            c.execute(
                text(
                    "SELECT COALESCE(sum(COALESCE(consumed, reserved)), 0) FROM ops.budget_ledger "
                    "WHERE operation_id LIKE 'ingest:kgcauswd-%-wbapi:%:requests' "
                    f"AND recorded_at >= {DAY_START_SQL}"
                )
            ).scalar_one()
        )


@dataclass
class Session:
    client: WikidataClient
    wdqs_client: WikidataClient | None
    sink: KgDbSink
    kg_engine: Any
    ledger_engine: Any
    run_id: str
    caps: dict[str, Any]
    wbapi: CappedHttp
    wdqs: CappedHttp | None
    usage: dict[str, Any] = field(default_factory=dict)


@contextlib.asynccontextmanager
async def causal_session(
    *,
    db: str = DEFAULT_DB,
    ledger_db: str = LEDGER_DB,
    cap_wbapi: int | None = None,
    cap_wdqs: int = 0,
    wbapi_retries: int = 3,
    mode: str = "auto",
    state: State | None = None,
) -> AsyncIterator[Session]:
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.operations.budgets import load_budget, metered, reserve_ingest, settle_metered

    if db == ledger_db:
        raise ValueError("load database and ledger database must differ (the ledger DB belongs to WDHARVEST)")
    settings = ensure_database(db)
    kg_engine = make_engine("worker", settings)
    ledger_engine = make_engine("worker", harvest_settings(ledger_db))  # ledger rows only, no migration run
    store = FilesystemArtifactStore(OBJECT_ROOT, settings.reva_s3_prefix)
    run_id = f"kgcauswd-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    computed, basis = wbapi_cap(ledger_usage_today(ledger_engine), wikidata_usage_today())
    if state is not None:  # the 30 % share is per UTC day across all CAUSWD sessions of that day
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        allowance = int(state.data.setdefault("day_allowance", {}).setdefault(today, computed))
        mine = causwd_usage_today(ledger_engine)
        basis.update(day_allowance=allowance, causwd_used_today=mine)
        computed = max(0, min(computed, allowance - mine))
        state.save()
    cap = computed if cap_wbapi is None else min(cap_wbapi, computed)
    caps: dict[str, Any] = {"wbapi": cap, "wbapi_basis": basis, "wdqs": 0}
    inner = live_http()
    handles: list[Any] = []
    session: Session | None = None
    try:
        budget = load_budget()
        if cap <= 0:
            raise BudgetExhausted("no wbapi budget left for CAUSWD today", reason="causwd_share", **basis)
        res_a = reserve_ingest(
            ledger_engine,
            job_id=f"{run_id}-wbapi",
            attempt=1,
            source_key=SOURCE_WBAPI,
            planned_requests=min(cap, 50),
            budget=budget,
        )
        m_a = metered(inner, ledger_engine, res_a, budget=budget)
        handles.append(m_a)
        wbapi = CappedHttp(m_a, cap, SOURCE_WBAPI)
        wdqs: CappedHttp | None = None
        if cap_wdqs > 0:
            try:
                res_q = reserve_ingest(
                    ledger_engine,
                    job_id=f"{run_id}-wdqs",
                    attempt=1,
                    source_key=SOURCE_WDQS,
                    planned_requests=min(cap_wdqs, 5),
                    budget=budget,
                )
                m_q = metered(inner, ledger_engine, res_q, budget=budget)
                handles.append(m_q)
                wdqs = CappedHttp(m_q, cap_wdqs, SOURCE_WDQS)
                caps["wdqs"] = cap_wdqs
            except BudgetExhausted as e:
                caps["wdqs_refused"] = {
                    "message": e.message,
                    **{k: v for k, v in e.details.items() if isinstance(v, int | str)},
                }
        archive = Archive(store, ARCHIVE_INDEX)
        refusing = CappedHttp(inner, 0, SOURCE_WDQS)
        client = WikidataClient(
            wdqs or refusing, wbapi, archive=archive, mode=mode, max_retries=wbapi_retries, reuse_seconds=24 * 3600.0
        )
        wdqs_client = (
            WikidataClient(wdqs, wbapi, archive=archive, mode="api", max_retries=0) if wdqs is not None else None
        )
        session = Session(
            client, wdqs_client, KgDbSink(kg_engine, store), kg_engine, ledger_engine, run_id, caps, wbapi, wdqs
        )
        yield session
    finally:
        settled = [settle_metered(ledger_engine, h) for h in handles]
        if session is not None:
            session.usage["budget"] = settled
        await inner.aclose()
        kg_engine.dispose()
        ledger_engine.dispose()


# --------------------------------------------------------------------------- state
class State:
    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        try:
            self.data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        for k in ("fetched", "frontier", "discovery", "wdqs", "missing", "redirects"):
            self.data.setdefault(k, {})
        self.data.setdefault("sessions", [])

    @property
    def fetched(self) -> dict[str, Any]:
        return self.data["fetched"]  # type: ignore[no-any-return]

    @property
    def frontier(self) -> dict[str, Any]:
        return self.data["frontier"]  # type: ignore[no-any-return]

    def add(self, qid: str, *, tier: int, reason: str, hop: int = 0, via_sitelinks: int = 0, rank: int = 0) -> None:
        if qid in self.fetched or qid in self.data["missing"] or qid in self.data["redirects"]:
            return
        f = self.frontier.get(qid)
        if f is None:
            self.frontier[qid] = {
                "tier": tier,
                "reason": reason,
                "hop": hop,
                "indegree": 0,
                "via_sitelinks": via_sitelinks,
                "rank": rank,
            }
            return
        if tier < f["tier"]:
            f.update(tier=tier, reason=reason, hop=min(hop, f.get("hop", hop)))
        f["via_sitelinks"] = max(int(f.get("via_sitelinks", 0)), via_sitelinks)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, sort_keys=True))
        tmp.replace(self.path)


def kg_seeds(engine: Any) -> dict[str, tuple[int, str]]:
    """Seeds from the WDHARVEST KG (read only): causal subjects/objects, objects of P793/P1344/P607."""
    from sqlalchemy import text

    out: dict[str, tuple[int, str]] = {}
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT DISTINCT subject_qid, property_pid, object_qid FROM kg.statement "
                "WHERE property_pid = ANY(:p) AND object_qid IS NOT NULL"
            ),
            {"p": sorted(core.CAUSAL_PIDS | {"P793", "P1344", "P607"})},
        ).all()
    for s, p, o in rows:
        if p in core.CAUSAL_PIDS:
            out.setdefault(s, (TIER_KG_CAUSAL, f"kg:{p}:subject"))
            out[o] = min(out.get(o, (99, "")), (TIER_KG_CAUSAL, f"kg:{p}:object"))
        elif o.startswith("Q"):
            out[o] = min(out.get(o, (99, "")), (TIER_KG_EVENTS, f"kg:{p}:object"))
    return out


def initial_seeds(state: State, *, kg_engine: Any | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    rec = sorted(REPO_ROOT.glob("registry/kg/queries/*/_recorded/snapshot_claims_*.json"))
    for q, why in core.seeds_from_recorded(rec).items():
        state.add(q, tier=_recorded_tier(why), reason=why)
        counts["recorded"] = counts.get("recorded", 0) + 1
    specs = [p for p in REPO_ROOT.glob("registry/kg/queries/**/*.yaml") if "causal" not in p.parts]
    for q, why in core.seeds_from_specs(specs).items():
        state.add(q, tier=TIER_KG_EVENTS, reason=why)
        counts["spec_values"] = counts.get("spec_values", 0) + 1
    if kg_engine is not None:
        for q, (tier, why) in kg_seeds(kg_engine).items():
            state.add(q, tier=tier, reason=why)
            counts["kg"] = counts.get("kg", 0) + 1
    state.data["seed_counts"] = counts
    return counts


def _recorded_tier(reason: str) -> int:
    """Recorded seeds tied to a causal claim come first; event-context objects (P793/P1344/P607) later."""
    tail = reason.rsplit(":", 1)[-1]
    return TIER_RECORDED if tail == "causal_subject" or tail in core.CAUSAL_PIDS else TIER_KG_EVENTS


def retier_recorded(state: State) -> None:
    for f in state.frontier.values():
        if str(f.get("reason", "")).startswith("recorded:") and int(f.get("hop", 0)) == 0:
            f["tier"] = _recorded_tier(str(f["reason"]))


# --------------------------------------------------------------------------- harvest
def footprint_bytes(engine: Any, db: str) -> int:
    from sqlalchemy import text

    with engine.connect() as c:
        size = int(c.execute(text("SELECT pg_database_size(:d)"), {"d": db}).scalar() or 0)
    arch = 0
    try:
        with ARCHIVE_INDEX.open() as f:
            for line in f:
                if CAUSWD_VERSION in line or '"causwd"' in line:
                    with contextlib.suppress(ValueError):
                        arch += int(json.loads(line).get("stored_bytes") or 0)
    except OSError:
        pass
    return size + arch


class CausalHarvest:
    def __init__(
        self,
        sess: Session,
        state: State,
        *,
        db: str = DEFAULT_DB,
        allow: AllowList | None = None,
        max_entities: int = MAX_ENTITIES,
        max_hops: int = 1,
    ) -> None:
        self.sess = sess
        self.state = state
        self.db = db
        self.allow = allow or load_allowlist()
        self.max_entities = max_entities
        self.max_hops = max_hops
        self.stats: dict[str, int] = {}

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    def ingest(
        self,
        raw_by_id: Mapping[str, Mapping[str, Any]],
        sha_by_id: Mapping[str, str],
        shas: Sequence[str],
        retrieved_at: datetime,
        *,
        requested: Sequence[str],
        missing: Sequence[str],
        redirects: Mapping[str, str],
        mode: str,
    ) -> dict[str, Any]:
        st = self.state
        ents: list[WdEntity] = []
        for qid, raw in raw_by_id.items():
            if not qid.startswith("Q"):
                continue
            e = core.enrich_causal_statements(
                extract_entity(
                    raw,
                    self.allow,
                    raw_sha256=sha_by_id[qid],
                    retrieved_at=retrieved_at,
                    kind_hint="unclassified",
                    properties=core.HARVEST_PIDS,
                ),
                raw,
                self.allow,
            )
            ents.append(e)
        stmts = [s for e in ents for s in e.statements]
        rep: dict[str, Any] = {}
        if ents:
            rep = self.sess.sink.load(
                Batch(
                    SOURCE_WBAPI,
                    ents,
                    stmts,
                    list(shas),
                    retrieved_at,
                    {
                        "agent": "CAUSWD",
                        "harvester": CAUSWD_VERSION,
                        "base": HARVEST_VERSION,
                        "mode": mode,
                        "properties": list(core.HARVEST_PIDS),
                        "causal_reference_pids": sorted(core.CAUSAL_REFERENCE_PIDS),
                        "causal_qualifier_pids": sorted(core.CAUSAL_QUALIFIER_PIDS),
                        "allowlist_sha256": self.allow.sha256,
                        "redirects": dict(redirects),
                    },
                )
            )
        for src, dst in redirects.items():
            st.data["redirects"][src] = dst
            st.frontier.pop(src, None)
        for q in missing:
            st.data["missing"][q] = retrieved_at.isoformat()
            st.frontier.pop(q, None)
        for e in ents:
            f = st.frontier.pop(e.qid, None) or {}
            hop = int(f.get("hop", 0))
            tier = int(f.get("tier", TIER_WDQS))
            ncausal = sum(1 for s in e.statements if s.property in core.CAUSAL_PIDS)
            st.fetched[e.qid] = {
                "rev": e.lastrevid,
                "sitelinks": e.sitelinks_count,
                "causal": ncausal,
                "dated": core.is_dated(e),
                "hop": hop,
                "tier": tier,
                "reason": f.get("reason", ""),
                "at": retrieved_at.isoformat(),
            }
            self._bump("entities")
            self._bump("causal_statements", ncausal)
            if hop < self.max_hops:
                # neighbours of dated items (events have start/end/point-in-time) before those of concepts
                step = 1 if core.is_dated(e) else 3
                for o in core.causal_objects(e):
                    st.add(
                        o,
                        tier=tier + step,
                        reason=f"hop{hop + 1}:{e.qid}",
                        hop=hop + 1,
                        via_sitelinks=e.sitelinks_count,
                    )
                    if o in st.frontier:
                        st.frontier[o]["indegree"] = int(st.frontier[o].get("indegree", 0)) + 1
        for q in requested:  # requested ids neither returned, missing nor redirected: keep out of the queue
            if q in st.frontier and q not in raw_by_id:
                st.frontier.pop(q)
                st.data["missing"][q] = "not_returned"
        return rep

    def next_batch(self, n: int = 50) -> list[str]:
        if len(self.state.fetched) >= self.max_entities:
            return []
        room = self.max_entities - len(self.state.fetched)
        return core.prioritise(self.state.frontier)[: min(n, room)]

    async def fetch(self, qids: Sequence[str]) -> dict[str, Any]:
        reused, rest = self._reuse_archived(qids)
        res = (
            await self.sess.client.get_entities(
                rest, props=ENTITY_PROPS, meta={"spec": "causal.snapshots", "agent": "causwd"}
            )
            if rest
            else None
        )
        out: dict[str, Any] = {}
        if res is not None:
            out = self.ingest(
                res.entities,
                res.raw_sha_by_id,
                res.raw_sha256s,
                res.retrieved_at,
                requested=rest,
                missing=res.missing,
                redirects=res.redirects,
                mode="wbgetentities",
            )
            self._bump("requests_ok", res.requests)
        return {"reused": len(reused), "fetched": len(rest), "load": out}

    def _reuse_archived(self, qids: Sequence[str]) -> tuple[list[str], list[str]]:
        """Seeds with a Special:EntityData snapshot archived < 24 h ago are read from the archive (no request)."""
        archive = self.sess.client.archive
        used: list[str] = []
        rest: list[str] = []
        for q in qids:
            hit = archive.recent_entitydata(q, 24 * 3600.0)
            if hit is None:
                rest.append(q)
                continue
            body, sha, when = hit
            ents = {}
            for key, ent in (_json(body).get("entities") or {}).items():
                ents[ent.get("id", key)] = ent
            self.ingest(
                ents,
                dict.fromkeys(ents, sha),
                [sha],
                when,
                requested=[q],
                missing=[],
                redirects={q: next(k for k in ents if k != q)} if any(k != q for k in ents) else {},
                mode="entitydata_archived",
            )
            used.append(q)
            self._bump("reused_archive")
        return used, rest

    # -- CirrusSearch discovery -------------------------------------------------------------------------
    async def discover(self, pid: str, *, pages: int = 1) -> dict[str, Any]:
        d = self.state.data["discovery"].setdefault(pid, {"pages": 0, "hits": 0})
        # events only: the item must carry a start time or a point in time (CirrusSearch OR on property ids);
        # classes (P279) excluded. Without this filter the top hits are diseases and concepts.
        query = f"haswbstatement:{pid} haswbstatement:P580|P585 -haswbstatement:P279"
        tier = TIER_DISCOVERY_SPECIFIC if pid not in ("P828", "P1542") else TIER_DISCOVERY_GENERAL
        while d["pages"] < pages:
            offset = d["pages"] * 500
            if d.get("total") is not None and offset >= int(d["total"]):
                break
            params = {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srnamespace": "0",
                "srlimit": "500",
                "sroffset": str(offset),
                "srprop": "",
                "srinfo": "totalhits",
                "srsort": "incoming_links_desc",
                "format": "json",
                "maxlag": "5",
            }
            result, sha = await self.sess.client._request(
                self.sess.client.wbapi_http,
                f"{WBAPI_URL}?{urlencode(params)}",
                endpoint="wbapi",
                accept="application/json",
                meta={
                    "kind": "search",
                    "query": query,
                    "offset": offset,
                    "spec": "causal.discovery",
                    "agent": "causwd",
                },
            )
            doc = _json(result.body)
            if "error" in doc:
                raise WikidataError(f"search error {doc['error'].get('code')}", api_code=doc["error"].get("code"))
            q = doc.get("query") or {}
            hits = [h.get("title", "") for h in q.get("search") or []]
            hits = [h for h in hits if h[:1] == "Q" and h[1:].isdigit()]
            d["total"] = int((q.get("searchinfo") or {}).get("totalhits") or 0)
            d.setdefault("raw_sha256", []).append(sha)
            d["query"] = query
            for i, h in enumerate(hits):
                self.state.add(h, tier=tier, reason=f"search:{pid}", rank=offset + i)
                if h in self.state.frontier:
                    self.state.frontier[h]["rank"] = min(int(self.state.frontier[h].get("rank") or 10**9), offset + i)
            d["pages"] += 1
            d["hits"] += len(hits)
            self.state.save()
            if len(hits) < 500:
                break
        return dict(d)

    # -- WDQS listings ----------------------------------------------------------------------------------
    def wdqs_due(self) -> bool:
        last = float(self.state.data.get("wdqs_last_attempt", 0.0))
        return (
            self.sess.wdqs_client is not None
            and time.time() - last >= WDQS_MIN_GAP_SECONDS
            and not self.sess.client.lock.cooling_down("wdqs")
            and any(v.get("status") != "done" for v in self._wdqs_specs_state().values())
        )

    def wdqs_pending(self) -> bool:
        return self.sess.wdqs_client is not None and any(
            v.get("status") != "done" for v in self._wdqs_specs_state().values()
        )

    def _wdqs_specs_state(self) -> dict[str, Any]:
        out = {}
        for s in discover_specs([SPEC_DIR]):
            out[s.id] = self.state.data["wdqs"].setdefault(s.id, {"chunks": {}, "status": "pending"})
        return out

    async def wdqs_listings(self) -> dict[str, Any]:
        """One pass over the causal listing specs; stops at the first throttle/time-out (next try in ≥ 1 h)."""
        assert self.sess.wdqs_client is not None
        self.state.data["wdqs_last_attempt"] = time.time()
        self.state.save()
        h = Harvester(self.sess.wdqs_client, self.sess.sink, allow=self.allow)
        for spec in discover_specs([SPEC_DIR]):
            ss = self.state.data["wdqs"].setdefault(spec.id, {"chunks": {}, "status": "pending"})
            if ss.get("status") == "done" and ss.get("spec_sha256") == spec.sha256:
                continue
            if ss.get("spec_sha256") != spec.sha256:
                ss.update(chunks={}, status="pending", spec_sha256=spec.sha256)
            for ch in render_chunks(spec, state_dir=STATE_DIR):
                if ch.key in ss["chunks"]:
                    continue
                try:
                    res = await self.sess.wdqs_client.sparql(
                        with_limit(ch.query, int(spec.data["max_rows"]) + 1),
                        meta={"spec": spec.id, "chunk": ch.key, "agent": "causwd"},
                    )
                except (RateLimited, QueryTimeout, BudgetExhausted) as e:
                    ss["last_error"] = {
                        "code": getattr(e, "code", type(e).__name__),
                        "at": datetime.now(UTC).isoformat(),
                    }
                    self.state.save()
                    return {"status": "stopped", "spec": spec.id, "error": ss["last_error"]}
                stmts, skipped = h.rows_to_statements(spec, res)
                kept = [s for s in stmts if s is not None]
                # Scope and disk: only rows touching an item already reached from the event seeds (fetched or
                # queued) are loaded; the full listing stays in the raw archive (counts in the state).
                known = set(self.state.fetched) | set(self.state.frontier)
                touching = [s for s in kept if s.subject in known or (s.object_qid or "") in known]
                todo = [s for s in touching if s.subject not in self.state.fetched]
                for i in range(0, len(todo), 50_000):
                    self.sess.sink.load(
                        Batch(
                            SOURCE_WDQS,
                            [],
                            todo[i : i + 50_000],
                            [res.raw_sha256],
                            res.retrieved_at,
                            {
                                "spec": spec.id,
                                "chunk": ch.key,
                                "query_sha256": res.query_sha256,
                                "agent": "CAUSWD",
                                "harvester": CAUSWD_VERSION,
                            },
                        )
                    )
                for s in touching:
                    for q in (s.subject, s.object_qid):
                        if q and q.startswith("Q") and q not in known:
                            self.state.add(q, tier=TIER_WDQS, reason=f"wdqs:{spec.id}", hop=1)
                ss["chunks"][ch.key] = {
                    "rows": len(res.bindings),
                    "statements": len(kept),
                    "distinct_subjects": len({s.subject for s in kept}),
                    "touching_seed_reach": len(touching),
                    "loaded_as_wdqs_rows": len(todo),
                    "skipped": skipped,
                    "raw_sha256": res.raw_sha256,
                    "retrieved_at": res.retrieved_at.isoformat(),
                }
                self.state.save()
            ss["status"] = "done"
            self.state.save()
        return {"status": "done"}


async def wait_for_wbapi(client: WikidataClient, *, poll: float = 15.0, max_wait: float = 3600.0) -> float:
    waited = 0.0
    while client.lock.cooling_down("wbapi") and waited < max_wait:
        await asyncio.sleep(poll)
        waited += poll
    return waited


def disk_ok() -> bool:
    return shutil.disk_usage(REPO_ROOT).free >= MIN_FREE_BYTES


async def run(
    *,
    db: str = DEFAULT_DB,
    ledger_db: str = LEDGER_DB,
    cap_wbapi: int | None = None,
    cap_wdqs: int = 0,
    discover_pages: Mapping[str, int] | None = None,
    until: float | None = None,
    max_entities: int = MAX_ENTITIES,
    max_hops: int = 1,
    mode: str = "auto",
    batch_size: int = 10,
    linger: bool = False,
) -> dict[str, Any]:
    """One budget session (ends at the UTC day boundary, the cap, ``until`` or an empty frontier)."""
    state = State()
    out: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(), "db": db}
    day = datetime.now(UTC).date()
    async with causal_session(
        db=db, ledger_db=ledger_db, cap_wbapi=cap_wbapi, cap_wdqs=cap_wdqs, mode=mode, state=state
    ) as sess:
        out["run_id"], out["caps"] = sess.run_id, sess.caps
        log.info("CAUSWD session %s caps %s", sess.run_id, sess.caps)
        sess.sink.register_properties(load_allowlist())
        if not state.data.get("seeded"):
            out["seeds"] = initial_seeds(state, kg_engine=sess.ledger_engine)
            state.data["seeded"] = datetime.now(UTC).isoformat()
            state.save()
        retier_recorded(state)
        hv = CausalHarvest(sess, state, db=db, max_entities=max_entities, max_hops=max_hops)
        stop = "frontier_empty"
        pending_discovery = [
            p
            for p in DISCOVERY_ORDER
            if (discover_pages or {}).get(p, 0) > int(state.data["discovery"].get(p, {}).get("pages", 0))
            and not (
                state.data["discovery"].get(p, {}).get("total") is not None
                and int(state.data["discovery"][p]["pages"]) * 500 >= int(state.data["discovery"][p]["total"])
            )
        ]
        try:
            while True:
                if until is not None and time.time() >= until:
                    stop = "until"
                    break
                if datetime.now(UTC).date() != day:
                    stop = "day_boundary"
                    break
                if not disk_ok():
                    stop = "disk_free_below_5GB"
                    break
                if footprint_bytes(sess.kg_engine, db) > MAX_FOOTPRINT_BYTES:
                    stop = "footprint_cap"
                    break
                if hv.wdqs_due():
                    out.setdefault("wdqs", []).append(await hv.wdqs_listings())
                batch = hv.next_batch(batch_size)
                if not batch and pending_discovery:
                    await wait_for_wbapi(sess.client)
                    pid = pending_discovery[0]
                    try:
                        await hv.discover(pid, pages=int((discover_pages or {})[pid]))
                        pending_discovery.pop(0)
                    except RateLimited:
                        continue
                    continue
                if not batch and linger and until is not None and hv.wdqs_pending():
                    await asyncio.sleep(60)  # keep the session for the hourly WDQS attempt
                    continue
                if not batch:
                    stop = "frontier_empty" if len(state.fetched) < max_entities else "max_entities"
                    break
                if mode == "api":  # auto/linked_data: the client falls back to Special:EntityData itself
                    await wait_for_wbapi(sess.client)
                try:
                    r = await hv.fetch(batch)
                    log.info(
                        "batch %s: %s (fetched %d, frontier %d, wbapi requests %d/%d)",
                        batch[0],
                        r.get("load", {}).get("statements_inserted", r),
                        len(state.fetched),
                        len(state.frontier),
                        sess.wbapi.requests,
                        sess.wbapi.cap,
                    )
                except RateLimited:
                    hv._bump("rate_limited")
                    continue
                finally:
                    state.save()
        except BudgetExhausted as e:
            stop = f"budget:{e.details.get('reason', 'exhausted')}"
        out["stop"] = stop
        out["stats"] = dict(hv.stats)
        out["client"] = sess.client.stats.as_dict()
        out["requests"] = {"wbapi": sess.wbapi.requests, "wdqs": sess.wdqs.requests if sess.wdqs else 0}
    out["budget"] = sess.usage.get("budget")
    out["finished_at"] = datetime.now(UTC).isoformat()
    out["fetched_total"] = len(state.fetched)
    out["frontier_left"] = len(state.frontier)
    state.data["sessions"].append(
        {k: out.get(k) for k in ("run_id", "started_at", "finished_at", "caps", "stop", "stats", "requests", "budget")}
    )
    state.save()
    return out
