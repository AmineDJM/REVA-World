"""Event seeds for the causal-candidate extractor (agent CAUSCAND): the events with the most sitelinks.

Which articles are read is decided by Wikidata, never by REVA's judgement of what is "important":
one WDQS query lists items that are a direct instance (``P31``) of a declared event class
(:data:`EVENT_CLASSES`: wars, revolutions, coups, crises, disasters, epidemics, ...) together with
their sitelink count and their English / French Wikipedia titles (``schema:about`` sitelinks). Seeds
are the top ``limit`` items by sitelinks. The query runs through the KG harvest's budgeted Wikidata
client (``reva.kg.harvest.budgeted_client``: ``reserve_ingest`` -> metered -> ``settle_metered`` on the
``wikidata.wdqs`` key, ledger in a throwaway ``reva_*`` database) and its raw response is archived
(``var/kg/archive_index.jsonl``). The result is a *recorded snapshot*: ``seeds.json`` with the query
sha256, the raw sha256 and the retrieval time; later runs read the snapshot, not the live service.

When the KG holds event entities with sitelink counts (EVENTS / CAUSWD harvests, not loaded when this
ran), the seed list should be read from them instead (a throwaway database's ``kg.entity_current``, never
the dev database ``reva``), titles coming from the recorded entity snapshots' ``enwiki`` / ``frwiki``
sitelinks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Event classes (Wikidata items; direct ``P31`` only, no subclass closure: cheap and predictable).
#: The labels are the classes' English Wikidata labels as returned by the 2026-10-16 query (``class_labels`` in
#: the snapshot). Q3002150 was queried by mistake (it is "aircraft crash", not a political crisis): it is not
#: in this table, so :func:`top_seeds` drops items matched only through it.
EVENT_CLASSES: dict[str, str] = {
    "Q198": "war",
    "Q8465": "civil war",
    "Q103495": "world war",
    "Q1006311": "war of national liberation",
    "Q350604": "armed conflict",
    "Q10931": "revolution",
    "Q45382": "coup d'état",
    "Q124734": "rebellion",
    "Q124757": "riot",
    "Q114380": "financial crisis",
    "Q290178": "economic crisis",
    "Q176494": "recession",
    "Q168247": "famine",
    "Q44512": "epidemic",
    "Q12184": "pandemic",
    "Q3839081": "disaster",
    "Q8065": "natural disaster",
    "Q7944": "earthquake",
    "Q8070": "tsunami",
    "Q41397": "genocide",
    "Q3199915": "massacre",
    "Q2223653": "terrorist attack",
    "Q467011": "invasion",
    "Q1361229": "conquest",
}

MIN_SITELINKS = 40


def seed_query(classes: Sequence[str] = tuple(EVENT_CLASSES), min_sitelinks: int = MIN_SITELINKS) -> str:
    values = " ".join(f"wd:{q}" for q in classes)
    return f"""
SELECT ?item ?sl ?class ?classLabel ?enTitle ?frTitle WHERE {{
  VALUES ?class {{ {values} }}
  ?item wdt:P31 ?class ; wikibase:sitelinks ?sl .
  FILTER(?sl >= {int(min_sitelinks)})
  OPTIONAL {{ ?class rdfs:label ?classLabel FILTER(LANG(?classLabel) = "en") }}
  OPTIONAL {{ ?en schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?enTitle }}
  OPTIONAL {{ ?fr schema:about ?item ; schema:isPartOf <https://fr.wikipedia.org/> ; schema:name ?frTitle }}
}}"""


@dataclass
class Seed:
    qid: str
    sitelinks: int
    classes: list[str] = field(default_factory=list)
    enwiki: str | None = None
    frwiki: str | None = None


def parse_seed_bindings(bindings: Sequence[Mapping[str, Mapping[str, str]]]) -> tuple[list[Seed], dict[str, str]]:
    """WDQS bindings -> seeds (one per item, classes merged), sorted by sitelinks desc then QID."""
    by: dict[str, Seed] = {}
    labels: dict[str, str] = {}
    for b in bindings:
        item = b.get("item", {}).get("value", "")
        qid = item.rsplit("/", 1)[-1]
        if not qid.startswith("Q") or not qid[1:].isdigit():
            continue
        cls = b.get("class", {}).get("value", "").rsplit("/", 1)[-1]
        if "classLabel" in b:
            labels[cls] = b["classLabel"]["value"]
        s = by.setdefault(qid, Seed(qid=qid, sitelinks=int(b.get("sl", {}).get("value", "0"))))
        if cls and cls not in s.classes:
            s.classes.append(cls)
        en = b.get("enTitle", {}).get("value")
        fr = b.get("frTitle", {}).get("value")
        s.enwiki = s.enwiki or en
        s.frwiki = s.frwiki or fr
    seeds = sorted(by.values(), key=lambda s: (-s.sitelinks, int(s.qid[1:])))
    for s in seeds:
        s.classes.sort(key=lambda q: int(q[1:]))
    return seeds, labels


def top_seeds(seeds: Sequence[Seed], limit: int, classes: Mapping[str, str] = EVENT_CLASSES) -> list[Seed]:
    """The ``limit`` seeds with most sitelinks that are an instance of a declared class and have at least one
    EN or FR article (input order kept: sitelinks desc, then QID)."""
    return [s for s in seeds if (s.enwiki or s.frwiki) and any(c in classes for c in s.classes)][:limit]


def load_seeds(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(Path(path).read_text())
    doc["seeds"] = [Seed(**s) for s in doc["seeds"]]
    return doc


async def fetch_seeds(*, db_name: str, limit: int, out: Path) -> dict[str, Any]:
    """Run :func:`seed_query` once (budgeted, archived) and write the seed snapshot to ``out``."""
    from reva.kg.harvest import budgeted_client

    q = seed_query()
    async with budgeted_client(purpose="causcand_seeds", db_name=db_name, planned_wdqs=2, planned_wbapi=0) as sess:
        res = await sess.client.sparql(q, meta={"purpose": "causcand_event_seeds"})
    seeds, labels = parse_seed_bindings(res.bindings)
    chosen = top_seeds(seeds, limit)
    doc = {
        "kind": "causcand_event_seeds",
        "query": q.strip(),
        "query_sha256": res.query_sha256,
        "raw_sha256": res.raw_sha256,
        "retrieved_at": res.retrieved_at.isoformat(),
        "endpoint": "https://query.wikidata.org/sparql",
        "licence": "Wikidata: CC0 1.0",
        "min_sitelinks": MIN_SITELINKS,
        "classes": EVENT_CLASSES,
        "class_labels": labels,
        "items_matching": len(seeds),
        "limit": limit,
        "budget": sess.usage.get("budget"),
        "seeds": [asdict(s) for s in chosen],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    doc["seeds"] = chosen
    return doc
