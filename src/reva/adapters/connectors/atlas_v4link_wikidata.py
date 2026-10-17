"""Wikidata external-identifier bridges for the V4 domains (V4, agent V4LINK).

A Wikidata item holding two identifiers of two authorities (ChEBI 15377 and PubChem CID 962; GBIF key 2436436 and
NCBI taxon 9606; ISO 3166-2 FR-IDF and NUTS FR1; ICD-10 E11 and MONDO 0005148; LEI and SEC CIK) is the evidence
that the two identifiers name the same thing. REVA writes, for each identifier an item holds,

    <scheme>:<value> --v4link:wikidata_item--> Q<item>        (rules with ``relation: identity``)
    Q<item> --v4link:classified_as--> <scheme>:<code>          (``relation: classification``: HS 2022, CPC 2.1,
                                                               ISIC Rev.4, NACE Rev.2, ISCO-08 codes of an item)

so that two identifiers meet through the item (two identity hops); nothing is merged and nothing is matched by
name. Rules (formats, normalisation, ambiguity guards) are in ``registry/kg/identity_rules_v4.yaml`` and applied by
:func:`reva.adapters.connectors.v4link_common.derive_bridges`: best-rank statements only; a value held by several
items, or an item holding several values of one identity property, is reported ambiguous and never linked. The
ISO 3166-2 / NUTS items also bring their best-rank ``P17`` (country) statements, so that an ADM1 region reached
from any V4 source is ``located_in`` its country.

Fetch: one WDQS query per *pack* of properties (``packs`` in the rules file; run option ``packs=a,b`` restricts a
run) through WDHARVEST's :class:`~reva.adapters.connectors.wikidata.WikidataClient` (machine-wide lock, pacing,
Retry-After back-off; query text and raw response archived), budget key ``wikidata.wdqs`` (no new budget). Huge
packs are bounded by ``max_items`` (the N items with most sitelinks: a documented size bound, never a judgement on the
items left out).

KG: source key ``wikidata.wdqs`` (CC0, community-edited), statement ids ``v4xref:<pack>:<Wikidata statement id>``
(country statements ``v4xref:<pack>:c:<id>``); a pack's statements that a new run no longer finds are retracted.
Items not yet in the KG are added as entities of kind ``wikidata_item`` with their English/French labels (an item
already loaded is never overwritten).
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

from reva.adapters.connectors import v4link_common as common
from reva.adapters.connectors.v4link_common import P_CLASSIFIED_AS, P_WIKIDATA_ITEM, Held, Pack, Rules
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.v4link_wikidata"
QL_NAME = "atlas.v4link_qlever"
VERSION = "1.1.1"
PARSER_VERSION = "atlas_v4link_wikidata/1"
SOURCE_KEY = "wikidata.wdqs"
ATLAS_SOURCE = "v4link"
DATASET = "wikidata_bridges"
WD_HOST = "query.wikidata.org"
QL_HOST = "qlever.dev"
QL_SOURCE_KEY = "qlever.wikidata"
QL_URL = f"https://{QL_HOST}/api/wikidata"
QL_PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
SPARQL_JSON = "application/sparql-results+json"
DAY = 86400
MAX_WD_BYTES = 60_000_000
PREFIX = "v4xref:"
RANKS = {"http://wikiba.se/ontology#PreferredRank": "preferred", "http://wikiba.se/ontology#NormalRank": "normal"}


def code_digest() -> str:
    h = hashlib.sha256()
    h.update(inspect.getsource(inspect.getmodule(code_digest)).encode())  # type: ignore[arg-type]
    h.update(inspect.getsource(common).encode())
    h.update(common.RULES_PATH.read_bytes())
    return "sha256:" + h.hexdigest()


# ============================================================================================== queries
def _selector(pack: Pack) -> str:
    """The items of a pack. ``max_items``: the N items with the most sitelinks (ORDER BY/LIMIT, not a FILTER: QLever
    answered numeric and IRI FILTERs inconsistently on 2026-10-17 -- 0 rows for ``FILTER(?sl >= 10)``)."""
    if pack.require_two and len(pack.properties) == 2:
        a, b = pack.properties
        if pack.max_items:
            return (
                f"{{ SELECT DISTINCT ?item ?sl WHERE {{\n    ?item wdt:{a} [] ; wdt:{b} [] ; wikibase:sitelinks ?sl .\n  }}"
                f" ORDER BY DESC(?sl) ?item LIMIT {int(pack.max_items)} }}"
            )
        return f"{{ SELECT DISTINCT ?item WHERE {{\n    ?item wdt:{a} [] ; wdt:{b} [] .\n  }} }}"
    if pack.max_items:
        raise ValueError(f"pack {pack.id}: max_items is supported for two-property require_two packs only")
    values = " ".join(f"wdt:{p}" for p in pack.properties)
    if pack.require_two:
        return (
            f"{{ SELECT ?item WHERE {{\n    VALUES ?pd {{ {values} }}\n    ?item ?pd [] .\n  }}"
            " GROUP BY ?item HAVING (COUNT(DISTINCT ?pd) >= 2) }"
        )
    return f"{{ SELECT DISTINCT ?item WHERE {{\n    VALUES ?pd {{ {values} }}\n    ?item ?pd [] .\n  }} }}"


def pack_query(pack: Pack) -> str:
    """Every non-deprecated statement of the pack's properties on the selected items, with en/fr labels."""
    unions = "\n  UNION\n".join(
        f'  {{ ?item p:{p} ?st . ?st ps:{p} ?v ; wikibase:rank ?rank . BIND("{p}" AS ?prop) }}' for p in pack.properties
    )
    return f"""SELECT ?item ?prop ?st ?v ?rank ?en ?fr WHERE {{
  {_selector(pack)}
{unions}
  OPTIONAL {{ ?item rdfs:label ?en . FILTER(LANG(?en) = "en") }}
  OPTIONAL {{ ?item rdfs:label ?fr . FILTER(LANG(?fr) = "fr") }}
}}"""


def duplicates_query(pid: str) -> str:
    """Values of one property held by several items anywhere in Wikidata (best rank)."""
    return f"""SELECT ?v WHERE {{
  ?i wdt:{pid} ?v .
}} GROUP BY ?v HAVING (COUNT(DISTINCT ?i) > 1)"""


def country_query(pack: Pack) -> str:
    """Best-rank-filtered P17 statements of every item holding one of the pack's properties. Written as a UNION
    without sub-select or DISTINCT: QLever answered those forms with 71 / 161 rows instead of ~9,000 on 2026-10-17
    (duplicate rows of an item holding both properties are removed in Python)."""
    holders = " UNION ".join(f"{{ ?item wdt:{p} [] }}" for p in pack.properties)
    return f"""SELECT ?item ?st ?country ?rank WHERE {{
  {holders}
  ?item p:P17 ?st . ?st ps:P17 ?country ; wikibase:rank ?rank .
}}"""


# ============================================================================================== parsing
def statement_id(uri: str | None) -> str | None:
    """Wikidata statement IRI -> ``Q42$GUID`` (the wbgetentities form). The munged dump QLever indexes writes the
    entity part in lower case (``.../statement/q42-GUID``); the item id is case-insensitive, so it is upper-cased."""
    if not uri:
        return None
    tail = uri.rsplit("/", 1)[-1]
    head, sep, rest = tail.partition("-")
    if sep and head[:1] in ("Q", "q") and head[1:].isdigit() and rest:
        return f"Q{head[1:]}${rest}"
    return None


def held_rows(bindings: Sequence[Mapping[str, Any]]) -> tuple[list[Held], dict[str, dict[str, str]], Counter[str]]:
    from reva.adapters.connectors.wikidata import qid_from_uri

    rows: list[Held] = []
    labels: dict[str, dict[str, str]] = defaultdict(dict)
    skipped: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    for b in bindings:
        qid = qid_from_uri((b.get("item") or {}).get("value"))
        sid = statement_id((b.get("st") or {}).get("value"))
        pid = str((b.get("prop") or {}).get("value") or "")
        val = str((b.get("v") or {}).get("value") or "")
        rank = RANKS.get(str((b.get("rank") or {}).get("value") or ""))
        if not qid or not qid.startswith("Q") or not sid or not pid or not val or rank is None:
            skipped["row_incomplete_or_deprecated"] += 1
            continue
        for lang in ("en", "fr"):
            lab = (b.get(lang) or {}).get("value")
            if lab:
                labels[qid].setdefault(lang, str(lab)[:500])
        if (sid, pid) in seen:  # label OPTIONALs may repeat a statement row
            continue
        seen.add((sid, pid))
        rows.append(Held(item=qid, property=pid, value=val, statement_id=sid, rank=rank))
    return rows, dict(labels), skipped


def bridge_statements(
    rows: Sequence[Held],
    rules: Rules,
    pack: Pack,
    *,
    raw_sha256: str,
    query_sha256: str,
    retrieved_at: datetime,
    global_duplicates: Mapping[str, set[str]] | None = None,
    stated_in: str = "Wikidata",
) -> tuple[list[Any], dict[str, Any]]:
    from reva.kg.model import KgStatement

    br = common.derive_bridges(rows, rules.rules, global_duplicates)
    revision = f"wdqs@{retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    out: list[Any] = []
    for h, key in br.links:
        rule = rules.rules[h.property]
        quals = {
            "wikidata_property": h.property,
            "wikidata_statement_id": h.statement_id,
            "value_as_stated": h.value,
            "rule": f"identity_rules_v4/{rules.version}:{h.property}",
            "relation": rule.relation,
            "pack": pack.id,
            "evidence": "wikidata_item_holds_identifier",
            "_revision": {
                "wikidata_revision": "unknown",
                "reason": "WDQS rows carry no entity revision",
                "retrieved_at": retrieved_at.isoformat(),
            },
        }
        subj, obj = (key, h.item) if rule.relation == "identity" else (h.item, key)
        out.append(
            KgStatement(
                subject_qid=subj,
                property_pid=P_WIKIDATA_ITEM if rule.relation == "identity" else P_CLASSIFIED_AS,
                object_qid=obj,
                qualifiers=quals,
                rank=h.rank,
                references=[{"stated_in": f"{stated_in} ({h.property} {rule.label})", "query_sha256": query_sha256}],
                statement_id=f"{PREFIX}{pack.id}:{h.statement_id}",
                source_revision=revision,
                raw_sha256=raw_sha256,
            )
        )
    return out, br.report


def country_statements(
    bindings: Sequence[Mapping[str, Any]],
    pack: Pack,
    *,
    raw_sha256: str,
    query_sha256: str,
    retrieved_at: datetime,
) -> tuple[list[Any], dict[str, Any]]:
    from reva.adapters.connectors.wikidata import qid_from_uri
    from reva.kg.model import KgStatement

    rows: list[Held] = []
    skipped: Counter[str] = Counter()
    for b in bindings:
        qid = qid_from_uri((b.get("item") or {}).get("value"))
        sid = statement_id((b.get("st") or {}).get("value"))
        country = qid_from_uri((b.get("country") or {}).get("value"))
        rank = RANKS.get(str((b.get("rank") or {}).get("value") or ""))
        if not qid or not sid or not country or not country.startswith("Q") or rank is None:
            skipped["row_incomplete_or_deprecated"] += 1
            continue
        rows.append(Held(item=qid, property="P17", value=country, statement_id=sid, rank=rank))
    best = {(h.item, h.statement_id): h for h in common.best_rank(rows)}
    revision = f"wdqs@{retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    out = [
        KgStatement(
            subject_qid=h.item,
            property_pid="P17",
            object_qid=h.value,
            qualifiers={"wikidata_statement_id": h.statement_id, "pack": pack.id},
            rank=h.rank,
            references=[{"stated_in": "Wikidata (P17 country)", "query_sha256": query_sha256}],
            statement_id=f"{PREFIX}{pack.id}:c:{h.statement_id}",
            source_revision=revision,
            raw_sha256=raw_sha256,
        )
        for h in sorted(best.values(), key=lambda x: x.statement_id)
    ]
    return out, {
        "rows": len(bindings),
        "statements": len(out),
        "items": len({h.item for h in best.values()}),
        "items_with_several_countries": sum(1 for v in Counter(h.item for h in best.values()).values() if v > 1),
        "skipped": dict(skipped),
    }


def item_entities(
    labels: Mapping[str, Mapping[str, str]], held: Sequence[Held], rules: Rules, known: set[str]
) -> list[Any]:
    from reva.kg.model import KgEntity

    props: dict[str, set[str]] = defaultdict(set)
    for h in held:
        props[h.item].add(h.property)
    out = []
    for qid in sorted(props):
        if qid in known:
            continue
        names = ", ".join(rules.rules[p].label for p in sorted(props[qid]) if p in rules.rules)
        lab = dict(labels.get(qid) or {})
        if not lab:
            lab = {"en": qid}  # no label in en/fr: the QID itself, never a guessed name
        out.append(
            KgEntity(
                qid=qid,
                kind="wikidata_item",
                labels=lab,
                descriptions={
                    "en": f"Wikidata item holding: {names} (loaded by V4LINK as an identifier bridge)"[:500],
                    "fr": f"Élément Wikidata portant : {names} (chargé par V4LINK comme pont d'identifiants)"[:500],
                },
            )
        )
    return out


# ============================================================================================== atlas protocol
def atlas_source() -> list[AtlasSourceSpec]:
    rules = common.load_rules()
    common_kw: dict[str, Any] = {
        "version": VERSION,
        "atlas_source": ATLAS_SOURCE,
        "licence": "CC0-1.0 (https://www.wikidata.org/wiki/Wikidata:Licensing)",
        "datasets": (DATASET,),
        "dataset_ids": tuple(f"wikidata:{p}" for p in sorted(rules.rules)),
        "cadence_seconds": 30 * DAY,
        "planned_requests": 2 * len(rules.packs) + len(rules.rules) + 4,
        "max_response_bytes": MAX_WD_BYTES,
        "parser_version": PARSER_VERSION,
        "code_digest": code_digest(),
        "field_mappings": {
            "identity": "<scheme>:<value> v4link:wikidata_item Q",
            "classification": "Q v4link:classified_as <scheme>:<code>",
        },
    }
    return [
        AtlasSourceSpec(
            name=NAME,
            source_key=SOURCE_KEY,
            budget_key=SOURCE_KEY,
            provider="Wikidata (WDQS), community-edited; external-identifier statements",
            allowed_hosts=(WD_HOST,),
            min_interval_seconds=2.0,
            notes="KG only: V4 identifier bridges through Wikidata items (registry/kg/identity_rules_v4.yaml), via WDQS.",
            **common_kw,
        ),
        AtlasSourceSpec(
            name=QL_NAME,
            source_key=QL_SOURCE_KEY,
            budget_key=QL_SOURCE_KEY,
            provider=(
                "Wikidata (community-edited) as indexed by QLever (University of Freiburg, qlever.dev): the complete "
                "Wikidata RDF dump, updated live; same statements and statement ids as WDQS"
            ),
            allowed_hosts=(QL_HOST,),
            min_interval_seconds=3.0,
            notes=(
                "Same packs and rules as atlas.v4link_wikidata, read from the QLever mirror when WDQS throttles the "
                "shared egress (429 with Retry-After ~1000 s on 2026-10-17); the index name/date is archived per run."
            ),
            **common_kw,
        ),
    ]


@dataclass
class Sparql:
    bindings: list[dict[str, Any]]
    raw_sha256: str
    query_sha256: str
    retrieved_at: datetime
    bytes: int

    def column(self, var: str) -> list[str | None]:
        return [b.get(var, {}).get("value") for b in self.bindings]


class WdqsFetcher:
    """WDHARVEST's locked, paced, archived WDQS client."""

    source_key = SOURCE_KEY

    def __init__(self, client: Any) -> None:
        self.client = client

    async def sparql(self, query: str, purpose: str) -> Sparql:
        r = await self.client.sparql(query, meta={"purpose": purpose, "connector": NAME})
        return Sparql(list(r.bindings), r.raw_sha256, r.query_sha256, r.retrieved_at, r.bytes)

    def stats(self) -> dict[str, Any]:
        return dict(self.client.stats.as_dict())


class QleverFetcher:
    """QLever's Wikidata endpoint through the run's budget-metered client (``run.get``, raw archived)."""

    source_key = QL_SOURCE_KEY

    def __init__(self, run: Any) -> None:
        self.run = run
        self.requests = 0

    async def get_json(self, url: str, key: str) -> tuple[Any, str, datetime, int]:
        import json as _json

        from reva.kernel.errors import SourceResponseError

        acq = await self.run.get(
            url, key=key, media_types=(SPARQL_JSON, "application/json"), headers={"Accept": SPARQL_JSON}
        )
        self.requests += 1
        try:
            doc = _json.loads(acq.body)
        except ValueError as e:
            raise SourceResponseError(f"QLever {key}: not JSON", retryable=False) from e
        return doc, self.run.archive(acq), acq.retrieved_at, len(acq.body)

    async def index_info(self) -> dict[str, Any]:
        doc, sha, _t, _n = await self.get_json(f"{QL_URL}?cmd=stats", "qlever_stats")
        return {
            "name_index": str(doc.get("name-index", ""))[:300],
            "version_server": doc.get("version-server"),
            "raw_sha256": sha,
        }

    async def sparql(self, query: str, purpose: str) -> Sparql:
        import hashlib as _h
        from urllib.parse import urlencode

        from reva.kernel.errors import SourceResponseError

        q = (QL_PREFIXES + query.strip()).strip()
        doc, sha, t, n = await self.get_json(f"{QL_URL}?{urlencode({'query': q})}", purpose)
        try:
            bindings = list(doc["results"]["bindings"])
        except (KeyError, TypeError) as e:
            raise SourceResponseError(f"QLever {purpose}: not SPARQL JSON results", retryable=False) from e
        return Sparql(bindings, sha, _h.sha256(q.encode()).hexdigest(), t, n)

    def stats(self) -> dict[str, Any]:
        return {"requests": self.requests}


async def global_duplicates(
    client: Any, rules: Rules, pack: Pack, raws: list[str]
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """For a filtered pack (``require_two`` / ``max_items``) the fetched items are a subset of Wikidata: ask WDQS
    which values of each identity property are held by several items anywhere. A query that fails (WDQS timeout on
    a very large property) leaves that property's check ``incomplete`` -- recorded, the run continues."""
    from reva.adapters.connectors.wikidata import WikidataError
    from reva.kernel.errors import SourceResponseError

    out: dict[str, set[str]] = {}
    check: dict[str, Any] = {}
    filtered = pack.require_two or bool(pack.max_items)
    for pid in pack.properties:
        rule = rules.rules[pid]
        if rule.relation != "identity" or rule.status != "active":
            continue
        if not filtered:
            check[pid] = "complete (all holders fetched)"
            continue
        try:
            res = await client.sparql(duplicates_query(pid), f"v4link_dups_{pid}")
        except (
            SourceResponseError,
            WikidataError,
        ) as e:  # WDQS failure: check incomplete, recorded (budget errors propagate)
            check[pid] = f"incomplete: {type(e).__name__}: {str(e)[:200]}"
            continue
        raws.append(res.raw_sha256)
        keys = {k for v in res.column("v") if v and (k := rule.key(v)) is not None}
        out[pid] = keys
        check[pid] = {"status": "complete", "values_on_several_items": len(keys), "raw_sha256": res.raw_sha256}
    return out, check


def _selected_packs(run: Any, rules: Rules) -> list[Pack]:
    want = run.options.get("packs") if getattr(run, "options", None) else None
    if isinstance(want, str):
        want = [w.strip() for w in want.split(",") if w.strip()]
    if not want:
        return list(rules.packs.values())
    unknown = [w for w in want if w not in rules.packs]
    if unknown:
        from reva.kernel.errors import ValidationProblem

        raise ValidationProblem("unknown V4LINK pack", unknown=unknown, known=sorted(rules.packs))
    return [rules.packs[w] for w in want]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    import tempfile

    from reva.adapters.connectors.wikidata import Archive, MachineLock, WikidataClient

    if not run.wants(DATASET):
        return {"skipped": True}
    rules = common.load_rules()
    out: dict[str, Any] = {"rules_version": rules.version, "packs": {}}
    with tempfile.TemporaryDirectory(prefix="reva-v4link-lock-") as tmp:
        fetcher: WdqsFetcher | QleverFetcher
        if run.spec.name == QL_NAME:
            fetcher = QleverFetcher(run)
            out["qlever_index"] = await fetcher.index_info()
        else:
            lock = MachineLock(Path(tmp), min_interval=0.0) if run.origin != "live" else None
            fetcher = WdqsFetcher(WikidataClient(run.http, archive=Archive(run.store), lock=lock, max_retries=6))
        for pack in _selected_packs(run, rules):
            out["packs"][pack.id] = await ingest_pack(run, fetcher, rules, pack)
        out["client"] = fetcher.stats()
    return out


async def ingest_pack(run: Any, fetcher: Any, rules: Rules, pack: Pack) -> dict[str, Any]:
    res = await fetcher.sparql(pack_query(pack), f"v4link_{pack.id}")
    held, labels, skipped = held_rows(res.bindings)
    raws = [res.raw_sha256]
    dups, dup_check = await global_duplicates(fetcher, rules, pack, raws)
    sts, brep = bridge_statements(
        held,
        rules,
        pack,
        raw_sha256=res.raw_sha256,
        query_sha256=res.query_sha256,
        retrieved_at=res.retrieved_at,
        global_duplicates=dups,
        stated_in="Wikidata via QLever" if fetcher.source_key == QL_SOURCE_KEY else "Wikidata",
    )
    brep["global_ambiguity_check"] = dup_check
    crep: dict[str, Any] | None = None
    if pack.with_country:
        cres = await fetcher.sparql(country_query(pack), f"v4link_{pack.id}_country")
        csts, crep = country_statements(
            cres.bindings,
            pack,
            raw_sha256=cres.raw_sha256,
            query_sha256=cres.query_sha256,
            retrieved_at=cres.retrieved_at,
        )
        sts.extend(csts)
        raws.append(cres.raw_sha256)
    linked_items = {s.object_qid if s.property_pid == P_WIKIDATA_ITEM else s.subject_qid for s in sts}
    linked = [h for h in held if h.item in linked_items]
    known = common.existing_entities(run.engine, sorted(linked_items), run.origin)
    ents = item_entities(labels, linked, rules, known)
    kg = common.load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=fetcher.source_key,
        raw_sha256s=raws,
        retrieved_at=res.retrieved_at,
        origin=run.origin,
        parameters={
            "connector": run.spec.name,
            "parser": PARSER_VERSION,
            "pack": pack.id,
            "query_sha256": res.query_sha256,
            "rules_version": rules.version,
        },
        retract_prefix=f"{PREFIX}{pack.id}:",
    )
    return {
        "raw_sha256": res.raw_sha256,
        "bytes": res.bytes,
        "rows": len(res.bindings),
        "statements_read": len(held),
        "items": len({h.item for h in held}),
        "skipped": dict(skipped),
        "bridges": brep,
        "country": crep,
        "entities_new": len(ents),
        "kg": kg,
    }
