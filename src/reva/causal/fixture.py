"""The offline causal fixture: real Wikidata causal statements around two documented examples.

* the Great Depression and the Wall Street crash of 1929;
* the 1918 influenza pandemic ("Spanish flu") and the First World War — as asserted by Wikidata.

Captured through WDHARVEST's locked client (``Special:EntityData`` one entity per request, machine-wide lock,
shared cool-downs), filtered to the causal properties (P828, P1542, P1478, P1479, P1536, P1537), the event
dates REVA's temporal check reads (P580, P582, P585, P571, P576) and P31. Every statement keeps its Wikidata
statement GUID, the entity revision id, its own references, and the sha256 of the raw response; the file is
marked ``origin: test_fixture``. Wikidata is CC0 and community-edited: nothing here is verified by REVA.

``uv run python -m reva.causal.fixture capture`` re-captures (network). Tests never touch the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_FILE = REPO_ROOT / "tests" / "unit" / "causal" / "fixtures" / "wikidata_causal_fixture.json"
CAUSAL = ("P828", "P1542", "P1478", "P1479", "P1536", "P1537")
DATES = ("P580", "P582", "P585", "P571", "P576")
KEEP = frozenset((*CAUSAL, *DATES, "P31"))
#: Great Depression, Wall Street crash of 1929, 1918 influenza pandemic ("Spanish flu"), First World War
#: (ids checked against their captured labels).
SEEDS = ("Q8698", "Q201684", "Q178275", "Q361")
LANGS = ("fr", "en")
MAX_NEIGHBOURS = 30


async def _entitydata(client: Any, qids: Iterable[str]) -> tuple[dict[str, Any], dict[str, str], datetime]:
    """One ``Special:EntityData`` request per entity through the locked client (pragma: network)."""
    ents: dict[str, Any] = {}
    sha_by_id: dict[str, str] = {}
    retrieved = datetime.now(UTC)
    for q in qids:
        r = await client.get_entities([q], props=("info", "claims", "labels", "descriptions"), languages=LANGS)
        for eid, ent in r.entities.items():
            ents[eid] = ent
            sha_by_id[eid] = r.raw_sha_by_id.get(eid) or r.raw_sha256s[0]
        retrieved = min(retrieved, r.retrieved_at)
    return ents, sha_by_id, retrieved


async def capture(
    seeds: Iterable[str] = SEEDS, *, max_neighbours: int = MAX_NEIGHBOURS
) -> dict[str, Any]:  # pragma: no cover - network
    from reva.adapters.connectors.wikidata import (
        Archive,
        WikidataClient,
        extract_entity,
        load_allowlist,
        make_live_http,
        user_agent,
    )
    from reva.kg.harvest import to_kg_statement

    client = WikidataClient(make_live_http(), archive=Archive(None), max_retries=40, mode="linked_data")
    allow = load_allowlist()
    want = list(dict.fromkeys(seeds))
    raw, shas, retrieved = await _entitydata(client, want)
    # one hop: the other end of every causal claim of the seeds (bounded)
    hop: list[str] = []
    for q in want:
        for pid in CAUSAL:
            for c in (raw.get(q, {}).get("claims") or {}).get(pid, []):
                v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
                o = v.get("id") if isinstance(v, Mapping) else None
                if c.get("rank") != "deprecated" and isinstance(o, str) and o not in want and o not in hop:
                    hop.append(o)
    hop = hop[:max_neighbours]
    raw2, shas2, retrieved2 = await _entitydata(client, hop)
    raw.update(raw2)
    shas.update(shas2)
    retrieved = min(retrieved, retrieved2)
    qset = set(raw)
    entities: dict[str, Any] = {}
    statements: list[dict[str, Any]] = []
    for qid, ent_raw in raw.items():
        ent = extract_entity(ent_raw, allow, raw_sha256=shas[qid], retrieved_at=retrieved)
        entities[qid] = {
            "labels": {k: v for k, v in ent.labels.items() if k in LANGS},
            "descriptions": {k: v for k, v in ent.descriptions.items() if k in LANGS},
            "lastrevid": ent.lastrevid,
            "instance_of": list(ent.instance_of),
            "raw_sha256": ent.raw_sha256,
            "seed": qid in want,
        }
        for st in ent.statements:
            if st.property not in KEEP:
                continue
            kg = to_kg_statement(st)
            if kg.property_pid in CAUSAL and kg.object_qid not in qset:
                continue  # both ends of every kept causal claim are fixture entities (labels, dates)
            if kg.property_pid == "P31" and qid not in want:
                continue
            statements.append(
                {
                    "statement_id": kg.statement_id,
                    "subject_qid": kg.subject_qid,
                    "property_pid": kg.property_pid,
                    "object_qid": kg.object_qid,
                    "value": kg.value,
                    "start": _t(kg.start),
                    "end": _t(kg.end),
                    "point_in_time": _t(kg.point_in_time),
                    "qualifiers": kg.qualifiers,
                    "rank": kg.rank,
                    "references": kg.references,
                    "source_revision": kg.source_revision,
                    "raw_sha256": kg.raw_sha256,
                }
            )
    statements.sort(key=lambda d: (d["subject_qid"], d["property_pid"], d["statement_id"] or ""))
    return {
        "origin": "test_fixture",
        "meta": {
            "description": "Real Wikidata causal statements (CC0, community-edited) for offline CAUSCORE tests.",
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "retrieved_at": retrieved.isoformat(),
            "endpoint": "https://www.wikidata.org/wiki/Special:EntityData/<Q>.json",
            "client": "reva.adapters.connectors.wikidata.WikidataClient (machine-wide lock, linked_data mode)",
            "user_agent": user_agent(),
            "seeds": want,
            "one_hop": hop,
            "raw_sha256s": sorted(set(shas.values())),
            "filter": "causal properties with both ends in the fixture; dates P580/P582/P585/P571/P576; P31 "
            "of seeds; deprecated rank dropped; references and qualifiers as extracted by WDHARVEST",
            "community_edited": True,
        },
        "entities": dict(sorted(entities.items())),
        "statements": statements,
    }


def _t(t: Any) -> dict[str, Any] | None:
    return None if t is None else {"time": t.iso, "precision": t.precision, "calendar": t.calendar}


def load_fixture(path: Path = FIXTURE_FILE) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("origin") != "test_fixture":
        raise ValueError("causal fixture must be marked origin=test_fixture")
    return doc


def load_into_kg(
    engine: Any, store: Any, *, source_key: str = "wikidata.causal_fixture", path: Path = FIXTURE_FILE
) -> Any:
    """Load the captured fixture into ``kg.*`` with KGCORE's ``load_batch`` (origin ``test_fixture``; the
    fixture file itself is archived as the raw artifact). Idempotent."""
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgEntity, KgStatement, KgTime, load_batch, register_properties

    register_properties(engine, kg_properties(load_allowlist()))
    body = path.read_bytes()
    raw = store.put_bytes(body, "raw", "application/json").sha256
    assert raw == hashlib.sha256(body).hexdigest()
    doc = load_fixture(path)

    def t(d: Any) -> Any:
        return None if d is None else KgTime(d["time"], d["precision"], d.get("calendar", "gregorian"))

    sts = [
        KgStatement(
            subject_qid=r["subject_qid"],
            property_pid=r["property_pid"],
            object_qid=r["object_qid"],
            value=r["value"],
            start=t(r["start"]),
            end=t(r["end"]),
            point_in_time=t(r["point_in_time"]),
            qualifiers=r["qualifiers"],
            rank=r["rank"],
            references=r["references"],
            source_key=source_key,
            source_revision=r["source_revision"],
            statement_id=r["statement_id"],
            raw_sha256=raw,
        )
        for r in doc["statements"]
    ]
    ents = [
        KgEntity(q, "event", e["labels"], e.get("descriptions") or {}, source_revision=str(e["lastrevid"]))
        for q, e in doc["entities"].items()
    ]
    return load_batch(
        engine,
        store,
        entities=ents,
        statements=sts,
        source_key=source_key,
        raw_sha256s=[raw],
        retrieved_at=datetime.fromisoformat(doc["meta"]["retrieved_at"]),
        origin="test_fixture",
    )


def main(argv: list[str]) -> int:  # pragma: no cover - CLI
    if not argv or argv[0] != "capture":
        print("usage: python -m reva.causal.fixture capture [out.json]")
        return 2
    out = Path(argv[1]) if len(argv) > 1 else FIXTURE_FILE
    doc = asyncio.run(capture())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(doc['entities'])} entities, {len(doc['statements'])} statements -> {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
