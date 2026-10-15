"""Wikidata harvest for the KG (agent WDHARVEST): executes query specs from ``registry/kg/queries``.

Spec format: ``registry/kg/queries/README.md`` and ``_schema.json``. For each spec:

1. validate (JSON schema, allow-list, placeholders, no LIMIT / label service);
2. render request chunks (``split`` by year windows, QID prefix or VALUES lists) and run them through the
   locked, paced, archived :class:`~reva.adapters.connectors.wikidata.WikidataClient`; ``LIMIT max_rows+1``
   is appended and a spec whose rows exceed ``max_rows`` fails (``row_bound_exceeded``) — never truncated;
3. ``seed_entities``: fetch full snapshots of the selected QIDs with ``wbgetentities`` (50 per call), keep the
   allow-listed, non-deprecated claims; ``statements``: turn rows into statements (source ``wikidata.wdqs``)
   and fetch label snapshots of the subjects;
4. ``expand_hops: 1``: label snapshots of entities referenced as objects that are not yet known;
5. hand every batch (entities + statements + the raw artifacts they were parsed from) to a *sink*
   (``KgDbSink`` -> ``reva.kg.model.load_batch``; ``JsonlSink`` for offline/dry runs).

Resumable and idempotent: a per-spec state file ``var/kg/harvest_state/<spec id>.json`` records the spec
sha256, completed chunks, the seed QIDs (used by ``split.source: spec:<id>``) and entity batches already
loaded; re-running a completed spec with the same sha256 is a no-op unless forced. Each spec run returns a
report (rows, entities, statements, requests, bytes, seconds, failures).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from reva.adapters.connectors.wikidata import (
    ENTITY_PROPS,
    LABEL_PROPS,
    REPO_ROOT,
    SOURCE_WBAPI,
    SOURCE_WDQS,
    AllowList,
    Archive,
    WdEntity,
    WdStatement,
    WikidataClient,
    WikidataError,
    binding_to_value,
    extract_entity,
    load_allowlist,
    property_from_uri,
    qid_from_uri,
    statement_id_from_uri,
    time_value_from_binding,
)
from reva.kernel.errors import RevaError
from reva.operations.budgets import BudgetExhausted

log = logging.getLogger("reva.kg.harvest")

HARVEST_VERSION = "wdharvest/1.0.0"
SPEC_ROOT = REPO_ROOT / "registry" / "kg" / "queries"
SCHEMA_PATH = SPEC_ROOT / "_schema.json"
STATE_DIR = REPO_ROOT / "var" / "kg" / "harvest_state"
COUNTRY_QIDS = REPO_ROOT / "registry" / "entities" / "country_qids.json"
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
KNOWN_PLACEHOLDERS = {"year_from", "year_to", "values", "qid_filter"}
RANKS = {
    "http://wikiba.se/ontology#PreferredRank": "preferred",
    "http://wikiba.se/ontology#NormalRank": "normal",
    "http://wikiba.se/ontology#DeprecatedRank": "deprecated",
}
LABEL_KIND = "unclassified"
WDQS_AS_OF_QUERY = "SELECT ?t WHERE { <http://www.wikidata.org> schema:dateModified ?t }"  # kind of entities known only through a label snapshot (no claims fetched)


class SpecError(RevaError):
    code = "kg_spec_invalid"
    http_status = 422


class RowBoundExceeded(RevaError):
    code = "row_bound_exceeded"
    http_status = 422


# --------------------------------------------------------------------------- specs
@dataclass(frozen=True)
class Spec:
    path: Path
    data: dict[str, Any]
    sha256: str

    @property
    def id(self) -> str:
        return str(self.data["id"])

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def split(self) -> dict[str, Any] | None:
        return self.data.get("split")

    @property
    def columns(self) -> dict[str, Any]:
        return dict(self.data.get("columns") or {})


def load_spec(path: Path) -> Spec:
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise SpecError(f"{path}: not a mapping")
    return Spec(path, data, hashlib.sha256(raw).hexdigest())


def discover_specs(paths: Sequence[Path] | None = None) -> list[Spec]:
    """Specs from files/directories (default: every ``registry/kg/queries/<domain>/*.yaml``)."""
    files: list[Path] = []
    for p in paths or [SPEC_ROOT]:
        p = Path(p)
        if p.is_dir():
            files.extend(sorted(x for x in p.rglob("*.yaml") if not x.name.startswith("_")))
        else:
            files.append(p)
    return [load_spec(f) for f in files]


def _schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
    return schema


def validate_spec(spec: Spec, allow: AllowList, *, known_ids: Iterable[str] = ()) -> list[str]:
    """Problems with ``spec`` (empty list = valid). No network."""
    import jsonschema

    errors: list[str] = []
    validator = jsonschema.Draft202012Validator(_schema())
    for e in sorted(validator.iter_errors(spec.data), key=lambda e: list(e.path)):
        loc = "/".join(str(x) for x in e.path) or "(root)"
        errors.append(f"schema: {loc}: {e.message[:300]}")
    if errors:
        return errors
    d = spec.data
    domain_dir = spec.path.parent.name
    if spec.path.parent.parent.resolve() == SPEC_ROOT.resolve() and d["domain"] != domain_dir:
        errors.append(f"domain {d['domain']!r} differs from directory {domain_dir!r}")
    if not d["id"].startswith(d["domain"] + "."):
        errors.append(f"id {d['id']!r} must start with '{d['domain']}.'")
    sparql = d["sparql"]
    texts = [sparql] + ([d["sparql_undated"]] if d.get("sparql_undated") else [])
    for q in texts:
        if re.search(r"\bLIMIT\s+\d+\s*$", q.strip(), re.I):
            errors.append("remove the trailing LIMIT: the harvester appends LIMIT max_rows+1")
        if re.search(r"wikibase:label", q):
            errors.append("do not use SERVICE wikibase:label: labels come from wbgetentities")
    used = set(PLACEHOLDER.findall(sparql))
    unknown = used - KNOWN_PLACEHOLDERS
    if unknown:
        errors.append(f"unknown placeholders {sorted(unknown)}")
    split = d.get("split")
    need: set[str] = set()
    if split:
        need = {"year": {"year_from", "year_to"}, "values": {"values"}, "qid_prefix": {"qid_filter"}}[split["by"]]
        if split["by"] == "year":
            if split["from"] > split["to"]:
                errors.append("split.from must be <= split.to")
            if split.get("include_undated") and not d.get("sparql_undated"):
                errors.append("split.include_undated requires sparql_undated")
        if split["by"] == "values":
            src = split["source"]
            if src == "list" and not split.get("qids"):
                errors.append("split.source=list requires qids")
            elif src.startswith("spec:"):
                dep = src[5:]
                if known_ids and dep not in set(known_ids):
                    errors.append(f"split.source refers to unknown spec {dep!r}")
                if dep not in (d.get("depends_on") or []):
                    errors.append(f"list {dep!r} in depends_on")
            elif src not in ("countries", "list"):
                errors.append(f"unknown split.source {src!r}")
    if used - unknown != need:
        errors.append(f"placeholders {sorted(used - unknown)} do not match split {split and split['by']} "
                      f"(expected {sorted(need)})")
    cols = spec.columns
    if d["purpose"] == "statements":
        if bool(cols.get("property")) == bool(cols.get("property_var")):
            errors.append("columns: exactly one of property / property_var")
        if cols.get("property") and cols["property"] not in allow:
            errors.append(f"property {cols['property']} is not allow-listed (registry/kg/properties.yaml)")
        variables = set(re.findall(r"\?([A-Za-z_][A-Za-z0-9_]*)", sparql))
        for key in ("subject", "object", "property_var", "statement", "rank", "start", "end", "point_in_time"):
            if cols.get(key) and cols[key] not in variables:
                errors.append(f"columns.{key}: ?{cols[key]} does not occur in the query")
        for pid, var in (cols.get("qualifiers") or {}).items():
            if var not in variables:
                errors.append(f"columns.qualifiers.{pid}: ?{var} does not occur in the query")
    else:
        item = cols.get("item", "item")
        if f"?{item}" not in sparql:
            errors.append(f"seed_entities query must select ?{item}")
    for pid in d.get("properties") or []:
        if pid not in allow:
            errors.append(f"properties: {pid} is not allow-listed")
    for dep in d.get("depends_on") or []:
        if known_ids and dep not in set(known_ids):
            errors.append(f"depends_on: unknown spec {dep!r}")
    return errors


def validate_all(specs: Sequence[Spec], allow: AllowList) -> dict[str, list[str]]:
    ids = [s.id for s in specs if isinstance(s.data.get("id"), str)]
    if SPEC_ROOT.exists():  # dependencies may live in other domains' directories
        for f in SPEC_ROOT.rglob("*.yaml"):
            if not f.name.startswith("_"):
                try:
                    ids.append(str(yaml.safe_load(f.read_text()).get("id")))
                except (yaml.YAMLError, AttributeError):
                    continue
    out: dict[str, list[str]] = {}
    seen: dict[str, Path] = {}
    for s in specs:
        errs = validate_spec(s, allow, known_ids=ids)
        sid = str(s.data.get("id"))
        if sid in seen:
            errs.append(f"duplicate id (also in {seen[sid]})")
        seen[sid] = s.path
        out[str(s.path.relative_to(REPO_ROOT) if s.path.is_absolute() and REPO_ROOT in s.path.parents
                else s.path)] = errs
    return out


def order_specs(specs: Sequence[Spec]) -> list[Spec]:
    """Topological order by ``depends_on`` (stable)."""
    by_id = {s.id: s for s in specs}
    done: set[str] = set()
    out: list[Spec] = []

    def visit(s: Spec, stack: tuple[str, ...]) -> None:
        if s.id in done:
            return
        if s.id in stack:
            raise SpecError(f"dependency cycle: {' -> '.join((*stack, s.id))}")
        for dep in s.get("depends_on") or []:
            if dep in by_id:
                visit(by_id[dep], (*stack, s.id))
        done.add(s.id)
        out.append(s)

    for s in specs:
        visit(s, ())
    return out


# --------------------------------------------------------------------------- chunks
@dataclass(frozen=True)
class Chunk:
    key: str
    query: str


def country_qids(path: Path = COUNTRY_QIDS) -> list[str]:
    doc = json.loads(path.read_text())
    return sorted({e["qid"] for e in doc["countries"].values() if e.get("qid")}, key=lambda q: int(q[1:]))


def _subst(query: str, values: Mapping[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], query)


def render_chunks(spec: Spec, *, state_dir: Path = STATE_DIR,
                  qids_for_source: Callable[[str], list[str]] | None = None) -> list[Chunk]:
    q = spec.data["sparql"]
    split = spec.split
    if not split:
        return [Chunk("all", q)]
    by = split["by"]
    if by == "year":
        out = []
        y = int(split["from"])
        while y <= int(split["to"]):
            hi = min(int(split["to"]), y + int(split["step"]) - 1)
            out.append(Chunk(f"year:{y}..{hi}", _subst(q, {"year_from": str(y), "year_to": str(hi)})))
            y = hi + 1
        if split.get("include_undated"):
            out.append(Chunk("undated", spec.data["sparql_undated"]))
        return out
    if by == "qid_prefix":
        var = split.get("variable", "item")
        return [
            Chunk(f"qid:{p}", _subst(q, {"qid_filter":
                  f'FILTER(STRSTARTS(STR(?{var}), "http://www.wikidata.org/entity/Q{p}"))'}))
            for p in split.get("prefixes") or [str(i) for i in range(1, 10)]
        ]
    if by == "values":
        src = split["source"]
        if qids_for_source is not None:
            qids = qids_for_source(src)
        elif src == "countries":
            qids = country_qids()
        elif src == "list":
            qids = list(split["qids"])
        else:
            qids = HarvestState(state_dir, src[5:]).seeds()
            if not qids:
                raise SpecError(f"{spec.id}: depends on {src} which has no seeds yet (run it first)")
        size = int(split.get("chunk_size", 50))
        out = []
        for i in range(0, len(qids), size):
            part = qids[i:i + size]
            digest = hashlib.sha256(" ".join(part).encode()).hexdigest()[:12]
            out.append(Chunk(f"values:{i // size}:{digest}", _subst(q, {"values": " ".join(f"wd:{x}" for x in part)})))
        return out
    raise SpecError(f"unknown split {by!r}")


def with_limit(query: str, limit: int) -> str:
    return f"{query.rstrip()}\nLIMIT {int(limit)}"


# --------------------------------------------------------------------------- state
class HarvestState:
    """Per-spec resume state (JSON file, atomically replaced)."""

    def __init__(self, directory: Path, spec_id: str) -> None:
        self.path = Path(directory) / f"{spec_id}.json"
        self.data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                self.data = {}

    def seeds(self) -> list[str]:
        return list(self.data.get("seeds") or [])

    def reset_for(self, spec_sha: str) -> None:
        if self.data.get("spec_sha256") != spec_sha:
            self.data = {"spec_sha256": spec_sha, "chunks": {}, "seeds": [], "subjects": [], "objects": [],
                         "loaded_batches": [], "status": "running", "rows": 0}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, sort_keys=True))
        tmp.replace(self.path)


# --------------------------------------------------------------------------- sinks
@dataclass
class Batch:
    """Entities and statements parsed from a set of archived raw responses (one load)."""

    source_key: str
    entities: list[WdEntity]
    statements: list[WdStatement]
    raw_sha256s: list[str]
    retrieved_at: datetime
    parameters: dict[str, Any]


class Sink(Protocol):
    def load(self, batch: Batch) -> dict[str, Any]: ...

    def known_entities(self, qids: Sequence[str]) -> set[str]: ...


class NullSink:
    """Counts only (try/dry runs)."""

    def __init__(self) -> None:
        self.batches = 0
        self.entities = 0
        self.statements = 0

    def load(self, batch: Batch) -> dict[str, Any]:
        self.batches += 1
        self.entities += len(batch.entities)
        self.statements += len(batch.statements)
        return {"entities": len(batch.entities), "statements": len(batch.statements)}

    def known_entities(self, qids: Sequence[str]) -> set[str]:
        return set()


class JsonlSink(NullSink):
    """Writes batches as JSON lines (gzip) — an offline mirror of what would be loaded."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        import gzip

        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = gzip.open(path, "at", encoding="utf-8")
        self._seen: set[str] = set()

    def load(self, batch: Batch) -> dict[str, Any]:
        out = super().load(batch)
        for e in batch.entities:
            self._seen.add(e.qid)
            self._f.write(json.dumps({"type": "entity", "source_key": batch.source_key, **entity_record(e)},
                                     ensure_ascii=False, default=str) + "\n")
        for s in batch.statements:
            self._f.write(json.dumps({"type": "statement", **s.as_dict()}, ensure_ascii=False, default=str) + "\n")
        self._f.flush()
        return out

    def known_entities(self, qids: Sequence[str]) -> set[str]:
        return {q for q in qids if q in self._seen}

    def close(self) -> None:
        self._f.close()


def entity_record(e: WdEntity) -> dict[str, Any]:
    return {
        "qid": e.qid, "kind": e.kind_hint, "labels": e.labels, "descriptions": e.descriptions,
        "aliases": e.aliases, "sitelinks_count": e.sitelinks_count, "sitelinks": e.sitelinks,
        "lastrevid": e.lastrevid, "modified": e.modified, "instance_of": list(e.instance_of),
        "claims_fetched": e.claims_fetched, "raw_sha256": e.raw_sha256,
        "retrieved_at": e.retrieved_at.isoformat(), "statements": len(e.statements), "dropped": e.dropped,
    }


# --------------------------------------------------------------------------- KG database sink
KIND_TO_KG = {"entity": "item", "coordinate": "globe_coordinate"}
GREGORIAN = "Q1985727"
JULIAN = "Q1985786"


def _kg_time(t: Mapping[str, Any] | None) -> Any:
    """Our time dict -> ``reva.kg.model.KgTime`` (astronomical years) or ``None``.

    wbgetentities times use historical year numbering + calendar model (converted with
    ``wikibase_json_time``); WDQS ``xsd:dateTime`` values are already astronomical and proleptic Gregorian.
    ``somevalue``/``novalue`` temporal qualifiers are not times: they are kept in the qualifiers instead.
    """
    from reva.kg.model import KgTime, wikibase_json_time

    if not t or t.get("snaktype") or not t.get("time") or t.get("precision") is None:
        return None
    if t.get("astronomical"):
        return KgTime(str(t["time"]), int(t["precision"]), "gregorian")
    cal = t.get("calendar") or GREGORIAN
    return wikibase_json_time({"time": t["time"], "precision": t["precision"],
                               "calendarmodel": f"http://www.wikidata.org/entity/{cal}"})


def _kg_qualifier_value(v: Any) -> Any:
    if isinstance(v, Mapping) and "time" in v and "precision" in v and not v.get("astronomical"):
        kt = _kg_time(v)
        if kt is not None:
            return {"time": kt.iso, "precision": kt.precision, "calendar": kt.calendar}
    return v


def kg_value(kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Typed literal for ``kg.statement.value`` (a dict with ``type``; decimals stay strings)."""
    if kind == "time":
        kt = _kg_time(value)
        if kt is None:
            if value.get("time") and value.get("precision") is None:
                return {"type": "time_unknown_precision", "time": value["time"]}
            return {"type": "other", "raw": dict(value)}
        return {"type": "time", "time": kt.iso, "precision": kt.precision, "calendar": kt.calendar}
    if kind == "quantity":
        q = {"type": "quantity", "amount": value.get("amount"), "unit": value.get("unit")}
        if value.get("lower") is not None:
            q["lowerBound"] = value["lower"]
        if value.get("upper") is not None:
            q["upperBound"] = value["upper"]
        return q
    if kind in ("somevalue", "novalue"):
        return {"type": kind}
    if kind == "coordinate":
        return {"type": "globe_coordinate", **{k: v for k, v in value.items()}}
    if kind in ("string", "external_id", "url", "monolingualtext"):
        return {"type": kind, **dict(value)}
    return {"type": "other", **{k: v for k, v in value.items() if k != "type"}}


def to_kg_statement(s: WdStatement) -> Any:
    from reva.kg.model import KgStatement

    quals: dict[str, Any] = {p: [_kg_qualifier_value(v) for v in vs] for p, vs in s.qualifiers.items()}
    for name, pid in (("start", "P580"), ("end", "P582"), ("point_in_time", "P585")):
        t = getattr(s, name)
        if t and t.get("snaktype"):  # unknown / no value: not a time, keep the fact in the qualifiers
            quals[pid] = [{"snaktype": t["snaktype"]}]
        elif t and t.get("precision") is None:  # precision not selected: kept as data, no bounds asserted
            quals[pid] = [{"time": t.get("time"), "precision": None, "precision_unknown": True}]
    refs = [{p: [_kg_qualifier_value(v) for v in vs] for p, vs in r.items()} for r in s.references]
    if s.selector.get("rank_selected") is False:
        quals["_rank_not_selected"] = True  # WDQS row without ?rank: 'normal' is a default, not a reading
    revision = str(s.source_revision) if s.source_revision is not None else None
    if s.source_key == SOURCE_WDQS:
        # WDQS results carry no entity revision. The revision is recorded as explicitly unknown, with the
        # query service's own data timestamp (schema:dateModified of <http://www.wikidata.org>, when probed)
        # and the retrieval time; the version label is the retrieval time.
        quals["_revision"] = {"wikidata_revision": "unknown", "reason": "WDQS rows carry no entity revision",
                              "wdqs_data_as_of": s.selector.get("wdqs_data_as_of"),
                              "retrieved_at": s.retrieved_at.isoformat()}
        revision = f"wdqs@{s.retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    return KgStatement(
        subject_qid=s.subject, property_pid=s.property,
        object_qid=s.object_qid if s.value_kind == "entity" else None,
        value=None if s.value_kind == "entity" else kg_value(s.value_kind, s.value),
        start=_kg_time(s.start), end=_kg_time(s.end), point_in_time=_kg_time(s.point_in_time),
        qualifiers=quals, rank=s.rank, references=refs, source_key=s.source_key,
        source_revision=revision, statement_id=s.statement_id, raw_sha256=s.raw_sha256,
    )


def to_kg_entity(e: WdEntity, *, kind: str | None = None) -> Any:
    from reva.kg.model import KgEntity

    rev = None if e.lastrevid is None else (str(e.lastrevid) if e.claims_fetched else f"{e.lastrevid}:labels")
    return KgEntity(qid=e.qid, kind=kind or e.kind_hint or LABEL_KIND, labels=e.labels,
                    descriptions=e.descriptions, aliases=e.aliases, sitelinks=e.sitelinks_count,
                    source_revision=rev)


def kg_properties(allow: AllowList) -> list[Any]:
    from reva.kg.model import KgProperty

    out = []
    for p in allow.properties.values():
        out.append(KgProperty(pid=p.property, value_kind=KIND_TO_KG.get(p.kind, p.kind),
                              reva_predicate=p.reva_predicate, label_en=p.label_en or p.reva_predicate,
                              label_fr=p.label_fr, inverse_name=p.inverse))
    return out


class KgDbSink:
    """Loads batches with ``reva.kg.model.load_batch`` (one ``kg.load`` per wbgetentities/WDQS response set).

    Label-only snapshots carry ``source_revision = '<lastrevid>:labels'`` so a later full snapshot of the
    same revision is a new version (and becomes current) instead of a conflict.
    """

    def __init__(self, engine: Any, store: Any, *, origin: str = "live") -> None:
        self.engine = engine
        self.store = store
        self.origin = origin
        self.reports: list[dict[str, Any]] = []
        self.totals: dict[str, int] = {}

    def register_properties(self, allow: AllowList) -> dict[str, int]:
        from reva.kg.model import register_properties

        r = register_properties(self.engine, kg_properties(allow))
        return {"properties_inserted": r.properties_inserted, "versions_inserted": r.versions_inserted,
                "unchanged": r.unchanged}

    def load(self, batch: Batch) -> dict[str, Any]:
        from reva.kg.model import load_batch

        rep = load_batch(
            self.engine, self.store,
            entities=[to_kg_entity(e) for e in batch.entities],
            statements=[to_kg_statement(s) for s in batch.statements],
            source_key=batch.source_key, raw_sha256s=sorted(set(batch.raw_sha256s)),
            retrieved_at=batch.retrieved_at, origin=self.origin, parameters=batch.parameters,
        )
        d = rep.as_dict()
        for k, v in d.items():
            if isinstance(v, int) and k != "load_id":
                self.totals[k] = self.totals.get(k, 0) + v
        for k, v in rep.rejected_counts.items():
            self.totals[f"rejected:{k}"] = self.totals.get(f"rejected:{k}", 0) + v
        if rep.rejected_counts:
            log.info("load %s rejected %s", rep.load_id, rep.rejected_counts)
        return d

    def known_entities(self, qids: Sequence[str]) -> set[str]:
        from sqlalchemy import text

        if not qids:
            return set()
        out: set[str] = set()
        with self.engine.connect() as conn:
            for i in range(0, len(qids), 10_000):
                out |= set(conn.execute(
                    text("SELECT DISTINCT qid FROM kg.entity WHERE qid = ANY(:q) AND origin = :o"),
                    {"q": list(qids[i:i + 10_000]), "o": self.origin}).scalars())
        return out


# --------------------------------------------------------------------------- reports
@dataclass
class SpecReport:
    spec_id: str
    purpose: str
    status: str = "pending"  # done | skipped | failed | partial | tried
    chunks_total: int = 0
    chunks_done: int = 0
    rows: int = 0
    seeds: int = 0
    entities_full: int = 0
    entities_labels: int = 0
    statements: int = 0
    statements_dropped_deprecated: int = 0
    rows_skipped: int = 0
    statements_enriched: int = 0  # WDQS rows replaced by the snapshot claim (revision, refs, precision)
    statements_unenriched: int = 0  # WDQS rows loaded as such (revision unknown, no references)
    requests: int = 0
    bytes: int = 0
    seconds: float = 0.0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["seconds"] = round(self.seconds, 1)
        return d


# --------------------------------------------------------------------------- harvester
def _first_value(b: Mapping[str, Any], var: str | None) -> Mapping[str, str] | None:
    return b.get(var) if var else None


class Harvester:
    def __init__(self, client: WikidataClient, sink: Sink, *, allow: AllowList | None = None,
                 state_dir: Path = STATE_DIR, entity_batch: int = 50,
                 qids_for_source: Callable[[str], list[str]] | None = None, via: str = "wdqs",
                 expand: bool = True) -> None:
        self.via = via
        self.expand = expand
        self.client = client
        self.sink = sink
        self.allow = allow or load_allowlist()
        self.state_dir = state_dir
        self.entity_batch = entity_batch
        self.qids_for_source = qids_for_source
        self.wdqs_as_of: str | None = None
        self._as_of_probed = False

    async def probe_wdqs_as_of(self) -> str | None:
        """Once per harvester: the WDQS replica's data timestamp (its update lag is not otherwise visible)."""
        if self._as_of_probed:
            return self.wdqs_as_of
        self._as_of_probed = True
        try:
            res = await self.client.sparql(WDQS_AS_OF_QUERY, meta={"purpose": "wdqs_data_as_of"})
            vals = res.column("t")
            self.wdqs_as_of = vals[0] if vals else None
        except WikidataError as e:
            log.warning("WDQS data timestamp probe failed: %s", e.message)
        return self.wdqs_as_of

    # -- one spec ---------------------------------------------------------------------------------
    async def run_spec(self, spec: Spec, *, force: bool = False, try_limit: int | None = None) -> SpecReport:
        rep = SpecReport(spec.id, spec.data["purpose"])
        t0 = time.monotonic()
        req0, bytes0 = self.client.stats.requests, self.client.stats.bytes
        try:
            await self._run(spec, rep, force=force, try_limit=try_limit)
        except (RevaError, OSError) as e:
            code = getattr(e, "code", type(e).__name__)
            msg = getattr(e, "message", str(e))
            rep.failures.append({"code": code, "error": str(msg)[:500]})
            rep.status = "failed"
            log.warning("spec %s failed: %s %s", spec.id, code, msg)
        finally:
            rep.requests = self.client.stats.requests - req0
            rep.bytes = self.client.stats.bytes - bytes0
            rep.seconds = time.monotonic() - t0
        return rep

    async def _run(self, spec: Spec, rep: SpecReport, *, force: bool, try_limit: int | None) -> None:
        errors = validate_spec(spec, self.allow)
        if errors:
            raise SpecError(f"{spec.id}: invalid spec: {'; '.join(errors)[:800]}")
        if spec.get("enabled", True) is False and try_limit is None:
            rep.status = "skipped"
            return
        trying = try_limit is not None
        state = HarvestState(self.state_dir, spec.id) if not trying else None
        if state is not None:
            if force:
                state.data = {}
            if state.data.get("spec_sha256") == spec.sha256 and state.data.get("status") == "done":
                rep.status = "skipped"
                rep.seeds = len(state.seeds())
                return
            state.reset_for(spec.sha256)
            state.data["status"] = "running"
            state.save()
        if self.via == "api" and not trying:
            if spec.data["purpose"] != "seed_entities" or not spec.get("api_search"):
                rep.status = "skipped"
                rep.failures.append({"code": "no_api_search", "error": "spec has no api_search block (WDQS only)"})
                return
            chunks = []
        else:
            chunks = render_chunks(spec, state_dir=self.state_dir, qids_for_source=self.qids_for_source)
        if trying:
            chunks = chunks[:1]
        rep.chunks_total = len(chunks)
        if spec.data["purpose"] == "statements" and not trying:
            await self.probe_wdqs_as_of()
        max_rows = int(spec.data["max_rows"])
        cols = spec.columns
        purpose = spec.data["purpose"]
        seeds: dict[str, None] = dict.fromkeys(state.seeds() if state else [])
        subjects: dict[str, None] = dict.fromkeys(state.data.get("subjects", []) if state else [])
        objects: dict[str, None] = dict.fromkeys(state.data.get("objects", []) if state else [])
        rows_total = int(state.data.get("rows", 0)) if state else 0
        snapshot_subjects: set[str] = set(state.data.get("snapshot_subjects", []) if state else [])
        for ch in chunks:
            if state is not None and ch.key in state.data["chunks"]:
                rep.chunks_done += 1
                continue
            limit = min(try_limit, 50) if try_limit is not None else max_rows + 1
            res = await self.client.sparql(with_limit(ch.query, limit),
                                           meta={"spec": spec.id, "chunk": ch.key, "spec_sha256": spec.sha256})
            n = len(res.bindings)
            rows_total += n
            rep.rows += n
            if not trying and rows_total > max_rows:
                raise RowBoundExceeded(f"{spec.id}: {rows_total} rows > max_rows {max_rows} (chunk {ch.key})",
                                       rows=rows_total, max_rows=max_rows)
            if purpose == "seed_entities":
                var = cols.get("item", "item")
                for b in res.bindings:
                    q = qid_from_uri((b.get(var) or {}).get("value"))
                    if q and q.startswith("Q"):
                        seeds.setdefault(q)
                    else:
                        rep.rows_skipped += 1
            else:
                stmts, skipped = self.rows_to_statements(spec, res)
                rep.rows_skipped += skipped
                rep.statements_dropped_deprecated += sum(1 for s in stmts if s is None)
                kept = [s for s in stmts if s is not None]
                for s in kept:
                    subjects.setdefault(s.subject)
                    if s.object_qid:
                        objects.setdefault(s.object_qid)
                if kept and not trying:
                    kept, fetched = await self.enrich_from_snapshots(spec, kept, rep)
                    snapshot_subjects.update(fetched)
                    if kept:
                        self.sink.load(Batch(SOURCE_WDQS, [], kept, [res.raw_sha256], res.retrieved_at,
                                             {"spec": spec.id, "chunk": ch.key, "query_sha256": res.query_sha256,
                                              "harvester": HARVEST_VERSION}))
                rep.statements += len(kept)
            rep.chunks_done += 1
            if state is not None:
                state.data["chunks"][ch.key] = {"rows": n, "raw_sha256": res.raw_sha256}
                state.data["rows"] = rows_total
                state.data["seeds"] = list(seeds)
                state.data["subjects"] = list(subjects)
                state.data["objects"] = list(objects)
                state.data["snapshot_subjects"] = sorted(snapshot_subjects)
                state.save()
        if self.via == "api" and not trying and state is not None and not state.data["chunks"].get("api"):
            found, info = await self.api_seeds(spec)
            rows_total += len(found)
            rep.rows += len(found)
            if rows_total > max_rows:
                raise RowBoundExceeded(f"{spec.id}: {rows_total} search results > max_rows {max_rows}",
                                       rows=rows_total, max_rows=max_rows)
            for q in found:
                seeds.setdefault(q)
            if info.get("window_exceeded"):
                rep.failures.append({"code": "search_window_exceeded", "queries": info["window_exceeded"]})
            state.data["chunks"]["api"] = info
            state.data["rows"] = rows_total
            state.data["seeds"] = list(seeds)
            state.save()
        rep.seeds = len(seeds)
        if trying:
            rep.status = "tried"
            return
        assert state is not None
        if purpose == "seed_entities":
            new_objects = await self.fetch_entities(spec, list(seeds), state, rep, full=True,
                                                    kind=spec.get("entity_kind"), properties=spec.get("properties"))
            for q in new_objects:
                objects.setdefault(q)
        else:
            todo = [s for s in subjects if s not in snapshot_subjects]
            known = self.sink.known_entities(todo)
            await self.fetch_entities(spec, [s for s in todo if s not in known], state, rep, full=False,
                                      kind=spec.get("entity_kind") or LABEL_KIND)
        if int(spec.get("expand_hops", 0)) >= 1 and self.expand:
            already = set(seeds) | set(subjects)
            todo = [o for o in objects if o not in already]
            known = self.sink.known_entities(todo)
            await self.fetch_entities(spec, [o for o in todo if o not in known], state, rep, full=False,
                                      kind=LABEL_KIND, tag="objects")
        state.data["status"] = "done"
        state.data["finished_at"] = datetime.now(UTC).isoformat()
        state.data["report"] = rep.as_dict()
        state.save()
        rep.status = "done"

    # -- WDQS rows -> snapshot statements (revision, references, exact time precision) ----------------
    async def enrich_from_snapshots(self, spec: Spec, stmts: list[WdStatement],
                                    rep: SpecReport) -> tuple[list[WdStatement], set[str]]:
        """Replace WDQS rows that carry a statement id by the same claim read from the subject's wbgetentities
        snapshot (``lastrevid`` as revision, references, exact precision and calendar). The subject snapshot
        replaces the label snapshot it would get anyway (same number of requests). Returns the WDQS rows that
        could not be matched (no statement id, or the claim changed/vanished since the WDQS replica) and the
        subjects fetched."""
        need = sorted({s.subject for s in stmts if s.statement_id}, key=lambda q: int(q[1:]))
        if not need:
            return stmts, set()
        by_id: dict[str, WdStatement] = {}
        fetched: set[str] = set()
        for i in range(0, len(need), self.entity_batch):
            part = need[i:i + self.entity_batch]
            res = await self.client.get_entities(part, props=ENTITY_PROPS, meta={"spec": spec.id, "mode": "enrich"})
            ents: list[WdEntity] = []
            for qid, raw in res.entities.items():
                if not qid.startswith("Q"):
                    continue
                e = extract_entity(raw, self.allow, raw_sha256=res.raw_sha_by_id[qid], retrieved_at=res.retrieved_at,
                                   kind_hint=spec.get("entity_kind") or LABEL_KIND)
                ents.append(e)
                fetched.add(qid)
                for st in e.statements:
                    if st.statement_id:
                        by_id[st.statement_id.lower()] = st
            wanted = {s.statement_id.lower() for s in stmts if s.statement_id and s.subject in set(part)}
            matched = [by_id[k] for k in sorted(wanted) if k in by_id]
            if ents:
                self.sink.load(Batch(SOURCE_WBAPI, ents, matched, list(res.raw_sha256s), res.retrieved_at,
                                     {"spec": spec.id, "mode": "enrich", "harvester": HARVEST_VERSION,
                                      "allowlist_sha256": self.allow.sha256}))
            rep.entities_labels += len(ents)
            rep.statements_enriched += len(matched)
        left = [s for s in stmts if not (s.statement_id and s.statement_id.lower() in by_id)]
        rep.statements_unenriched += len(left)
        return left, fetched

    # -- WDQS-free seeds (CirrusSearch) ---------------------------------------------------------------
    async def api_seeds(self, spec: Spec) -> tuple[list[str], dict[str, Any]]:
        a = spec.data["api_search"]
        if a.get("source") == "countries":
            qs = self.qids_for_source("countries") if self.qids_for_source else country_qids()
            return qs, {"method": "countries"}
        classes = list(dict.fromkeys(a["instance_of"]))
        frontier = list(classes)
        shas: list[str] = []
        info: dict[str, Any] = {"method": "cirrussearch", "instance_of": classes,
                                "subclass_depth": int(a.get("subclass_depth", 0)), "window_exceeded": []}
        for _depth in range(int(a.get("subclass_depth", 0))):
            new: list[str] = []
            for q in or_groups("haswbstatement:", [f"P279={c}" for c in frontier]):
                hits, total, sh = await self.client.search_all(q, meta={"spec": spec.id, "step": "subclasses"})
                shas.extend(sh)
                if total > len(hits):
                    info["window_exceeded"].append(q[:200])
                new.extend(h for h in hits if h not in classes and h not in new)
            classes.extend(new)
            frontier = new
            if not frontier:
                break
        seeds: list[str] = []
        extra = f" {a['extra']}" if a.get("extra") else ""
        for q in or_groups("haswbstatement:", [f"P31={c}" for c in classes], extra):
            hits, total, sh = await self.client.search_all(q, meta={"spec": spec.id, "step": "instances"})
            shas.extend(sh)
            if total > len(hits):
                info["window_exceeded"].append(q[:200])
            seeds.extend(hits)
        info.update({"classes": len(classes), "raw_sha256": shas})
        return list(dict.fromkeys(seeds)), info

    # -- entities ---------------------------------------------------------------------------------
    async def fetch_entities(self, spec: Spec, qids: Sequence[str], state: HarvestState, rep: SpecReport, *,
                             full: bool, kind: str | None, properties: Sequence[str] | None = None,
                             tag: str = "") -> list[str]:
        """Fetch + load snapshots in batches of 50 (resumable per batch). Returns object QIDs referenced."""
        done = set(state.data.get("loaded_batches") or [])
        objects: dict[str, None] = {}
        props = ENTITY_PROPS if full else LABEL_PROPS
        mode = "full" if full else "labels"
        ordered = sorted(set(qids), key=lambda q: int(q[1:]))
        for i in range(0, len(ordered), self.entity_batch):
            part = ordered[i:i + self.entity_batch]
            key = f"{mode}{tag}:{hashlib.sha256(' '.join(part).encode()).hexdigest()[:16]}"
            if key in done:
                continue
            res = await self.client.get_entities(part, props=props, meta={"spec": spec.id, "mode": mode})
            ents: list[WdEntity] = []
            stmts: list[WdStatement] = []
            for qid, raw in res.entities.items():
                if not qid.startswith("Q"):
                    continue
                e = extract_entity(raw, self.allow, raw_sha256=res.raw_sha_by_id[qid],
                                   retrieved_at=res.retrieved_at, kind_hint=kind, properties=properties)
                ents.append(e)
                stmts.extend(e.statements)
                rep.statements_dropped_deprecated += e.dropped["deprecated"]
                for s in e.statements:
                    if s.object_qid and s.object_qid.startswith("Q"):
                        objects.setdefault(s.object_qid)
            if res.missing:
                rep.failures.append({"code": "missing_entities", "ids": list(res.missing)[:50]})
            norev = [e.qid for e in ents if e.lastrevid is None]
            if norev:
                rep.failures.append({"code": "missing_revision", "ids": norev[:50]})
            if ents:
                self.sink.load(Batch(SOURCE_WBAPI, ents, stmts, list(res.raw_sha256s), res.retrieved_at,
                                     {"spec": spec.id, "mode": mode, "redirects": res.redirects,
                                      "harvester": HARVEST_VERSION, "allowlist_sha256": self.allow.sha256}))
            if full:
                rep.entities_full += len(ents)
            else:
                rep.entities_labels += len(ents)
            rep.statements += len(stmts)
            done.add(key)
            state.data["loaded_batches"] = sorted(done)
            if full:
                state.data["objects"] = sorted(set(state.data.get("objects") or []) | set(objects))
            state.save()
        if full and not objects:  # resumed: objects of already-loaded batches are in the state
            return list(state.data.get("objects") or [])
        return list(objects)

    # -- rows -> statements -----------------------------------------------------------------------
    def rows_to_statements(self, spec: Spec, res: Any) -> tuple[list[WdStatement | None], int]:
        cols = spec.columns
        out: list[WdStatement | None] = []
        skipped = 0
        seen: set[tuple[Any, ...]] = set()
        for b in res.bindings:
            subj = qid_from_uri((b.get(cols["subject"]) or {}).get("value"))
            pid = cols.get("property") or property_from_uri((b.get(cols.get("property_var", "")) or {}).get("value"))
            if not subj or not subj.startswith("Q") or not pid or pid not in self.allow:
                skipped += 1
                continue
            kind, obj, value = binding_to_value(b.get(cols["object"]))
            if kind == "novalue" and value.get("snaktype") == "absent":
                skipped += 1
                continue
            if obj and not obj.startswith("Q"):
                kind, obj, value = "string", None, {"value": obj}
            rank_uri = (_first_value(b, cols.get("rank")) or {}).get("value")
            rank = RANKS.get(rank_uri or "", "normal")
            if rank == "deprecated":
                out.append(None)
                continue
            sid = statement_id_from_uri((_first_value(b, cols.get("statement")) or {}).get("value"))
            temporal = {k: with_precision(time_value_from_binding(_first_value(b, cols.get(k))),
                                          _first_value(b, cols.get(f"{k}_precision")))
                        for k in ("start", "end", "point_in_time")}
            if kind == "time":
                value = with_precision(value, _first_value(b, cols.get("object_precision"))) or value
            quals: dict[str, list[Any]] = {}
            for qp, var in (cols.get("qualifiers") or {}).items():
                vb = b.get(var)
                if vb:
                    k2, o2, v2 = binding_to_value(vb)
                    quals.setdefault(qp, []).append(o2 if k2 == "entity" else v2)
            ident = (subj, pid, obj or json.dumps(value, sort_keys=True), sid,
                     json.dumps(temporal, sort_keys=True, default=str), json.dumps(quals, sort_keys=True))
            if ident in seen:  # OPTIONAL fan-out duplicates
                continue
            seen.add(ident)
            out.append(WdStatement(
                subject=subj, property=pid, rank=rank, value_kind=kind, object_qid=obj, value=value,
                statement_id=sid, start=temporal["start"], end=temporal["end"],
                point_in_time=temporal["point_in_time"], qualifiers=quals, references=[],
                source_key=SOURCE_WDQS, source_revision=None, raw_sha256=res.raw_sha256,
                retrieved_at=res.retrieved_at,
                selector={"spec": spec.id, "query_sha256": res.query_sha256,
                          "rank_selected": bool(cols.get("rank")), "statement_id": sid,
                          "wdqs_data_as_of": self.wdqs_as_of},
            ))
        return out, skipped


def with_precision(t: dict[str, Any] | None, binding: Mapping[str, str] | None) -> dict[str, Any] | None:
    """Attach the Wikibase precision selected by the spec (``wikibase:timePrecision`` via ``psv:``/``pqv:``).

    WDQS ``xsd:dateTime`` literals carry no precision and a date's shape says nothing about it (a century is
    written ``-0400-01-01``). Without a selected precision the time is marked ``precision_unknown``: it is kept
    as data but asserts no time bounds (never a guessed precision)."""
    if not t or t.get("snaktype") or not t.get("time"):
        return t
    raw = (binding or {}).get("value")
    if raw is not None and str(raw).lstrip("-").isdigit():
        p = int(raw)
        if 0 <= p <= 14:
            return {**t, "precision": p, "precision_unknown": False}
    return {**t, "precision": None, "precision_unknown": True}


# --------------------------------------------------------------------------- run many
async def run_specs(h: Harvester, specs: Sequence[Spec], *, force: bool = False,
                    on_report: Callable[[SpecReport], None] | None = None) -> list[SpecReport]:
    reports = []
    failed: set[str] = set()
    for s in order_specs(specs):
        deps = set(s.get("depends_on") or [])
        if deps & failed:
            r = SpecReport(s.id, s.data.get("purpose", "?"), status="failed",
                           failures=[{"code": "dependency_failed", "error": sorted(deps & failed)}])
        else:
            r = await h.run_spec(s, force=force)
        if r.status == "failed":
            failed.add(s.id)
        reports.append(r)
        if on_report:
            on_report(r)
    return reports


# --------------------------------------------------------------------------- database runs
PROTECTED_DATABASES = frozenset({"reva", "postgres", "template0", "template1"})
DEFAULT_DB = "reva_kg_wdharvest"
OBJECT_ROOT = REPO_ROOT / "var" / "kg" / "objects"
REPORT_DIR = REPO_ROOT / "var" / "kg" / "reports"


def harvest_settings(db_name: str) -> Any:
    """Settings for a *throwaway* harvest database. The dev database ``reva`` is refused."""
    from reva.settings import Settings

    if db_name in PROTECTED_DATABASES or not re.match(r"^reva_[a-z0-9_]{1,40}$", db_name):
        raise SpecError(f"refusing database {db_name!r}: harvest runs only target throwaway reva_* databases")
    base = Settings(_env_file=REPO_ROOT / ".env")
    if base.reva_migration_database_url is None:
        raise SpecError("REVA_MIGRATION_DATABASE_URL is not configured")
    url = base.migration_url().set(database=db_name).render_as_string(hide_password=False)
    data = base.model_dump()
    data.update(reva_migration_database_url=url, reva_db_name=db_name, reva_env="test",
                reva_object_root=OBJECT_ROOT)
    return Settings.model_validate(data)


def ensure_database(db_name: str) -> Any:
    """Create (if needed), migrate and seed the throwaway harvest database; returns its settings."""
    from sqlalchemy import create_engine, text

    settings = harvest_settings(db_name)
    admin = create_engine(settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}).scalar()
            if not exists:
                c.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin.dispose()
    if exists:
        probe = create_engine(settings.migration_url())
        try:
            with probe.connect() as c:
                ready = c.execute(text("SELECT to_regclass('kg.load') IS NOT NULL AND "
                                       "to_regclass('ops.budget_ledger') IS NOT NULL")).scalar()
        finally:
            probe.dispose()
        if ready:  # already migrated and seeded: no re-bootstrap per command
            return settings
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    bootstrap(settings)  # migrations to head (incl. 0009_kg) + runtime role passwords; idempotent
    owner = make_engine("migration", settings)
    try:
        seed(owner, FilesystemArtifactStore(OBJECT_ROOT, settings.reva_s3_prefix))
    finally:
        owner.dispose()
    return settings


def live_http(*, max_bytes: int = 52_428_800, timeout_seconds: float = 75.0, proxy_mode: str | None = None) -> Any:
    """The raw SafeHttpClient for the two Wikidata hosts. Every ``src`` caller wraps it in the budget meter
    (:func:`budgeted_client`); only offline-fixture capture tools in ``tests`` use it directly.

    ``max_attempts=1``: retries/back-off are done by :class:`WikidataClient` (WDQS time-outs, 429/503, maxlag,
    machine-wide cool-downs). The in-process rate limiter is a second guard behind the machine-wide lock.
    """
    from reva.adapters.connectors.wikidata import ALLOWED_HOSTS, DEFAULT_MIN_INTERVAL
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
    from reva.adapters.http.safe_client import SafeHttpClient

    policies = {h: HostPolicy(min_interval_seconds=DEFAULT_MIN_INTERVAL, max_concurrency=1,
                              max_retry_after_seconds=900.0) for h in ALLOWED_HOSTS}
    return SafeHttpClient(allowed_hosts=ALLOWED_HOSTS, timeout_seconds=timeout_seconds, max_bytes=max_bytes,
                          max_attempts=1, rate_limiter=RateLimiter(policies), proxy_mode=proxy_mode,  # type: ignore[arg-type]
                          contact_email=None)  # the request carries the generic project User-Agent only


@dataclass
class BudgetedSession:
    client: WikidataClient
    engine: Any
    store: Any
    run_id: str
    usage: dict[str, Any] = field(default_factory=dict)


@contextlib.asynccontextmanager
async def budgeted_client(*, purpose: str, db_name: str = DEFAULT_DB, planned_wdqs: int = 1,
                          planned_wbapi: int = 1) -> AsyncIterator[BudgetedSession]:
    """The only live Wikidata entry point (invariant 30): reserve -> meter every request -> settle.

    The ledger lives in the throwaway harvest database. ``reserve_ingest`` refuses a run that does not fit the
    per-source/total daily budget (``BudgetExhausted``, nothing requested); ``metered`` tops the reservation up
    per request or refuses it before the transport is called; ``settle_metered`` records actual usage whatever
    happened. One reservation per source key (``wikidata.wdqs``, ``wikidata.wbapi``).
    """
    import uuid

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.operations.budgets import load_budget, metered, reserve_ingest, settle_metered

    settings = ensure_database(db_name)
    engine = make_engine("worker", settings)
    store = FilesystemArtifactStore(OBJECT_ROOT, settings.reva_s3_prefix)
    run_id = f"kg{purpose}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    inner = live_http()
    wdqs = wbapi = None
    session: BudgetedSession | None = None
    try:
        budget = load_budget()
        res_q = reserve_ingest(engine, job_id=run_id, attempt=1, source_key=SOURCE_WDQS,
                               planned_requests=planned_wdqs, budget=budget)
        res_a = reserve_ingest(engine, job_id=run_id, attempt=1, source_key=SOURCE_WBAPI,
                               planned_requests=planned_wbapi, budget=budget)
        wdqs = metered(inner, engine, res_q, budget=budget)
        wbapi = metered(inner, engine, res_a, budget=budget)
        client = WikidataClient(wdqs, wbapi, archive=Archive(store, REPO_ROOT / "var" / "kg" / "archive_index.jsonl"))
        session = BudgetedSession(client, engine, store, run_id)
        yield session
    finally:
        settled = [settle_metered(engine, h) for h in (wdqs, wbapi) if h is not None]
        if session is not None:
            session.usage["budget"] = settled
        await inner.aclose()
        engine.dispose()


async def run_into_db(paths: Sequence[Path] | None, *, db_name: str = DEFAULT_DB, force: bool = False,
                      planned_wdqs: int = 50, planned_wbapi: int = 100, via: str = "wdqs",
                      expand: bool = True) -> dict[str, Any]:
    """Run specs into the throwaway database through :func:`budgeted_client`."""
    allow = load_allowlist()
    specs = discover_specs(paths or None)
    bad = {k: v for k, v in validate_all(specs, allow).items() if v}
    if bad:
        raise SpecError(f"invalid specs: {json.dumps(bad)[:1500]}")
    out: dict[str, Any] = {"database": db_name, "started_at": datetime.now(UTC).isoformat(),
                           "allowlist_sha256": allow.sha256, "specs": []}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    async with budgeted_client(purpose="harvest", db_name=db_name, planned_wdqs=planned_wdqs,
                               planned_wbapi=planned_wbapi) as sess:
        out["run_id"] = sess.run_id
        report_path = REPORT_DIR / f"{sess.run_id}.json"
        sink = KgDbSink(sess.engine, sess.store)
        client = sess.client

        def save(r: SpecReport) -> None:
            out["specs"].append(r.as_dict())
            out["client"] = client.stats.as_dict()
            out["load_totals"] = dict(sink.totals)
            report_path.write_text(json.dumps(out, indent=2, default=str))
            log.info("spec %s: %s rows=%s seeds=%s full=%s labels=%s statements=%s req=%s %.0fs", r.spec_id,
                     r.status, r.rows, r.seeds, r.entities_full, r.entities_labels, r.statements, r.requests,
                     r.seconds)

        try:
            out["properties"] = sink.register_properties(allow)
            out["via"], out["expand"] = via, expand
            await run_specs(Harvester(client, sink, allow=allow, via=via, expand=expand), specs, force=force,
                            on_report=save)
        finally:
            out["client"] = client.stats.as_dict()
            out["load_totals"] = dict(sink.totals)
            report_path.write_text(json.dumps(out, indent=2, default=str))
    out["budget"] = sess.usage.get("budget")
    out["finished_at"] = datetime.now(UTC).isoformat()
    report_path.write_text(json.dumps(out, indent=2, default=str))
    out["report_path"] = str(report_path)
    return out


# --------------------------------------------------------------------------- country QIDs
CROSSWALK = REPO_ROOT / "registry" / "entities" / "country_crosswalk.json"
COUNTRY_CLASSES = {"Q6256": "country", "Q3624078": "sovereign state", "Q161243": "dependent territory",
                   "Q46395": "British Overseas Territory", "Q15634554": "state with limited recognition",
                   "Q1763527": "constituent country", "Q202216": "overseas department and region of France",
                   "Q1048835": "political territorial entity", "Q10711424": "state with limited recognition"}
MAP_QUERY = """SELECT ?item ?code ?rank ?end ?dissolved ?class WHERE {
  VALUES ?code { %s }
  ?item p:P298 ?st . ?st ps:P298 ?code ; wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
  OPTIONAL { ?st pq:P582 ?end }
  OPTIONAL { ?item wdt:P576 ?dissolved }
  OPTIONAL { ?item wdt:P31 ?class }
}"""


async def map_countries(client: WikidataClient, *, out_path: Path = COUNTRY_QIDS,
                        manual_titles: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Map every crosswalk entity to a Wikidata QID.

    Method 1 (``P298``): items holding a non-deprecated ISO 3166-1 alpha-3 (P298) statement with that code,
    excluding statements with an end time (P582) and items with a dissolution date (P576); if several remain,
    the one classed country/sovereign state/territory is kept and the others are listed.
    Method 2 (``enwiki_sitelink``, only for codes outside ISO 3166, e.g. CHI, XKX): the item whose English
    Wikipedia sitelink is the given title (``manual_titles``), recorded with the title used.
    """
    cw = json.loads(CROSSWALK.read_text())
    ents = {e["key"]: e for e in cw["entities"]}
    codes = sorted(k for k, e in ents.items() if e.get("iso3"))
    res = await client.sparql(MAP_QUERY % " ".join(f'"{c}"' for c in codes), meta={"purpose": "country_qids"})
    cand: dict[str, dict[str, dict[str, Any]]] = {}
    for b in res.bindings:
        code = b["code"]["value"]
        q = qid_from_uri(b["item"]["value"])
        if not q:
            continue
        c = cand.setdefault(code, {}).setdefault(q, {"ended": False, "dissolved": False, "classes": set(),
                                                      "rank": RANKS.get(b["rank"]["value"], "normal")})
        c["ended"] |= "end" in b
        c["dissolved"] |= "dissolved" in b
        if "class" in b:
            cls = qid_from_uri(b["class"]["value"])
            if cls:
                c["classes"].add(cls)
    countries: dict[str, Any] = {}
    unmapped: list[str] = []
    for code in sorted(ents):
        options = cand.get(code, {})
        live = {q: c for q, c in options.items() if not c["ended"] and not c["dissolved"]}
        pick: str | None = None
        note = None
        if len(live) == 1:
            pick = next(iter(live))
        elif len(live) > 1:
            classed = [q for q, c in live.items() if c["classes"] & set(COUNTRY_CLASSES)]
            preferred = [q for q in classed if live[q]["rank"] == "preferred"]
            if len(classed) == 1:
                pick, note = classed[0], "several items hold the code; kept the one classed as a country/territory"
            elif len(preferred) == 1:
                pick, note = preferred[0], "several country-classed items; kept the preferred-rank statement"
        if pick:
            countries[code] = {"qid": pick, "method": "P298", "crosswalk_label": ents[code]["label"],
                               "classes": sorted(live[pick]["classes"] & set(COUNTRY_CLASSES))}
            if note:
                countries[code]["note"] = note
                countries[code]["other_candidates"] = sorted(q for q in live if q != pick)
            excluded = sorted(q for q in options if q not in live)
            if excluded:
                countries[code]["excluded_candidates"] = excluded
        else:
            unmapped.append(code)
    titles = {c: t for c, t in (manual_titles or {}).items() if c in unmapped}
    sitelink_raw: list[str] = []
    for code, title in titles.items():
        url = WBAPI_SITELINK.format(title=title.replace(" ", "_"))
        result, sha = await client._request(client.wbapi_http, url, endpoint="wbapi", accept="application/json",
                                            meta={"kind": "wbgetentities_sitelink", "title": title})
        sitelink_raw.append(sha)
        doc = json.loads(result.body)
        found = [q for q, e in (doc.get("entities") or {}).items() if q.startswith("Q") and "missing" not in e]
        if len(found) == 1:
            countries[code] = {"qid": found[0], "method": "enwiki_sitelink", "enwiki_title": title,
                               "crosswalk_label": ents[code]["label"], "raw_sha256": sha}
            unmapped.remove(code)
    # labels (en/fr) of the mapped items: one labels-only wbgetentities pass
    lab = await client.get_entities([v["qid"] for v in countries.values()], props=("labels",),
                                    languages=("en", "fr"), meta={"purpose": "country_qids_labels"})
    for v in countries.values():
        e = lab.entities.get(lab.redirects.get(v["qid"], v["qid"])) or {}
        v["label_en"] = (e.get("labels") or {}).get("en", {}).get("value")
        v["label_fr"] = (e.get("labels") or {}).get("fr", {}).get("value")
    doc_out: dict[str, Any] = {
        "schema": "reva-country-qids/1",
        "review_status": "machine_derived_pending_maintainer_review",
        "built_by": f"reva.kg.harvest map-countries ({HARVEST_VERSION}, agent WDHARVEST)",
        "note": "Extends registry/entities/country_crosswalk.json (unchanged): crosswalk key -> Wikidata QID.",
        "method": map_countries.__doc__.strip() if map_countries.__doc__ else "",
        "sources": {
            "wdqs": {"query_sha256": res.query_sha256, "raw_sha256": res.raw_sha256,
                     "retrieved_at": res.retrieved_at.isoformat()},
            "sitelink_lookups_raw_sha256": sitelink_raw,
            "labels_raw_sha256": list(lab.raw_sha256s),
            "licence": "CC0-1.0 (Wikidata)",
        },
        "crosswalk_sha256": hashlib.sha256(CROSSWALK.read_bytes()).hexdigest(),
        "stats": {"crosswalk_entities": len(ents), "mapped": len(countries), "unmapped": len(unmapped),
                  "by_method": {m: sum(1 for v in countries.values() if v["method"] == m)
                                for m in sorted({v["method"] for v in countries.values()})},
                  "distinct_qids": len({v["qid"] for v in countries.values()})},
        "countries": dict(sorted(countries.items())),
        "unmapped": unmapped,
    }
    out_path.write_text(json.dumps(doc_out, indent=1, ensure_ascii=False) + "\n")
    stats: dict[str, Any] = doc_out["stats"]
    return stats


SEARCH_MAX_CHARS = 290  # CirrusSearch rejects queries longer than 300 characters


def or_groups(prefix: str, terms: Sequence[str], suffix: str = "") -> list[str]:
    """Pack ``prefix + 't1|t2|...' + suffix`` search queries under the CirrusSearch length limit."""
    out: list[str] = []
    cur: list[str] = []
    for t in terms:
        if cur and len(prefix) + len("|".join([*cur, t])) + len(suffix) > SEARCH_MAX_CHARS:
            out.append(prefix + "|".join(cur) + suffix)
            cur = []
        cur.append(t)
    if cur:
        out.append(prefix + "|".join(cur) + suffix)
    return out


async def map_countries_search(client: WikidataClient, *, out_path: Path = COUNTRY_QIDS,
                               manual_titles: Mapping[str, str] | None = None,
                               extra_codes: Sequence[str] = ("TWN",)) -> dict[str, Any]:
    """Map every crosswalk entity to a QID through the Wikibase API only (no WDQS).

    Method ``P298``: CirrusSearch ``haswbstatement:P298=<a>|P298=<b>|...`` (OR groups under the 300-character
    query limit) gives every item holding one of the ISO 3166-1 alpha-3 codes; their snapshots are read with
    wbgetentities (props info|claims|labels) and each code is assigned from the items' own P298 statements:
    non-deprecated, without end time (P582), on items without dissolution date (P576). Several candidates ->
    the one classed as a country/sovereign state/territory (P31); still several -> left unmapped (listed).
    Codes outside ISO 3166 (CHI, XKX) use ``enwiki_sitelink`` with the stated title. ``extra_codes`` (TWN)
    are mapped even when the crosswalk does not list them yet (flagged ``not_in_crosswalk``).
    """
    cw = json.loads(CROSSWALK.read_text())
    ents = {e["key"]: e for e in cw["entities"]}
    codes = sorted({k for k, e in ents.items() if e.get("iso3")} | set(extra_codes))
    raws: list[str] = []
    cands: list[str] = []
    for q in or_groups("haswbstatement:", [f"P298={c}" for c in codes]):
        hits, _total, shas = await client.search_all(q, meta={"purpose": "country_qids"})
        raws.extend(shas)
        cands.extend(hits)
    cands = sorted(set(cands), key=lambda q: int(q[1:]))
    res = await client.get_entities(cands, props=("info", "claims", "labels"), languages=("en", "fr"),
                                    meta={"purpose": "country_qids"})
    raws.extend(res.raw_sha256s)
    by_code: dict[str, list[str]] = {}
    info: dict[str, dict[str, Any]] = {}
    for qid, ent in res.entities.items():
        claims = ent.get("claims") or {}
        dissolved = any(c.get("rank") != "deprecated" for c in claims.get("P576", []))
        classes = {((c.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id")
                   for c in claims.get("P31", []) if c.get("rank") != "deprecated"}
        info[qid] = {"classes": sorted(x for x in classes & set(COUNTRY_CLASSES)), "lastrevid": ent.get("lastrevid"),
                     "label_en": (ent.get("labels") or {}).get("en", {}).get("value"),
                     "label_fr": (ent.get("labels") or {}).get("fr", {}).get("value"),
                     "raw_sha256": res.raw_sha_by_id.get(qid)}
        if dissolved:
            continue
        for c in claims.get("P298", []):
            v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(v, str) and c.get("rank") != "deprecated" and not (c.get("qualifiers") or {}).get("P582"):
                by_code.setdefault(v, []).append(qid)
    countries: dict[str, Any] = {}
    ambiguous: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for code in codes:
        opts = sorted(set(by_code.get(code, [])), key=lambda q: int(q[1:]))
        pick, note = None, None
        if len(opts) == 1:
            pick = opts[0]
        elif len(opts) > 1:
            classed = [q for q in opts if info[q]["classes"]]
            if len(classed) == 1:
                pick, note = classed[0], "several items hold the code; kept the one classed as a country/territory"
            else:
                ambiguous[code] = opts
        if pick:
            countries[code] = {"qid": pick, "method": "P298", **{k: info[pick][k] for k in
                               ("label_en", "label_fr", "classes", "lastrevid", "raw_sha256")},
                               "crosswalk_label": (ents.get(code) or {}).get("label")}
            if note:
                countries[code].update(note=note, other_candidates=[q for q in opts if q != pick])
            if code not in ents:
                countries[code]["not_in_crosswalk"] = True
        else:
            unmapped.append(code)
    for code in sorted(ents):
        if code not in codes:
            unmapped.append(code)
    for code, title in (manual_titles or {}).items():
        if code not in unmapped:
            continue
        url = WBAPI_SITELINK.format(title=title.replace(" ", "_"))
        result, sha = await client._request(client.wbapi_http, url, endpoint="wbapi", accept="application/json",
                                            meta={"kind": "wbgetentities_sitelink", "title": title})
        raws.append(sha)
        doc = json.loads(result.body)
        found = [(q, e) for q, e in (doc.get("entities") or {}).items() if q.startswith("Q") and "missing" not in e]
        if len(found) == 1:
            q, e = found[0]
            countries[code] = {"qid": q, "method": "enwiki_sitelink", "enwiki_title": title,
                               "label_en": (e.get("labels") or {}).get("en", {}).get("value"),
                               "label_fr": (e.get("labels") or {}).get("fr", {}).get("value"),
                               "lastrevid": e.get("lastrevid"), "raw_sha256": sha,
                               "crosswalk_label": (ents.get(code) or {}).get("label")}
            unmapped.remove(code)
    dup: dict[str, list[str]] = {}
    for code, v in countries.items():
        dup.setdefault(v["qid"], []).append(code)
    doc_out: dict[str, Any] = {
        "schema": "reva-country-qids/1",
        "review_status": "machine_derived_pending_maintainer_review",
        "built_by": f"reva.kg.harvest map-countries ({HARVEST_VERSION}, agent WDHARVEST)",
        "note": "Extends registry/entities/country_crosswalk.json (unchanged): crosswalk key -> Wikidata QID.",
        "method": (map_countries_search.__doc__ or "").strip(),
        "sources": {"endpoint": "www.wikidata.org API (CirrusSearch + wbgetentities), CC0",
                    "retrieved_at": res.retrieved_at.isoformat(), "raw_sha256": raws},
        "crosswalk_sha256": hashlib.sha256(CROSSWALK.read_bytes()).hexdigest(),
        "stats": {"crosswalk_entities": len(ents), "mapped": len(countries),
                  "mapped_crosswalk": sum(1 for c in countries if c in ents), "unmapped": len(unmapped),
                  "by_method": {m: sum(1 for v in countries.values() if v["method"] == m)
                                for m in sorted({v["method"] for v in countries.values()})},
                  "distinct_qids": len(dup), "shared_qids": {q: c for q, c in dup.items() if len(c) > 1}},
        "countries": dict(sorted(countries.items())),
        "unmapped": sorted(unmapped),
        "ambiguous": ambiguous,
    }
    out_path.write_text(json.dumps(doc_out, indent=1, ensure_ascii=False) + "\n")
    stats: dict[str, Any] = doc_out["stats"]
    return stats


WBAPI_SITELINK = ("https://www.wikidata.org/w/api.php?action=wbgetentities&sites=enwiki&titles={title}"
                  "&props=info&format=json&maxlag=5")
MANUAL_TITLES = {"CHI": "Channel Islands", "XKX": "Kosovo"}


# --------------------------------------------------------------------------- property requests
PROPERTIES_FILE = REPO_ROOT / "registry" / "kg" / "properties.yaml"
REQUESTS_DIR = REPO_ROOT / "registry" / "kg" / "property_requests"
DATATYPE_KIND = {"wikibase-item": "entity", "time": "time", "quantity": "quantity",
                 "monolingualtext": "monolingualtext", "external-id": "external_id", "url": "url",
                 "globe-coordinate": "coordinate", "string": "string"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _q(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False) if v is not None else "null"


async def merge_property_requests(client: WikidataClient, *, path: Path = PROPERTIES_FILE,
                                  requests_dir: Path = REQUESTS_DIR) -> dict[str, Any]:
    """Merge ``property_requests/*.yaml`` into the allow-list after checking each id against Wikidata.

    A request is accepted when the property exists, its Wikidata datatype matches the requested ``kind``
    and the predicate names are valid and unused; labels are the property entity's own en/fr labels.
    """
    doc = yaml.safe_load(path.read_text())
    have = {p["property"] for p in doc["properties"]}
    have_q = {q["property"] for q in doc.get("qualifiers") or []}
    names = {p["reva_predicate"] for p in doc["properties"]}
    main_req: list[tuple[str, dict[str, Any]]] = []
    qual_req: list[tuple[str, dict[str, Any]]] = []
    for f in sorted(requests_dir.glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        items = yaml.safe_load(f.read_text()) or []
        if isinstance(items, dict):
            items = items.get("requests") or items.get("properties") or []
        for r in items:
            pid = str(r.get("property", ""))
            if r.get("role") == "qualifier":
                if pid not in have_q and pid not in {x[1]["property"] for x in qual_req}:
                    qual_req.append((f.stem, r))
            elif pid not in have and pid not in {x[1]["property"] for x in main_req}:
                main_req.append((f.stem, r))
    ids = [r["property"] for _, r in main_req + qual_req]
    result: dict[str, Any] = {"accepted": [], "rejected": [], "qualifiers_added": []}
    if not ids:
        return result
    res = await client.get_entities(ids, props=("labels", "datatype"), languages=("en", "fr"),
                                    meta={"purpose": "property_requests"})
    stamp = f"wbgetentities labels|datatype {res.retrieved_at.isoformat()} raw {','.join(x[:12] for x in res.raw_sha256s)}"
    blocks: list[str] = []
    for who, r in main_req:
        pid = r["property"]
        ent = res.entities.get(pid)
        if ent is None:
            result["rejected"].append({"property": pid, "by": who, "reason": "property does not exist on Wikidata"})
            continue
        kind = DATATYPE_KIND.get(str(ent.get("datatype")), "other")
        inv = r.get("inverse")
        why = None
        if kind != r.get("kind"):
            why = f"Wikidata datatype {ent.get('datatype')} -> kind {kind}, request says {r.get('kind')}"
        elif not NAME_RE.match(str(r.get("reva_predicate", ""))) or (inv and not NAME_RE.match(str(inv))):
            why = "invalid reva_predicate/inverse name"
        elif r["reva_predicate"] in names:
            why = f"reva_predicate {r['reva_predicate']} already used"
        elif kind != "entity" and inv:
            why = "inverse given for a literal-valued property"
        if why:
            result["rejected"].append({"property": pid, "by": who, "reason": why})
            continue
        names.add(r["reva_predicate"])
        en = (ent.get("labels") or {}).get("en", {}).get("value")
        fr = (ent.get("labels") or {}).get("fr", {}).get("value")
        lines = [f"  - property: {pid}", f"    reva_predicate: {r['reva_predicate']}", f"    inverse: {_q(inv)}",
                 f"    kind: {kind}", f"    group: {r.get('group') or 'requested'}",
                 f"    expected_en: {_q(r.get('label_en'))}", f"    label_en: {_q(en)}", f"    label_fr: {_q(fr)}",
                 f"    wikidata_datatype: {ent.get('datatype')}", f"    requested_by: {who.upper()}",
                 f"    why: {_q(r.get('why'))}"]
        if inv and inv == r["reva_predicate"]:
            lines.append("    symmetric: true")
        blocks.append("\n".join(lines))
        result["accepted"].append({"property": pid, "by": who, "label_en": en,
                                   "label_matches_request": en == r.get("label_en")})
    qlines: list[str] = []
    for who, r in qual_req:
        pid = r["property"]
        ent = res.entities.get(pid)
        if ent is None:
            result["rejected"].append({"property": pid, "by": who, "reason": "qualifier property does not exist"})
            continue
        en = (ent.get("labels") or {}).get("en", {}).get("value")
        fr = (ent.get("labels") or {}).get("fr", {}).get("value")
        qlines.append(f"  - {{property: {pid}, expected_en: {_q(r.get('label_en'))}, label_en: {_q(en)}, "
                      f"label_fr: {_q(fr)}, requested_by: {who.upper()}}}")
        result["qualifiers_added"].append(pid)
    text_lines = path.read_text().splitlines()
    qi = next(i for i, ln in enumerate(text_lines) if ln.startswith("# Qualifiers kept"))
    ri = next(i for i, ln in enumerate(text_lines) if ln.startswith("# Reference properties kept"))
    new = (text_lines[:qi] + ([f"# merged property requests ({stamp})", *blocks] if blocks else [])
           + text_lines[qi:ri] + qlines + text_lines[ri:])
    path.write_text("\n".join(new) + "\n")
    load_allowlist(path)  # parses and has no duplicates
    return result


# --------------------------------------------------------------------------- CLI
def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _budget_guard(fn: Callable[[], Any]) -> int:
    """Run a live command; a daily budget refusal is a typed outcome (exit 3), never a success."""
    try:
        return int(asyncio.run(fn()))
    except BudgetExhausted as e:
        _print({"status": "budget_exhausted", "message": e.message, **e.details})
        return 3


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reva.kg.harvest", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate specs (no network)")
    v.add_argument("paths", nargs="*", type=Path)
    t = sub.add_parser("try", help="run the first chunk of one spec with a small LIMIT (no DB writes)")
    t.add_argument("spec", type=Path)
    t.add_argument("--limit", type=int, default=10)
    st = sub.add_parser("status", help="harvest state per spec")
    st.add_argument("paths", nargs="*", type=Path)
    sub.add_parser("merge-property-requests", help="merge registry/kg/property_requests into the allow-list")
    mc = sub.add_parser("map-countries", help="write registry/entities/country_qids.json (crosswalk -> QID)")
    mc.add_argument("--via-wdqs", action="store_true", help="one SPARQL query instead of per-code API searches")
    r = sub.add_parser("run", help="run specs into a throwaway database (never 'reva'), budget-metered")
    r.add_argument("paths", nargs="*", type=Path)
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--force", action="store_true", help="re-run specs already completed with the same sha256")
    r.add_argument("--planned-wdqs", type=int, default=50)
    r.add_argument("--planned-wbapi", type=int, default=100)
    r.add_argument("--via", choices=("wdqs", "api"), default="wdqs",
                   help="api: select seeds with the specs' api_search blocks (CirrusSearch) instead of WDQS")
    r.add_argument("--no-expand", action="store_true", help="skip label snapshots of object entities")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    allow = load_allowlist()
    if args.cmd == "validate":
        specs = discover_specs(args.paths or None)
        res = validate_all(specs, allow)
        bad = {k: v for k, v in res.items() if v}
        _print({"specs": len(res), "valid": len(res) - len(bad), "invalid": bad})
        return 1 if bad else 0
    if args.cmd == "status":
        specs = discover_specs(args.paths or None)
        _print({s.id: {k: v for k, v in HarvestState(STATE_DIR, s.id).data.items()
                       if k in ("status", "rows", "finished_at", "report")} | {"spec_changed":
                       HarvestState(STATE_DIR, s.id).data.get("spec_sha256") not in (None, s.sha256)}
                for s in specs})
        return 0
    if args.cmd == "try":
        if args.limit > 50:
            print("--limit is capped at 50", file=sys.stderr)
        spec = load_spec(args.spec)
        errs = validate_spec(spec, allow, known_ids=[s.id for s in discover_specs()])
        if errs:
            _print({"spec": spec.id, "invalid": errs})
            return 1

        async def go() -> int:
            async with budgeted_client(purpose="try", planned_wdqs=1, planned_wbapi=0) as sess:
                return await _try(sess.client)

        async def _try(client: WikidataClient) -> int:
            try:
                sink = NullSink()
                h = Harvester(client, sink, allow=allow)
                chunks = render_chunks(spec)
                res = await client.sparql(with_limit(chunks[0].query, min(args.limit, 50)),
                                          meta={"spec": spec.id, "mode": "try"})
                rows = [{k: v.get("value") for k, v in b.items()} for b in res.bindings]
                out: dict[str, Any] = {"spec": spec.id, "chunk": chunks[0].key, "chunks": len(chunks),
                                       "rows": len(rows), "sample": rows[:min(args.limit, 50)],
                                       "elapsed_seconds": round(res.elapsed_seconds, 2),
                                       "raw_sha256": res.raw_sha256}
                if spec.data["purpose"] == "statements":
                    stmts, skipped = h.rows_to_statements(spec, res)
                    out["statements"] = len([s for s in stmts if s])
                    out["rows_skipped"] = skipped
                _print(out)
                return 0
            except WikidataError as e:
                _print({"spec": spec.id, "error": e.code, "message": e.message})
                return 1

        return _budget_guard(go)
    if args.cmd == "run":
        try:
            run_out = asyncio.run(run_into_db(args.paths or None, db_name=args.db, force=args.force,
                                              planned_wdqs=args.planned_wdqs,
                                              planned_wbapi=args.planned_wbapi, via=args.via,
                                              expand=not args.no_expand))
        except BudgetExhausted as e:
            _print({"status": "budget_exhausted", "message": e.message, **e.details})
            return 3
        keys = ("spec_id", "status", "rows", "seeds", "entities_full", "entities_labels", "statements", "requests",
                "bytes", "seconds", "failures")
        spec_reports: list[dict[str, Any]] = run_out["specs"]
        _print({k: v for k, v in run_out.items() if k != "specs"}
               | {"specs": [{k: r[k] for k in keys} for r in spec_reports]})
        return 0 if all(r["status"] in ("done", "skipped") for r in spec_reports) else 1
    if args.cmd == "map-countries":
        async def mapc() -> int:
            async with budgeted_client(purpose="mapcountries", planned_wdqs=0, planned_wbapi=30) as sess:
                if args.via_wdqs:
                    _print(await map_countries(sess.client, manual_titles=MANUAL_TITLES))
                else:
                    _print(await map_countries_search(sess.client, manual_titles=MANUAL_TITLES))
            _print({"budget": sess.usage.get("budget")})
            return 0

        return _budget_guard(mapc)
    if args.cmd == "merge-property-requests":
        async def merge() -> int:
            async with budgeted_client(purpose="properties", planned_wdqs=0, planned_wbapi=2) as sess:
                _print(await merge_property_requests(sess.client))
            return 0

        return _budget_guard(merge)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
