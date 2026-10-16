"""Verify the Wikidata ids the event taxonomy and event specs use (agent EVENTS).

``uv run python -m reva.events.verify [--titles-only|--ids-only] [--out PATH]``

No QID or PID in ``registry/events/taxonomy.yaml`` or ``registry/kg/queries/events/`` is typed from memory: each
one is confirmed by a recorded Wikidata response (labels en/fr, ``P31``/``P279`` claims with ranks, property
datatypes, ``lastrevid``). Lookups:

* **enwiki title lookup** — ``wbgetentities sites=enwiki&titles=...`` (≤ 50 titles per call): the item behind
  the English Wikipedia article of a class ("Coup d'état", "Assassination", ...);
* **id lookup** — ``wbgetentities ids=...`` (≤ 50 per call) for class QIDs found in the ``P31`` claims of
  recorded event items, for candidate QIDs, and for properties (``datatype``).

Every request goes through WDHARVEST's locked, budget-metered client (:func:`reva.kg.harvest.budgeted_client`,
ledger in the throwaway harvest database, never ``reva``); raw bodies are archived content-addressed in
``var/kg/objects`` and their sha256 is cited per entity in the compact record written to
``registry/events/_recorded/wikidata_verification.json``. Wikidata is community-edited (CC0): a recorded class
chain is what Wikidata said at that revision, not a truth.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = REPO_ROOT / "registry" / "events" / "_recorded" / "wikidata_verification.json"
WBAPI_URL = "https://www.wikidata.org/w/api.php"
VERIFY_VERSION = "reva.events.verify/1"

#: English Wikipedia article titles of candidate event classes (and of the demonstration events).
TITLES: tuple[str, ...] = (
    "Assassination", "Coup d'état", "Self-coup", "Election", "General election", "Presidential election",
    "Referendum", "Protest", "Demonstration (political)", "Riot", "Strike action", "General strike", "Rebellion",
    "Declaration of independence", "Independence", "Reform", "Land reform", "Human migration",
    "Population transfer", "Refugee crisis", "Genocide", "Massacre", "Crisis", "Political crisis",
    "International crisis", "Constitutional crisis", "Financial crisis", "Siege", "Invasion", "Military occupation",
    "Armistice", "Peacekeeping", "Economic sanctions", "Pogrom", "Insurgency", "Colonial war", "World war",
    "Battle", "Treaty", "Natural disaster", "Epidemic", "Pandemic", "Famine", "Revolution", "Civil war",
    "Wall Street crash of 1929", "Suez Crisis", "Algerian War", "Treaty of Tordesillas", "Great Depression",
)

#: Class QIDs seen as P31 objects of recorded event items (registry/kg/queries/history/_recorded) and other
#: candidates named by existing specs; each is confirmed (or rejected) by its recorded labels and P279 claims.
CLASS_IDS: tuple[str, ...] = (
    "Q1190554", "Q198", "Q8465", "Q21994376", "Q1006311", "Q864113", "Q103495", "Q178561", "Q645883", "Q2001676",
    "Q1384277", "Q350604", "Q180684", "Q10931", "Q45382", "Q1155622", "Q131569", "Q625298", "Q6944158",
    "Q194465", "Q518554", "Q1361229", "Q188686", "Q230533", "Q3318733", "Q59858687", "Q381072", "Q18564543",
    "Q25929224", "Q12772819", "Q2401485", "Q209715", "Q3882219", "Q40231", "Q43109", "Q273120", "Q175331",
    "Q124757", "Q49776", "Q124734", "Q41397", "Q3199915", "Q2223653", "Q467011", "Q188055", "Q290178",
    "Q114380", "Q176494", "Q1020018", "Q185565",
)
CLASS_IDS_2: tuple[str, ...] = (
    "Q1814941", "Q207781", "Q123193388", "Q7944", "Q8070", "Q8068", "Q8092", "Q8065", "Q7692360", "Q168247",
    "Q44512", "Q12184", "Q3241045", "Q1673271", "Q1510761", "Q15835236", "Q1920219", "Q17091170", "Q831663",
    "Q2635077", "Q4164871", "Q1752346", "Q900406", "Q177626", "Q858439", "Q1076105", "Q152450", "Q159821",
)
#: Properties the event view and the event specs use or request (datatype + labels).
PROPERTY_IDS: tuple[str, ...] = (
    "P1923", "P8031", "P8032", "P991", "P726", "P1590", "P1339", "P1120", "P1346", "P710", "P1344", "P607",
    "P1891", "P828", "P1542", "P1478", "P1479", "P1536", "P1537", "P361", "P527", "P155", "P156", "P793",
    "P585", "P580", "P582", "P571", "P576", "P276", "P17", "P131", "P664", "P2868", "P3831", "P7166",
    "P2347", "P7305", "P8472", "P12596", "P13750", "P9545", "P10565", "P13772", "P2163", "P6293", "P5986",
)


def _claims(ent: Mapping[str, Any], pid: str) -> list[dict[str, Any]]:
    out = []
    for c in (ent.get("claims") or {}).get(pid) or []:
        snak = c.get("mainsnak") or {}
        dv = (snak.get("datavalue") or {}).get("value")
        obj = dv.get("id") if isinstance(dv, dict) else None
        out.append({"o": obj, "rank": c.get("rank"), "statement": c.get("id"), "snaktype": snak.get("snaktype")})
    return out


def compact(ent: Mapping[str, Any], *, raw_sha256: str, retrieved_at: str, lookup: str,
            title: str | None = None) -> dict[str, Any]:
    labels = {k: v.get("value") for k, v in (ent.get("labels") or {}).items() if k in ("en", "fr")}
    rec: dict[str, Any] = {
        "id": ent.get("id"), "labels": labels, "lastrevid": ent.get("lastrevid"), "lookup": lookup,
        "raw_sha256": raw_sha256, "retrieved_at": retrieved_at,
    }
    if title is not None:
        rec["enwiki_title"] = title
    if ent.get("datatype"):
        rec["datatype"] = ent["datatype"]
    for pid in ("P31", "P279"):
        cl = [c for c in _claims(ent, pid) if c["rank"] != "deprecated"]
        if cl:
            rec[pid] = cl
    return rec


async def _get(client: Any, params: Mapping[str, str], meta: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    url = f"{WBAPI_URL}?{urlencode(params, safe='|')}"
    result, sha = await client._request(client.wbapi_http, url, endpoint="wbapi", accept="application/json",
                                        meta=meta)
    doc = json.loads(result.body)
    if "error" in doc:
        raise RuntimeError(f"wbgetentities error: {str(doc['error'])[:300]}")
    return doc, sha, result.retrieved_at.isoformat()


async def lookup_titles(client: Any, titles: Sequence[str]) -> list[dict[str, Any]]:
    params = {"action": "wbgetentities", "sites": "enwiki", "titles": "|".join(titles), "props": "info|labels|claims",
              "languages": "en|fr", "redirects": "yes", "format": "json", "maxlag": "5"}
    doc, sha, ret = await _get(client, params, {"kind": "title_lookup", "purpose": "events_taxonomy",
                                                "titles": list(titles)})
    out = []
    by_title: dict[str, str] = {}
    for n in (doc.get("normalized") or {}).get("n", []) if isinstance(doc.get("normalized"), dict) else []:
        by_title[n.get("to")] = n.get("from")
    for key, ent in (doc.get("entities") or {}).items():
        if "missing" in ent:
            out.append({"id": None, "lookup": "enwiki_title", "enwiki_title": ent.get("title"), "missing": True,
                        "raw_sha256": sha, "retrieved_at": ret})
            continue
        title = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
        out.append(compact(ent, raw_sha256=sha, retrieved_at=ret, lookup="enwiki_title", title=title or key))
    return out


async def lookup_ids(client: Any, ids: Sequence[str], *, props: str = "info|labels|claims") -> list[dict[str, Any]]:
    params = {"action": "wbgetentities", "ids": "|".join(ids), "props": props, "languages": "en|fr",
              "format": "json", "maxlag": "5"}
    doc, sha, ret = await _get(client, params, {"kind": "wbgetentities", "purpose": "events_taxonomy",
                                                "ids": list(ids), "props": props.split("|")})
    out = []
    for key, ent in (doc.get("entities") or {}).items():
        if "missing" in ent:
            out.append({"id": key, "missing": True, "lookup": "id", "raw_sha256": sha, "retrieved_at": ret})
            continue
        out.append(compact(ent, raw_sha256=sha, retrieved_at=ret, lookup="id"))
    return out


class _NoWdqs:
    """WDQS is not used by EVENTS (its shared daily budget is spent by the harvest): any call is refused."""

    async def get(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("reva.events does not query WDQS")


@contextlib.asynccontextmanager
async def wbapi_client(*, purpose: str, planned_wbapi: int, mode: str = "api",
                       reuse_hours: float = 0.0) -> AsyncIterator[Any]:
    """Like :func:`reva.kg.harvest.budgeted_client`, but reserves only ``wikidata.wbapi`` (reserve -> meter every
    request -> settle, ledger in the throwaway harvest database): EVENTS never queries WDQS, so a spent WDQS
    budget must not block the API lookups. Same lock, pacing, archive and User-Agent."""
    import uuid

    from reva.adapters.connectors.wikidata import SOURCE_WBAPI, Archive, WikidataClient
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.kg.harvest import DEFAULT_DB, OBJECT_ROOT, BudgetedSession, ensure_database, live_http
    from reva.operations.budgets import load_budget, metered, reserve_ingest, settle_metered

    settings = ensure_database(DEFAULT_DB)
    engine = make_engine("worker", settings)
    store = FilesystemArtifactStore(OBJECT_ROOT, settings.reva_s3_prefix)
    run_id = f"kg{purpose}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    inner = live_http()
    wbapi = None
    session = None
    try:
        budget = load_budget()
        res = reserve_ingest(engine, job_id=run_id, attempt=1, source_key=SOURCE_WBAPI,
                             planned_requests=planned_wbapi, budget=budget)
        wbapi = metered(inner, engine, res, budget=budget)
        client = WikidataClient(_NoWdqs(), wbapi,
                                archive=Archive(store, REPO_ROOT / "var" / "kg" / "archive_index.jsonl"),
                                mode=mode, reuse_seconds=reuse_hours * 3600.0)
        session = BudgetedSession(client, engine, store, run_id)
        yield session
    finally:
        if wbapi is not None:
            settled = settle_metered(engine, wbapi)
            if session is not None:
                session.usage["budget"] = [settled]
        await inner.aclose()
        engine.dispose()


async def run(*, titles: bool = True, ids: bool = True, out: Path = OUT) -> dict[str, Any]:
    prev: dict[str, Any] = json.loads(out.read_text()) if out.exists() else {}
    records: dict[str, dict[str, Any]] = dict(prev.get("entities") or {})
    title_records: dict[str, dict[str, Any]] = dict(prev.get("titles") or {})
    planned = (1 if titles else 0) + (3 if ids else 0)
    async with wbapi_client(purpose="events", planned_wbapi=planned, mode="api") as sess:
        if titles:
            for r in await lookup_titles(sess.client, TITLES):
                title_records[str(r.get("enwiki_title"))] = r
                if r.get("id"):
                    records[r["id"]] = r
        if ids:
            for chunk in (CLASS_IDS, CLASS_IDS_2):
                for r in await lookup_ids(sess.client, chunk):
                    records[r["id"]] = {**records.get(r["id"], {}), **r} if not r.get("missing") else r
            for r in await lookup_ids(sess.client, PROPERTY_IDS, props="info|labels|datatype"):
                records[r["id"]] = r
        usage = sess.usage
    doc = {
        "schema": "reva-events-wikidata-verification/1",
        "built_by": VERIFY_VERSION,
        "note": "Compact records of wbgetentities responses (labels en/fr, non-deprecated P31/P279 claims with rank, "
                "property datatype, lastrevid). Raw bodies are archived content-addressed in var/kg/objects "
                "(raw_sha256, gzip, see var/kg/archive_index.jsonl). Wikidata is community-edited (CC0).",
        "updated_at": datetime.now(UTC).isoformat(),
        "titles": dict(sorted(title_records.items())),
        "entities": dict(sorted(records.items(), key=lambda kv: (kv[0][0], int(kv[0][1:]) if kv[0][1:].isdigit() else 0))),
    }
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return {"entities": len(records), "titles": len(title_records), "usage": usage.get("budget")}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reva.events.verify")
    ap.add_argument("--titles-only", action="store_true")
    ap.add_argument("--ids-only", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    res = asyncio.run(run(titles=not a.ids_only, ids=not a.titles_only, out=a.out))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
