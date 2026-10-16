"""Governed import of archived Wikidata harvests into a KG database (agent LOADALL, owner decision 2026-10-16).

The Wikidata harvests (WDHARVEST ``reva.kg.harvest``, CAUSWD ``reva.causal_harvest``) load into *throwaway*
databases (``reva_kg_wdharvest``, ``reva_kg_causwd``); the dev database ``reva`` is refused by name there. This
module is the one governed path that brings those statements into another KG database (by default the one the
settings point at, i.e. ``reva``):

* **Never row copies.** The source database is read only as an *index*: its ``kg.load`` rows say which archived
  raw responses were loaded, when they were retrieved and with which harvester parameters. Every entity and
  statement is **re-derived from the archived raw Wikidata response** (``var/kg/objects``, content-addressed,
  gzip) with the harvester's own extraction code, and written with :func:`reva.kg.model.load_batch` (through
  :class:`reva.kg.harvest.KgDbSink`): statement ids, revisions (``lastrevid``), ranks, qualifiers, references,
  ``raw_sha256`` and retrieval time are those of the archived response, and the KG integrity triggers of the
  target apply unchanged.
* **Raw bytes are verified, then copied into the target's object store** (same sha256: the archive stores the
  gzip object and its sha256 is the artifact address statements cite), so the target's ``meta.artifact`` rows
  point at bytes the target's own store holds. A raw object whose bytes do not hash to its address, or whose
  decompressed body does not match ``body_sha256`` in ``var/kg/archive_index.jsonl``, refuses its load.
* **Extraction profiles** (from ``kg.load.parameters.harvester``):

  - ``wdharvest/1.0.0``: :func:`~reva.adapters.connectors.wikidata.extract_entity` with the spec's
    ``entity_kind`` and ``properties`` (labels-mode loads: ``unclassified``); Special:EntityData bodies are pruned
    to the requested props/languages exactly as :class:`~reva.adapters.connectors.wikidata.WikidataClient` does;
    seed specs discovered with ``whatlinkshere`` keep only items whose own P31 contains the class (the
    harvester's ``require_p31``). Labels-mode loads of a statements spec with an ``entity_kind`` are skipped
    (``ambiguous_labels_kind``: subjects and objects share the mode but not the kind).
  - ``causwd/1.0.0``: kind ``unclassified``, ``core.HARVEST_PIDS``, then
    :func:`~reva.causal_harvest.core.enrich_causal_statements` (full references/qualifiers on causal
    statements); ``entitydata_archived`` bodies were not pruned by CAUSWD and are not pruned here.
  - anything else (e.g. ``wikidata.wdqs`` row loads) is skipped and reported, never guessed.
* **Allow-list**: the current ``registry/kg/properties.yaml`` (registered in the target first); a load harvested
  under another allow-list sha256 is counted ``allowlist_changed`` in the report.
* **Idempotent.** Each import carries ``parameters.import.import_key`` = sha256 of (importer version, source key,
  raw sha256s, retrieval time, extraction profile); a load whose key is already in the target is skipped without
  writing anything, and ``load_batch`` is itself idempotent per (statement key, origin, revision).
* **Verification** (``verify=True``): compares, for every statement key of the imported sources, the source
  database's current ``content_sha256`` with the target's (``identical`` / ``differing`` / ``missing``) — read
  only, a check that the re-derivation reproduced the harvest, nothing is copied from it.

Two profiles can derive different content for the same Wikidata statement (CAUSWD keeps more reference
properties on causal statements). ``load_batch`` keys statements by Wikidata GUID + revision, so the second
import of such a statement is reported ``conflicting`` and not stored — import the richer profile first when
that matters.

CLI: ``reva kg import-harvest --from-db reva_kg_wdharvest [--sources 'wikidata*'] [--dry-run] [--no-verify]``
(or ``python -m reva.kg.import_harvest``).
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import logging
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from reva.adapters.connectors.wikidata import (
    ENTITY_PROPS,
    LABEL_PROPS,
    LANGUAGES,
    AllowList,
    WdEntity,
    _prune,
    extract_entity,
    load_allowlist,
)
from reva.kernel.errors import IntegrityFailure, RevaError

log = logging.getLogger("reva.kg.import_harvest")

IMPORT_VERSION = "kg-import-harvest/1"
WDHARVEST = "wdharvest/1.0.0"
CAUSWD = "causwd/1.0.0"
SUPPORTED_SOURCE_KEYS = frozenset({"wikidata.wbapi"})
DEFAULT_SOURCES = ("wikidata*",)
GZIP_MAGIC = b"\x1f\x8b"


class ImportRefused(RevaError):
    code = "kg_import_refused"
    http_status = 422


@dataclass(frozen=True)
class SourceLoad:
    """One ``kg.load`` row of the source database (the index entry)."""

    id: int
    source_key: str
    origin: str
    raw_sha256s: tuple[str, ...]
    retrieved_at: datetime
    parameters: dict[str, Any]


@dataclass
class ImportReport:
    from_db: str
    loads_seen: int = 0
    loads_imported: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    notes: Counter[str] = field(default_factory=Counter)
    totals: Counter[str] = field(default_factory=Counter)
    raw_copied: int = 0
    raw_present: int = 0
    entities_derived: int = 0
    statements_derived: int = 0
    verification: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_db": self.from_db,
            "importer": IMPORT_VERSION,
            "loads_seen": self.loads_seen,
            "loads_imported": self.loads_imported,
            "skipped": dict(self.skipped),
            "notes": dict(self.notes),
            "raw_copied": self.raw_copied,
            "raw_present": self.raw_present,
            "entities_derived": self.entities_derived,
            "statements_derived": self.statements_derived,
            "load_totals": dict(self.totals),
            "verification": self.verification,
            "errors": self.errors[:50],
        }


# --------------------------------------------------------------------------------------------- index
def source_loads(engine: Engine, *, sources: Sequence[str] = DEFAULT_SOURCES) -> list[SourceLoad]:
    """The source database's ``kg.load`` rows whose source key matches one of ``sources`` (fnmatch), in order."""
    with engine.connect() as c:
        rows = c.execute(
            text("""SELECT id, source_key, origin, raw_sha256s, retrieved_at, parameters
                                   FROM kg.load ORDER BY id""")
        ).all()
    return [
        SourceLoad(int(r.id), r.source_key, r.origin, tuple(r.raw_sha256s), r.retrieved_at, dict(r.parameters))
        for r in rows
        if any(fnmatch.fnmatchcase(r.source_key, p) for p in sources)
    ]


def archive_body_shas(index_path: Path | None) -> dict[str, str]:
    """stored sha256 -> provider body sha256 from ``archive_index.jsonl`` (entries that record it)."""
    out: dict[str, str] = {}
    if index_path is None or not index_path.exists():
        return out
    with index_path.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("sha256") and e.get("body_sha256"):
                out[str(e["sha256"])] = str(e["body_sha256"])
    return out


def read_raw(archive: Any, sha: str, body_shas: Mapping[str, str]) -> tuple[bytes, dict[str, Any]]:
    """Archived object bytes (verified against their address) and the decoded JSON body (verified against
    ``body_sha256`` when the index records it)."""
    data = archive.get("raw", sha)
    if hashlib.sha256(data).hexdigest() != sha:
        raise IntegrityFailure("archived raw object does not hash to its address", sha256=sha)
    body = gzip.decompress(data) if data[:2] == GZIP_MAGIC else data
    want = body_shas.get(sha)
    if want is not None and hashlib.sha256(body).hexdigest() != want:
        raise IntegrityFailure("archived raw body does not match the archive index body_sha256", sha256=sha)
    doc = json.loads(body)
    if not isinstance(doc, dict) or not isinstance(doc.get("entities"), dict):
        raise ImportRefused("archived raw object is not a Wikibase entities document", sha256=sha)
    return data, doc


# --------------------------------------------------------------------------------------------- profiles
@dataclass(frozen=True)
class Profile:
    name: str
    props: tuple[str, ...]
    prune: bool
    kind: str | None
    properties: tuple[str, ...] | None
    require_p31: frozenset[str] | None
    enrich: Callable[[WdEntity, Mapping[str, Any], AllowList], WdEntity] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "props": list(self.props),
            "prune": self.prune,
            "kind": self.kind,
            "properties": list(self.properties) if self.properties else None,
            "require_p31": sorted(self.require_p31) if self.require_p31 is not None else None,
            "enrich": self.enrich is not None,
        }


def profile_for(load: SourceLoad, specs: Mapping[str, Any]) -> Profile | str:
    """The extraction the harvester applied to this load, or a skip reason."""
    from reva.kg.harvest import LABEL_KIND

    p = load.parameters
    if load.source_key not in SUPPORTED_SOURCE_KEYS:
        return "unsupported_source_key"
    harvester = p.get("harvester")
    if harvester == WDHARVEST:
        spec = specs.get(str(p.get("spec")))
        if spec is None:
            return "unknown_spec"
        mode = p.get("mode")
        if mode == "full":
            a = spec.get("api_search") or {}
            # WDHARVEST's seed runs used --via api; whatlinkshere candidates are filtered on their own P31
            req = (
                frozenset(a.get("instance_of") or [])
                if (spec.data.get("purpose") == "seed_entities" and a.get("discover") == "whatlinkshere")
                else None
            )
            props = spec.get("properties")
            return Profile(
                WDHARVEST, ENTITY_PROPS, True, spec.get("entity_kind"), tuple(props) if props else None, req, None
            )
        if mode == "labels":
            # seed specs: label snapshots are expand_hops objects (kind 'unclassified'); statements specs label
            # their subjects with entity_kind and their objects 'unclassified' under the same mode: ambiguous
            if spec.data.get("purpose") != "seed_entities" and spec.get("entity_kind"):
                return "ambiguous_labels_kind"
            return Profile(WDHARVEST, LABEL_PROPS, True, LABEL_KIND, None, None, None)
        return "unknown_mode"
    if harvester == CAUSWD:
        from reva.causal_harvest import core

        mode = p.get("mode")
        if mode not in ("wbgetentities", "entitydata_archived"):
            return "unknown_mode"
        return Profile(
            CAUSWD,
            ENTITY_PROPS,
            mode != "entitydata_archived",
            "unclassified",
            tuple(core.HARVEST_PIDS),
            None,
            core.enrich_causal_statements,
        )
    return "unknown_harvester"


def import_key(load: SourceLoad, prof: Profile) -> str:
    payload = {
        "importer": IMPORT_VERSION,
        "source_key": load.source_key,
        "origin": load.origin,
        "raw_sha256s": sorted(load.raw_sha256s),
        "retrieved_at": load.retrieved_at.isoformat(),
        "profile": prof.as_dict(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def derive_entities(
    docs: Mapping[str, Mapping[str, Any]], prof: Profile, allow: AllowList, *, retrieved_at: datetime
) -> tuple[list[WdEntity], Counter[str]]:
    """Entities (with statements) of the load's archived documents, exactly as the harvester extracted them."""
    ents: dict[str, WdEntity] = {}
    notes: Counter[str] = Counter()
    for sha, doc in docs.items():
        for key, ent in (doc.get("entities") or {}).items():
            if not isinstance(ent, dict) or "missing" in ent:
                notes["missing_entity"] += 1
                continue
            raw = _prune(ent, prof.props, LANGUAGES) if prof.prune else ent
            qid = str(raw.get("id", key))
            if not qid.startswith("Q"):
                continue
            e = extract_entity(
                raw, allow, raw_sha256=sha, retrieved_at=retrieved_at, kind_hint=prof.kind, properties=prof.properties
            )
            if prof.enrich is not None:
                e = prof.enrich(e, raw, allow)
            if prof.require_p31 is not None and not (set(e.instance_of) & prof.require_p31):
                notes["candidate_rejected_p31"] += 1
                continue
            if qid in ents:
                notes["entity_in_several_raws"] += 1
            ents[qid] = e
    return list(ents.values()), notes


# --------------------------------------------------------------------------------------------- import
def _imported_keys(engine: Engine) -> set[str]:
    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT parameters->'import'->>'import_key' FROM kg.load
                                       WHERE parameters ? 'import'""")
            ).scalars()
        )


def import_harvest(
    from_engine: Engine,
    to_engine: Engine,
    *,
    archive: Any,
    to_store: Any,
    from_db: str,
    index_path: Path | None = None,
    sources: Sequence[str] = DEFAULT_SOURCES,
    specs: Mapping[str, Any] | None = None,
    allow: AllowList | None = None,
    dry_run: bool = False,
    verify: bool = True,
) -> ImportReport:
    """Re-load the source database's archived Wikidata loads into the target (see the module docstring)."""
    from reva.kg.harvest import Batch, KgDbSink, discover_specs

    allow = allow or load_allowlist()
    if specs is None:
        specs = {s.id: s for s in discover_specs(None)}
    rep = ImportReport(from_db=from_db)
    loads = source_loads(from_engine, sources=sources)
    rep.loads_seen = len(loads)
    body_shas = archive_body_shas(index_path)
    sinks: dict[str, Any] = {}
    sink = sinks.setdefault("live", KgDbSink(to_engine, to_store, origin="live"))
    done = _imported_keys(to_engine)
    if not dry_run:
        rep.notes.update({f"properties_{k}": v for k, v in sink.register_properties(allow).items()})
    for load in loads:
        prof = profile_for(load, specs)
        if isinstance(prof, str):
            rep.skipped[prof] += 1
            continue
        key = import_key(load, prof)
        if key in done:
            rep.skipped["already_imported"] += 1
            continue
        if load.parameters.get("allowlist_sha256") not in (None, allow.sha256):
            rep.notes["allowlist_changed"] += 1
        docs: dict[str, dict[str, Any]] = {}
        blobs: dict[str, bytes] = {}
        try:
            for sha in load.raw_sha256s:
                blobs[sha], docs[sha] = read_raw(archive, sha, body_shas)
        except (IntegrityFailure, ImportRefused, FileNotFoundError, KeyError, ValueError) as e:
            rep.skipped["raw_unusable"] += 1
            rep.errors.append({"load": load.id, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        ents, notes = derive_entities(docs, prof, allow, retrieved_at=load.retrieved_at)
        rep.notes.update(notes)
        if not ents:
            rep.skipped["no_entities"] += 1
            continue
        stmts = [s for e in ents for s in e.statements]
        rep.entities_derived += len(ents)
        rep.statements_derived += len(stmts)
        if dry_run:
            continue
        for sha, data in blobs.items():
            if to_store.head("raw", sha) is not None:
                rep.raw_present += 1
                continue
            stored = to_store.put_bytes(data, "raw", "application/gzip")
            if stored.sha256 != sha:
                raise IntegrityFailure("copied raw object changed address", sha256=sha, got=stored.sha256)
            rep.raw_copied += 1
        params = dict(load.parameters)
        params["import"] = {
            "importer": IMPORT_VERSION,
            "import_key": key,
            "from_db": from_db,
            "from_load_id": load.id,
            "profile": prof.as_dict(),
            "allowlist_sha256": allow.sha256,
        }
        sink = sinks.setdefault(load.origin, KgDbSink(to_engine, to_store, origin=load.origin))
        out = sink.load(Batch(load.source_key, ents, stmts, list(load.raw_sha256s), load.retrieved_at, params))
        done.add(key)
        rep.loads_imported += 1
        for k, v in out.items():
            if isinstance(v, int) and k != "load_id":
                rep.totals[k] += v
        for k, v in (out.get("rejected_counts") or {}).items():
            rep.totals[f"rejected:{k}"] += int(v)
    if verify and not dry_run:
        rep.verification = verify_import(from_engine, to_engine, sources=sources)
    return rep


def verify_import(
    from_engine: Engine, to_engine: Engine, *, sources: Sequence[str] = DEFAULT_SOURCES, chunk: int = 20_000
) -> dict[str, Any]:
    """Compare current statement content hashes (source vs target) for the imported source keys."""
    keys = [k for k in SUPPORTED_SOURCE_KEYS if any(fnmatch.fnmatchcase(k, p) for p in sources)]
    with from_engine.connect() as c:
        src: dict[str, str] = dict(
            c.execute(
                text("""SELECT statement_key, content_sha256 FROM kg.statement_current
                                       WHERE source_key = ANY(:k)"""),
                {"k": keys},
            ).all()
        )
    same = differ = missing = 0
    sample: list[str] = []
    items = sorted(src.items())
    with to_engine.connect() as c:
        for i in range(0, len(items), chunk):
            part = dict(items[i : i + chunk])
            got: dict[str, str] = dict(
                c.execute(
                    text("""SELECT statement_key, content_sha256 FROM kg.statement_current
                                           WHERE statement_key = ANY(:k)"""),
                    {"k": list(part)},
                ).all()
            )
            for k, h in part.items():
                if k not in got:
                    missing += 1
                elif got[k] == h:
                    same += 1
                else:
                    differ += 1
                    if len(sample) < 10:
                        sample.append(k)
    return {
        "source_current_statements": len(src),
        "identical": same,
        "differing": differ,
        "missing_in_target": missing,
        "differing_sample": sample,
    }


# --------------------------------------------------------------------------------------------- CLI
def run(
    from_db: str, *, sources: Sequence[str] = DEFAULT_SOURCES, dry_run: bool = False, verify: bool = True
) -> dict[str, Any]:
    """Import ``from_db`` (a throwaway ``reva_*`` harvest database) into the configured database."""
    from reva.adapters.connectors.wikidata import REPO_ROOT
    from reva.adapters.objects.factory import get_store
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.kg.harvest import OBJECT_ROOT, harvest_settings
    from reva.settings import get_settings

    src_settings = harvest_settings(from_db)  # refuses reva/postgres/template* and non reva_* names
    target = get_settings()
    if target.reva_db_name == from_db:
        raise ImportRefused("source and target are the same database", database=from_db)
    from_engine = make_engine("worker", src_settings)
    to_engine = make_engine("worker", target)
    try:
        rep = import_harvest(
            from_engine,
            to_engine,
            archive=FilesystemArtifactStore(OBJECT_ROOT, src_settings.reva_s3_prefix),
            to_store=get_store(),
            from_db=from_db,
            index_path=REPO_ROOT / "var" / "kg" / "archive_index.jsonl",
            sources=sources,
            dry_run=dry_run,
            verify=verify,
        )
    finally:
        from_engine.dispose()
        to_engine.dispose()
    return {"target": target.reva_db_name, **rep.as_dict()}


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reva.kg.import_harvest", description=__doc__.split("\n")[0])
    ap.add_argument("--from-db", required=True, help="throwaway harvest database (reva_kg_wdharvest, ...)")
    ap.add_argument("--sources", action="append", default=None, help="kg.load source_key glob (default wikidata*)")
    ap.add_argument("--dry-run", action="store_true", help="derive and count, write nothing")
    ap.add_argument("--no-verify", action="store_true", help="skip the source/target content comparison")
    a = ap.parse_args(list(argv) if argv is not None else None)
    out = run(a.from_db, sources=tuple(a.sources or DEFAULT_SOURCES), dry_run=a.dry_run, verify=not a.no_verify)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
