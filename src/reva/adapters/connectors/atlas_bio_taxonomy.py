"""Tree of life -> KG taxa with ranks, parents and species counts (V4, agent V4BIO).

Three atlas sources (KG only, no atlas table: nothing here is per country):

``atlas.gbif_backbone`` (source key ``gbif.backbone``)
    The **GBIF Backbone Taxonomy** (dataset ``d7dddbf4-2cf0-4f39-9b2a-bb099caae36c``, CC BY 4.0, the 2023-08-28
    backbone with integer keys -- Wikidata P846). GBIF's own API (``api.gbif.org``) serves the dataset record and
    the **counts**; the listings are read through ChecklistBank dataset 53147, which is the same backbone imported
    with the GBIF keys as ids (its ``gbifKey`` is checked against the GBIF dataset key and its version against
    GBIF's ``pubDate`` on every run) because ChecklistBank's search returns ~1.3 kB per usage where GBIF's species
    search returns ~3-10 kB (descriptions, vernacular names of all languages) -- the difference between ~35 MB and
    ~300 MB of archived raw bytes for the same accepted usages.

    * entities ``gbif:<key>`` (kind ``taxon``) for every **accepted** usage of rank kingdom, phylum, class, order and
      family; the accepted species (and their genera, from each species' own classification) of the species groups
      of ``registry/bio/taxon_scope.yaml`` (vertebrate classes...); the curated scope species (crops, livestock,
      pathogens, model organisms, iconic species: REVA's selection of *which* keys to load, each key read from GBIF's
      ``/species/{key}`` and loaded only if GBIF's canonical name, rank and status equal what the scope file states).
    * statements ``gbif:parent`` (the usage's ``parentId`` / ``parentKey``, or the next classification element for a
      genus read from a species' classification), ``gbif:rank``, ``gbif:authorship``, ``gbif:accepted_species_count``.
    * counts: GBIF species-search facet ``higherTaxonKey`` over usages with ``rank=SPECIES`` and
      ``status=ACCEPTED`` of the backbone. A facet lists at most 50,000 values (GBIF answers 500 above that and
      refuses ``facetOffset``), so the run partitions it: root, then (when a partition is truncated) the children of
      the partition taxon, recursively, until every loaded taxon has a count or no deeper partition exists. A listed
      facet value is an exact count; a taxon absent from a *complete* (non-truncated) partition containing it has no
      accepted species under that definition: its count is stated as 0 with ``basis: absent_from_exhaustive_facet``.
      A taxon only under truncated partitions gets no count statement (reported as missing).

``atlas.col_checklist`` (source key ``col.checklistbank``)
    The latest **Catalogue of Life** release (ChecklistBank ``3LR``, CC BY 4.0, DOI per release): release metrics
    (import metrics: taxa/names by rank, extinct taxa by rank, synonyms, vernacular names...) on a ``col:release.<key>``
    entity, and the accepted taxa of rank domain/realm/kingdom/phylum/class with ``col:parent`` (nearest listed
    ancestor in the taxon's own classification, intermediate ranks recorded), ``col:rank`` and ``col:species_count``
    (ChecklistBank search facet ``TAXON_ID`` over ``rank=species, status=accepted``; and the same with
    ``extinct=false``). COL ids are never mapped to GBIF keys here: only Wikidata items holding both (P10585 + P846)
    bridge them (``atlas.bio_wikidata``).

``atlas.bio_wikidata`` (source key ``wikidata.wdqs``, community-edited, CC0)
    Identifier bridges and common names for the loaded taxa, through WDHARVEST's ``WikidataClient`` (machine-wide
    lock, back-off, archived). Per rank (kingdom..family) one query for every item with a GBIF key (P846) and that
    rank (P105), with its NCBI taxon id (P685), COL id (P10585) and fr/en common names (P1843); then the loaded
    species/genus GBIF keys and the loaded ``ncbitaxon:`` ids by VALUES chunks. Statements: ``bio:wikidata_item``
    (identifier -> item), ``bio:same_taxon_via_wikidata`` (ncbitaxon / col -> gbif, only when one item holds exactly
    one value of each property and each value is held by one item: anything else is reported as ambiguous and not
    linked), ``bio:common_name`` (verbatim P1843 text, fr/en).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import yaml

from reva.adapters.connectors import bio_common as bc
from reva.atlas.sources import AtlasSourceSpec

VERSION = "1.0.1"
PARSER_VERSION = "atlas_bio_taxonomy/1"

GBIF_NAME = "atlas.gbif_backbone"
GBIF_SOURCE_KEY = "gbif.backbone"
COL_NAME = "atlas.col_checklist"
COL_SOURCE_KEY = "col.checklistbank"
WD_NAME = "atlas.bio_wikidata"
WD_SOURCE_KEY = "wikidata.wdqs"
ATLAS_SOURCE = "bio"
GBIF_DATASET = "gbif_backbone"
COL_DATASET = "col_checklist"
WD_DATASET = "bio_wikidata"

GBIF_API = "https://api.gbif.org/v1"
CLB_API = "https://api.checklistbank.org"
GBIF_HOST = "api.gbif.org"
CLB_HOST = "api.checklistbank.org"
WD_HOST = "query.wikidata.org"
BACKBONE_KEY = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
CLB_BACKBONE = 53147
COL_LATEST = "3LR"

HIGHER_RANKS = ("kingdom", "phylum", "class", "order", "family")
COL_RANKS = ("domain", "realm", "kingdom", "phylum", "class")
PAGE = 1000
MAX_OFFSET = 100_000  # ChecklistBank search: offset + limit <= 100,000
FACET_LIMIT = 50_000  # GBIF answers 500 above this and refuses facetOffset
MAX_PARTITIONS = 600
MAX_PAGE_BYTES = 8_000_000
MAX_FACET_BYTES = 6_000_000
MAX_WD_BYTES = 40_000_000
WD_CHUNK = 350  # VALUES per WDQS query (URL <= 7,500 chars, WikidataClient.MAX_GET_URL)
SPECIES_UNIT = "species"

P_PARENT = "gbif:parent"
P_RANK = "gbif:rank"
P_AUTH = "gbif:authorship"
P_COUNT = "gbif:accepted_species_count"
C_PARENT = "col:parent"
C_RANK = "col:rank"
C_COUNT = "col:species_count"
C_METRIC = "col:release_metric"
W_ITEM = "bio:wikidata_item"
W_SAME = "bio:same_taxon_via_wikidata"
W_NAME = "bio:common_name"

GBIF_RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "GBIF Secretariat: GBIF Backbone Taxonomy. Checklist dataset https://doi.org/10.15468/39omei "
    "accessed via GBIF.org and ChecklistBank (dataset 53147).",
}
COL_RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Bánki, O. et al. Catalogue of Life (release named in each statement's revision, DOI per release). "
    "Species 2000 & ITIS Catalogue of Life, Leiden, Netherlands. https://www.catalogueoflife.org",
}
GBIF_DEFINITION = (
    "GBIF species search (api.gbif.org/v1/species/search) over the GBIF Backbone Taxonomy with rank=SPECIES and "
    "status=ACCEPTED: facet count of the taxon key in higherTaxonKey (the number of accepted species-rank usages "
    "classified under the taxon; fossil and extant species alike, the backbone's own classification)."
)
COL_DEFINITION = (
    "ChecklistBank name-usage search of the Catalogue of Life release with rank=species and status=accepted: facet "
    "count of the taxon id in TAXON_ID (accepted species-rank usages classified under the taxon)."
)


class TaxonomyError(bc.BioSourceError):
    pass


def code_digest() -> str:
    import sys

    return bc.digest(sys.modules[__name__], bc, files=(bc.SCOPE_FILE,))


# ============================================================================================== scope file
@dataclass(frozen=True)
class ScopeSpecies:
    key: int
    name: str
    group: str


@dataclass(frozen=True)
class Scope:
    species_groups: tuple[tuple[int, str, bool], ...]  # (gbif key, name, loaded by default)
    species: tuple[ScopeSpecies, ...]
    version: str

    def group_keys(self) -> list[int]:
        return [k for k, _n, load in self.species_groups if load]


def load_scope(path: Any = None) -> Scope:
    doc = yaml.safe_load((path or bc.SCOPE_FILE).read_text(encoding="utf-8"))
    groups = tuple((int(g["gbif_key"]), str(g["name"]), bool(g.get("load", True))) for g in doc["species_groups"])
    sp: list[ScopeSpecies] = []
    seen: set[int] = set()
    for grp, rows in doc["species"].items():
        for r in rows:
            k = int(r["gbif_key"])
            if k in seen:
                continue
            seen.add(k)
            sp.append(ScopeSpecies(k, str(r["name"]), str(grp)))
    return Scope(groups, tuple(sp), str(doc["version"]))


# ============================================================================================== usages
@dataclass
class Usage:
    """One accepted name usage as a source states it."""

    id: str
    name: str
    rank: str
    authorship: str = ""
    parent_id: str | None = None
    classification: tuple[tuple[str, str, str], ...] = ()  # (id, name, rank) root first, self last
    raw: str = ""  # sha256 of the raw page it was read from
    selector: dict[str, Any] = field(default_factory=dict)
    parent_basis: str = "parentId"

    def path_names(self) -> list[str]:
        return [n for (_i, n, r) in self.classification if r in HIGHER_RANKS and n != self.name]


def clb_search_url(dataset: int | str, params: Sequence[tuple[str, str]]) -> str:
    return f"{CLB_API}/dataset/{dataset}/nameusage/search?{urlencode(list(params))}"


def parse_clb_page(body: bytes, *, what: str) -> tuple[list[dict[str, Any]], int, bool]:
    try:
        doc = json.loads(body)
        res = doc.get("result") or []
        total = int(doc["total"])
    except (ValueError, KeyError, TypeError) as e:
        raise TaxonomyError(f"{what}: not a ChecklistBank search page", retryable=False, kind="schema_drift") from e
    return list(res), total, bool(doc.get("last", not res))


def usage_from_clb(rec: Mapping[str, Any], *, raw: str, selector: Mapping[str, Any]) -> Usage | None:
    """A ChecklistBank search result -> Usage (None for anything not an accepted usage with an id)."""
    u = rec.get("usage") or {}
    n = u.get("name") or {}
    status = u.get("status")
    if status != "accepted" or not rec.get("id"):
        return None
    cls = tuple(
        (str(c.get("id")), str(c.get("name") or ""), str(c.get("rank") or ""))
        for c in rec.get("classification") or []
        if c.get("id")
    )
    return Usage(
        id=str(rec["id"]),
        name=str(n.get("scientificName") or u.get("label") or ""),
        rank=str(n.get("rank") or ""),
        authorship=str(n.get("authorship") or ""),
        parent_id=str(u["parentId"]) if u.get("parentId") else None,
        classification=cls,
        raw=raw,
        selector=dict(selector),
    )


async def clb_pages(
    run: Any, dataset: int | str, params: Sequence[tuple[str, str]], *, key: str, raws: list[Any]
) -> tuple[list[tuple[dict[str, Any], str, dict[str, Any]]], int]:
    """All result records of a ChecklistBank search (paged by ``offset``), each with its raw sha and selector."""
    out: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    offset, total = 0, 0
    while True:
        if offset + PAGE > MAX_OFFSET:
            raise TaxonomyError(f"{key}: more than {MAX_OFFSET} usages (search offset limit)", retryable=False)
        url = clb_search_url(dataset, [*params, ("limit", str(PAGE)), ("offset", str(offset))])
        raw = await run.get(url, key=f"{key}:offset={offset}", max_bytes=MAX_PAGE_BYTES)
        sha = run.archive(raw)
        raws.append(raw)
        res, total, last = parse_clb_page(raw.body, what=key)
        for i, rec in enumerate(res):
            out.append((rec, sha, {"url_key": key, "offset": offset, "index": i}))
        offset += PAGE
        if last or not res or offset >= total:
            break
    if len(out) != total:
        raise TaxonomyError(
            f"{key}: paging returned {len(out)} records for total {total}", retryable=False, kind="truncated"
        )
    if len({str(rec.get("id")) for rec, _s, _sel in out}) != total:  # unstable sort: a page repeated a usage
        raise TaxonomyError(f"{key}: paging repeated usages (unstable order)", retryable=True, kind="paging")
    return out, total


# ============================================================================================== GBIF metadata
@dataclass(frozen=True)
class BackboneMeta:
    title: str
    pub_date: str  # YYYY-MM-DD
    licence_url: str
    clb_version: str
    clb_attempt: int | None
    doi: str | None

    @property
    def revision(self) -> str:
        att = f" attempt {self.clb_attempt}" if self.clb_attempt is not None else ""
        return f"backbone {self.pub_date} (ChecklistBank {CLB_BACKBONE}{att})"


def parse_backbone_meta(gbif_body: bytes, clb_body: bytes) -> BackboneMeta:
    try:
        g = json.loads(gbif_body)
        c = json.loads(clb_body)
    except ValueError as e:
        raise TaxonomyError("backbone dataset records are not JSON", retryable=False, kind="schema_drift") from e
    lic = str(g.get("license") or "")
    if "creativecommons.org/licenses/by/4.0" not in lic:
        raise TaxonomyError(f"GBIF backbone licence changed: {lic!r}", retryable=False, kind="licence")
    if str(c.get("gbifKey")) != BACKBONE_KEY:
        raise TaxonomyError(
            "ChecklistBank 53147 is not the GBIF backbone (gbifKey differs)", retryable=False, kind="schema_drift"
        )
    pub = str(g.get("pubDate") or "")[:10]
    if str(c.get("version") or "")[:10] != pub:
        raise TaxonomyError(
            f"ChecklistBank backbone version {c.get('version')!r} differs from GBIF pubDate {pub!r}",
            retryable=False,
            kind="version_mismatch",
        )
    if str(c.get("license") or "").lower() not in ("cc by", "cc-by", "cc by 4.0"):
        raise TaxonomyError(
            f"ChecklistBank licence of 53147 changed: {c.get('license')!r}", retryable=False, kind="licence"
        )
    doi = g.get("doi")
    return BackboneMeta(
        title=str(g.get("title") or "GBIF Backbone Taxonomy"),
        pub_date=pub,
        licence_url=lic,
        clb_version=str(c.get("version")),
        clb_attempt=int(c["attempt"]) if isinstance(c.get("attempt"), int) else None,
        doi=str(doi) if doi else None,
    )


def usage_from_gbif_species(rec: Mapping[str, Any], *, raw: str) -> Usage:
    """GBIF ``/species/{key}`` -> Usage with a classification from the record's ``<rank>Key`` fields."""
    cls: list[tuple[str, str, str]] = []
    for r in ("kingdom", "phylum", "class", "order", "family", "genus"):
        k, n = rec.get(f"{r}Key"), rec.get(r)
        if k is not None and n:
            cls.append((str(k), str(n), r))
    cls.append((str(rec["key"]), str(rec.get("canonicalName") or ""), str(rec.get("rank") or "").lower()))
    return Usage(
        id=str(rec["key"]),
        name=str(rec.get("canonicalName") or ""),
        rank=str(rec.get("rank") or "").lower(),
        authorship=str(rec.get("authorship") or "").strip(),
        parent_id=str(rec["parentKey"]) if rec.get("parentKey") is not None else None,
        classification=tuple(cls),
        raw=raw,
        selector={"endpoint": "species", "key": rec["key"]},
        parent_basis="parentKey",
    )


def check_scope_record(rec: Mapping[str, Any], sp: ScopeSpecies) -> str | None:
    """Why a GBIF record cannot be loaded for a scope entry (None: it can)."""
    if str(rec.get("key")) != str(sp.key):
        return "key_differs"
    if rec.get("datasetKey") != BACKBONE_KEY:
        return "not_backbone"
    if str(rec.get("taxonomicStatus")) != "ACCEPTED":
        return f"status_{rec.get('taxonomicStatus')}"
    if str(rec.get("rank")) != "SPECIES":
        return f"rank_{rec.get('rank')}"
    if rec.get("canonicalName") != sp.name:
        return "canonical_name_differs"
    return None


# ============================================================================================== GBIF facet counts
def facet_url(partition: str | None) -> str:
    q: list[tuple[str, str]] = [
        ("datasetKey", BACKBONE_KEY),
        ("rank", "SPECIES"),
        ("status", "ACCEPTED"),
        ("limit", "0"),
        ("facet", "higherTaxonKey"),
        ("facetLimit", str(FACET_LIMIT)),
    ]
    if partition is not None:
        q.append(("highertaxonKey", partition))
    return f"{GBIF_API}/species/search?{urlencode(q)}"


def parse_facet(body: bytes) -> tuple[list[tuple[str, int]], int]:
    try:
        doc = json.loads(body)
        total = int(doc["count"])
        facets = doc.get("facets") or []
        counts = facets[0]["counts"] if facets else []
        vals = [(str(c["name"]), bc.as_int(c["count"], "facet count")) for c in counts]
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise TaxonomyError("GBIF facet response has an unexpected shape", retryable=False, kind="schema_drift") from e
    return vals, total


@dataclass
class FacetCounts:
    counts: dict[str, tuple[int, str, str, str]]  # key -> (count, basis, partition, raw sha)
    partitions: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    missing: list[str]


def subtree(node: str, children: Mapping[str, Sequence[str]]) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        for c in children.get(n, ()):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


async def facet_counts(
    run: Any, *, needed: set[str], children: Mapping[str, Sequence[str]], roots: Sequence[str], raws: list[Any]
) -> FacetCounts:
    """Partitioned higherTaxonKey facets until every needed key is counted (see module doc)."""
    counts: dict[str, tuple[int, str, str, str]] = {}
    complete_cover: dict[str, tuple[str, str]] = {}  # key -> (partition, raw) of a complete partition containing it
    partitions: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    queue: list[str | None] = [None]
    done: set[str | None] = set()
    while queue:
        part = queue.pop(0)
        if part in done:
            continue
        done.add(part)
        if len(done) > MAX_PARTITIONS:
            raise TaxonomyError(f"more than {MAX_PARTITIONS} facet partitions", retryable=False, kind="too_large")
        raw = await run.get(facet_url(part), key=f"gbif:facet:{part or 'root'}", max_bytes=MAX_FACET_BYTES)
        sha = run.archive(raw)
        raws.append(raw)
        vals, total = parse_facet(raw.body)
        truncated = len(vals) >= FACET_LIMIT
        pname = part or "root"
        for k, c in vals:
            prev = counts.get(k)
            if prev is None:
                counts[k] = (c, "facet_value", pname, sha)
            elif prev[0] != c:
                conflicts.append({"key": k, "counts": [prev[0], c], "partitions": [prev[2], pname]})
        if part is not None and part not in counts:
            counts[part] = (total, "partition_total", pname, sha)
        partitions.append({"partition": pname, "values": len(vals), "truncated": truncated, "total": total})
        kids = list(roots) if part is None else list(children.get(part, ()))
        if not truncated:
            scope = needed if part is None else (subtree(part, children) & needed)
            for k in scope:
                complete_cover.setdefault(k, (pname, sha))
            continue
        for kid in kids:
            if kid in done:
                continue
            if ({kid} | subtree(kid, children)) & (needed - set(counts)):
                queue.append(kid)
    missing: list[str] = []
    for k in sorted(needed - set(counts)):
        cov = complete_cover.get(k)
        if cov is not None:
            counts[k] = (0, "absent_from_exhaustive_facet", cov[0], cov[1])
        else:
            missing.append(k)
    return FacetCounts(counts, partitions, conflicts, missing)


# ============================================================================================== GBIF KG
@dataclass
class KgParts:
    entities: list[Any]
    statements: list[Any]
    report: dict[str, Any]


RANK_FR = {
    "kingdom": "règne",
    "phylum": "embranchement",
    "class": "classe",
    "order": "ordre",
    "family": "famille",
    "genus": "genre",
    "species": "espèce",
    "domain": "domaine",
    "realm": "royaume (virus)",
}


def gbif_kg(
    usages: Mapping[str, Usage], counts: FacetCounts | None, *, meta: BackboneMeta, scope_groups: Mapping[str, str]
) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    rev = meta.revision
    when = bc.day_time(meta.pub_date)

    def st(subject: str, pid: str, sid: str, raw: str, sel: Mapping[str, Any], **kw: Any) -> None:
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                statement_id=sid,
                source_revision=rev,
                raw_sha256=raw,
                references=[{"stated_in": "GBIF Backbone Taxonomy", "selector": dict(sel)}],
                **kw,
            )
        )
        rep[pid] += 1

    for uid in sorted(usages, key=lambda x: (len(x), x)):
        u = usages[uid]
        node = f"gbif:{uid}"
        path = " > ".join(u.path_names())
        grp = scope_groups.get(uid)
        desc_en = f"{u.rank} in the GBIF Backbone Taxonomy ({meta.pub_date})" + (f"; {path}" if path else "")
        desc_fr = f"{RANK_FR.get(u.rank, u.rank)} de la taxonomie de référence GBIF ({meta.pub_date})" + (
            f" ; {path}" if path else ""
        )
        if grp:
            desc_en += f"; REVA scope list: {grp}"
            desc_fr += f" ; liste de périmètre REVA : {grp}"
        ents.append(
            KgEntity(
                qid=node,
                kind=bc.KIND_TAXON,
                labels={"en": u.name, "fr": u.name},
                descriptions={"en": desc_en[:500], "fr": desc_fr[:500]},
                source_revision=rev,
            )
        )
        st(node, P_RANK, f"rank:{uid}", u.raw, {**u.selector, "field": "rank"}, value=bc.string_value(u.rank))
        if u.authorship:
            st(
                node,
                P_AUTH,
                f"authorship:{uid}",
                u.raw,
                {**u.selector, "field": "authorship"},
                value=bc.string_value(u.authorship),
            )
        if u.parent_id and u.parent_id != uid:
            st(
                node,
                P_PARENT,
                f"parent:{uid}",
                u.raw,
                {**u.selector, "field": u.parent_basis},
                object_qid=f"gbif:{u.parent_id}",
                qualifiers={"rank": u.rank},
            )
        if counts is not None and uid in counts.counts:
            n, basis, part, sha = counts.counts[uid]
            st(
                node,
                P_COUNT,
                f"species_count:{uid}",
                sha,
                {"facet": "higherTaxonKey", "partition": part, "value": uid},
                value=bc.count_value(n, SPECIES_UNIT),
                point_in_time=when,
                qualifiers={"definition": GBIF_DEFINITION, "basis": basis, "facet_partition": part},
            )
    report = {
        "entities": len(ents),
        "statements": len(sts),
        "by_property": dict(rep),
        "by_rank": dict(Counter(u.rank for u in usages.values())),
    }
    return KgParts(ents, sts, report)


def genus_usages_from_species(species: Iterable[Usage]) -> dict[str, Usage]:
    """Genus usages stated by species' own classifications (id, name; parent = the element above the genus)."""
    out: dict[str, Usage] = {}
    for s in species:
        cls = list(s.classification)
        for i, (gid, gname, grank) in enumerate(cls):
            if grank != "genus" or gid in out:
                continue
            parent = cls[i - 1][0] if i > 0 else None
            out[gid] = Usage(
                id=gid,
                name=gname,
                rank="genus",
                parent_id=parent,
                classification=tuple(cls[: i + 1]),
                raw=s.raw,
                selector={**s.selector, "classification_of": s.id, "classification_index": i},
                parent_basis="classification",
            )
    return out


# ============================================================================================== GBIF ingest
async def ingest_gbif(run: Any) -> dict[str, Any]:
    from reva.kernel.errors import ValidationProblem

    if not run.wants(GBIF_DATASET):
        return {"skipped": True}
    scope = load_scope()
    opts = run.options or {}
    ranks = tuple(bc.opt_list(opts.get("ranks")) or HIGHER_RANKS)
    if any(r not in HIGHER_RANKS for r in ranks):
        raise ValidationProblem("unknown rank option", ranks=list(ranks))
    group_opt = bc.opt_list(opts.get("groups"))
    groups = [str(g) for g in group_opt] if group_opt is not None else [str(k) for k in scope.group_keys()]
    with_scope = str(opts.get("scope_species", "true")).lower() != "false"
    only_keys = set(bc.opt_list(opts.get("scope_keys")) or [])
    full = ranks == HIGHER_RANKS and group_opt is None and with_scope and not only_keys
    raws: list[Any] = []
    g_meta = await run.get(f"{GBIF_API}/dataset/{BACKBONE_KEY}", key="gbif:dataset", max_bytes=2_000_000)
    c_meta = await run.get(f"{CLB_API}/dataset/{CLB_BACKBONE}", key="clb:dataset:53147", max_bytes=2_000_000)
    meta = parse_backbone_meta(g_meta.body, c_meta.body)
    shas = [run.archive(g_meta), run.archive(c_meta)]
    raws += [g_meta, c_meta]
    usages: dict[str, Usage] = {}
    report: dict[str, Any] = {"meta": meta.__dict__ | {"revision": meta.revision}, "listings": {}}
    for rank in ranks:
        recs, total = await clb_pages(
            run,
            CLB_BACKBONE,
            [("rank", rank), ("status", "accepted"), ("sortBy", "NAME")],
            key=f"clb:53147:{rank}",
            raws=raws,
        )
        n = 0
        for rec, sha, sel in recs:
            u = usage_from_clb(rec, raw=sha, selector=sel)
            if u is not None and u.rank == rank:
                usages[u.id] = u
                n += 1
        report["listings"][rank] = {"total": total, "loaded": n}
    species: list[Usage] = []
    group_names = {k: n for k, n, _l in scope.species_groups}
    for g in groups:
        recs, total = await clb_pages(
            run,
            CLB_BACKBONE,
            [("TAXON_ID", g), ("rank", "species"), ("status", "accepted"), ("sortBy", "NAME")],
            key=f"clb:53147:species_of:{g}",
            raws=raws,
        )
        got = [u for rec, sha, sel in recs if (u := usage_from_clb(rec, raw=sha, selector=sel)) is not None]
        species += [u for u in got if u.rank == "species"]
        report["listings"][f"species_of:{g}"] = {"name": group_names.get(int(g)), "total": total, "loaded": len(got)}
    scope_groups: dict[str, str] = {}
    refused: list[dict[str, Any]] = []
    if with_scope:
        for sp in scope.species:
            if only_keys and str(sp.key) not in only_keys:
                continue
            raw = await run.get(f"{GBIF_API}/species/{sp.key}", key=f"gbif:species:{sp.key}", max_bytes=1_000_000)
            sha = run.archive(raw)
            raws.append(raw)
            rec = bc.json_body(raw, f"gbif species {sp.key}")
            why = check_scope_record(rec, sp)
            if why:
                refused.append({"key": sp.key, "name": sp.name, "group": sp.group, "reason": why})
                continue
            u = usage_from_gbif_species(rec, raw=sha)
            species.append(u)
            scope_groups[u.id] = sp.group
    for u in species:
        usages.setdefault(u.id, u)
    for gid, gu in genus_usages_from_species(species).items():
        usages.setdefault(gid, gu)
    # GBIF /species records state their genus by genusKey: the genus usage comes from that classification too
    children: dict[str, list[str]] = defaultdict(list)
    for u in usages.values():
        if u.parent_id and u.rank != "species":
            children[u.parent_id].append(u.id)
    roots = sorted(u.id for u in usages.values() if u.rank == "kingdom")
    needed = {u.id for u in usages.values() if u.rank != "species"}
    counts = await facet_counts(run, needed=needed, children=children, roots=roots, raws=raws)
    parts = gbif_kg(usages, counts, meta=meta, scope_groups=scope_groups)
    retrieved = max(r.retrieved_at for r in raws)
    kg = bc.load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=GBIF_SOURCE_KEY,
        raw_sha256s=sorted(set(shas + [u.raw for u in usages.values()] + [s.raw_sha256 for s in parts.statements])),
        retrieved_at=retrieved,
        origin=run.origin,
        parameters={
            "connector": GBIF_NAME,
            "parser": PARSER_VERSION,
            "revision": meta.revision,
            "ranks": list(ranks),
            "groups": groups,
            "scope_version": scope.version,
            "full": full,
        },
        prefixes=("gbif:",),
        retract_prefix="" if full else None,
    )
    return {
        **report,
        "raw": {"requests": len(raws), "bytes": bc.raw_bytes_total(raws)},
        "scope": {
            "version": scope.version,
            "species": len(scope.species),
            "refused": refused,
            "loaded": len(scope_groups),
        },
        "counts": {
            "partitions": counts.partitions,
            "conflicts": counts.conflicts[:50],
            "missing_count": len(counts.missing),
            "missing_sample": counts.missing[:50],
            "zero_by_exhaustive_facet": sum(
                1 for v in counts.counts.values() if v[1] != "facet_value" and v[1] != "partition_total"
            ),
        },
        "build": parts.report,
        "kg": kg,
    }


# ============================================================================================== COL
@dataclass(frozen=True)
class ColRelease:
    key: int
    title: str
    version: str
    issued: str
    doi: str
    alias: str
    licence: str

    @property
    def revision(self) -> str:
        return f"{self.alias} doi:{self.doi}"[:100]


def parse_col_release(body: bytes) -> ColRelease:
    try:
        d = json.loads(body)
        rel = ColRelease(
            key=int(d["key"]),
            title=str(d["title"]),
            version=str(d.get("version") or ""),
            issued=str(d.get("issued") or "")[:10],
            doi=str(d.get("doi") or ""),
            alias=str(d.get("alias") or ""),
            licence=str(d.get("license") or ""),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise TaxonomyError("COL release record has an unexpected shape", retryable=False, kind="schema_drift") from e
    if rel.licence.lower() not in ("cc by", "cc-by", "cc by 4.0"):
        raise TaxonomyError(f"COL licence changed: {rel.licence!r}", retryable=False, kind="licence")
    if not rel.title.startswith("Catalogue of Life") or not rel.doi or not re.match(r"^\d{4}-\d{2}-\d{2}$", rel.issued):
        raise TaxonomyError("not a Catalogue of Life release record", retryable=False, kind="schema_drift")
    return rel


METRIC_KEYS = (
    "taxonCount",
    "synonymCount",
    "nameCount",
    "vernacularCount",
    "distributionCount",
    "referenceCount",
    "typeMaterialCount",
    "estimateCount",
)
METRIC_MAPS = ("taxaByRankCount", "extinctTaxaByRankCount", "namesByRankCount", "namesByCodeCount")


def col_facet_url(release: int, extant_only: bool) -> str:
    q = [
        ("rank", "species"),
        ("status", "accepted"),
        ("limit", "0"),
        ("facet", "TAXON_ID"),
        ("facetLimit", str(FACET_LIMIT)),
    ]
    if extant_only:
        q.append(("extinct", "false"))
    return clb_search_url(release, q)


def parse_clb_facet(body: bytes) -> tuple[dict[str, int], int]:
    try:
        doc = json.loads(body)
        vals = (doc.get("facets") or {}).get("taxonId") or []
        return {str(v["value"]): bc.as_int(v["count"], "facet") for v in vals}, int(doc["total"])
    except (ValueError, KeyError, TypeError) as e:
        raise TaxonomyError("ChecklistBank facet response has an unexpected shape", retryable=False) from e


def col_kg(
    rel: ColRelease,
    metrics: Mapping[str, Any],
    metrics_sha: str,
    usages: Mapping[str, Usage],
    facets: Mapping[str, tuple[dict[str, int], int, str]],  # variant -> (counts, total, raw sha)
    complete: Mapping[str, bool],
) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    rev = rel.revision
    when = bc.day_time(rel.issued)
    rel_node = f"col:release.{rel.key}"

    def st(subject: str, pid: str, sid: str, raw: str, sel: Mapping[str, Any], **kw: Any) -> None:
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                statement_id=sid,
                source_revision=rev,
                raw_sha256=raw,
                references=[{"stated_in": f"Catalogue of Life {rel.alias}", "selector": dict(sel)}],
                **kw,
            )
        )
        rep[pid] += 1

    ents.append(
        KgEntity(
            qid=rel_node,
            kind="dataset",
            labels={"en": f"Catalogue of Life {rel.alias}", "fr": f"Catalogue of Life {rel.alias}"},
            descriptions={
                "en": f"Catalogue of Life checklist release issued {rel.issued} (doi:{rel.doi})",
                "fr": f"Version du Catalogue of Life publiée le {rel.issued} (doi:{rel.doi})",
            },
            source_revision=rev,
        )
    )
    for k in METRIC_KEYS:
        if isinstance(metrics.get(k), int):
            st(
                rel_node,
                C_METRIC,
                f"metric:{k}",
                metrics_sha,
                {"field": k},
                value=bc.count_value(int(metrics[k]), "records"),
                point_in_time=when,
                qualifiers={"metric": k},
            )
    for m in METRIC_MAPS:
        for rk, v in sorted((metrics.get(m) or {}).items()):
            if isinstance(v, int) and rk in ("species", "genus", "family", "order", "class", "phylum", "kingdom"):
                st(
                    rel_node,
                    C_METRIC,
                    f"metric:{m}:{rk}",
                    metrics_sha,
                    {"field": m, "key": rk},
                    value=bc.count_value(v, "records"),
                    point_in_time=when,
                    qualifiers={"metric": m, "rank": rk},
                )
    ids = set(usages)
    for uid in sorted(usages):
        u = usages[uid]
        node = f"col:{uid}"
        path = " > ".join(n for (_i, n, r) in u.classification if r in COL_RANKS and n != u.name)
        ents.append(
            KgEntity(
                qid=node,
                kind=bc.KIND_TAXON,
                labels={"en": u.name, "fr": u.name},
                descriptions={
                    "en": f"{u.rank} in the Catalogue of Life ({rel.alias})" + (f"; {path}" if path else ""),
                    "fr": f"{RANK_FR.get(u.rank, u.rank)} du Catalogue of Life ({rel.alias})"
                    + (f" ; {path}" if path else ""),
                },
                source_revision=rev,
            )
        )
        st(node, C_RANK, f"rank:{uid}", u.raw, {**u.selector, "field": "rank"}, value=bc.string_value(u.rank))
        anc = [c for c in u.classification[:-1] if c[0] in ids]
        if anc:
            pid_, _pn, _pr = anc[-1]
            idx = [c[0] for c in u.classification].index(pid_)
            skipped = [f"{n} ({r})" for (_i, n, r) in u.classification[idx + 1 : -1]]
            st(
                node,
                C_PARENT,
                f"parent:{uid}",
                u.raw,
                {**u.selector, "field": "classification"},
                object_qid=f"col:{pid_}",
                qualifiers={"rank": u.rank, "intermediate_ranks_skipped": skipped},
            )
        for variant, (vals, _total, sha) in facets.items():
            sid = "species_count" if variant == "all" else "species_count_extant"
            definition = COL_DEFINITION + (" Restricted to extinct=false." if variant == "extant" else "")
            if uid in vals:
                n, basis = vals[uid], "facet_value"
            elif complete.get(variant):
                n, basis = 0, "absent_from_exhaustive_facet"
            else:
                continue
            st(
                node,
                C_COUNT,
                f"{sid}:{uid}",
                sha,
                {"facet": "TAXON_ID", "variant": variant, "value": uid},
                value=bc.count_value(n, SPECIES_UNIT),
                point_in_time=when,
                qualifiers={"definition": definition, "variant": variant, "basis": basis},
            )
    return KgParts(ents, sts, {"entities": len(ents), "statements": len(sts), "by_property": dict(rep)})


async def ingest_col(run: Any) -> dict[str, Any]:
    if not run.wants(COL_DATASET):
        return {"skipped": True}
    raws: list[Any] = []
    r0 = await run.get(f"{CLB_API}/dataset/{COL_LATEST}", key="clb:dataset:3LR", max_bytes=4_000_000)
    rel = parse_col_release(r0.body)
    shas = [run.archive(r0)]
    raws.append(r0)
    r1 = await run.get(f"{CLB_API}/dataset/{rel.key}/import?limit=1", key=f"clb:import:{rel.key}", max_bytes=4_000_000)
    msha = run.archive(r1)
    shas.append(msha)
    raws.append(r1)
    imp = bc.json_body(r1, "COL import metrics")
    metrics = imp[0] if isinstance(imp, list) and imp else {}
    usages: dict[str, Usage] = {}
    listed: dict[str, int] = {}
    for rank in COL_RANKS:
        recs, total = await clb_pages(
            run,
            rel.key,
            [("rank", rank), ("status", "accepted"), ("sortBy", "NAME")],
            key=f"clb:{rel.key}:{rank}",
            raws=raws,
        )
        for rec, sha, sel in recs:
            u = usage_from_clb(rec, raw=sha, selector=sel)
            if u is not None and u.rank == rank:
                usages[u.id] = u
        listed[rank] = total
    facets: dict[str, tuple[dict[str, int], int, str]] = {}
    complete: dict[str, bool] = {}
    for variant in ("all", "extant"):
        r = await run.get(
            col_facet_url(rel.key, variant == "extant"), key=f"clb:{rel.key}:facet:{variant}", max_bytes=MAX_FACET_BYTES
        )
        sha = run.archive(r)
        raws.append(r)
        vals, total = parse_clb_facet(r.body)
        facets[variant] = (vals, total, sha)
        complete[variant] = len(vals) < FACET_LIMIT
    parts = col_kg(rel, metrics, msha, usages, facets, complete)
    kg = bc.load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=COL_SOURCE_KEY,
        raw_sha256s=sorted({*shas, *(u.raw for u in usages.values()), *(f[2] for f in facets.values())}),
        retrieved_at=max(r.retrieved_at for r in raws),
        origin=run.origin,
        parameters={"connector": COL_NAME, "parser": PARSER_VERSION, "release": rel.__dict__},
        prefixes=("col:",),
        retract_prefix="",
    )
    missing = sorted(u for u in usages if not any(u in f[0] for f in facets.values()))
    return {
        "release": rel.__dict__ | {"revision": rel.revision},
        "listed": listed,
        "facets": {k: {"values": len(v[0]), "total": v[1], "complete": complete[k]} for k, v in facets.items()},
        "taxa_without_facet_value": len(missing),
        "raw": {"requests": len(raws), "bytes": bc.raw_bytes_total(raws)},
        "build": parts.report,
        "kg": kg,
    }


# ============================================================================================== Wikidata bridges
RANK_QIDS = {"kingdom": "Q36732", "phylum": "Q38348", "class": "Q37517", "order": "Q36602", "family": "Q35409"}
WD_RANKS = {"http://wikiba.se/ontology#PreferredRank": "preferred", "http://wikiba.se/ontology#NormalRank": "normal"}
GBIF_KEY_RE = re.compile(r"^[1-9][0-9]{0,11}$")
NCBI_RE = re.compile(r"^[1-9][0-9]{0,9}$")
COL_ID_RE = re.compile(r"^[A-Z0-9]{1,12}$")


def wd_rank_query(rank_qid: str) -> str:
    return f"""SELECT ?item ?st ?gbif ?rk ?ncbi ?col ?cn WHERE {{
  ?item p:P846 ?st . ?st ps:P846 ?gbif ; wikibase:rank ?rk . FILTER(?rk != wikibase:DeprecatedRank)
  ?item wdt:P105 wd:{rank_qid} .
  OPTIONAL {{ ?item wdt:P685 ?ncbi }}
  OPTIONAL {{ ?item wdt:P10585 ?col }}
  OPTIONAL {{ ?item wdt:P1843 ?cn . FILTER(LANG(?cn) = "fr" || LANG(?cn) = "en") }}
}}"""


def wd_values_query(prop: str, values: Sequence[str]) -> str:
    vals = " ".join(f'"{v}"' for v in values)
    if prop == "P846":
        return f"""SELECT ?item ?st ?gbif ?rk ?ncbi ?col ?cn WHERE {{
  VALUES ?gbif {{ {vals} }}
  ?item p:P846 ?st . ?st ps:P846 ?gbif ; wikibase:rank ?rk . FILTER(?rk != wikibase:DeprecatedRank)
  OPTIONAL {{ ?item wdt:P685 ?ncbi }}
  OPTIONAL {{ ?item wdt:P10585 ?col }}
  OPTIONAL {{ ?item wdt:P1843 ?cn . FILTER(LANG(?cn) = "fr" || LANG(?cn) = "en") }}
}}"""
    return f"""SELECT ?item ?nst ?ncbi ?nrk ?gbif WHERE {{
  VALUES ?ncbi {{ {vals} }}
  ?item p:P685 ?nst . ?nst ps:P685 ?ncbi ; wikibase:rank ?nrk . FILTER(?nrk != wikibase:DeprecatedRank)
  OPTIONAL {{ ?item wdt:P846 ?gbif }}
}}"""


@dataclass
class WdRows:
    """Bindings of several WDQS results, each row with the raw sha of its result."""

    rows: list[tuple[Mapping[str, Any], str]] = field(default_factory=list)


def _b(row: Mapping[str, Any], var: str) -> str | None:
    v = (row.get(var) or {}).get("value")
    return str(v) if v is not None else None


def wikidata_kg(
    rows: Sequence[tuple[Mapping[str, Any], str]],
    *,
    retrieved_at: datetime,
    loaded_gbif: set[str],
    loaded_ncbi: set[str],
    loaded_col: set[str],
) -> KgParts:
    """WDQS rows -> bridges and common names (see module doc); ambiguity rules as V4LINK's identity rules."""
    from reva.adapters.connectors.wikidata import qid_from_uri, statement_id_from_uri
    from reva.kg.model import KgStatement

    rev = f"wdqs@{retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    item_vals: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    val_items: dict[tuple[str, str], set[str]] = defaultdict(set)
    gbif_st: dict[tuple[str, str], tuple[str, str, str]] = {}  # (item, key) -> (statement id, rank, raw)
    ncbi_st: dict[tuple[str, str], tuple[str, str, str]] = {}
    names: dict[tuple[str, str, str], str] = {}  # (item, lang, text) -> raw
    item_raw: dict[str, str] = {}
    skipped: Counter[str] = Counter()
    for row, sha in rows:
        qid = qid_from_uri(_b(row, "item"))
        if not qid:
            skipped["no_item"] += 1
            continue
        item_raw.setdefault(qid, sha)
        g, n, c = _b(row, "gbif"), _b(row, "ncbi"), _b(row, "col")
        if g is not None:
            if GBIF_KEY_RE.match(g):
                item_vals[qid]["P846"].add(g)
                val_items[("P846", g)].add(qid)
                sid = statement_id_from_uri(_b(row, "st"))
                rk = WD_RANKS.get(_b(row, "rk") or "", "normal")
                if sid:
                    gbif_st[(qid, g)] = (sid, rk, sha)
            else:
                skipped["malformed_P846"] += 1
        if n is not None:
            if NCBI_RE.match(n):
                item_vals[qid]["P685"].add(n)
                val_items[("P685", n)].add(qid)
                sid = statement_id_from_uri(_b(row, "nst"))
                if sid:
                    ncbi_st[(qid, n)] = (sid, WD_RANKS.get(_b(row, "nrk") or "", "normal"), sha)
            else:
                skipped["malformed_P685"] += 1
        if c is not None:
            if COL_ID_RE.match(c):
                item_vals[qid]["P10585"].add(c)
                val_items[("P10585", c)].add(qid)
            else:
                skipped["malformed_P10585"] += 1
        cn = (row.get("cn") or {}) if isinstance(row.get("cn"), Mapping) else {}
        lang = cn.get("xml:lang")
        if cn.get("value") and lang in ("fr", "en"):
            names.setdefault((qid, str(lang), str(cn["value"])), sha)
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    amb: Counter[str] = Counter()

    def ref(prop: str, qid: str) -> list[dict[str, Any]]:
        return [{"stated_in": f"Wikidata ({prop})", "item": qid}]

    def unique(prop: str, qid: str) -> str | None:
        vals = item_vals[qid].get(prop) or set()
        if len(vals) != 1:
            if vals:
                amb[f"{prop}_several_values_on_item"] += 1
            return None
        v = next(iter(vals))
        if len(val_items[(prop, v)]) != 1:
            amb[f"{prop}_value_on_several_items"] += 1
            return None
        return v

    for qid in sorted(item_vals):
        g = unique("P846", qid)
        n = unique("P685", qid)
        c = unique("P10585", qid)
        raw = item_raw[qid]
        if g is not None and g in loaded_gbif:
            sid, rk, sha = gbif_st.get((qid, g), (f"{qid}:P846:{g}", "normal", raw))
            sts.append(
                KgStatement(
                    subject_qid=f"gbif:{g}",
                    property_pid=W_ITEM,
                    object_qid=qid,
                    rank=rk,
                    qualifiers={"wikidata_property": "P846", "wikidata_statement_id": sid},
                    references=ref("P846", qid),
                    statement_id=f"bio_wd:gbif:{sid}",
                    source_revision=rev,
                    raw_sha256=sha,
                )
            )
            rep["gbif_item"] += 1
        if n is not None and (n in loaded_ncbi or (g is not None and g in loaded_gbif)):
            sid, rk, sha = ncbi_st.get((qid, n), (f"{qid}:P685:{n}", "normal", raw))
            sts.append(
                KgStatement(
                    subject_qid=f"ncbitaxon:{n}",
                    property_pid=W_ITEM,
                    object_qid=qid,
                    rank=rk,
                    qualifiers={"wikidata_property": "P685", "wikidata_statement_id": sid},
                    references=ref("P685", qid),
                    statement_id=f"bio_wd:ncbi:{sid}",
                    source_revision=rev,
                    raw_sha256=sha,
                )
            )
            rep["ncbi_item"] += 1
        if g is not None and n is not None and (g in loaded_gbif or n in loaded_ncbi):
            sts.append(
                KgStatement(
                    subject_qid=f"ncbitaxon:{n}",
                    property_pid=W_SAME,
                    object_qid=f"gbif:{g}",
                    qualifiers={"via_item": qid, "properties": ["P685", "P846"]},
                    references=ref("P685+P846", qid),
                    statement_id=f"bio_wd:same:ncbi:{n}:gbif:{g}",
                    source_revision=rev,
                    raw_sha256=raw,
                )
            )
            rep["ncbi_same_gbif"] += 1
        if g is not None and c is not None and (g in loaded_gbif or c in loaded_col):
            sts.append(
                KgStatement(
                    subject_qid=f"col:{c}",
                    property_pid=W_SAME,
                    object_qid=f"gbif:{g}",
                    qualifiers={"via_item": qid, "properties": ["P10585", "P846"]},
                    references=ref("P10585+P846", qid),
                    statement_id=f"bio_wd:same:col:{c}:gbif:{g}",
                    source_revision=rev,
                    raw_sha256=raw,
                )
            )
            rep["col_same_gbif"] += 1
    gbif_of_item = {q: unique("P846", q) for q in item_vals}
    for (qid, lang, txt), sha in sorted(names.items()):
        g = gbif_of_item.get(qid)
        if g is None or g not in loaded_gbif:
            continue
        sts.append(
            KgStatement(
                subject_qid=f"gbif:{g}",
                property_pid=W_NAME,
                value=bc.text_value(txt[:400], lang),
                qualifiers={"wikidata_property": "P1843", "via_item": qid},
                references=ref("P1843", qid),
                statement_id=f"bio_wd:name:{g}:{lang}:{bc_hash(txt)}",
                source_revision=rev,
                raw_sha256=sha,
            )
        )
        rep[f"common_name_{lang}"] += 1
    return KgParts(
        [],
        sts,
        {
            "statements": len(sts),
            "by_kind": dict(rep),
            "ambiguous": dict(amb),
            "skipped": dict(skipped),
            "items": len(item_vals),
        },
    )


def bc_hash(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode()).hexdigest()[:16]


async def ingest_wikidata(run: Any) -> dict[str, Any]:
    import tempfile
    from pathlib import Path

    from reva.adapters.connectors.wikidata import (
        Archive,
        MachineLock,
        QueryTimeout,
        WikidataClient,
        WikidataError,
    )

    if not run.wants(WD_DATASET):
        return {"skipped": True}
    opts = run.options or {}
    max_queries = int(opts.get("max_queries", 150))
    loaded_gbif = {q.split(":", 1)[1] for q in bc.current_entities(run.engine, run.origin, "gbif:%")}
    loaded_ncbi = {q.split(":", 1)[1] for q in bc.current_entities(run.engine, run.origin, "ncbitaxon:%")}
    loaded_col = {q.split(":", 1)[1] for q in bc.current_entities(run.engine, run.origin, "col:%")} - {""}
    ranks = _ranks_of_loaded(run.engine, run.origin)
    rows: list[tuple[Mapping[str, Any], str]] = []
    queries: list[dict[str, Any]] = []
    shas: list[str] = []
    retrieved: list[datetime] = []
    timeouts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="reva-v4bio-lock-") as tmp:
        lock = MachineLock(Path(tmp), min_interval=0.0) if run.origin != "live" else None
        client = WikidataClient(run.http, archive=Archive(run.store), lock=lock, max_retries=6)

        async def ask(q: str, purpose: str) -> None:
            if len(queries) >= max_queries:
                raise _Budget()
            try:
                res = await client.sparql(q, meta={"purpose": purpose, "connector": WD_NAME})
            except QueryTimeout:  # this query only: the next ones still run (the run is then incomplete)
                queries.append({"purpose": purpose, "error": "query_timeout"})
                timeouts.append(purpose)
                return
            queries.append({"purpose": purpose, "rows": len(res.bindings), "raw_sha256": res.raw_sha256})
            shas.append(res.raw_sha256)
            retrieved.append(res.retrieved_at)
            rows.extend((b, res.raw_sha256) for b in res.bindings)

        complete = True
        stopped: str | None = None
        try:
            # 1. per-rank queries (kingdom..family: every item with a GBIF key and that rank)
            for rank, rq in RANK_QIDS.items():
                await ask(wd_rank_query(rq), f"gbif_rank:{rank}")
            covered = {g for r, _s in rows if (g := _b(r, "gbif"))}
            # 2. curated scope species, 3. NCBI ids that carry genomes / reviewed proteins, 4. the other species/genera
            scope = {str(sp.key) for sp in load_scope().species} & loaded_gbif
            prio_ncbi = _priority_ncbi(run.engine, run.origin, int(opts.get("min_reviewed", 100))) & loaded_ncbi
            rest = {k for k, rk in ranks.items() if rk in ("genus", "species")} - covered - scope
            plan = [
                ("P846", "scope", sorted(scope - covered, key=lambda x: (len(x), x))),
                ("P685", "ncbi_priority", sorted(prio_ncbi, key=lambda x: (len(x), x))),
                ("P846", "gbif_rest", sorted(rest, key=lambda x: (len(x), x))),
                ("P685", "ncbi_rest", sorted(loaded_ncbi - prio_ncbi, key=lambda x: (len(x), x))),
            ]
            for prop, label, keys in plan:
                for i in range(0, len(keys), WD_CHUNK):
                    await ask(wd_values_query(prop, keys[i : i + WD_CHUNK]), f"{label}:{i}")
        except _Budget:
            complete, stopped = False, f"max_queries={max_queries} reached"
        except WikidataError as e:  # throttled / timed out: keep what was answered, never retract
            complete, stopped = False, f"{type(e).__name__}: {str(e)[:200]}"
        if timeouts:
            complete = False
    if not retrieved:
        return {"skipped": True, "reason": "no query sent"}
    parts = wikidata_kg(
        rows, retrieved_at=max(retrieved), loaded_gbif=loaded_gbif, loaded_ncbi=loaded_ncbi, loaded_col=loaded_col
    )
    kg = bc.load_kg(
        run.engine,
        run.store,
        entities=[],
        statements=parts.statements,
        source_key=WD_SOURCE_KEY,
        raw_sha256s=sorted(set(shas)),
        retrieved_at=max(retrieved),
        origin=run.origin,
        parameters={"connector": WD_NAME, "parser": PARSER_VERSION, "queries": len(queries), "complete": complete},
        prefixes=("bio:",),
        retract_prefix="bio_wd:" if complete else None,
    )
    return {
        "queries": queries,
        "complete": complete,
        "stopped": stopped,
        "loaded": {"gbif": len(loaded_gbif), "ncbi": len(loaded_ncbi), "col": len(loaded_col)},
        "build": parts.report,
        "kg": kg,
        "client": client.stats.as_dict(),
    }


class _Budget(Exception):
    pass


def _priority_ncbi(engine: Any, origin: str, min_reviewed: int) -> set[str]:
    """NCBI taxon ids that genome assemblies point to, plus organisms with >= ``min_reviewed`` Swiss-Prot entries."""
    from sqlalchemy import text

    with engine.connect() as c:
        a = set(
            c.execute(
                text("""SELECT DISTINCT split_part(object_qid, ':', 2) FROM kg.statement_current
                        WHERE origin = :o AND property_pid IN ('ensembl:assembly_of', 'ncbi:assembly_of')"""),
                {"o": origin},
            ).scalars()
        )
        b = set(
            c.execute(
                text("""SELECT split_part(subject_qid, ':', 2) FROM kg.statement_current
                        WHERE origin = :o AND property_pid = 'uniprot:reviewed_protein_count'
                          AND (value->>'amount')::numeric >= :m"""),
                {"o": origin, "m": min_reviewed},
            ).scalars()
        )
    return {str(x) for x in a | b if x}


def _ranks_of_loaded(engine: Any, origin: str) -> dict[str, str]:
    """gbif key -> rank as the loaded gbif:rank statements state it."""
    from sqlalchemy import text

    with engine.connect() as c:
        return {
            r.subject_qid.split(":", 1)[1]: str((r.value or {}).get("value"))
            for r in c.execute(
                text("""SELECT subject_qid, value FROM kg.statement_current
                        WHERE origin = :o AND property_pid = 'gbif:rank' AND source_key = :s"""),
                {"o": origin, "s": GBIF_SOURCE_KEY},
            )
        }


# ============================================================================================== atlas protocol
def atlas_source() -> list[AtlasSourceSpec]:
    digest = code_digest()
    return [
        AtlasSourceSpec(
            name=GBIF_NAME,
            version=VERSION,
            source_key=GBIF_SOURCE_KEY,
            budget_key=GBIF_SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="GBIF Secretariat (GBIF Backbone Taxonomy via api.gbif.org and ChecklistBank dataset 53147)",
            licence=f"{GBIF_RIGHTS['licence']} ({GBIF_RIGHTS['licence_url']}); attribution: {GBIF_RIGHTS['attribution']}",
            allowed_hosts=(GBIF_HOST, CLB_HOST),
            datasets=(GBIF_DATASET,),
            dataset_ids=(f"gbif:{BACKBONE_KEY}", "doi:10.15468/39omei", f"checklistbank:{CLB_BACKBONE}"),
            cadence_seconds=90 * bc.DAY,
            planned_requests=400,
            max_response_bytes=MAX_PAGE_BYTES,
            min_interval_seconds=0.5,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={
                "entity": "ChecklistBank 53147 result id (= GBIF key) / GBIF /species key -> gbif:<key>",
                "parent": "usage.parentId / parentKey",
                "species_count": "GBIF species search facet higherTaxonKey (rank=SPECIES, status=ACCEPTED)",
            },
            notes="KG only: accepted kingdom..family usages, species of the scope groups and curated scope species, "
            "with ranks, parents, authorships and accepted-species counts.",
        ),
        AtlasSourceSpec(
            name=COL_NAME,
            version=VERSION,
            source_key=COL_SOURCE_KEY,
            budget_key=COL_SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="Catalogue of Life (ChecklistBank API, latest release 3LR)",
            licence=f"{COL_RIGHTS['licence']} ({COL_RIGHTS['licence_url']}); attribution: {COL_RIGHTS['attribution']}",
            allowed_hosts=(CLB_HOST,),
            datasets=(COL_DATASET,),
            dataset_ids=("checklistbank:3LR",),
            cadence_seconds=30 * bc.DAY,
            planned_requests=20,
            max_response_bytes=MAX_PAGE_BYTES,
            min_interval_seconds=0.5,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={
                "release": "dataset/3LR + import metrics",
                "taxa": "search rank in domain..class",
                "species_count": "search facet TAXON_ID (rank=species, status=accepted)",
            },
            notes="KG only: COL release metrics and top-rank taxa with species counts.",
        ),
        AtlasSourceSpec(
            name=WD_NAME,
            version=VERSION,
            source_key=WD_SOURCE_KEY,
            budget_key=WD_SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="Wikidata (WDQS), community-edited; P846 GBIF key, P685 NCBI taxon id, P10585 COL id, P1843",
            licence="CC0-1.0 (https://www.wikidata.org/wiki/Wikidata:Licensing)",
            allowed_hosts=(WD_HOST,),
            datasets=(WD_DATASET,),
            dataset_ids=("wikidata:P846", "wikidata:P685", "wikidata:P10585", "wikidata:P1843"),
            cadence_seconds=30 * bc.DAY,
            planned_requests=160,
            max_response_bytes=MAX_WD_BYTES,
            min_interval_seconds=2.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={
                "bridge": "one item holding P846 and P685 / P10585 (unique both ways)",
                "common_name": "wdt:P1843 fr/en",
            },
            notes="KG only: identifier bridges and common names for loaded gbif:/ncbitaxon:/col: taxa.",
        ),
    ]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == GBIF_NAME:
        return await ingest_gbif(run)
    if run.spec.name == COL_NAME:
        return await ingest_col(run)
    return await ingest_wikidata(run)
