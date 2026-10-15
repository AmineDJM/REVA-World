"""The offline links fixture: a few hundred real Wikidata statements around two chains.

* Columbus → Crown of Castile / Kingdom of Portugal → Treaty of Tordesillas → colonial Brazil → Brazil →
  Portuguese;
* the United States and decolonisation in the 1950s–60s (Eisenhower, Suez Crisis 1956, UNGA Resolution
  1514 (1960), the UN).

The statements were copied from Wikidata (``wbgetentities`` through WDHARVEST's locked client, see
:func:`capture`), filtered to the allow-listed properties and to statements whose object is inside the
fixture's entity set (plus dates and ISO codes). Every statement keeps its Wikidata statement GUID, the
entity revision id and the sha256 of the raw response; the file is marked ``origin: test_fixture``.

Run ``uv run python -m reva.links.fixture capture`` to re-capture (network; one request at a time through
``var/locks/wikidata.lock``). Tests never touch the network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from reva.links.model import Stmt, stmt_from_any
from reva.links.taxonomy import LINKS_DIR, REPO_ROOT

SEED_FILE = LINKS_DIR / "fixture_entities.yaml"
FIXTURE_FILE = REPO_ROOT / "tests" / "unit" / "links" / "fixtures" / "wikidata_links_fixture.json"
#: literal-valued properties kept for every fixture entity (dates, ISO codes)
LITERAL_KEEP = frozenset({"P298", "P297", "P571", "P576", "P580", "P582", "P585", "P569", "P570"})
FIXTURE_LANGS = ("fr", "en")


@dataclass(frozen=True)
class Fixture:
    meta: Mapping[str, Any]
    entities: Mapping[str, Mapping[str, Any]]
    statements: list[Stmt]
    raw_statements: list[Mapping[str, Any]]

    @property
    def labels(self) -> dict[str, dict[str, str]]:
        return {q: dict(e.get("labels") or {}) for q, e in self.entities.items()}

    def iso3_to_qid(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in self.statements:
            if s.pid == "P298" and s.value:
                out.setdefault(s.value.upper(), s.subject)
        return out


def load_seed(path: Path = SEED_FILE) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def seed_qids(seed: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for group in seed["groups"]:
        for item in group["entities"]:
            q = str(item["qid"])
            if q not in out:
                out.append(q)
    return out


def load_fixture(path: Path = FIXTURE_FILE) -> Fixture:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("origin") != "test_fixture":
        raise ValueError("links fixture must be marked origin=test_fixture")
    raw = list(doc["statements"])
    stmts = [stmt_from_any({**s, "origin": "test_fixture"}) for s in raw]
    return Fixture(doc["meta"], doc["entities"], stmts, raw)


# ---------------------------------------------------------------------------------------------- capture
def _keep(st: Any, qids: set[str]) -> bool:
    if st.object_qid is not None:
        return st.object_qid in qids and st.object_qid != st.subject_qid
    return st.property_pid in LITERAL_KEEP


async def _entitydata(client: Any, chunk: list[str]) -> dict[str, Any]:  # pragma: no cover - network
    """Fallback when ``wbgetentities`` keeps answering ``maxlag``: ``Special:EntityData/<Q>.json`` (same
    entity JSON), one entity per request, through the same locked/paced client loop."""
    import json as _json

    entities: dict[str, Any] = {}
    sha_by_id: dict[str, str] = {}
    shas: list[str] = []
    retrieved = datetime.now(UTC)
    for q in chunk:
        url = f"https://www.wikidata.org/wiki/Special:EntityData/{q}.json"
        result, sha = await client._request(
            client.wbapi_http, url, endpoint="wbapi", accept="application/json", meta={"kind": "entitydata", "id": q}
        )
        doc = _json.loads(result.body)
        for key, ent in (doc.get("entities") or {}).items():
            entities[ent.get("id", key)] = ent
            sha_by_id[ent.get("id", key)] = sha
        shas.append(sha)
        retrieved = result.retrieved_at
    return {
        "entities": entities,
        "sha_by_id": sha_by_id,
        "redirects": {},
        "missing": [],
        "shas": shas,
        "retrieved_at": retrieved.isoformat(),
        "endpoint": "Special:EntityData",
    }


async def capture(
    qids: Iterable[str],
    *,
    batch_size: int = 20,
    cache_dir: Path | None = None,
    max_retries: int = 40,
    entitydata: bool = False,
) -> dict[str, Any]:  # pragma: no cover - network
    """Fetch the fixture entities through WDHARVEST's client (machine-wide lock, shared cool-downs,
    Retry-After honoured). ``cache_dir`` keeps each successful batch so an interrupted capture resumes
    without re-requesting it. ``entitydata=True`` uses ``Special:EntityData`` (one entity per request)."""
    import hashlib

    from reva.adapters.connectors.wikidata import (
        Archive,
        WikidataClient,
        extract_entity,
        load_allowlist,
        make_live_http,
        user_agent,
    )
    from reva.kg.harvest import to_kg_statement

    want = list(dict.fromkeys(qids))
    qset = set(want)
    client = WikidataClient(make_live_http(), archive=Archive(None), max_retries=max_retries)
    raw_entities: dict[str, Any] = {}
    sha_by_id: dict[str, str] = {}
    redirects: dict[str, str] = {}
    missing: list[str] = []
    shas: list[str] = []
    requests = 0
    retrieved_at = datetime.now(UTC)
    for i in range(0, len(want), batch_size):
        chunk = want[i : i + batch_size]
        key = hashlib.sha256("|".join(chunk).encode()).hexdigest()[:20]
        cached = cache_dir / f"batch_{key}.json" if cache_dir else None
        if cached is not None and cached.exists():
            part = json.loads(cached.read_text(encoding="utf-8"))
        elif entitydata:
            part = await _entitydata(client, chunk)
            requests += len(chunk)
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(part), encoding="utf-8")
        else:
            r = await client.get_entities(chunk, batch_size=batch_size)
            requests += r.requests
            part = {
                "entities": r.entities,
                "sha_by_id": getattr(r, "raw_sha_by_id", None) or getattr(r, "sha_by_id", {}),
                "redirects": r.redirects,
                "missing": list(r.missing),
                "shas": list(r.raw_sha256s),
                "retrieved_at": r.retrieved_at.isoformat(),
            }
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(part), encoding="utf-8")
        raw_entities.update(part["entities"])
        sha_by_id.update(part["sha_by_id"])
        redirects.update(part["redirects"])
        missing += part["missing"]
        shas += part["shas"]
        retrieved_at = datetime.fromisoformat(part["retrieved_at"])

    class _Res:  # the subset of EntitiesResult used below
        entities = raw_entities
        raw_sha256s = tuple(shas)

    res = _Res()
    res_redirects, res_missing, res_requests = redirects, missing, requests
    allow = load_allowlist()
    entities: dict[str, Any] = {}
    statements: list[dict[str, Any]] = []
    for qid in want:
        raw = res.entities.get(res_redirects.get(qid, qid))
        if raw is None:
            continue
        ent = extract_entity(raw, allow, raw_sha256=sha_by_id[raw["id"]], retrieved_at=retrieved_at)
        entities[ent.qid] = {
            "labels": {k: v for k, v in ent.labels.items() if k in FIXTURE_LANGS},
            "descriptions": {k: v for k, v in ent.descriptions.items() if k in FIXTURE_LANGS},
            "lastrevid": ent.lastrevid,
            "instance_of": [q for q in ent.instance_of],
            "raw_sha256": ent.raw_sha256,
        }
        for st in ent.statements:
            kg = to_kg_statement(st)
            if not _keep(kg, qset):
                continue
            d = {
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
                "source_key": "wikidata",
                "source_revision": kg.source_revision,
                "raw_sha256": kg.raw_sha256,
                "retrieved_at": retrieved_at.isoformat(),
            }
            statements.append(d)
    return {
        "origin": "test_fixture",
        "meta": {
            "description": "Real Wikidata statements copied for offline LINKS tests (CC0).",
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "endpoint": "https://www.wikidata.org/wiki/Special:EntityData/<Q>.json"
            if entitydata
            else "https://www.wikidata.org/w/api.php?action=wbgetentities",
            "client": "reva.adapters.connectors.wikidata.WikidataClient (machine-wide lock)",
            "user_agent": user_agent(),
            "raw_sha256s": sorted(set(res.raw_sha256s)),
            "requests": res_requests,
            "missing": sorted(set(res_missing)),
            "redirects": dict(res_redirects),
            "filter": "allow-listed properties; entity-valued statements whose object is in the fixture set; "
            "literal dates/ISO codes",
            "community_edited": True,
            "rank_policy": "deprecated dropped; preferred/normal kept",
        },
        "entities": entities,
        "statements": statements,
    }


def _t(t: Any) -> dict[str, Any] | None:
    return None if t is None else {"time": t.iso, "precision": t.precision, "calendar": t.calendar}


def main(argv: list[str]) -> int:  # pragma: no cover - CLI
    if not argv or argv[0] != "capture":
        print("usage: python -m reva.links.fixture capture [out.json]")
        return 2
    argv = [a for a in argv if a != "--entitydata"] + (["--entitydata"] if "--entitydata" in argv else [])
    out = Path(argv[1]) if len(argv) > 1 and argv[1] != "--entitydata" else FIXTURE_FILE
    cache = Path(argv[2]) if len(argv) > 2 and argv[2] != "--entitydata" else None
    doc = asyncio.run(capture(seed_qids(load_seed()), cache_dir=cache, entitydata="--entitydata" in argv))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(doc['entities'])} entities, {len(doc['statements'])} statements -> {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
