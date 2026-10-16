"""Verify the Wikidata ids the event taxonomy and event specs use (agent EVENTS).

``uv run python -m reva.events.verify [--titles-only|--ids-only] [--out PATH]``

No QID or PID in ``registry/events/taxonomy.yaml`` or ``registry/kg/queries/events/`` is typed from memory: each
one is confirmed by a recorded Wikidata response (labels en/fr, ``P31``/``P279`` claims with ranks, property
datatypes, ``lastrevid``). Lookups:

* **id lookup** — entity snapshots (``Special:EntityData``, one per request, CDN-cacheable; the action API was
  saturated machine-wide on 2026-10-16) for class QIDs found in the ``P31`` claims of recorded event items, for
  candidate class QIDs, and for properties (``datatype``);
* **enwiki title lookup** — ``Special:ItemByTitle/enwiki/<title>`` (redirects to the item behind the English
  Wikipedia article of a class, e.g. "Coup d'état"), then the item's snapshot.

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
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = REPO_ROOT / "registry" / "events" / "_recorded" / "wikidata_verification.json"
VERIFY_VERSION = "reva.events.verify/1"

#: Class QIDs seen as P31 objects of recorded event items (registry/kg/queries/history/_recorded) and candidate
#: class QIDs; each is confirmed (or rejected) by its recorded labels and P279 claims.
CLASS_IDS: tuple[str, ...] = (
    "Q1190554",
    "Q350604",
    "Q198",
    "Q8465",
    "Q21994376",
    "Q1006311",
    "Q103495",
    "Q864113",
    "Q178561",
    "Q188055",
    "Q645883",
    "Q2001676",
    "Q467011",
    "Q188686",
    "Q1384277",
    "Q10931",
    "Q45382",
    "Q1752346",
    "Q3882219",
    "Q124734",
    "Q1155622",
    "Q273120",
    "Q175331",
    "Q124757",
    "Q49776",
    "Q40231",
    "Q43109",
    "Q1076105",
    "Q858439",
    "Q900406",
    "Q3318733",
    "Q59858687",
    "Q194465",
    "Q518554",
    "Q230533",
    "Q41397",
    "Q3199915",
    "Q2223653",
    "Q381072",
    "Q131569",
    "Q625298",
    "Q6944158",
    "Q18564543",
    "Q25929224",
    "Q290178",
    "Q114380",
    "Q176494",
    "Q1020018",
    "Q185565",
    "Q1814941",
    "Q207781",
    "Q123193388",
    "Q8065",
    "Q7944",
    "Q8070",
    "Q8068",
    "Q8092",
    "Q7692360",
    "Q168247",
    "Q44512",
    "Q12184",
    "Q3241045",
    "Q12772819",
    "Q2401485",
    "Q177626",
    "Q180684",
)
#: Properties the event view and the event specs use or request (datatype + labels), and the identifier
#: properties with small numeric values found on archived war items (checked for UCDP / COW identifiers).
PROPERTY_IDS: tuple[str, ...] = (
    "P1923",
    "P8031",
    "P8032",
    "P991",
    "P726",
    "P1590",
    "P1339",
    "P1120",
    "P1346",
    "P710",
    "P1344",
    "P607",
    "P1891",
    "P828",
    "P1542",
    "P1478",
    "P1479",
    "P1536",
    "P1537",
    "P361",
    "P527",
    "P155",
    "P156",
    "P793",
    "P585",
    "P580",
    "P582",
    "P571",
    "P576",
    "P276",
    "P17",
    "P131",
    "P664",
    "P137",
    "P2868",
    "P3831",
    "P7166",
    "P7695",
    "P13956",
    "P8472",
)
ITEM_BY_TITLE = "https://www.wikidata.org/wiki/Special:ItemByTitle/enwiki/{title}"


def _claims(ent: Mapping[str, Any], pid: str) -> list[dict[str, Any]]:
    out = []
    for c in (ent.get("claims") or {}).get(pid) or []:
        snak = c.get("mainsnak") or {}
        dv = (snak.get("datavalue") or {}).get("value")
        obj = dv.get("id") if isinstance(dv, dict) else None
        out.append({"o": obj, "rank": c.get("rank"), "statement": c.get("id"), "snaktype": snak.get("snaktype")})
    return out


def compact(
    ent: Mapping[str, Any], *, raw_sha256: str, retrieved_at: str, lookup: str, title: str | None = None
) -> dict[str, Any]:
    labels = {k: v.get("value") for k, v in (ent.get("labels") or {}).items() if k in ("en", "fr")}
    rec: dict[str, Any] = {
        "id": ent.get("id"),
        "labels": labels,
        "lastrevid": ent.get("lastrevid"),
        "lookup": lookup,
        "raw_sha256": raw_sha256,
        "retrieved_at": retrieved_at,
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


async def lookup_entities(client: Any, ids: Sequence[str]) -> list[dict[str, Any]]:
    """Snapshots through the client (Special:EntityData in ``linked_data`` mode: one entity per request, archive
    reuse), reduced to the compact verification record."""
    res = await client.get_entities(
        ids, props=("info", "labels", "claims"), languages=("en", "fr"), meta={"purpose": "events_taxonomy"}
    )
    ret = res.retrieved_at.isoformat()
    out = [
        compact(ent, raw_sha256=res.raw_sha_by_id.get(eid, ""), retrieved_at=ret, lookup="id")
        for eid, ent in res.entities.items()
    ]
    out += [{"id": m, "missing": True, "lookup": "id", "retrieved_at": ret} for m in res.missing]
    for old, new in res.redirects.items():
        out.append({"id": old, "redirect_to": new, "lookup": "id", "retrieved_at": ret})
    return out


async def lookup_titles(client: Any, titles: Sequence[str]) -> list[dict[str, Any]]:
    """enwiki title -> item through ``Special:ItemByTitle`` (a page view that redirects to the item; outside the
    action-API limiter), then the item's snapshot."""
    from reva.adapters.connectors.wikidata import EntityMissing, qid_from_uri

    out: list[dict[str, Any]] = []
    for title in titles:
        url = ITEM_BY_TITLE.format(title=quote(title.replace(" ", "_"), safe=""))
        try:
            result, sha = await client._request(
                client.wbapi_http,
                url,
                endpoint="pages",
                accept="text/html",
                meta={"kind": "item_by_title", "title": title, "purpose": "events_taxonomy"},
            )
        except EntityMissing:
            out.append({"id": None, "enwiki_title": title, "missing": True, "lookup": "enwiki_title"})
            continue
        target = qid_from_uri("http://www.wikidata.org/entity/" + result.final_url.rstrip("/").rsplit("/", 1)[-1])
        rec: dict[str, Any] = {
            "id": target,
            "enwiki_title": title,
            "lookup": "enwiki_title",
            "item_by_title_sha256": sha,
            "retrieved_at": result.retrieved_at.isoformat(),
        }
        if target:
            snap = await lookup_entities(client, [target])
            rec = {**(snap[0] if snap else {}), **rec}
        out.append(rec)
    return out


class _NoWdqs:
    """WDQS is not used by EVENTS (its shared daily budget is spent by the harvest): any call is refused."""

    async def get(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("reva.events does not query WDQS")


@contextlib.asynccontextmanager
async def wbapi_client(
    *, purpose: str, planned_wbapi: int, mode: str = "api", reuse_hours: float = 0.0
) -> AsyncIterator[Any]:
    """Like :func:`reva.kg.harvest.budgeted_client`, but reserves only ``wikidata.wbapi`` (reserve -> meter every
    request -> settle, ledger in the throwaway harvest database): EVENTS never queries WDQS, so a spent WDQS
    budget must not block the API lookups. Same lock, pacing, archive and User-Agent."""
    import uuid

    from reva.adapters.connectors.wikidata import SOURCE_WBAPI, Archive, WikidataClient
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.kg.harvest import DEFAULT_DB, OBJECT_ROOT, BudgetedSession, harvest_settings, live_http
    from reva.operations.budgets import load_budget, metered, reserve_ingest, settle_metered

    # The harvest database exists (WDHARVEST creates and migrates it); EVENTS only writes ledger rows there.
    settings = harvest_settings(DEFAULT_DB)
    engine = make_engine("worker", settings)
    store = FilesystemArtifactStore(OBJECT_ROOT, settings.reva_s3_prefix)
    run_id = f"kg{purpose}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    inner = live_http()
    wbapi = None
    session = None
    try:
        budget = load_budget()
        res = reserve_ingest(
            engine, job_id=run_id, attempt=1, source_key=SOURCE_WBAPI, planned_requests=planned_wbapi, budget=budget
        )
        wbapi = metered(inner, engine, res, budget=budget)
        client = WikidataClient(
            _NoWdqs(),
            wbapi,
            archive=Archive(store, REPO_ROOT / "var" / "kg" / "archive_index.jsonl"),
            mode=mode,
            reuse_seconds=reuse_hours * 3600.0,
        )
        session = BudgetedSession(client, engine, store, run_id)
        yield session
    finally:
        if wbapi is not None:
            settled = settle_metered(engine, wbapi)
            if session is not None:
                session.usage["budget"] = [settled]
        await inner.aclose()
        engine.dispose()


async def run(
    *, titles: Sequence[str] = (), ids: Sequence[str] = (), out: Path = OUT, reuse_hours: float = 72.0
) -> dict[str, Any]:
    prev: dict[str, Any] = json.loads(out.read_text()) if out.exists() else {}
    records: dict[str, dict[str, Any]] = dict(prev.get("entities") or {})
    title_records: dict[str, dict[str, Any]] = dict(prev.get("titles") or {})
    planned = len(ids) + 2 * len(titles)
    async with wbapi_client(
        purpose="events", planned_wbapi=max(planned, 1), mode="linked_data", reuse_hours=reuse_hours
    ) as sess:
        if ids:
            for r in await lookup_entities(sess.client, ids):
                records[str(r["id"])] = r
        if titles:
            for r in await lookup_titles(sess.client, titles):
                title_records[str(r.get("enwiki_title"))] = r
                if r.get("id") and not r.get("missing"):
                    records[str(r["id"])] = r
        usage = sess.usage
    doc = {
        "schema": "reva-events-wikidata-verification/1",
        "built_by": VERIFY_VERSION,
        "note": "Compact records of Wikidata entity snapshots (Special:EntityData through WDHARVEST's locked, "
        "budget-metered client): labels en/fr, non-deprecated P31/P279 claims with rank and statement id, "
        "property datatype, lastrevid. Raw bodies are archived content-addressed in var/kg/objects "
        "(raw_sha256, gzip; var/kg/archive_index.jsonl). Wikidata is community-edited (CC0).",
        "updated_at": datetime.now(UTC).isoformat(),
        "titles": dict(sorted(title_records.items())),
        "entities": dict(
            sorted(records.items(), key=lambda kv: (kv[0][0], int(kv[0][1:]) if kv[0][1:].isdigit() else 0))
        ),
    }
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return {"entities": len(records), "titles": len(title_records), "usage": usage.get("budget")}


async def probe_specs(paths: Sequence[Path], *, max_specs: int = 11) -> dict[str, Any]:
    """Small API-mode probe of event specs: one search per spec root class (``haswbstatement:P31=<root>``,
    REST search in ``linked_data`` mode: at most 100 hits, no paging) -> hit counts and a few QIDs. Nothing is
    loaded; the probe only shows that each ``api_search`` block selects real items."""
    from reva.kg.harvest import discover_specs

    specs = [sp for sp in discover_specs(list(paths)) if sp.get("api_search")][:max_specs]
    out: dict[str, Any] = {}
    async with wbapi_client(purpose="eventsprobe", planned_wbapi=len(specs), mode="linked_data") as sess:
        for sp in specs:
            root = sp.get("api_search")["instance_of"][0]
            hits, total, sha = await sess.client.search_page(
                f"haswbstatement:P31={root}", limit=100, meta={"purpose": "events_spec_probe", "spec": sp.id}
            )
            out[sp.id] = {
                "root": root,
                "hits": len(hits),
                "total_reported": total,
                "first": hits[:5],
                "raw_sha256": sha,
            }
        out["_usage"] = sess.usage.get("budget")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reva.events.verify")
    ap.add_argument("--classes", action="store_true", help="look up CLASS_IDS")
    ap.add_argument("--properties", action="store_true", help="look up PROPERTY_IDS")
    ap.add_argument("--ids", nargs="*", default=[], help="extra Q/P ids")
    ap.add_argument("--titles", nargs="*", default=[], help="enwiki titles (Special:ItemByTitle)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--probe-specs", nargs="*", type=Path, help="API-mode probe of event specs (no KG writes)")
    a = ap.parse_args(argv)
    if a.probe_specs is not None:
        res = asyncio.run(probe_specs(a.probe_specs or [REPO_ROOT / "registry" / "kg" / "queries" / "events"]))
        print(json.dumps(res, indent=1, default=str))
        return 0
    ids = list(a.ids) + (list(CLASS_IDS) if a.classes else []) + (list(PROPERTY_IDS) if a.properties else [])
    res = asyncio.run(run(titles=a.titles, ids=list(dict.fromkeys(ids)), out=a.out))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
