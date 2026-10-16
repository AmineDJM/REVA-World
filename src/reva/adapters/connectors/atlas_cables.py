"""Submarine communications cables from Wikidata -> country-pair cable links (agent A-INFRA, V3).

Why Wikidata
------------
The reference dataset, TeleGeography's Submarine Cable Map, is **not used**: its former GitHub repository
(``telegeography/www.submarinecablemap.com``) published the data under CC BY-NC-SA 3.0 (non-commercial) and is
no longer reachable (HTTP 404, 2026-10-16); the live site's JSON API states no licence at all. Wikidata (CC0) is
the open alternative: every item that is an instance of *submarine communications cable* (``Q506572``) or of one
of its subclasses (e.g. *transatlantic communications cable* ``Q361047``) with the countries it lists (P17).

Access (one request at a time, machine-wide)
--------------------------------------------
The Wikibase action API is often saturated for the shared egress (HTTP 429), so this connector uses the two
paths WDHARVEST uses when it is: ``Special:WhatLinksHere/<class>`` (HTML, namespace 0, 5,000 per page) to find
candidate items, and ``Special:EntityData/<QID>.json`` for each candidate's full snapshot (``lastrevid``
included). Every request goes through ``run.get`` (budget-metered, ``source_key`` ``wikidata.wbapi``, shared with
WDHARVEST) **inside** WDHARVEST's machine-wide Wikidata lock (``MachineLock``: >= 2 s between requests of any
process, shared per-endpoint cool-downs after a 429), with WDHARVEST's project User-Agent (no personal data).
Subclasses found among the candidates (``P279`` -> a cable class) are expanded the same way (depth <= 2).

What is derived (dataset ``submarine_cables``; all counts ``derived: count``, Wikidata community-edited)
-------------------------------------------------------------------------------------------------------
A *cable* is an item with ``P31`` in the cable classes (normal/preferred rank). Its countries are its ``P17``
values (non-deprecated), mapped to crosswalk countries through ``registry/entities/country_qids.json``
(WDHARVEST, QID per ISO3); a P17 value that is not a crosswalk country (e.g. a historical state) is kept as
``provider_geo = <QID>`` with ``iso3`` null. Its service year is ``P729`` (service entry), else ``P571``
(inception) -- the property used is named in the flags.

* ``CABLES_LISTED`` (snapshot: retrieval day): cables listing the country;
* ``CABLE_PARTNER_COUNTRIES`` (snapshot): distinct other countries sharing at least one cable;
* ``CABLES_ENTERING_SERVICE`` (calendar year): cables listing the country whose service year is that year;
* ``CABLES_SHARED|partner=<ISO3>`` (snapshot, undirected: emitted for both countries): cables listing both;
  ``flags.cables`` names them (QID, label, year, revision);
* ``CABLE_FIRST_SERVICE_YEAR|partner=<ISO3>`` (snapshot): the earliest service year among the shared cables
  that have one (``derived: min``) -- "connected by cable since".

Limits: Wikidata has items for only part of the world's ~600 cable systems, P17 is the community's list of
countries (normally landing countries, not verified) and retired cables are not distinguished unless ``P730``/
``P576`` is set (recorded per cable, not filtered). Absent rows mean "no Wikidata cable item lists both", never
"no cable".
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_infra_common as ic
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.wikidata_cables"
VERSION = "1.1.0"  # 1.1.0: wbgetentities batches with EntityData fallback; 404 pages skipped
SOURCE_KEY = "wikidata.wbapi"
ATLAS_SOURCE = "wikidata"
DATASET = "submarine_cables"
PARSER_VERSION = "atlas_wikidata_cables/1"
HOST = "www.wikidata.org"
ROOT_CLASS = "Q506572"  # submarine communications cable
WLH_URL = "https://www.wikidata.org/w/index.php?title=Special:WhatLinksHere/{qid}&namespace=0&limit=5000"
ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
API_URL = (
    "https://www.wikidata.org/w/api.php?action=wbgetentities&ids={ids}&props=info%7Cclaims%7Clabels"
    "&languages=en&format=json&maxlag=5"
)
API_BATCH = 50
#: tests replace the machine-wide Wikidata lock (``wikidata.MachineLock``) with a no-op one
LOCK_FACTORY: Any = None
MAX_SUBCLASS_DEPTH = 2
MAX_CANDIDATES = 1500
MAX_ATTEMPTS = 4
INDICATORS = (
    "CABLES_LISTED",
    "CABLE_PARTNER_COUNTRIES",
    "CABLES_ENTERING_SERVICE",
)
RIGHTS = {
    "licence": "CC0-1.0",
    "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "attribution": "Wikidata (community-edited), items of class Q506572 'submarine communications cable'",
    "verification": "community_edited_not_verified",
}
SKIPPED = {
    "telegeography": "TeleGeography Submarine Cable Map: CC BY-NC-SA 3.0 (non-commercial) per its former GitHub "
    "repository (now 404); the live API states no licence -> not used",
}
_LI = re.compile(r'<li>(?:<bdi[^>]*>)?<a href="/wiki/(Q[0-9]+)"')


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Wikidata (Wikimedia Foundation; community-edited), submarine communications cable items",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); community-edited, not verified",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(f"wikidata:class:{ROOT_CLASS}",),
        cadence_seconds=30 * ic.DAY,
        planned_requests=600,
        max_response_bytes=5_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="Cable items via Special:WhatLinksHere + Special:EntityData under the machine-wide Wikidata lock.",
    )


# ----------------------------------------------------------------------------- fetch (locked, paced)
async def _locked_get(
    run: Any, url: str, *, key: str, endpoint: str, media: tuple[str, ...], attempts: int = MAX_ATTEMPTS
) -> ic.Fetched:
    from reva.adapters.connectors.wikidata import MachineLock, user_agent

    if run.origin != "live":  # recorded fixtures: no provider traffic, no machine-wide pacing
        raw = await run.get(url, key=key, media_types=media, max_bytes=run.spec.max_response_bytes,
                            headers={"User-Agent": user_agent(), "Accept": media[0]})  # fmt: skip
        return ic.Fetched(name=key, url=url, body=raw.body, sha256=run.archive(raw), retrieved_at=raw.retrieved_at)
    lock = (LOCK_FACTORY or MachineLock)()
    for attempt in range(1, attempts + 1):
        async with lock.slot(endpoint) as slot:
            try:
                raw = await run.get(url, key=key, media_types=media, max_bytes=run.spec.max_response_bytes,
                                    headers={"User-Agent": user_agent(), "Accept": media[0]})  # fmt: skip
            except SourceResponseError as e:
                status = e.details.get("status")
                if status in (429, 503):
                    slot.cooldown(min(600.0, 30.0 * 2 ** (attempt - 1)))  # every process waits
                if status in (429, 503) and attempt < attempts:
                    continue
                raise
        sha = run.archive(raw)
        return ic.Fetched(name=key, url=url, body=raw.body, sha256=sha, retrieved_at=raw.retrieved_at)
    raise SourceResponseError("Wikidata still throttled", retryable=True, kind="throttled")


def parse_links(html: bytes, target: str) -> list[str]:
    text = html.decode("utf-8", "replace")
    if "mw-whatlinkshere-list" not in text and "Special:WhatLinksHere" not in text:
        raise SourceResponseError("not a WhatLinksHere page", retryable=False, kind="schema_drift")
    return [q for q in dict.fromkeys(_LI.findall(text)) if q != target]


# ----------------------------------------------------------------------------- entity parsing
def _ids(claims: dict[str, Any], pid: str) -> list[str]:
    out = []
    for c in claims.get(pid, []):
        if c.get("rank") == "deprecated":
            continue
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and str(v.get("id", "")).startswith("Q"):
            out.append(v["id"])
    return list(dict.fromkeys(out))


def _year(claims: dict[str, Any], pid: str) -> int | None:
    best: list[tuple[int, int]] = []  # (rank order, year)
    for c in claims.get(pid, []):
        if c.get("rank") == "deprecated":
            continue
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if not isinstance(v, dict) or "time" not in v or int(v.get("precision", 0)) < 9:
            continue  # coarser than a year: no service year
        m = re.match(r"^\+(\d{4})-", v["time"])
        if m:
            best.append((0 if c.get("rank") == "preferred" else 1, int(m.group(1))))
    if not best:
        return None
    top = min(r for r, _ in best)
    return min(y for r, y in best if r == top)


def _amount(claims: dict[str, Any], pid: str) -> dict[str, str] | None:
    for c in claims.get(pid, []):
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if c.get("rank") != "deprecated" and isinstance(v, dict) and "amount" in v:
            return {"amount": str(v["amount"]).lstrip("+"), "unit": str(v.get("unit", "")).rsplit("/", 1)[-1]}
    return None


def entity_of(body: bytes, qid: str) -> dict[str, Any]:
    try:
        ents = json.loads(body)["entities"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceResponseError(f"EntityData {qid}: not Wikibase JSON", retryable=False, kind="schema_drift") from exc
    ent: dict[str, Any] = ents.get(qid) or next(iter(ents.values()))  # a redirect answers with the target id
    return ent


def describe(e: dict[str, Any]) -> dict[str, Any]:
    cl = e.get("claims") or {}
    svc, inc = _year(cl, "P729"), _year(cl, "P571")
    return {
        "qid": e["id"],
        "label": ((e.get("labels") or {}).get("en") or {}).get("value") or e["id"],
        "revision": e.get("lastrevid"),
        "p31": _ids(cl, "P31"),
        "p279": _ids(cl, "P279"),
        "countries": _ids(cl, "P17"),
        "year": svc if svc is not None else inc,
        "year_property": "P729" if svc is not None else ("P571" if inc is not None else None),
        "retired": _year(cl, "P730") or _year(cl, "P576"),
        "length": _amount(cl, "P2043"),
        "owners": _ids(cl, "P127"),
    }


def country_index() -> dict[str, str]:
    """Wikidata QID -> crosswalk ISO3 (WDHARVEST's registry/entities/country_qids.json)."""
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    return {q: iso for iso, q in load_country_qids().items()}


def build(cables: list[dict[str, Any]], raws: dict[str, str], day: date) -> tuple[ic.RowBuilder, dict[str, Any]]:
    """Rows from parsed cables. ``raws``: cable QID -> raw sha256 of its EntityData snapshot."""
    cidx = country_index()
    if not cidx:
        raise SourceResponseError("registry/entities/country_qids.json missing: countries cannot be mapped",
                                  retryable=False, kind="config")  # fmt: skip
    snap = ic.day_span(day)
    by_country: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cables:
        for q in c["countries"]:
            by_country[q].append(c)
    rb = ic.RowBuilder(ATLAS_SOURCE, DATASET)
    base: dict[str, Any] = {"verification": "community_edited_not_verified", "licence": "CC0-1.0", "class": ROOT_CLASS,
            "coverage": "Wikidata cable items only (not every cable system has one); countries = P17 as edited"}  # fmt: skip

    def ref(cs: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        cs = sorted(cs, key=lambda c: int(c["qid"][1:]))
        return raws[cs[0]["qid"]], {
            "cables": [
                {"qid": c["qid"], "label": c["label"], "year": c["year"], "year_property": c["year_property"],
                 "revision": c["revision"], "raw_sha256": raws[c["qid"]]}
                for c in cs[:60]
            ],
            "cables_total": len(cs),
        }  # fmt: skip

    def put(ind: str, geo: str, period: tuple[date, date], value: int, unit: str, cs: list[dict[str, Any]],
            formula: str, **extra: Any) -> None:  # fmt: skip
        sha, fl = ref(cs)
        rb.count(indicator=ind, provider_geo=geo, iso3=cidx.get(geo), period=period, value=value, unit=unit,
                 raw_sha256=sha, selector={"wikidata_entities": [c["qid"] for c in cs][:60], "aggregate": ind},
                 flags={**ic.derived(formula, **base, **extra), **fl})  # fmt: skip

    pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in cables:
        cs = sorted(set(c["countries"]))
        for a in cs:
            for b in cs:
                if a != b:
                    pairs[(a, b)].append(c)
    for q, cs in sorted(by_country.items()):
        put("CABLES_LISTED", q, snap, len(cs), "cables", cs,
            f"count of Wikidata items with P31 in the cable classes and P17 = {q}")  # fmt: skip
        partners = sorted({b for (a, b) in pairs if a == q})
        if partners:
            put("CABLE_PARTNER_COUNTRIES", q, snap, len(partners), "countries", cs,
                "count of distinct other P17 values on the cables listing the country", partners=partners)  # fmt: skip
        years: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for c in cs:
            if c["year"] is not None:
                years[c["year"]].append(c)
        for y, ys in sorted(years.items()):
            put("CABLES_ENTERING_SERVICE", q, ic.year_span(y), len(ys), "cables", ys,
                "count of cables listing the country whose service year (P729 service entry, else P571 inception) "
                "is the year")  # fmt: skip
    for (a, b), cs in sorted(pairs.items()):
        p = ic.Partner(cidx.get(b), b)
        put(f"CABLES_SHARED|{p.dim()}", a, snap, len(cs), "cables", cs,
            "count of cables (Wikidata items in the cable classes) whose P17 lists both countries",
            undirected=True, **p.flags())  # fmt: skip
        dated = [c for c in cs if c["year"] is not None]
        if dated:
            first = min(c["year"] for c in dated)
            sha, fl = ref(dated)
            if (f"CABLE_FIRST_SERVICE_YEAR|{p.dim()}", a, snap[0], snap[1]) not in rb._seen:
                rb._seen.add((f"CABLE_FIRST_SERVICE_YEAR|{p.dim()}", a, snap[0], snap[1]))
                rb.rows.append({
                    "source": ATLAS_SOURCE, "dataset": DATASET, "indicator": f"CABLE_FIRST_SERVICE_YEAR|{p.dim()}",
                    "concept_id": None, "iso3": cidx.get(a), "provider_geo": a, "entity_qid": None,
                    "period_start": snap[0], "period_end": snap[1], "value": str(first), "value_text": None,
                    "unit": "year",
                    "flags": ic.canon({**base, "derived": "min", "undirected": True, **p.flags(), **fl,
                                       "formula": "earliest service year (P729, else P571) among the cables listing "
                                                  "both countries that have one", "undated_cables": len(cs) - len(dated)}),
                    "raw_sha256": sha,
                    "selector": ic.canon({"wikidata_entities": [c["qid"] for c in dated][:60], "aggregate": "min_year"}),
                })  # fmt: skip
    report = {
        "cables": len(cables),
        "cables_without_country": sum(1 for c in cables if not c["countries"]),
        "cables_with_year": sum(1 for c in cables if c["year"] is not None),
        "countries": len(by_country),
        "unmapped_p17": sorted(q for q in by_country if q not in cidx),
        "undirected_pairs": len(pairs) // 2,
    }
    return rb, report


class _Gone(Exception):
    """HTTP 404: an empty Special:WhatLinksHere page or a deleted item."""


async def _get_or_gone(run: Any, url: str, *, key: str, endpoint: str, media: tuple[str, ...]) -> ic.Fetched:
    try:
        return await _locked_get(run, url, key=key, endpoint=endpoint, media=media)
    except SourceResponseError as e:
        if e.details.get("status") == 404:
            raise _Gone(url) from e
        raise


async def _api_batch(run: Any, ids: list[str]) -> tuple[ic.Fetched, dict[str, Any]] | None:
    """One wbgetentities call (<= 50 ids) when the action API answers; None -> use Special:EntityData."""
    if run.origin != "live":  # recorded fixtures hold EntityData snapshots only
        return None
    try:
        got = await _locked_get(run, API_URL.format(ids="|".join(ids)), key=f"wikidata:wbgetentities:{ids[0]}+{len(ids)}",
                                endpoint="wbapi", media=("application/json",), attempts=1)  # fmt: skip
    except SourceResponseError:
        return None  # throttled / unavailable: fall back, never fail the run on the faster path
    try:
        doc = json.loads(got.body)
    except ValueError:
        return None
    if "error" in doc or not isinstance(doc.get("entities"), dict):
        return None  # e.g. maxlag
    return got, doc["entities"]


async def fetch_entities(run: Any, ids: list[str], report: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str]]:
    """QID -> (entity JSON, raw sha256): wbgetentities batches, else one Special:EntityData per item."""
    out: dict[str, tuple[dict[str, Any], str]] = {}
    for i in range(0, len(ids), API_BATCH):
        chunk = ids[i : i + API_BATCH]
        api = await _api_batch(run, chunk)
        if api is not None:
            got, ents = api
            report["api_batches"] += 1
            for q, e in ents.items():
                if "missing" in e or "claims" not in e:
                    report["missing_items"].append(q)
                    continue
                out[e.get("id", q)] = (e, got.sha256)
            continue
        for q in chunk:
            try:
                ent = await _get_or_gone(run, ENTITY_URL.format(qid=q), key=f"wikidata:entitydata:{q}",
                                         endpoint="entitydata", media=("application/json",))  # fmt: skip
            except _Gone:
                report["missing_items"].append(q)
                continue
            report["entitydata_requests"] += 1
            e = entity_of(ent.body, q)
            out[e["id"]] = (e, ent.sha256)
    return out


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"ok": True, "tables": [], "skipped": DATASET}
    classes = {ROOT_CLASS}
    pages: list[str] = []
    entities: dict[str, dict[str, Any]] = {}
    raws: dict[str, str] = {}
    frontier = [(ROOT_CLASS, 0)]
    seen_class_pages: set[str] = set()
    first_day: date | None = None
    fetch_report: dict[str, Any] = {"api_batches": 0, "entitydata_requests": 0, "missing_items": [],
                                    "empty_class_pages": []}  # fmt: skip
    while frontier:
        cls, depth = frontier.pop(0)
        if cls in seen_class_pages:
            continue
        seen_class_pages.add(cls)
        try:
            page = await _get_or_gone(run, WLH_URL.format(qid=cls), key=f"wikidata:whatlinkshere:{cls}",
                                      endpoint="pages", media=("text/html",))  # fmt: skip
        except _Gone:
            fetch_report["empty_class_pages"].append(cls)  # MediaWiki answers 404 when nothing links here
            continue
        first_day = first_day or page.retrieved_at.date()
        pages.append(page.sha256)
        todo = [q for q in parse_links(page.body, cls) if q not in entities][: max(0, MAX_CANDIDATES - len(entities))]
        for q, (e, sha) in (await fetch_entities(run, todo, fetch_report)).items():
            d = describe(e)
            entities[q] = d
            raws[q] = sha
            if set(d["p279"]) & classes and depth + 1 <= MAX_SUBCLASS_DEPTH:
                classes.add(q)
                frontier.append((q, depth + 1))
    cables = [d for d in entities.values() if set(d["p31"]) & classes]
    rb, report = build(cables, raws, first_day or date.today())
    out = run.register(
        dataset=DATASET,
        frame=rb.frame(),
        raw_sha256s=sorted(set(pages) | {raws[c["qid"]] for c in cables}),
        coverage_note=(
            f"Wikidata (CC0, community-edited): {report['cables']} cable items of classes {sorted(classes)}, "
            f"{report['countries']} P17 countries, {report['undirected_pairs']} country pairs; unmapped P17: "
            f"{', '.join(report['unmapped_p17']) or 'none'}"
        ),
        rights={**RIGHTS, "source_key": SOURCE_KEY, "skipped_sources": SKIPPED},
    )
    table = {
        "table": out["dataset_key"].removeprefix("atlas."),
        "rows": out["row_count"],
        "created": out["created"],
        "artifact_sha256": out["artifact_sha256"],
        "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
    }
    return {"ok": True, "tables": [table], "report": {**report, "classes": sorted(classes),
                                                       "candidates_fetched": len(entities), **fetch_report}}  # fmt: skip
