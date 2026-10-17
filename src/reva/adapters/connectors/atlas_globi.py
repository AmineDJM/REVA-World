"""Global Biotic Interactions (GloBI) for selected taxa (agent V4ECO): who eats, preys on, parasitises, infects,
pollinates or hosts whom -> KG statements (no atlas table).

Source
------
GloBI web API ``https://api.globalbioticinteractions.org/interaction`` (no account). For every seed taxon of the
GBIF layer (:data:`reva.adapters.connectors.atlas_gbif_occ.SEEDS`, GBIF backbone keys) two bounded requests:
``sourceTaxon=GBIF:<key>`` (what the seed does) and ``targetTaxon=GBIF:<key>`` (what is done to the seed), each
with the interaction types of :data:`QUERY_TYPES`, the fields of :data:`FIELDS` and ``limit`` :data:`LIMIT`.
GloBI answers with **taxon-level aggregates**: one row per (source taxon id, interaction type, target taxon id)
with the number of interaction records, studies and contributing sources.

How the answer is read (identifiers only, never names)
------------------------------------------------------
* GloBI resolves the queried ``GBIF:<key>`` through its own taxon graph and returns the ids it indexes the records
  under (``COL:4CGXP``, ``NCBI:9689``, ``EOL:328672``, ``GBIF:5219404``...): the taxon itself or a descendant.
  REVA keeps those ids as KG nodes (``col:4CGXP``...; :func:`reva.adapters.connectors.eco_common.taxon_node`)
  and records GloBI's resolution as ``globi:resolved_within`` (returned id -> ``gbif:<key>``), so the GBIF node
  is reached through a statement GloBI made, not through a name match.
* Each edge is ``source id --globi:<type>--> target id`` exactly as GloBI returns it; the two partner ids are
  never merged with other ids of the same name. ``endoparasiteOf`` / ``ectoparasiteOf`` / ``parasitoidOf`` are
  read as ``globi:parasite_of`` with the exact type kept in ``interaction_types``.
* Partners without a usable id (``no:match``, single-letter or URL prefixes) are dropped and counted.
* A response with exactly :data:`LIMIT` rows is truncated: reported per query (``truncated``), never padded.
* Interactions are records of observed or published biotic interactions, not causal claims: ``pathogenOf`` says
  a dataset recorded the pathogen on that host, not that it causes a given outcome (no causal loader input).

Licence: GloBI aggregates datasets under their own terms and the API response does not state them; every
statement reference carries ``licence_status: unclear`` (owner decision: per-dataset licence review).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from reva.adapters.connectors.eco_common import guarded_load, taxon_node
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.globi"
CONNECTOR_VERSION = "1.0.1"  # 1.0.0 was registered from an in-flight snapshot with different code
PARSER_VERSION = "atlas-globi-1"
SOURCE_KEY = "globi.interactions"
ATLAS_SOURCE = "globi"
DATASET = "interactions_kg"  # KG only: no atlas table
HOST = "api.globalbioticinteractions.org"
API = f"https://{HOST}/interaction"
LIMIT = 2000
MAX_BYTES = 20_000_000
DIRECTIONS = ("source", "target")
QUERY_TYPES = ("eats", "preysOn", "parasiteOf", "pathogenOf", "pollinates", "visitsFlowersOf", "vectorOf", "hasHost")
#: GloBI interaction type -> KG predicate (registry/kg/predicates_eco.yaml). Types outside the map are counted.
TYPE_PID: dict[str, str] = {
    "eats": "globi:eats",
    "preysOn": "globi:preys_on",
    "parasiteOf": "globi:parasite_of",
    "endoparasiteOf": "globi:parasite_of",
    "ectoparasiteOf": "globi:parasite_of",
    "parasitoidOf": "globi:parasite_of",
    "pathogenOf": "globi:pathogen_of",
    "pollinates": "globi:pollinates",
    "visitsFlowersOf": "globi:visits_flowers_of",
    "vectorOf": "globi:vector_of",
    "hasHost": "globi:has_host",
}
RESOLVED_PID = "globi:resolved_within"
PIDS = (*sorted(set(TYPE_PID.values())), RESOLVED_PID)
FIELDS = (
    "source_taxon_external_id",
    "source_taxon_name",
    "interaction_type",
    "target_taxon_external_id",
    "target_taxon_name",
    "number_of_interactions",
    "number_of_studies",
    "number_of_sources",
)
LICENCE_STATUS = "unclear: GloBI aggregates datasets under their own licences; not stated in the API response"
FIXTURE_SEEDS = (5219404, 1341976)


class GlobiParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="schema_drift")


def seeds_for(origin: str) -> tuple[int, ...]:
    from reva.adapters.connectors.atlas_gbif_occ import SEEDS

    return FIXTURE_SEEDS if origin == "test_fixture" else SEEDS


def query_url(seed: int, direction: str) -> str:
    params: list[tuple[str, str]] = [(f"{direction}Taxon", f"GBIF:{seed}")]
    params += [("interactionType", t) for t in QUERY_TYPES]
    params += [("fields", ",".join(FIELDS)), ("limit", str(LIMIT))]
    return f"{API}?{urlencode(params)}"


@dataclass(frozen=True)
class Row:
    source_id: str
    source_name: str | None
    itype: str
    target_id: str
    target_name: str | None
    n_interactions: int | None
    n_studies: int | None
    n_sources: int | None


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise GlobiParseError(f"count {v!r} is not a non-negative integer")
    return v


def parse_response(body: bytes) -> list[Row]:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise GlobiParseError("GloBI response is not JSON") from e
    if not isinstance(doc, dict) or doc.get("columns") != list(FIELDS) or not isinstance(doc.get("data"), list):
        raise GlobiParseError(f"GloBI columns {doc.get('columns') if isinstance(doc, dict) else None} != requested")
    out = []
    for r in doc["data"]:
        if not isinstance(r, list) or len(r) != len(FIELDS):
            raise GlobiParseError("GloBI row does not match the requested fields")
        if not isinstance(r[2], str):
            raise GlobiParseError(f"interaction type {r[2]!r} is not a string")
        out.append(
            Row(
                source_id=str(r[0] or ""),
                source_name=r[1],
                itype=r[2],
                target_id=str(r[3] or ""),
                target_name=r[4],
                n_interactions=_opt_int(r[5]),
                n_studies=_opt_int(r[6]),
                n_sources=_opt_int(r[7]),
            )
        )
    return out


@dataclass
class Query:
    seed: int
    direction: str
    url: str
    rows: list[Row]
    raw_sha256: str
    retrieved_at: datetime


def kg_parts(queries: Sequence[Query]) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """Entities (partner taxa, fill-missing) and statements for ``load_batch`` (source key :data:`SOURCE_KEY`)."""
    from reva.kg.model import KgEntity, KgStatement

    rep: dict[str, Any] = {"rows": 0, "no_usable_id": 0, "other_type": defaultdict(int), "truncated": [], "edges": 0,
                           "resolutions": 0}  # fmt: skip
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    within: dict[tuple[str, int], dict[str, Any]] = {}
    names: dict[str, set[str]] = defaultdict(set)
    for q in queries:
        if len(q.rows) >= LIMIT:
            rep["truncated"].append({"seed": q.seed, "direction": q.direction, "rows": len(q.rows)})
        for r in q.rows:
            rep["rows"] += 1
            pid = TYPE_PID.get(r.itype)
            if pid is None:
                rep["other_type"][r.itype] += 1
                continue
            src, tgt = taxon_node(r.source_id), taxon_node(r.target_id)
            if src is None or tgt is None:
                rep["no_usable_id"] += 1
                continue
            if r.source_name:
                names[src].add(r.source_name)
            if r.target_name:
                names[tgt].add(r.target_name)
            seed_side = src if q.direction == "source" else tgt
            seed_raw = r.source_id if q.direction == "source" else r.target_id
            if seed_side != f"gbif:{q.seed}":
                w = within.setdefault((seed_side, q.seed), {"raw": q.raw_sha256, "url": q.url, "returned_id": seed_raw})
                w.setdefault("directions", set()).add(q.direction)
            e = edges.setdefault((src, pid, tgt), {"rows": {}, "raw": q.raw_sha256, "url": q.url})
            e["rows"][(r.source_id, r.itype, r.target_id)] = {
                "source_id": r.source_id,
                "interaction_type": r.itype,
                "target_id": r.target_id,
                "number_of_interactions": r.n_interactions,
                "number_of_studies": r.n_studies,
                "number_of_sources": r.n_sources,
            }
    sts: list[Any] = []
    for (src, pid, tgt), e in sorted(edges.items()):
        rows = [e["rows"][k] for k in sorted(e["rows"])]
        n = [x["number_of_interactions"] for x in rows]
        sts.append(
            KgStatement(
                subject_qid=src,
                property_pid=pid,
                object_qid=tgt,
                qualifiers={
                    "interaction_types": sorted({x["interaction_type"] for x in rows}),
                    "number_of_interactions": sum(n) if all(v is not None for v in n) else None,
                    "globi_rows": rows[:20],
                    "globi_rows_total": len(rows),
                },
                references=[{"stated_in": "GloBI", "P854": e["url"], "licence_status": LICENCE_STATUS}],
                statement_id=f"{pid.split(':', 1)[1]}:{src}:{tgt}",
                raw_sha256=e["raw"],
            )
        )
    rep["edges"] = len(sts)
    for (node, seed), w in sorted(within.items()):
        sts.append(
            KgStatement(
                subject_qid=node,
                property_pid=RESOLVED_PID,
                object_qid=f"gbif:{seed}",
                qualifiers={
                    "globi_returned_id": w["returned_id"],
                    "query": f"{'/'.join(sorted(w['directions']))}Taxon=GBIF:{seed}",
                    "basis": "GloBI taxon resolution: records indexed under this id are returned for the GBIF id "
                    "(the taxon itself or a descendant)",
                },
                references=[{"stated_in": "GloBI", "P854": w["url"], "licence_status": LICENCE_STATUS}],
                statement_id=f"within:{node}:gbif:{seed}",
                raw_sha256=w["raw"],
            )
        )
        rep["resolutions"] += 1
    ents = [
        KgEntity(
            qid=node,
            kind="taxon",
            labels={"en": sorted(ns)[0]},
            aliases={"en": sorted(ns)[1:]} if len(ns) > 1 else {},
            descriptions={"en": f"taxon {node} as named by GloBI", "fr": f"taxon {node} tel que nommé par GloBI"},
        )
        for node, ns in sorted(names.items())
        if not node.startswith("gbif:") or all(node != f"gbif:{q.seed}" for q in queries)
    ]
    rep["other_type"] = dict(rep["other_type"])
    return ents, sts, rep


# ----------------------------------------------------------------------------- atlas source protocol
def atlas_source() -> Any:
    from reva.adapters.connectors.atlas_gbif_occ import SEEDS
    from reva.adapters.connectors.base import module_digest
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Global Biotic Interactions (GloBI) web API",
        licence="varies per contributing dataset (GloBI aggregator; not stated in the API response)",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("interaction",),
        cadence_seconds=30 * 86400,
        planned_requests=len(DIRECTIONS) * len(SEEDS) + 2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(atlas_source),
        field_mappings={"kg": list(PIDS), "seeds": "atlas_gbif_occ.SEEDS (GBIF backbone keys)"},
        notes="KG only; taxon-level aggregates of GloBI for seed taxa, both directions, bounded by limit.",
    )


async def atlas_ingest(run: Any, *, load_kg: bool = True) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": DATASET}
    queries: list[Query] = []
    for seed in seeds_for(run.origin):
        for direction in DIRECTIONS:
            url = query_url(seed, direction)
            raw = await run.get(url, key=f"{direction}:{seed}", max_bytes=MAX_BYTES)
            rows = parse_response(raw.body)
            queries.append(Query(seed, direction, url, rows, run.archive(raw), raw.retrieved_at))
    ents, sts, rep = kg_parts(queries)
    out: dict[str, Any] = {"queries": len(queries), "kg_build": rep}
    if load_kg:
        out["kg"] = guarded_load(
            run.engine,
            run.store,
            pids=PIDS,
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            raw_sha256s=sorted({q.raw_sha256 for q in queries}),
            retrieved_at=max(q.retrieved_at for q in queries),
            origin=run.origin,
            parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "limit": LIMIT},
        )
    return out


def edge_summary(statements: Sequence[Any]) -> Mapping[str, int]:
    """Count of statements per predicate (report helper)."""
    out: dict[str, int] = defaultdict(int)
    for s in statements:
        out[s.property_pid] += 1
    return dict(out)


__all__ = [
    "CONNECTOR_NAME",
    "FIELDS",
    "LIMIT",
    "PIDS",
    "TYPE_PID",
    "atlas_ingest",
    "atlas_source",
    "edge_summary",
    "kg_parts",
    "parse_response",
    "query_url",
]
