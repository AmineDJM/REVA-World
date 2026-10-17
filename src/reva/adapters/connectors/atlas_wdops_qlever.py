"""Wikidata statement packs read through QLever (V6, agent WDOPS).

The WDHARVEST *statements* specs re-read each subject from a Wikidata entity snapshot: one ``Special:EntityData``
request per entity while the action API answers 429, i.e. ~4,000 entities per day on the ``wikidata.wbapi`` budget,
for ~150k subjects selected by the V4 specs. A *pack* (``registry/kg/qlever_packs_wdops.yaml``) reads the same
statements -- same allow-listed properties, same Wikidata statement ids -- in one SPARQL query from QLever's
live-updated index of the Wikidata dump (University of Freiburg), through the run's budget-metered client
(``run.get``; raw response archived; budget key ``qlever.wikidata``, shared with ``atlas.v4link_qlever``).

KG: ``<item> --P<nnn>--> <item>`` with the Wikidata property id (the V4CORE families map it as for harvested
statements), source key ``qlever.wikidata``, statement id ``wdops:<pack>:<Wikidata statement id>`` (the Wikidata id is
also in the qualifiers), revision ``qlever@<retrieval time>`` with ``_revision.wikidata_revision = "unknown"``.
Qualifiers and references are **not read**: references are recorded as unknown, never as absent. Deprecated rows are
dropped in Python (QLever answered rank FILTERs inconsistently on 2026-10-17, see V4LINK). A statement the KG already
holds from a Wikidata snapshot (same statement id, source ``wikidata.wbapi``/``wikidata.wdqs``) is not loaded twice;
a pack's statements that a later run no longer finds (or that a snapshot now holds) are retracted. Subjects and
objects not yet in the KG become entities of kind ``wikidata_item`` with their en/fr labels (the QID when Wikidata
has none -- never a guessed name). Community-edited data: nothing here is verified, nothing is causal.
"""

from __future__ import annotations

import hashlib
import inspect
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from reva.adapters.connectors import v4link_common as common
from reva.adapters.connectors.atlas_v4link_wikidata import (
    QL_HOST,
    QL_SOURCE_KEY,
    RANKS,
    QleverFetcher,
    statement_id,
)
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.wdops_qlever"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_wdops_qlever/1"
ATLAS_SOURCE = "wdops"
DATASET = "wikidata_statement_packs"
PACKS_PATH = Path(__file__).resolve().parents[4] / "registry" / "kg" / "qlever_packs_wdops.yaml"
PREFIX = "wdops:"
DAY = 86400
MAX_BYTES = 120_000_000
SNAPSHOT_SOURCES = ("wikidata.wbapi", "wikidata.wdqs")
WD_ENTITY = "http://www.wikidata.org/entity/"


@dataclass(frozen=True)
class StatementPack:
    id: str
    properties: tuple[str, ...]
    max_rows: int
    items: str | None = None
    max_items: int | None = None
    mirrors: str | None = None
    note: str = ""


def load_packs(path: Path = PACKS_PATH) -> dict[str, StatementPack]:
    """Packs from the registry file; every property must be an allow-listed, entity-valued Wikidata property."""
    from reva.adapters.connectors.wikidata import load_allowlist

    allow = load_allowlist()
    doc = yaml.safe_load(path.read_text())
    out: dict[str, StatementPack] = {}
    for p in doc["packs"]:
        props = tuple(str(x) for x in p["properties"])
        for pid in props:
            spec = allow.properties.get(pid)
            if spec is None or getattr(spec, "kind", None) != "entity":
                raise ValueError(f"pack {p['id']}: {pid} is not an allow-listed entity-valued property")
        if p.get("max_items") and (p.get("items") or len(props) != 1):
            raise ValueError(f"pack {p['id']}: max_items needs exactly one property and no items pattern")
        out[str(p["id"])] = StatementPack(
            id=str(p["id"]),
            properties=props,
            max_rows=int(p["max_rows"]),
            items=p.get("items"),
            max_items=int(p["max_items"]) if p.get("max_items") else None,
            mirrors=p.get("mirrors"),
            note=str(p.get("note") or ""),
        )
    return out


def code_digest() -> str:
    h = hashlib.sha256()
    h.update(inspect.getsource(inspect.getmodule(code_digest)).encode())  # type: ignore[arg-type]
    h.update(PACKS_PATH.read_bytes())
    return "sha256:" + h.hexdigest()


# ============================================================================================== query
def pack_query(pack: StatementPack) -> str:
    """Every statement of the pack's properties on the selected items, with subject/object en/fr labels.
    No rank or numeric FILTER (QLever answered them inconsistently); ``LIMIT max_rows+1`` detects overflow."""
    if pack.max_items:
        (pid,) = pack.properties
        selector = (
            f"{{ SELECT DISTINCT ?item ?sl WHERE {{ ?item wdt:{pid} [] ; wikibase:sitelinks ?sl . }}"
            f" ORDER BY DESC(?sl) ?item LIMIT {int(pack.max_items)} }}"
        )
    else:
        selector = pack.items or ""
    unions = "\n  UNION\n".join(
        f'  {{ ?item p:{p} ?st . ?st ps:{p} ?v ; wikibase:rank ?rank . BIND("{p}" AS ?prop) }}' for p in pack.properties
    )
    return f"""SELECT ?item ?prop ?st ?v ?rank ?en ?fr ?ven ?vfr WHERE {{
  {selector}
{unions}
  OPTIONAL {{ ?item rdfs:label ?en . FILTER(LANG(?en) = "en") }}
  OPTIONAL {{ ?item rdfs:label ?fr . FILTER(LANG(?fr) = "fr") }}
  OPTIONAL {{ ?v rdfs:label ?ven . FILTER(LANG(?ven) = "en") }}
  OPTIONAL {{ ?v rdfs:label ?vfr . FILTER(LANG(?vfr) = "fr") }}
}} LIMIT {pack.max_rows + 1}"""


# ============================================================================================== parsing
@dataclass(frozen=True)
class Row:
    item: str
    property: str
    value: str
    statement_id: str
    rank: str


def _qid(uri: str | None) -> str | None:
    if not uri or not uri.startswith(WD_ENTITY):
        return None
    q = uri[len(WD_ENTITY) :]
    return q if q[:1] == "Q" and q[1:].isdigit() else None


def parse_rows(
    bindings: Sequence[Mapping[str, Any]], pack: StatementPack
) -> tuple[list[Row], dict[str, dict[str, str]], Counter[str]]:
    rows: list[Row] = []
    labels: dict[str, dict[str, str]] = defaultdict(dict)
    skipped: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    for b in bindings:
        item = _qid((b.get("item") or {}).get("value"))
        sid = statement_id((b.get("st") or {}).get("value"))
        pid = str((b.get("prop") or {}).get("value") or "")
        rank_uri = str((b.get("rank") or {}).get("value") or "")
        val = _qid((b.get("v") or {}).get("value"))
        if not item or not sid or pid not in pack.properties:
            skipped["row_incomplete"] += 1
            continue
        if rank_uri.endswith("DeprecatedRank"):
            skipped["deprecated"] += 1
            continue
        rank = RANKS.get(rank_uri)
        if rank is None:
            skipped["rank_unknown"] += 1
            continue
        if val is None:  # some value / no value / non-item object: not an item edge
            skipped["object_not_item"] += 1
            continue
        for q, en, fr in ((item, "en", "fr"), (val, "ven", "vfr")):
            for lang, var in (("en", en), ("fr", fr)):
                lab = (b.get(var) or {}).get("value")
                if lab:
                    labels[q].setdefault(lang, str(lab)[:500])
        if (sid, pid) in seen:  # label OPTIONALs or several ?root paths may repeat a statement row
            continue
        seen.add((sid, pid))
        rows.append(Row(item=item, property=pid, value=val, statement_id=sid, rank=rank))
    return rows, dict(labels), skipped


def snapshot_statement_ids(engine: Any, sids: Sequence[str], origin: str) -> set[str]:
    """Statement ids the KG already holds from a Wikidata snapshot (they are not loaded twice)."""
    from sqlalchemy import text

    out: set[str] = set()
    ids = sorted(set(sids))
    with engine.connect() as c:
        for i in range(0, len(ids), 5000):
            out.update(
                c.execute(
                    text(
                        "SELECT statement_id FROM kg.statement_current WHERE origin = :o "
                        "AND source_key = ANY(:s) AND statement_id = ANY(:q)"
                    ),
                    {"o": origin, "s": list(SNAPSHOT_SOURCES), "q": ids[i : i + 5000]},
                ).scalars()
            )
    return out


def build(
    rows: Sequence[Row],
    labels: Mapping[str, Mapping[str, str]],
    pack: StatementPack,
    *,
    raw_sha256: str,
    query_sha256: str,
    retrieved_at: datetime,
    qlever_index: str,
    held_by_snapshot: set[str],
    known_entities: set[str],
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    from reva.kg.model import KgEntity, KgStatement

    revision = f"qlever@{retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    sts: list[Any] = []
    for r in rows:
        if r.statement_id in held_by_snapshot:
            continue
        sts.append(
            KgStatement(
                subject_qid=r.item,
                property_pid=r.property,
                object_qid=r.value,
                qualifiers={
                    "wikidata_statement_id": r.statement_id,
                    "pack": pack.id,
                    "mirrors_spec": pack.mirrors,
                    "qualifiers_read": False,
                    "_revision": {
                        "wikidata_revision": "unknown",
                        "reason": "QLever rows carry no entity revision",
                        "qlever_index": qlever_index,
                        "retrieved_at": retrieved_at.isoformat(),
                    },
                },
                rank=r.rank,
                references=[
                    {
                        "stated_in": f"Wikidata via QLever ({r.property})",
                        "query_sha256": query_sha256,
                        "wikidata_references": "unknown (not read)",
                    }
                ],
                statement_id=f"{PREFIX}{pack.id}:{r.statement_id}",
                source_revision=revision,
                raw_sha256=raw_sha256,
            )
        )
    ends: dict[str, set[str]] = defaultdict(set)
    for s in sts:
        ends[s.subject_qid].add(f"subject of {s.property_pid}")
        ends[str(s.object_qid)].add(f"object of {s.property_pid}")
    ents = []
    for qid in sorted(ends):
        if qid in known_entities:
            continue
        lab = dict(labels.get(qid) or {}) or {"en": qid}  # no en/fr label: the QID itself, never a guessed name
        roles = ", ".join(sorted(ends[qid]))
        ents.append(
            KgEntity(
                qid=qid,
                kind="wikidata_item",
                labels=lab,
                descriptions={
                    "en": f"Wikidata item ({roles}; loaded by WDOPS from QLever, pack {pack.id})"[:500],
                    "fr": f"Élément Wikidata ({roles} ; chargé par WDOPS depuis QLever, paquet {pack.id})"[:500],
                },
            )
        )
    return (
        ents,
        sts,
        {
            "statements_read": len(rows),
            "held_by_snapshot": sum(1 for r in rows if r.statement_id in held_by_snapshot),
            "statements": len(sts),
            "by_property": dict(Counter(s.property_pid for s in sts)),
            "subjects": len({s.subject_qid for s in sts}),
            "objects": len({s.object_qid for s in sts}),
        },
    )


# ============================================================================================== atlas protocol
def atlas_source() -> list[AtlasSourceSpec]:
    packs = load_packs()
    return [
        AtlasSourceSpec(
            name=NAME,
            version=VERSION,
            atlas_source=ATLAS_SOURCE,
            source_key=QL_SOURCE_KEY,
            budget_key=QL_SOURCE_KEY,
            licence="CC0-1.0 (https://www.wikidata.org/wiki/Wikidata:Licensing)",
            provider=(
                "Wikidata (community-edited) as indexed by QLever (University of Freiburg, qlever.dev): the complete "
                "Wikidata RDF dump, updated live; same statements and statement ids as WDQS"
            ),
            datasets=(DATASET,),
            dataset_ids=tuple(f"wdops_pack:{k}" for k in sorted(packs)),
            allowed_hosts=(QL_HOST,),
            min_interval_seconds=3.0,
            cadence_seconds=30 * DAY,
            planned_requests=len(packs) + 1,
            max_response_bytes=MAX_BYTES,
            parser_version=PARSER_VERSION,
            code_digest=code_digest(),
            field_mappings={"statement": "Q --P<allow-listed Wikidata property>--> Q (statement id kept)"},
            notes=(
                "KG only: allow-listed Wikidata statements of the V4 statements specs (registry/kg/"
                "qlever_packs_wdops.yaml) read from QLever while the Wikibase API and WDQS throttle the shared egress."
            ),
        )
    ]


def _selected(run: Any, packs: Mapping[str, StatementPack]) -> list[StatementPack]:
    want = run.options.get("packs") if getattr(run, "options", None) else None
    if isinstance(want, str):
        want = [w.strip() for w in want.split(",") if w.strip()]
    if not want:
        return list(packs.values())
    unknown = [w for w in want if w not in packs]
    if unknown:
        from reva.kernel.errors import ValidationProblem

        raise ValidationProblem("unknown WDOPS pack", unknown=unknown, known=sorted(packs))
    return [packs[w] for w in want]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import register_properties

    packs = load_packs()
    selected = _selected(run, packs)
    wanted = {p for pk in selected for p in pk.properties}
    register_properties(run.engine, [p for p in kg_properties(load_allowlist()) if p.pid in wanted])
    fetcher = QleverFetcher(run)
    out: dict[str, Any] = {"packs_version": 1, "qlever_index": await fetcher.index_info(), "packs": {}}
    for pack in selected:
        out["packs"][pack.id] = await ingest_pack(run, fetcher, pack, str(out["qlever_index"].get("name_index")))
    out["client"] = fetcher.stats()
    return out


async def ingest_pack(run: Any, fetcher: QleverFetcher, pack: StatementPack, qlever_index: str) -> dict[str, Any]:
    res = await fetcher.sparql(pack_query(pack), f"wdops_{pack.id}")
    if len(res.bindings) > pack.max_rows:
        return {
            "status": "failed",
            "reason": "row_bound_exceeded",
            "rows": len(res.bindings),
            "max_rows": pack.max_rows,
            "raw_sha256": res.raw_sha256,
        }
    rows, labels, skipped = parse_rows(res.bindings, pack)
    held = snapshot_statement_ids(run.engine, [r.statement_id for r in rows], run.origin)
    qids = sorted({r.item for r in rows} | {r.value for r in rows})
    known = common.existing_entities(run.engine, qids, run.origin)
    ents, sts, rep = build(
        rows,
        labels,
        pack,
        raw_sha256=res.raw_sha256,
        query_sha256=res.query_sha256,
        retrieved_at=res.retrieved_at,
        qlever_index=qlever_index,
        held_by_snapshot=held,
        known_entities=known,
    )
    kg = common.load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=QL_SOURCE_KEY,
        raw_sha256s=[res.raw_sha256],
        retrieved_at=res.retrieved_at,
        origin=run.origin,
        parameters={
            "connector": NAME,
            "parser": PARSER_VERSION,
            "pack": pack.id,
            "mirrors_spec": pack.mirrors,
            "query_sha256": res.query_sha256,
            "qlever_index": qlever_index,
        },
        retract_prefix=f"{PREFIX}{pack.id}:",
    )
    return {
        "status": "done",
        "raw_sha256": res.raw_sha256,
        "bytes": res.bytes,
        "rows": len(res.bindings),
        "skipped": dict(skipped),
        **rep,
        "entities_new": len(ents),
        "kg": kg,
    }
