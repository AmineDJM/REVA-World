"""GBIF occurrence-index facets (agent V4ECO): where organisms are recorded, per country, taxon group and year.

Source
------
The Global Biodiversity Information Facility (GBIF) occurrence index, read **only through the provider's own
aggregations** (no occurrence download, no record-level data):

* ``graphql.gbif.org`` (GBIF GraphQL API, POST): ``occurrenceSearch(predicate)`` with a ``countryCode`` facet;
  each facet bucket carries its record count and a nested ``cardinality { speciesKey }`` (distinct GBIF backbone
  species keys among the bucket's records);
* ``api.gbif.org/v1/species/{key}`` (backbone name usage: scientific name, rank) and
  ``/v1/species/{key}/iucnRedListCategory`` (the IUCN category GBIF attaches to a species from the checklist
  "The IUCN Red List of Threatened Species", GBIF dataset 19491596-35ae-4a91-9a98-85cf505f1bd3, licence stated
  there: CC BY 4.0).

Filter (declared, identical for every query, :data:`BASE_PREDICATES`): ``occurrenceStatus = PRESENT`` (absence
records excluded) and ``basisOfRecord`` not in ``FOSSIL_SPECIMEN`` / ``LIVING_SPECIMEN`` (fossils and zoo/garden
specimens excluded). Nothing else is filtered: GBIF's interpretation (countryCode, backbone classification, issue
flags) is used as published.

What the numbers mean (and do not mean)
---------------------------------------
* ``occurrences.*`` = number of occurrence records in the index at the snapshot date. Records document that an
  organism was recorded somewhere; their number reflects **sampling effort and data mobilisation** (eBird alone
  dominates birds), never abundance, population size or density. Every cell says so (``flags.caveat``).
* ``distinct_species.*`` = distinct ``speciesKey`` values among those records, computed by GBIF's search engine
  with an **approximate** cardinality aggregation (Elasticsearch HyperLogLog++; exact below a few thousand,
  typically within a few percent above). Flagged ``approximate: true``. Records identified only above species
  rank do not count. This is not a species checklist of the country.
* ``country_iucn``: the category is the species' **global** IUCN Red List category (not a national red list) as
  indexed by GBIF; "distinct CR species with records in X" includes vagrants and introduced organisms.
* Taxon groups are GBIF backbone kingdoms / phyla / classes selected by key (:data:`GROUPS`). Ray-finned fishes
  have no class in the backbone used by the index (``classKey`` is empty for them), so no fish class group exists:
  they are counted in Animalia and Chordata only (documented, never patched).
* Geography: GBIF ``countryCode`` (ISO 3166-1 alpha-2 plus ``XK`` Kosovo, ``XZ`` international waters, ``ZZ``
  unknown). Records without a country are in the global totals only (coverage note), never assigned.
* Period: snapshot cells cover the retrieval day ``[d, d+1)``; ``country_year`` cells cover the calendar year of
  the record's event date (values as of the snapshot day, ``flags.snapshot``).

Licences: occurrence records carry their own licences (CC0 1.0, CC BY 4.0, CC BY-NC 4.0); the counts here are
facts aggregated by GBIF's index (GBIF data user agreement: cite GBIF.org). Cells are flagged
``licence_basis: aggregate_counts_over_mixed_record_licences``; no record content is stored.

KG (``reva.kg.model.load_batch``, source key ``gbif.occurrence``): seed taxa (:data:`SEEDS`, GBIF backbone keys)
get ``gbif_occ:recorded_in`` -> country (only countries with >= :data:`MIN_RECORDS_FOR_EDGE` records),
``gbif:iucn_red_list_category`` and ``gbif_occ:measured_by`` -> ``atlas:gbif_occ.<dataset>`` statements; taxon
entities are written only when the KG has none (V4BIO owns taxa, see ``eco_common``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors.eco_common import canon, country_node, guarded_load
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.gbif_occ"
CONNECTOR_VERSION = "1.0.1"  # 1.0.0 was registered from an in-flight snapshot with different code
PARSER_VERSION = "atlas-gbif-occ-1"
SOURCE_KEY = "gbif.occurrence"
ATLAS_SOURCE = "gbif_occ"
GRAPHQL_HOST = "graphql.gbif.org"
API_HOST = "api.gbif.org"
GRAPHQL_URL = f"https://{GRAPHQL_HOST}/graphql"
SPECIES_URL = f"https://{API_HOST}/v1/species"
DS_GROUP, DS_YEAR, DS_IUCN, DS_TAXON = "country_group", "country_year", "country_iucn", "taxon_country"
DATASETS = (DS_GROUP, DS_YEAR, DS_IUCN, DS_TAXON)
FACET_SIZE = 300  # > number of GBIF country codes (252 at 2026-10-17); a full bucket list means truncation
MAX_GRAPHQL_BYTES = 2_000_000
MAX_SPECIES_BYTES = 200_000
MIN_RECORDS_FOR_EDGE = 10
YEARS = tuple(range(1970, 2026))
IUCN_CODES = ("EX", "EW", "CR", "EN", "VU", "NT", "LC", "DD", "CD")  # values present in GBIF's index facet
IUCN_DATASET_KEY = "19491596-35ae-4a91-9a98-85cf505f1bd3"

RIGHTS: dict[str, str] = {
    "licence": "GBIF data user agreement; aggregate counts over records licensed CC0 1.0 / CC BY 4.0 / CC BY-NC 4.0",
    "licence_url": "https://www.gbif.org/terms",
    "attribution": "GBIF.org, GBIF Occurrence Search (GraphQL facets), accessed <retrieval date>",
    "iucn_category_licence": f"CC BY 4.0 as stated on GBIF dataset {IUCN_DATASET_KEY} (IUCN Red List checklist)",
    "non_commercial_note": "part of the counted records are CC BY-NC 4.0; counts are facts, no record content is stored",
}
CAVEAT = "occurrence records reflect sampling effort and data mobilisation, not abundance or population size"

BASE_PREDICATES: tuple[dict[str, Any], ...] = (
    {"type": "equals", "key": "occurrenceStatus", "value": "PRESENT"},
    {
        "type": "not",
        "predicate": {"type": "in", "key": "basisOfRecord", "values": ["FOSSIL_SPECIMEN", "LIVING_SPECIMEN"]},
    },
)
FILTER_TEXT = "occurrenceStatus=PRESENT; basisOfRecord not in (FOSSIL_SPECIMEN, LIVING_SPECIMEN)"

COUNTRY_QUERY = (
    "query country_facet($predicate: Predicate) { occurrenceSearch(predicate: $predicate) { "
    "documents(size: 0) { total } cardinality { speciesKey } "
    f"facet {{ countryCode(size: {FACET_SIZE}) {{ key count occurrences {{ cardinality {{ speciesKey }} }} }} }} }} }}"
)
TAXON_QUERY = (
    "query taxon_country($predicate: Predicate) { occurrenceSearch(predicate: $predicate) { "
    f"documents(size: 0) {{ total }} facet {{ countryCode(size: {FACET_SIZE}) {{ key count }} }} }} }}"
)


@dataclass(frozen=True)
class Group:
    slug: str
    rank_key: str | None  # GraphQL predicate key: kingdomKey / phylumKey / classKey; None = all records
    key: int | None


GROUPS: tuple[Group, ...] = (
    Group("all", None, None),
    Group("animalia", "kingdomKey", 1),
    Group("archaea", "kingdomKey", 2),
    Group("bacteria", "kingdomKey", 3),
    Group("chromista", "kingdomKey", 4),
    Group("fungi", "kingdomKey", 5),
    Group("plantae", "kingdomKey", 6),
    Group("protozoa", "kingdomKey", 7),
    Group("viruses", "kingdomKey", 8),
    Group("incertae_sedis", "kingdomKey", 0),
    Group("chordata", "phylumKey", 44),
    Group("arthropoda", "phylumKey", 54),
    Group("mollusca", "phylumKey", 52),
    Group("tracheophyta", "phylumKey", 7707728),
    Group("mammalia", "classKey", 359),
    Group("aves", "classKey", 212),
    Group("amphibia", "classKey", 131),
    Group("squamata", "classKey", 11592253),
    Group("testudines", "classKey", 11418114),
    Group("crocodylia", "classKey", 11493978),
    Group("elasmobranchii", "classKey", 121),
    Group("insecta", "classKey", 216),
    Group("arachnida", "classKey", 367),
    Group("malacostraca", "classKey", 229),
    Group("gastropoda", "classKey", 225),
    Group("bivalvia", "classKey", 137),
    Group("anthozoa", "classKey", 206),
    Group("magnoliopsida", "classKey", 220),
    Group("liliopsida", "classKey", 196),
    Group("pinopsida", "classKey", 194),
    Group("polypodiopsida", "classKey", 7228684),
    Group("bryopsida", "classKey", 327),
    Group("agaricomycetes", "classKey", 186),
    Group("lecanoromycetes", "classKey", 180),
)
GROUPS_BY_SLUG = {g.slug: g for g in GROUPS}

#: Seed taxa (GBIF backbone species keys, looked up with GBIF's own /species/match when this list was written):
#: vertebrates, pollinators, crops, pests, vectors and pathogens shared with the GloBI interaction layer.
SEEDS: tuple[int, ...] = (
    5219404, 5219416, 5219436, 5219173, 2433433, 2433451, 5219243, 2435350, 2440483, 2440735,  # mammals
    2439261, 7705930, 2440958, 2440012, 2433399, 2441022, 2441110,
    2480446, 2481047, 2497921, 2481912, 5231190, 9326020,  # birds
    8084280, 8351946, 7595433, 2373980, 2420694,  # fishes
    2427091, 2441341,  # bullfrog, Nile crocodile
    1341976, 1340503, 5133088, 1650518, 1651891, 1311477,  # insects
    5290052, 2930262, 2895345, 3152205, 3001244,  # crops
    10540993, 2627503, 3203716, 8295448,  # pathogens
)  # fmt: skip

#: Fixture runs replay an excerpt (tests/fixtures/source/atlas.gbif_occ): same code path, fewer queries.
FIXTURE_GROUPS = ("all", "mammalia")
FIXTURE_YEARS = (2020,)
FIXTURE_IUCN = ("CR",)
FIXTURE_SEEDS = (5219404, 1341976)


class GbifParseError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


# ----------------------------------------------------------------------------- query plan
def predicate(*extra: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "and", "predicates": [*BASE_PREDICATES, *extra]}


def group_predicate(g: Group) -> dict[str, Any]:
    if g.rank_key is None:
        return predicate()
    return predicate({"type": "equals", "key": g.rank_key, "value": str(g.key)})


def year_predicate(y: int) -> dict[str, Any]:
    return predicate({"type": "equals", "key": "year", "value": str(y)})


def iucn_predicate(code: str) -> dict[str, Any]:
    return predicate({"type": "equals", "key": "iucnRedListCategory", "value": code})


def taxon_predicate(key: int) -> dict[str, Any]:
    return predicate({"type": "equals", "key": "taxonKey", "value": str(key)})


def graphql_body(query: str, pred: Mapping[str, Any]) -> dict[str, Any]:
    return {"query": query, "variables": {"predicate": dict(pred)}}


@dataclass(frozen=True)
class Plan:
    groups: tuple[Group, ...]
    years: tuple[int, ...]
    iucn: tuple[str, ...]
    seeds: tuple[int, ...]


def plan_for(origin: str) -> Plan:
    if origin == "test_fixture":
        return Plan(tuple(GROUPS_BY_SLUG[s] for s in FIXTURE_GROUPS), FIXTURE_YEARS, FIXTURE_IUCN, FIXTURE_SEEDS)
    return Plan(GROUPS, YEARS, IUCN_CODES, SEEDS)


def planned_requests(p: Plan | None = None) -> int:
    p = p or plan_for("live")
    groups_with_key = sum(1 for g in p.groups if g.key is not None)
    return len(p.groups) + len(p.years) + len(p.iucn) + 3 * len(p.seeds) + groups_with_key


# ----------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Bucket:
    geo: str
    count: int
    distinct_species: int | None


@dataclass(frozen=True)
class FacetResult:
    total: int
    distinct_species: int | None
    buckets: tuple[Bucket, ...]


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise GbifParseError(f"{what} is not a non-negative integer ({v!r})")
    return v


def parse_facet(body: bytes, *, with_cardinality: bool) -> FacetResult:
    """Parse one GraphQL ``occurrenceSearch`` facet response; GraphQL errors and truncated facets block."""
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise GbifParseError("GraphQL response is not JSON") from e
    if not isinstance(doc, dict):
        raise GbifParseError("GraphQL response is not an object")
    if doc.get("errors"):
        raise GbifParseError(f"GraphQL errors: {json.dumps(doc['errors'])[:300]}", kind="provider_error")
    try:
        s = doc["data"]["occurrenceSearch"]
        total = _int(s["documents"]["total"], "documents.total")
        facet = s["facet"]["countryCode"]
    except (KeyError, TypeError) as e:
        raise GbifParseError("GraphQL response lacks data.occurrenceSearch.{documents,facet.countryCode}") from e
    card = None
    if with_cardinality:
        try:
            card = _int(s["cardinality"]["speciesKey"], "cardinality.speciesKey")
        except (KeyError, TypeError) as e:
            raise GbifParseError("GraphQL response lacks cardinality.speciesKey") from e
    if not isinstance(facet, list):
        raise GbifParseError("facet.countryCode is not a list")
    if len(facet) >= FACET_SIZE:
        raise GbifParseError(f"countryCode facet holds {len(facet)} buckets (= size {FACET_SIZE}): truncated")
    out: list[Bucket] = []
    seen: set[str] = set()
    for b in facet:
        if not isinstance(b, dict):
            raise GbifParseError("facet bucket is not an object")
        key = b.get("key")
        if not isinstance(key, str) or not key or len(key) > 8:
            raise GbifParseError(f"facet bucket key {key!r} is not a country code")
        if key in seen:
            raise GbifParseError(f"duplicate facet bucket {key}")
        seen.add(key)
        n = _int(b.get("count"), f"count of {key}")
        dc = None
        if with_cardinality:
            try:
                dc = _int(b["occurrences"]["cardinality"]["speciesKey"], f"cardinality of {key}")
            except (KeyError, TypeError) as e:
                raise GbifParseError(f"bucket {key} lacks occurrences.cardinality.speciesKey") from e
        out.append(Bucket(key, n, dc))
    if sum(b.count for b in out) > total:
        raise GbifParseError("facet counts exceed the documents total")
    return FacetResult(total, card, tuple(out))


@dataclass(frozen=True)
class Taxon:
    key: int
    scientific_name: str
    canonical_name: str | None
    rank: str | None
    status: str | None
    kingdom: str | None
    vernacular_name: str | None = None


def parse_species(body: bytes, key: int) -> Taxon:
    try:
        d = json.loads(body)
    except ValueError as e:
        raise GbifParseError(f"species {key}: not JSON") from e
    if not isinstance(d, dict) or d.get("key") != key or not isinstance(d.get("scientificName"), str):
        raise GbifParseError(f"species {key}: unexpected record (key {d.get('key') if isinstance(d, dict) else None})")
    return Taxon(
        key=key,
        scientific_name=d["scientificName"],
        canonical_name=d.get("canonicalName"),
        rank=d.get("rank"),
        status=d.get("taxonomicStatus"),
        kingdom=d.get("kingdom"),
        vernacular_name=d.get("vernacularName") if isinstance(d.get("vernacularName"), str) else None,
    )


@dataclass(frozen=True)
class IucnCategory:
    code: str
    category: str | None
    iucn_taxon_id: str | None
    usage_key: int | None


def parse_iucn(body: bytes, key: int) -> IucnCategory | None:
    """``None`` when GBIF has no category for the species (empty body / no ``code``): Missing, never a guess."""
    if not body.strip():
        return None
    try:
        d = json.loads(body)
    except ValueError as e:
        raise GbifParseError(f"iucnRedListCategory {key}: not JSON") from e
    if not isinstance(d, dict) or not d.get("code"):
        return None
    code = str(d["code"])
    if code not in (*IUCN_CODES, "NE"):
        raise GbifParseError(f"iucnRedListCategory {key}: unknown code {code!r}")
    tid = d.get("iucnTaxonID")
    return IucnCategory(code, d.get("category"), None if tid is None else str(tid), d.get("usageKey"))


# ----------------------------------------------------------------------------- atlas rows
def _row(
    dataset: str,
    indicator: str,
    concept: str | None,
    geo: str,
    period: tuple[date, date],
    value: int,
    unit: str,
    raw_sha256: str,
    selector: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source": ATLAS_SOURCE,
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": concept,
        "iso3": None,
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": period[0],
        "period_end": period[1],
        "value": str(value),
        "value_text": None,
        "unit": unit,
        "flags": canon(flags),
        "raw_sha256": raw_sha256,
        "selector": canon(selector),
    }


def facet_rows(
    dataset: str,
    suffix: str,
    res: FacetResult,
    *,
    period: tuple[date, date],
    snapshot: date,
    raw_sha256: str,
    query_filter: str,
    with_cardinality: bool = True,
) -> list[dict[str, Any]]:
    """Two indicators per country bucket: ``occurrences.<suffix>`` and ``distinct_species.<suffix>``."""
    base = {
        "aggregation": "count",
        "filter": f"{FILTER_TEXT}; {query_filter}",
        "snapshot": snapshot.isoformat(),
        "caveat": CAVEAT,
        "licence_basis": "aggregate_counts_over_mixed_record_licences",
    }
    rows: list[dict[str, Any]] = []
    for b in res.buckets:
        sel = {"api": "graphql occurrenceSearch", "facet": "countryCode", "bucket": b.geo, "filter": query_filter}
        rows.append(
            _row(
                dataset,
                f"occurrences.{suffix}",
                f"ecology.gbif.{dataset}.occurrences.{suffix}",
                b.geo,
                period,
                b.count,
                "occurrence records",
                raw_sha256,
                {**sel, "field": "count"},
                {**base, "approximate": False},
            )
        )
        if with_cardinality and b.distinct_species is not None:
            rows.append(
                _row(
                    dataset,
                    f"distinct_species.{suffix}",
                    f"ecology.gbif.{dataset}.distinct_species.{suffix}",
                    b.geo,
                    period,
                    b.distinct_species,
                    "species (distinct GBIF speciesKey)",
                    raw_sha256,
                    {**sel, "field": "occurrences.cardinality.speciesKey"},
                    {
                        **base,
                        "aggregation": "distinct_count",
                        "approximate": True,
                        "method": "Elasticsearch cardinality aggregation (HyperLogLog++) run by GBIF",
                    },
                )
            )
    return rows


def snapshot_period(d: date) -> tuple[date, date]:
    return (d, d + timedelta(days=1))


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


# ----------------------------------------------------------------------------- KG
def series_entity(dataset: str) -> Any:
    from reva.kg.model import KgEntity

    titles = {
        DS_GROUP: ("GBIF occurrence records and distinct species per country and taxon group",
                   "Observations GBIF et espèces distinctes par pays et groupe taxonomique"),
        DS_TAXON: ("GBIF occurrence records per country for selected taxa",
                   "Observations GBIF par pays pour des taxons choisis"),
    }  # fmt: skip
    en, fr = titles[dataset]
    return KgEntity(
        qid=f"atlas:{ATLAS_SOURCE}.{dataset}",
        kind="atlas_dataset",
        labels={"en": en, "fr": fr},
        descriptions={
            "en": f"REVA atlas table {ATLAS_SOURCE}.{dataset} (GBIF GraphQL facets; {FILTER_TEXT})",
            "fr": f"Table de l'atlas REVA {ATLAS_SOURCE}.{dataset} (facettes GraphQL GBIF ; {FILTER_TEXT})",
        },
    )


def taxon_entity(t: Taxon) -> Any:
    from reva.kg.model import KgEntity

    label = t.canonical_name or t.scientific_name
    rank = (t.rank or "taxon").lower()
    return KgEntity(
        qid=f"gbif:{t.key}",
        kind="taxon",
        labels={"en": label},
        aliases={"en": [t.vernacular_name]} if t.vernacular_name else {},
        descriptions={
            "en": f"{rank} in the GBIF Backbone Taxonomy: {t.scientific_name}",
            "fr": f"{rank} dans la taxonomie de référence GBIF : {t.scientific_name}",
        },
    )


def kg_parts(
    *,
    groups: Sequence[tuple[Group, Taxon]],
    seeds: Sequence[tuple[Taxon, IucnCategory | None, FacetResult | None]],
    raw_by_key: Mapping[str, str],
    snapshot: date,
    country_qids: Mapping[str, str],
    iso3_of: Any = None,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """Entities and statements for ``load_batch`` (source key :data:`SOURCE_KEY`).

    ``raw_by_key`` maps ``species:<key>``, ``iucn:<key>``, ``taxon:<key>`` and ``group:<slug>`` to the raw sha256
    each statement is parsed from."""
    from reva.kg.model import KgStatement, KgTime

    if iso3_of is None:
        from reva.atlas.tables import country_iso3

        def iso3_of(c: str) -> str | None:
            return country_iso3(c, "iso2")

    rev = snapshot.isoformat()
    pit = KgTime(rev, 11)
    ents: list[Any] = []
    sts: list[Any] = []
    rep: dict[str, Any] = {"recorded_in": 0, "below_threshold": 0, "unmapped_geo": [], "iucn": 0, "measured_by": 0}

    def ref(url: str, what: str) -> list[dict[str, Any]]:
        return [{"stated_in": "GBIF", "P854": url, "P813": rev, "selector": {"what": what}}]

    if groups:
        ents.append(series_entity(DS_GROUP))
    for g, t in groups:
        ents.append(taxon_entity(t))
        sts.append(
            KgStatement(
                subject_qid=f"gbif:{t.key}",
                property_pid="gbif_occ:measured_by",
                object_qid=f"atlas:{ATLAS_SOURCE}.{DS_GROUP}",
                qualifiers={
                    "indicators": [f"occurrences.{g.slug}", f"distinct_species.{g.slug}"],
                    "group_predicate": f"{g.rank_key}={g.key}",
                },
                references=ref(GRAPHQL_URL, f"countryCode facet with {g.rank_key}={g.key}"),
                statement_id=f"measured_by:{DS_GROUP}:{g.slug}",
                source_revision=rev,
                raw_sha256=raw_by_key[f"group:{g.slug}"],
            )
        )
        rep["measured_by"] += 1
    if seeds:
        ents.append(series_entity(DS_TAXON))
    for t, iucn, facet in seeds:
        node = f"gbif:{t.key}"
        ents.append(taxon_entity(t))
        if iucn is not None:
            sts.append(
                KgStatement(
                    subject_qid=node,
                    property_pid="gbif:iucn_red_list_category",
                    value={"type": "string", "value": iucn.code},
                    qualifiers={
                        "category": iucn.category,
                        "iucn_taxon_id": iucn.iucn_taxon_id,
                        "checklist_dataset": IUCN_DATASET_KEY,
                    },
                    point_in_time=pit,
                    references=ref(f"{SPECIES_URL}/{t.key}/iucnRedListCategory", "code"),
                    statement_id=f"iucn:{t.key}",
                    source_revision=rev,
                    raw_sha256=raw_by_key[f"iucn:{t.key}"],
                )
            )
            rep["iucn"] += 1
        if facet is None:
            continue
        sts.append(
            KgStatement(
                subject_qid=node,
                property_pid="gbif_occ:measured_by",
                object_qid=f"atlas:{ATLAS_SOURCE}.{DS_TAXON}",
                qualifiers={"indicators": [f"occurrences.gbif_{t.key}"]},
                references=ref(GRAPHQL_URL, f"countryCode facet with taxonKey={t.key}"),
                statement_id=f"measured_by:{DS_TAXON}:{t.key}",
                source_revision=rev,
                raw_sha256=raw_by_key[f"taxon:{t.key}"],
            )
        )
        rep["measured_by"] += 1
        for b in facet.buckets:
            if b.count < MIN_RECORDS_FOR_EDGE:
                rep["below_threshold"] += 1
                continue
            iso3 = iso3_of(b.geo)
            if iso3 is None:
                rep["unmapped_geo"].append(b.geo)
                continue
            sts.append(
                KgStatement(
                    subject_qid=node,
                    property_pid="gbif_occ:recorded_in",
                    object_qid=country_node(iso3, country_qids),
                    qualifiers={
                        "occurrences": b.count,
                        "gbif_country_code": b.geo,
                        "filter": FILTER_TEXT,
                        "min_records_for_edge": MIN_RECORDS_FOR_EDGE,
                    },
                    point_in_time=pit,
                    references=ref(GRAPHQL_URL, f"countryCode facet with taxonKey={t.key}, bucket {b.geo}"),
                    statement_id=f"recorded_in:{t.key}:{b.geo}",
                    source_revision=rev,
                    raw_sha256=raw_by_key[f"taxon:{t.key}"],
                )
            )
            rep["recorded_in"] += 1
    rep["unmapped_geo"] = sorted(set(rep["unmapped_geo"]))
    return ents, sts, rep


# ----------------------------------------------------------------------------- atlas source protocol
def atlas_source() -> Any:
    from reva.adapters.connectors.base import module_digest
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Global Biodiversity Information Facility (GBIF), occurrence index (GraphQL and species APIs)",
        licence=RIGHTS["licence"],
        allowed_hosts=(GRAPHQL_HOST, API_HOST),
        datasets=DATASETS,
        dataset_ids=("occurrenceSearch:countryCode", "species", "species/iucnRedListCategory"),
        cadence_seconds=30 * 86400,
        planned_requests=planned_requests(),
        max_response_bytes=MAX_GRAPHQL_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(atlas_source),
        field_mappings={
            "atlas_tables": [f"{ATLAS_SOURCE}.{d}" for d in DATASETS],
            "geo": "countryCode (ISO 3166-1 alpha-2 + XK/XZ/ZZ)",
            "kg": ["gbif_occ:recorded_in", "gbif_occ:measured_by", "gbif:iucn_red_list_category"],
        },
        notes="GraphQL countryCode facets with nested speciesKey cardinality; no occurrence download.",
    )


async def _graphql(run: Any, key: str, query: str, pred: Mapping[str, Any]) -> Any:
    return await run.post(GRAPHQL_URL, key=key, json_body=graphql_body(query, pred), max_bytes=MAX_GRAPHQL_BYTES)


async def _species(run: Any, key: int) -> tuple[Any, Taxon]:
    raw = await run.get(f"{SPECIES_URL}/{key}", key=f"species:{key}", max_bytes=MAX_SPECIES_BYTES)
    return raw, parse_species(raw.body, key)


async def atlas_ingest(run: Any, *, load_kg: bool = True) -> dict[str, Any]:
    """Run the facet plan, archive every response, register the wanted tables, load the KG statements."""
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    plan = plan_for(run.origin)
    out: dict[str, Any] = {"plan": {"groups": len(plan.groups), "years": len(plan.years), "iucn": len(plan.iucn),
                                    "seeds": len(plan.seeds)}, "atlas": {}, "global_totals": {}}  # fmt: skip
    raw_by_key: dict[str, str] = {}
    group_taxa: list[tuple[Group, Taxon]] = []
    seeds: list[tuple[Taxon, IucnCategory | None, FacetResult | None]] = []
    retrieved: list[datetime] = []

    def reg(dataset: str, rows: list[dict[str, Any]], shas: list[str], note: str) -> None:
        r = run.register(
            dataset=dataset,
            frame=atlas_frame(rows),
            raw_sha256s=shas,
            geo_scheme="iso2",
            rights=dict(RIGHTS),
            coverage_note=note,
        )
        out["atlas"][dataset] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}

    if run.wants(DS_GROUP):
        rows, shas = [], []
        for g in plan.groups:
            raw = await _graphql(run, f"group:{g.slug}", COUNTRY_QUERY, group_predicate(g))
            res = parse_facet(raw.body, with_cardinality=True)
            sha = run.archive(raw)
            shas.append(sha)
            raw_by_key[f"group:{g.slug}"] = sha
            snap = raw.retrieved_at.date()
            retrieved.append(raw.retrieved_at)
            filt = "all taxa" if g.key is None else f"{g.rank_key}={g.key}"
            rows += facet_rows(DS_GROUP, g.slug, res, period=snapshot_period(snap), snapshot=snap,
                               raw_sha256=sha, query_filter=filt)  # fmt: skip
            out["global_totals"][g.slug] = {"occurrences": res.total, "distinct_species": res.distinct_species,
                                            "in_country_buckets": sum(b.count for b in res.buckets)}  # fmt: skip
            if g.key is not None:
                sraw, taxon = await _species(run, g.key)
                raw_by_key[f"species:{g.key}"] = run.archive(sraw)
                shas.append(raw_by_key[f"species:{g.key}"])
                group_taxa.append((g, taxon))
        reg(DS_GROUP, rows, shas, "GBIF occurrence index snapshot: records and distinct species (approximate) per "
            f"country and taxon group ({len(plan.groups)} groups); {FILTER_TEXT}; {CAVEAT}")  # fmt: skip

    if run.wants(DS_YEAR):
        rows, shas = [], []
        for y in plan.years:
            raw = await _graphql(run, f"year:{y}", COUNTRY_QUERY, year_predicate(y))
            res = parse_facet(raw.body, with_cardinality=True)
            sha = run.archive(raw)
            shas.append(sha)
            retrieved.append(raw.retrieved_at)
            rows += facet_rows(DS_YEAR, "all", res, period=(date(y, 1, 1), date(y + 1, 1, 1)),
                               snapshot=raw.retrieved_at.date(), raw_sha256=sha, query_filter=f"year={y}")  # fmt: skip
        reg(DS_YEAR, rows, shas, f"GBIF occurrence records and distinct species per country and event year "
            f"{plan.years[0]}-{plan.years[-1]} (values as of the snapshot day); {FILTER_TEXT}; {CAVEAT}")  # fmt: skip

    if run.wants(DS_IUCN):
        rows, shas = [], []
        for code in plan.iucn:
            raw = await _graphql(run, f"iucn:{code}", COUNTRY_QUERY, iucn_predicate(code))
            res = parse_facet(raw.body, with_cardinality=True)
            sha = run.archive(raw)
            shas.append(sha)
            retrieved.append(raw.retrieved_at)
            snap = raw.retrieved_at.date()
            rows += facet_rows(DS_IUCN, code.lower(), res, period=snapshot_period(snap), snapshot=snap,
                               raw_sha256=sha, query_filter=f"iucnRedListCategory={code}")  # fmt: skip
        reg(DS_IUCN, rows, shas, "GBIF occurrence records and distinct species per country by the species' global "
            f"IUCN Red List category as indexed by GBIF (checklist {IUCN_DATASET_KEY}, CC BY 4.0); {FILTER_TEXT}; "
            "not a national red list")  # fmt: skip

    if run.wants(DS_TAXON):
        rows, shas = [], []
        for key in plan.seeds:
            sraw, taxon = await _species(run, key)
            raw_by_key[f"species:{key}"] = run.archive(sraw)
            iraw = await run.get(f"{SPECIES_URL}/{key}/iucnRedListCategory", key=f"iucn:{key}",
                                 max_bytes=MAX_SPECIES_BYTES)  # fmt: skip
            iucn = parse_iucn(iraw.body, key)
            raw_by_key[f"iucn:{key}"] = run.archive(iraw)
            raw = await _graphql(run, f"taxon:{key}", TAXON_QUERY, taxon_predicate(key))
            res = parse_facet(raw.body, with_cardinality=False)
            sha = run.archive(raw)
            raw_by_key[f"taxon:{key}"] = sha
            shas += [raw_by_key[f"species:{key}"], raw_by_key[f"iucn:{key}"], sha]
            retrieved.append(raw.retrieved_at)
            snap = raw.retrieved_at.date()
            rows += facet_rows(DS_TAXON, f"gbif_{key}", res, period=snapshot_period(snap), snapshot=snap,
                               raw_sha256=sha, query_filter=f"taxonKey={key}", with_cardinality=False)  # fmt: skip
            seeds.append((taxon, iucn, res))
        reg(DS_TAXON, rows, shas, f"GBIF occurrence records per country for {len(plan.seeds)} selected taxa "
            f"(GBIF backbone keys, descendants included); {FILTER_TEXT}; {CAVEAT}")  # fmt: skip

    if load_kg and (group_taxa or seeds):
        ents, sts, rep = kg_parts(
            groups=group_taxa,
            seeds=seeds,
            raw_by_key=raw_by_key,
            snapshot=max(retrieved).date(),
            country_qids=load_country_qids(),
        )
        out["kg_build"] = rep
        out["kg"] = guarded_load(
            run.engine,
            run.store,
            pids=("gbif_occ:recorded_in", "gbif_occ:measured_by", "gbif:iucn_red_list_category"),
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            raw_sha256s=sorted(set(raw_by_key.values())),
            retrieved_at=max(retrieved),
            origin=run.origin,
            parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION},
        )
    return out


__all__ = [
    "ATLAS_SOURCE",
    "BASE_PREDICATES",
    "CONNECTOR_NAME",
    "DATASETS",
    "GROUPS",
    "SEEDS",
    "atlas_ingest",
    "atlas_source",
    "facet_rows",
    "kg_parts",
    "parse_facet",
    "parse_iucn",
    "parse_species",
    "plan_for",
]
