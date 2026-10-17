"""Psychology, sociology and behavioural science as sciences: the OpenAlex research taxonomy (KG) and research
works per country for the psychology / social-science subfields (atlas) (V4, agent V4MIND).

Source: OpenAlex (OurResearch), data **CC0** ("all OpenAlex data is CC0", https://openalex.org/about).

1. ``atlas.openalex_taxonomy`` -- the OpenAlex *snapshot* on the public S3 bucket
   (``https://openalex.s3.amazonaws.com/data/jsonl/<entity>/manifest.json`` and the gzip JSON-lines files the
   manifest lists; no API key, not metered by the API's daily budget). Entities ``domains`` (4), ``fields`` (26),
   ``subfields`` (~252) and ``topics`` (~4,500) become KG entities ``openalex:domain.<id>``,
   ``openalex:field.<id>``, ``openalex:subfield.<id>``, ``openalex:<T-id>`` (kinds ``research_domain`` ...
   ``research_topic``), each with the OpenAlex description verbatim, linked by ``openalex:part_of`` following the
   ids OpenAlex publishes on each record (topic -> subfield -> field -> domain). Wikidata QIDs that OpenAlex gives
   (``ids.wikidata`` on domains, fields, subfields) are kept as ``openalex:wikidata_id`` identifier statements (an
   identifier the source publishes, for V4LINK's ``same_as`` work -- never a merge here); topic Wikipedia URLs as
   ``openalex:wikipedia_url``; topic keywords (with OpenAlex's score) as ``openalex:keyword``; ``works_count`` and
   ``cited_by_count`` as quantities at the snapshot date (live index: counts change as records are added or
   re-attributed). The whole taxonomy is loaded (small, identifier-linked); psychology is field 32, the social
   sciences field 33 (sociology = subfield 3312), economics field 20.
   KG-only source: its declared dataset ``taxonomy`` registers no atlas table.

2. ``atlas.openalex_mind`` -- works per affiliation country and year for the psychology subfields (3200-3207) and
   sociology (3312), through A-SCI's group_by machinery (:mod:`reva.adapters.connectors.atlas_openalex`:
   ``/works?filter=publication_year:Y,type:article|review,primary_topic.subfield.id:S&group_by=authorships.countries``,
   full counting, complement queries for the countries beyond the 200 largest groups, ``_all_works`` denominator
   row). The OpenAlex API meters requests without a key on a budget shared by everyone behind the same IP address;
   it was exhausted on 2026-10-17 (HTTP 429, "resets at midnight UTC"), so this table has no live run and no
   captured fixture yet (commands to run it are in ``docs/dev/v4_v4mind.md``). No contact address is sent.

Counts of research works say how much is published under a label, not how true or important it is; correlations
between research volume and anything else are associations only.
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from reva.adapters.connectors import atlas_mind_common as mc
from reva.adapters.connectors import atlas_openalex as oa
from reva.adapters.connectors.atlas_sci_common import atlas_frame, canon, code_digest, trim_registration
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

# ----------------------------------------------------------------------------- taxonomy (S3 snapshot)
TAX_CONNECTOR = "atlas.openalex_taxonomy"
TAX_VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB from an in-progress copy of this module
TAX_SOURCE_KEY = "openalex.snapshot"
S3_HOST = "openalex.s3.amazonaws.com"
S3_BASE = f"https://{S3_HOST}/"
TAX_DATASET = "taxonomy"
TAX_ENTITIES = ("domains", "fields", "subfields", "topics")
MAX_FILE_BYTES = 40_000_000
MAX_DECOMPRESSED = 200_000_000
KINDS = {"domains": "research_domain", "fields": "research_field", "subfields": "research_subfield",
         "topics": "research_topic"}  # fmt: skip
PARSER_VERSION = "atlas_mind_openalex/1"
RIGHTS: dict[str, str] = {**oa.RIGHTS, "agent": "V4MIND"}


class OpenAlexSnapshotError(SourceResponseError):
    pass


def _err(msg: str) -> OpenAlexSnapshotError:
    return OpenAlexSnapshotError(msg[:2000], retryable=False, kind="schema_drift")


def manifest_url(entity: str) -> str:
    return f"{S3_BASE}data/jsonl/{entity}/manifest.json"


def file_url(s3_url: str) -> str:
    """``s3://openalex/data/...`` (as the manifest lists it) -> the bucket's HTTPS URL."""
    prefix = "s3://openalex/"
    if not s3_url.startswith(prefix + "data/jsonl/") or ".." in s3_url or not s3_url.endswith(".gz"):
        raise _err(f"unexpected manifest file url {s3_url!r}")
    return S3_BASE + s3_url[len(prefix) :]


def parse_manifest(body: bytes, entity: str) -> dict[str, Any]:
    try:
        doc = json.loads(body)
        files = [(f["url"], int(f["meta"]["record_count"])) for f in doc["files"]]
        total = int(doc["record_count"])
    except (ValueError, KeyError, TypeError) as e:
        raise _err(f"{entity} manifest is not the expected JSON ({e})") from e
    if doc.get("entity") != entity or sum(n for _, n in files) != total:
        raise _err(f"{entity} manifest: entity {doc.get('entity')!r}, files {sum(n for _, n in files)} != {total}")
    return {"date": str(doc.get("date")), "total": total, "files": files}


def read_jsonl_gz(body: bytes, what: str) -> list[dict[str, Any]]:
    try:
        with gzip.GzipFile(fileobj=__import__("io").BytesIO(body)) as fh:
            data = fh.read(MAX_DECOMPRESSED + 1)
    except (OSError, EOFError) as e:
        raise _err(f"{what}: not gzip ({e})") from e
    if len(data) > MAX_DECOMPRESSED:
        raise _err(f"{what}: decompressed size above {MAX_DECOMPRESSED}")
    out = []
    for n, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise _err(f"{what} line {n}: not JSON ({e})") from e
    return out


def _tail(url: str | None, marker: str) -> str:
    """``https://openalex.org/subfields/3207`` -> ``3207``; ``https://openalex.org/T10475`` -> ``T10475``."""
    if not url or not url.startswith("https://openalex.org/"):
        raise _err(f"unexpected OpenAlex id {url!r}")
    rest = url[len("https://openalex.org/") :]
    if marker:
        if not rest.startswith(marker + "/"):
            raise _err(f"id {url!r} is not a {marker} id")
        rest = rest[len(marker) + 1 :]
    if not rest or "/" in rest:
        raise _err(f"unexpected OpenAlex id {url!r}")
    return rest


def entity_key(entity: str, url: str | None) -> str:
    if entity == "topics":
        return f"openalex:{_tail(url, '')}"
    sid = _tail(url, entity)
    return f"openalex:{entity[:-1]}.{sid}"


def _wikidata_qid(url: str | None) -> str | None:
    if not url:
        return None
    q = url.rstrip("/").rsplit("/", 1)[-1]
    return q if q.startswith("Q") and q[1:].isdigit() else None


@dataclass
class Taxonomy:
    records: dict[str, list[tuple[dict[str, Any], str, int]]]  # entity -> (record, raw sha, line)
    snapshot_date: dict[str, str]
    duplicates: dict[str, int]


def build_taxonomy(manifests: Mapping[str, bytes], files: Mapping[str, list[tuple[str, bytes, str]]]) -> Taxonomy:
    """``files[entity]`` = [(s3 url, gz body, raw sha256)] for every file the manifest lists."""
    recs: dict[str, list[tuple[dict[str, Any], str, int]]] = {}
    dates: dict[str, str] = {}
    dups: dict[str, int] = {}
    for entity in TAX_ENTITIES:
        man = parse_manifest(manifests[entity], entity)
        dates[entity] = man["date"]
        listed = {u for u, _ in man["files"]}
        got = {u for u, _, _ in files.get(entity, [])}
        if listed != got:
            raise _err(f"{entity}: fetched files differ from the manifest (missing {sorted(listed - got)[:3]})")
        best: dict[str, tuple[dict[str, Any], str, int]] = {}
        n_dup = 0
        n_all = 0
        for url, body, sha in files[entity]:
            rows = read_jsonl_gz(body, url)
            expected = dict(man["files"])[url]
            if len(rows) != expected:
                raise _err(f"{url}: {len(rows)} records, manifest says {expected}")
            for line, r in enumerate(rows, start=1):
                n_all += 1
                k = entity_key(entity, r.get("id"))
                if k in best:
                    n_dup += 1
                    if str(r.get("updated_date")) <= str(best[k][0].get("updated_date")):
                        continue
                best[k] = (r, sha, line)
        dups[entity] = n_dup
        recs[entity] = [best[k] for k in sorted(best)]
    return Taxonomy(recs, dates, dups)


def taxonomy_kg(tax: Taxonomy) -> tuple[list[Any], list[Any], dict[str, Any]]:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    stats: Counter[str] = Counter()
    keys = {e: {entity_key(e, r["id"]) for r, _, _ in tax.records[e]} for e in TAX_ENTITIES}
    parent_of = {"topics": ("subfield", "subfields"), "subfields": ("field", "fields"), "fields": ("domain", "domains")}
    for entity in TAX_ENTITIES:
        when = mc.year_time(int(tax.snapshot_date[entity][:4]))
        snap = tax.snapshot_date[entity]
        for r, sha, line in tax.records[entity]:
            k = entity_key(entity, r["id"])
            ref = {"raw_sha256": sha, "selector": {"entity": entity, "line": line, "id": r["id"]}}
            common: dict[str, Any] = {"source_key": TAX_SOURCE_KEY, "references": (ref,), "raw_sha256": sha}
            desc = (r.get("description") or "").strip()
            ents.append(KgEntity(qid=k, kind=KINDS[entity], labels={"en": str(r.get("display_name"))[:480]},
                                 descriptions={"en": desc[:480]} if desc else {}))  # fmt: skip
            stats[f"{entity}_entities"] += 1
            if entity in parent_of:
                attr, pent = parent_of[entity]
                pk = entity_key(pent, (r.get(attr) or {}).get("id"))
                if pk not in keys[pent]:
                    raise _err(f"{k}: parent {pk} not in the {pent} snapshot")
                sts.append(KgStatement(subject_qid=k, property_pid="openalex:part_of", object_qid=pk,
                                       qualifiers={"level": attr}, **common))  # fmt: skip
            ids = r.get("ids") or {}
            q = _wikidata_qid(ids.get("wikidata"))
            if q:
                sts.append(KgStatement(subject_qid=k, property_pid="openalex:wikidata_id",
                                       value={"type": "external_id", "value": q}, **common))  # fmt: skip
                stats["wikidata_ids"] += 1
            if ids.get("wikipedia"):
                sts.append(KgStatement(subject_qid=k, property_pid="openalex:wikipedia_url",
                                       value={"type": "url", "value": str(ids["wikipedia"])[:500]}, **common))  # fmt: skip
            for kw in r.get("keywords") or []:
                if kw.get("display_name"):
                    sts.append(KgStatement(subject_qid=k, property_pid="openalex:keyword",
                                           value={"type": "string", "value": str(kw["display_name"])[:300]},
                                           qualifiers={"score": str(kw.get("score")), "keyword_id": kw.get("id")},
                                           **common))  # fmt: skip
                    stats["keywords"] += 1
            for prop, field in (("openalex:works_count", "works_count"), ("openalex:cited_by_count", "cited_by_count")):
                v = r.get(field)
                if isinstance(v, int) and v >= 0:
                    sts.append(KgStatement(subject_qid=k, property_pid=prop,
                                           value={"type": "quantity", "amount": str(v), "unit": "works"
                                                  if field == "works_count" else "citations"},
                                           point_in_time=when,
                                           qualifiers={"snapshot_date": snap, "live_index": True}, **common))  # fmt: skip
    return ents, sts, dict(stats)


TAX_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "openalex:part_of": ("item", "openalex_part_of", "part of (OpenAlex taxonomy)", "fait partie de (taxonomie OpenAlex)",
                         "openalex_has_part", "has part (OpenAlex taxonomy)"),
    "openalex:wikidata_id": ("external_id", "wikidata_id_per_openalex", "Wikidata ID (as OpenAlex gives it)",
                             "identifiant Wikidata (selon OpenAlex)", None, None),
    "openalex:wikipedia_url": ("url", "wikipedia_url", "Wikipedia article (as OpenAlex gives it)",
                               "article Wikipédia (selon OpenAlex)", None, None),
    "openalex:keyword": ("string", "openalex_keyword", "keyword (OpenAlex)", "mot-clé (OpenAlex)", None, None),
    "openalex:works_count": ("quantity", "works_count", "number of works (OpenAlex)", "nombre de travaux (OpenAlex)",
                             None, None),
    "openalex:cited_by_count": ("quantity", "cited_by_count", "number of citations (OpenAlex)",
                                "nombre de citations (OpenAlex)", None, None),
}  # fmt: skip


def tax_spec() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=TAX_CONNECTOR,
        version=TAX_VERSION,
        source_key=TAX_SOURCE_KEY,
        budget_key=TAX_SOURCE_KEY,
        atlas_source="openalex",
        provider="OpenAlex (OurResearch), public snapshot on S3",
        licence="CC0-1.0",
        allowed_hosts=(S3_HOST,),
        datasets=(TAX_DATASET,),
        dataset_ids=tuple(f"data/jsonl/{e}" for e in TAX_ENTITIES),
        cadence_seconds=30 * 86400,
        planned_requests=30,
        max_response_bytes=MAX_FILE_BYTES,
        min_interval_seconds=0.5,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__]),
        field_mappings={"entities": list(TAX_ENTITIES), "kinds": KINDS, "kg_source_key": TAX_SOURCE_KEY},
        notes="KG-only source: 4 manifests + the gzip JSON-lines files they list (~7.5 MB); no atlas table.",
    )


async def _ingest_taxonomy(run: Any) -> dict[str, Any]:
    from reva.kg.model import KgProperty

    manifests: dict[str, bytes] = {}
    files: dict[str, list[tuple[str, bytes, str]]] = {}
    shas: list[str] = []
    fetched = []
    retrieved: list[datetime] = []
    for entity in TAX_ENTITIES:
        raw = await run.get(manifest_url(entity), key=f"{entity}:manifest", max_bytes=1_000_000)
        sha = run.archive(raw)
        manifests[entity] = run.store.get("raw", sha)
        shas.append(sha)
        man = parse_manifest(manifests[entity], entity)
        fetched.append({"item": f"{entity}:manifest", "bytes": len(raw.body), "sha256": sha})
        for url, _n in man["files"]:
            fr = await run.get(file_url(url), key=f"{entity}:{url.rsplit('/', 2)[-2]}", max_bytes=MAX_FILE_BYTES)
            fsha = run.archive(fr)
            files.setdefault(entity, []).append((url, run.store.get("raw", fsha), fsha))
            shas.append(fsha)
            fetched.append({"item": url, "bytes": len(fr.body), "sha256": fsha})
            if fr.retrieved_at:
                retrieved.append(fr.retrieved_at)
    tax = build_taxonomy(manifests, files)
    ents, sts, stats = taxonomy_kg(tax)
    out: dict[str, Any] = {"fetched": fetched, "snapshot_date": tax.snapshot_date, "duplicates": tax.duplicates,
                           "kg_records": stats}  # fmt: skip
    if not mc.kg_available(run.engine):
        out["kg"] = {"skipped": "no KG schema"}
        return out
    from reva.kg.model import register_properties

    register_properties(
        run.engine,
        [KgProperty(pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv,
                    inverse_label_en=ile) for p, (k, rp, le, lf, inv, ile) in TAX_PROPERTIES.items()],
    )  # fmt: skip
    out["kg"] = mc.load(run.engine, run.store, entities=ents, statements=sts, source_key=TAX_SOURCE_KEY,
                        raw_sha256s=shas, retrieved_at=max(retrieved, default=datetime.now(UTC)), origin=run.origin)  # fmt: skip
    return out


# ----------------------------------------------------------------------------- works per country (API)
MIND_CONNECTOR = "atlas.openalex_mind"
MIND_VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB from an in-progress copy of this module
MIND_DATASET = "works_country_subfield_mind"
#: OpenAlex subfield ids -> names (verbatim from the 2026-10-14 snapshot's subfields file).
SUBFIELDS: dict[str, str] = {
    "3200": "General Psychology",
    "3202": "Applied Psychology",
    "3203": "Clinical Psychology",
    "3204": "Developmental and Educational Psychology",
    "3205": "Experimental and Cognitive Psychology",
    "3206": "Neuropsychology and Physiological Psychology",
    "3207": "Social Psychology",
    "3312": "Sociology and Political Science",
}
MIND_YEARS = (2015, 2025)
FIXTURE_YEARS = (2020, 2020)


@dataclass(frozen=True)
class SubfieldQuery(oa.Query):
    """A-SCI's query with an OpenAlex subfield filter (``dim = "subfield"``)."""

    @property
    def filter(self) -> str:
        return f"publication_year:{self.year},type:{oa.WORK_TYPES},primary_topic.subfield.id:{self.dim_id}"


def mind_queries(years: tuple[int, int] = MIND_YEARS) -> list[SubfieldQuery]:
    return [SubfieldQuery(MIND_DATASET, y, "subfield", s) for y in range(years[0], years[1] + 1) for s in SUBFIELDS]


def mind_rows(q: SubfieldQuery, pages: Sequence[Any], *, retrieved_at: datetime) -> list[dict[str, Any]]:
    rows = oa.atlas_rows(q, pages, retrieved_at=retrieved_at)
    for r in rows:
        f = json.loads(r["flags"])
        f["subfield"] = SUBFIELDS.get(q.dim_id or "", str(q.dim_id))
        r["flags"] = canon(f)
        r["concept_id"] = None
    return rows


def mind_spec() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=MIND_CONNECTOR,
        version=MIND_VERSION,
        source_key=oa.SOURCE_KEY,
        budget_key=oa.SOURCE_KEY,
        atlas_source=oa.ATLAS_SOURCE,
        provider="OpenAlex (OurResearch)",
        licence="CC0-1.0",
        allowed_hosts=(oa.HOST,),
        datasets=(MIND_DATASET,),
        dataset_ids=("works group_by authorships.countries, primary_topic.subfield.id",),
        cadence_seconds=90 * 86400,
        planned_requests=300,
        max_response_bytes=oa.MAX_BYTES,
        min_interval_seconds=0.25,
        parser_version=PARSER_VERSION + "+" + oa.PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__]),
        field_mappings={"subfields": SUBFIELDS, "years": list(MIND_YEARS), "group_by": "authorships.countries"},
        notes="88 queries (11 years x 8 subfields), ~1-4 requests each without an API key; no contact address sent.",
    )


async def _ingest_mind(run: Any) -> dict[str, Any]:
    from reva.kernel.errors import RevaError

    fixture = run.origin == "test_fixture"
    plan = mind_queries(FIXTURE_YEARS if fixture else MIND_YEARS)
    rows: list[dict[str, Any]] = []
    shas: list[str] = []
    incomplete: list[dict[str, Any]] = []
    state = {"requests": 0}
    stopped: str | None = None
    for q in plan:
        if stopped:
            incomplete.append({"query": q.key, "status": "not_attempted", "reason": stopped})
            continue
        try:
            fetched = await oa.fetch_query(run, q, api_key=None, limit=run.spec.planned_requests, state=state)
        except RevaError as e:
            status = e.details.get("status")
            incomplete.append({"query": q.key, "status": "failed", "http_status": status, "error": e.message[:300]})
            if status in (429, 401, 403):
                stopped = f"provider refused (HTTP {status}); no further requests this run"
            continue
        except Exception as e:
            stopped = f"{type(e).__name__}: {str(e)[:200]}"
            incomplete.append({"query": q.key, "status": "not_attempted", "reason": stopped})
            continue
        rows.extend(mind_rows(q, fetched, retrieved_at=datetime.now(UTC)))
        shas.extend(f.sha for f in fetched)
    out: dict[str, Any] = {"requests": state["requests"], "queries": len(plan), "incomplete": incomplete}
    if rows:
        reg = run.register(
            dataset=MIND_DATASET, frame=atlas_frame(rows), raw_sha256s=sorted(set(shas)), geo_scheme=oa.GEO_SCHEME,
            coverage_note="OpenAlex group_by authorships.countries (articles+reviews, full counting) per psychology "
            "subfield (3200-3207) and sociology (3312), as indexed at retrieval; '_all_works' and 'unknown' rows "
            "kept unmapped. Queries that did not complete contribute no rows (listed in the run summary).",
            rights=RIGHTS,
        )  # fmt: skip
        out["atlas"] = trim_registration(reg)
    return out


# ----------------------------------------------------------------------------- entry points
def atlas_source() -> list[AtlasSourceSpec]:
    return [tax_spec(), mind_spec()]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == TAX_CONNECTOR:
        return await _ingest_taxonomy(run) if run.wants(TAX_DATASET) else {"skipped": True}
    return await _ingest_mind(run) if run.wants(MIND_DATASET) else {"skipped": True}


__all__ = [
    "MIND_CONNECTOR",
    "SUBFIELDS",
    "TAX_CONNECTOR",
    "SubfieldQuery",
    "atlas_ingest",
    "atlas_source",
    "build_taxonomy",
    "entity_key",
    "mind_queries",
    "mind_rows",
    "parse_manifest",
    "taxonomy_kg",
]
