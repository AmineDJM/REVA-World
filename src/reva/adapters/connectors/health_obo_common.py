"""Shared plumbing of the V4HEALTH ontology connectors (UBERON, Human Disease Ontology, Mondo) and ClinGen.

Nothing here makes a request: connectors fetch through ``run.get`` only (atlas platform, invariant 30).

What lives here
---------------
* :func:`parse_obo` -- a small, strict OBO 1.2/1.4 flat-file reader (stanzas, tags, quoted strings, trailing
  qualifiers ``{...}`` and comments ``! ...``). Nothing is normalised beyond splitting the syntax.
* :func:`curie` / :func:`kg_id` -- identifiers exactly as the files publish them (``UBERON:0000948``,
  ``http://purl.obolibrary.org/obo/DOID_399``, ``http://identifiers.org/hgnc/4800``) -> CURIE -> KG id
  (``uberon:0000948``, ``doid:399``, ``hgnc:4800``). Never a name.
* :class:`RelationIndex` -- the Relation Ontology (``ro.obo``, archived by each run) plus the ontology's own
  relation stanzas: labels, verbatim definitions, ``is_a`` closure. :meth:`RelationIndex.is_causal` is the
  **causal decision rule** (docs/dev/v4_v4health.md, "Causal axioms"): a relation is causal when RO classifies it
  under ``RO:0002410`` "causally related to" (or the ontology defines it as causation, e.g. DOID's
  ``IDO:0000664`` "has material basis in" -- "Relating the disease to it's causation"), or when its own label
  carries a causal marker. A causal relation never becomes a KG link: it is delivered to the causal loader as an
  attributed T1 claim whose codebook definition is quoted verbatim from the archived file.
* :func:`load_predicates` / :func:`load_kg` -- ``registry/kg/predicates_health.yaml`` and a bounded, retracting
  KG loader (same contract as the language layer's).
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import SourceResponseError

REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_health.yaml"

RO_URL = "https://raw.githubusercontent.com/oborel/obo-relations/master/ro.obo"
RO_HOST = "raw.githubusercontent.com"
CAUSAL_ROOT = "RO:0002410"  # "causally related to"
#: Label markers of a causal relation (a superset of V4CORE's never_map markers): a relation whose label carries
#: one is never loaded as a plain KG link, even when RO does not place it under "causally related to".
CAUSAL_MARKERS = (
    "cause",
    "causal",
    "causation",
    "effect",
    "material basis",
    "basis in",
    "risk factor",
    "induce",
    "leads to",
    "predispos",
    "trigger",
    "driver",
    "susceptib",
    "arises from",
)
#: Causal relations whose SUBJECT (the disease) is the cause ("disease causes disruption of <process>").
SUBJECT_IS_CAUSE = frozenset({"RO:0004024", "RO:0004025"})
#: Causal relations read as "contributing factor" (a susceptibility factor), the others as "has_cause".
CONTRIBUTING = frozenset({"RO:0004005"})

#: KG kinds by CURIE prefix (entity kind = what the ontology file says the term is a class of).
KIND_BY_PREFIX: dict[str, str] = {
    "UBERON": "anatomical_structure",
    "CL": "cell_type",
    "DOID": "disease",
    "MONDO": "disease",
    "NCBITaxon": "taxon",
    "SYMP": "symptom",
    "HP": "phenotype",
    "GENO": "genetic_inheritance_pattern",
    "SO": "sequence_feature",
    "TRANS": "transmission_process",
    "FOODON": "food_material",
    "DISDRIV": "disease_driver",
}

_OBO_IRI = re.compile(r"^https?://purl\.obolibrary\.org/obo/([A-Za-z][A-Za-z0-9]*)_([A-Za-z0-9_.-]+)$")
_HGNC_IRI = re.compile(r"^https?://identifiers\.org/hgnc/([0-9]+)$")
_CURIE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*):([A-Za-z0-9_.:-]+)$")


class HealthSourceError(SourceResponseError):
    pass


def drift(msg: str, kind: str = "schema_drift") -> HealthSourceError:
    return HealthSourceError(msg, retryable=False, kind=kind)


def digest(*objs: Any, files: Sequence[Path] = (PREDICATES_FILE,)) -> str:
    """Code digest of the modules defining ``objs`` plus the registry files they read."""
    h = hashlib.sha256()
    for o in objs:
        h.update(inspect.getsource(inspect.getmodule(o)).encode())  # type: ignore[arg-type]
    for f in files:
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


# ============================================================================================== identifiers
def curie(ref: str) -> str | None:
    """CURIE of an identifier as published (OBO PURL, identifiers.org HGNC IRI or CURIE); ``None`` otherwise."""
    ref = ref.strip()
    m = _OBO_IRI.match(ref)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    m = _HGNC_IRI.match(ref)
    if m:
        return f"HGNC:{m.group(1)}"
    m = _CURIE.match(ref)
    if m and not ref.startswith(("http:", "https:")):
        return ref
    return None


def prefix(c: str) -> str:
    return c.split(":", 1)[0]


def kg_id(c: str) -> str:
    """KG entity id of a CURIE: lower-cased prefix (``NCBITaxon:1773`` -> ``ncbitaxon:1773``)."""
    p, _, local = c.partition(":")
    return f"{p.lower()}:{local}"


# ============================================================================================== OBO reader
@dataclass
class OboStanza:
    kind: str  # Term | Typedef | Instance
    id: str
    line: int
    #: (tag, value, rest): rest = the trailing ``{...}`` qualifier block of an unquoted value, or everything after
    #: the closing quote of a quoted value (synonym scope and xrefs, def xrefs)
    tags: list[tuple[str, str, str]] = field(default_factory=list)

    def values(self, tag: str) -> list[str]:
        return [v for t, v, _ in self.tags if t == tag]

    def first(self, tag: str) -> str | None:
        vs = self.values(tag)
        return vs[0] if vs else None

    def tagged(self, tag: str) -> list[tuple[str, str]]:
        return [(v, q) for t, v, q in self.tags if t == tag]

    @property
    def obsolete(self) -> bool:
        return self.first("is_obsolete") == "true"


def unquote(v: str) -> tuple[str, str]:
    """``"text with \\" escapes" rest`` -> (text, rest). Raises on an unterminated string."""
    if not v.startswith('"'):
        raise drift(f"OBO quoted value expected: {v[:60]!r}")
    out: list[str] = []
    i = 1
    while i < len(v):
        ch = v[i]
        if ch == "\\" and i + 1 < len(v):
            nxt = v[i + 1]
            out.append({"n": "\n", "t": "\t", "W": " "}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(out), v[i + 1 :].strip()
        out.append(ch)
        i += 1
    raise drift(f"OBO unterminated quoted value: {v[:60]!r}")


def _split_value(tag: str, raw: str) -> tuple[str, str]:
    """(value, trailing qualifier block) of an OBO tag value; comments (`` ! ...``) removed."""
    raw = raw.strip()
    if raw.startswith('"'):
        # quoted (def, synonym, property_value ...): the third slot keeps what follows the string verbatim
        # (synonym scope + xrefs, def xrefs, qualifiers), comments removed
        text_, rest = unquote(raw)
        return text_, re.sub(r"\s!\s.*$", "", rest).strip()
    # unquoted: strip comment first (a '!' is never part of an OBO identifier), then trailing qualifiers
    body = re.sub(r"\s!\s.*$", "", raw)
    body = re.sub(r"\s!$", "", body)
    quals = ""
    m = re.search(r"\s*\{(.*)\}\s*$", body)
    if m:
        quals = m.group(1)
        body = body[: m.start()].rstrip()
    return body.strip(), quals


def parse_obo(body: bytes, *, what: str) -> tuple[dict[str, list[str]], list[OboStanza]]:
    """Header tags and stanzas of an OBO file. Structural problems raise :class:`HealthSourceError`."""
    try:
        text_ = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise drift(f"{what}: not UTF-8") from e
    header: dict[str, list[str]] = {}
    stanzas: list[OboStanza] = []
    pending_kind: str | None = None
    pending_line = 0
    tags: list[tuple[str, str, str]] = []
    for n, line in enumerate(text_.splitlines(), start=1):
        if not line.strip():
            continue
        if line.startswith("["):
            if pending_kind is not None:
                stanzas.append(_close(pending_kind, pending_line, tags, what))
            pending_kind, pending_line, tags = line.strip()[1:-1], n, []
            continue
        tag, sep, raw = line.partition(":")
        if not sep:
            raise drift(f"{what}: line {n} is not 'tag: value'")
        tag = tag.strip()
        try:
            value, quals = _split_value(tag, raw)
        except HealthSourceError as e:
            raise drift(f"{what}: line {n}: {e.message}") from e
        if pending_kind is None:
            header.setdefault(tag, []).append(value)
        else:
            tags.append((tag, value, quals))
    if pending_kind is not None:
        stanzas.append(_close(pending_kind, pending_line, tags, what))
    if "format-version" not in header:
        raise drift(f"{what}: no OBO format-version header")
    return header, stanzas


def _close(kind: str | None, line: int, tags: list[tuple[str, str, str]], what: str) -> OboStanza:
    ids = [v for t, v, _ in tags if t == "id"]
    if len(ids) != 1:
        raise drift(f"{what}: stanza at line {line} has {len(ids)} ids")
    return OboStanza(kind or "", ids[0], line, list(tags))


def data_version(header: Mapping[str, list[str]], what: str) -> str:
    v = (header.get("data-version") or [None])[0]
    if not v:
        raise drift(f"{what}: no data-version header")
    return str(v)


def relationship(value: str) -> tuple[str, str]:
    """``part_of UBERON:0000062`` -> (relation, target)."""
    parts = value.split()
    if len(parts) != 2:
        raise drift(f"OBO relationship value is not '<relation> <target>': {value[:80]!r}")
    return parts[0], parts[1]


def exact_synonyms(st: OboStanza) -> list[str]:
    """EXACT synonyms of a stanza (scope read from what follows the quoted text)."""
    return [v for v, rest in st.tagged("synonym") if rest.split(" ", 1)[0] == "EXACT"]


# ============================================================================================== relations
@dataclass
class Relation:
    id: str  # CURIE (RO:0004026) or the ontology's shorthand (disease_has_location)
    name: str | None
    definition: str | None
    is_a: tuple[str, ...]
    xrefs: tuple[str, ...]
    document: str  # which archived file the stanza / node comes from


@dataclass
class RelationIndex:
    """RO + the ontology's own relation declarations. ``documents``: key -> {sha256, url, title}."""

    relations: dict[str, Relation] = field(default_factory=dict)
    documents: dict[str, dict[str, str]] = field(default_factory=dict)

    def add_obo(self, stanzas: Iterable[OboStanza], *, document: str) -> None:
        for st in stanzas:
            if st.kind != "Typedef":
                continue
            d = st.first("def")
            self.relations.setdefault(
                st.id,
                Relation(
                    st.id,
                    st.first("name"),
                    d if d else None,
                    tuple(v for v in st.values("is_a")),
                    tuple(st.values("xref")),
                    document,
                ),
            )

    def add(self, rel: Relation) -> None:
        self.relations.setdefault(rel.id, rel)

    def resolve(self, rid: str) -> str:
        """A shorthand relation (``disease_has_infectious_agent``) -> its RO CURIE when its stanza has exactly one
        RO/BFO xref, else itself."""
        r = self.relations.get(rid)
        if r is not None and not _CURIE.match(rid):
            xs = [x for x in r.xrefs if x.startswith(("RO:", "BFO:"))]
            if len(xs) == 1:
                return xs[0]
        return rid

    def _parents(self, rid: str) -> list[str]:
        out: list[str] = []
        for key in (rid, self.resolve(rid)):
            r = self.relations.get(key)
            if r is not None:
                out.extend(r.is_a)
        return out

    def closure(self, rid: str) -> set[str]:
        seen: set[str] = set()
        stack = [rid, self.resolve(rid)]
        while stack:
            for p in self._parents(stack.pop()):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen

    def name(self, rid: str) -> str:
        for key in (rid, self.resolve(rid)):
            r = self.relations.get(key)
            if r is not None and r.name:
                return r.name
        return rid

    def is_causal(self, rid: str) -> bool:
        if CAUSAL_ROOT in self.closure(rid) or self.resolve(rid) == CAUSAL_ROOT:
            return True
        r = self.relations.get(rid) or self.relations.get(self.resolve(rid))
        texts = [rid.replace("_", " "), self.name(rid)]
        if r is not None and r.definition:
            texts.append(r.definition)
        low = " ".join(texts).lower()
        return any(m in low for m in CAUSAL_MARKERS)

    def definition(self, rid: str) -> tuple[str, dict[str, Any]] | None:
        """Codebook text for a causal relation: its own verbatim definition, else (explicitly marked) the verbatim
        definition of its nearest RO ancestor that has one. ``None`` when nothing defines it."""
        order: list[tuple[str, int]] = []
        frontier = [rid, self.resolve(rid)]
        seen: set[str] = set()
        depth = 0
        while frontier:
            nxt: list[str] = []
            for f in frontier:
                if f in seen:
                    continue
                seen.add(f)
                order.append((f, depth))
                nxt.extend(self._parents(f))
            frontier = nxt
            depth += 1
        for key, i in order:
            r = self.relations.get(key)
            if r is None or not r.definition:
                continue
            src = {**self.documents.get(r.document, {"title": r.document}), "paragraph": f"[Typedef] id: {r.id}"}
            src["document_key"] = r.document
            src["extraction"] = "obo_typedef_def" if r.document.endswith(".obo") else "obograph_property_definition"
            if key in (rid, self.resolve(rid)):
                return f'"{r.definition}"', src
            return (
                f'[no definition is given for {rid} "{self.name(rid)}"; definition of its ancestor relation {r.id} '
                f'"{r.name}" ({i} step(s) up the is_a hierarchy):] "{r.definition}"',
                src,
            )
        return None


def claim_relation(rid: str, ro: str) -> tuple[str, str]:
    """(claim vocabulary relation, which side is the cause: ``subject`` | ``object``) of a causal relation."""
    side = "subject" if ro in SUBJECT_IS_CAUSE else "object"
    rel = "contributing_factor" if ro in CONTRIBUTING else ("has_effect" if side == "subject" else "has_cause")
    return rel, side


# ============================================================================================== KG loading
def load_predicates(path: Path = PREDICATES_FILE) -> list[Any]:
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = (
        "pid",
        "value_kind",
        "reva_predicate",
        "label_en",
        "label_fr",
        "inverse_name",
        "inverse_label_en",
        "inverse_label_fr",
    )
    return [KgProperty(**{k: p[k] for k in keys if k in p}) for p in doc["predicates"]]


def predicate_ids(path: Path = PREDICATES_FILE) -> set[str]:
    return {p.pid for p in load_predicates(path)}


def current_keys(engine: Any, source_key: str, origin: str) -> set[str]:
    from sqlalchemy import text

    with engine.connect() as c:
        return set(
            c.execute(
                text("SELECT statement_key FROM kg.statement_current WHERE source_key = :s AND origin = :o"),
                {"s": source_key, "o": origin},
            ).scalars()
        )


def load_kg(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    parameters: Mapping[str, Any],
    retract: bool = True,
) -> dict[str, Any]:
    """Register the predicates, then load in bounded batches; statements of this source that the new build no
    longer has are retracted (reason derived by the KG loader)."""
    from reva.kg.model import (
        MAX_ENTITIES_PER_BATCH,
        MAX_RETRACTIONS_PER_BATCH,
        MAX_STATEMENTS_PER_BATCH,
        load_batch,
        register_properties,
        statement_key,
    )

    props = register_properties(engine, load_predicates()).__dict__
    gone: list[str] = []
    if retract:
        new = {statement_key(s, source_key) for s in statements}
        gone = sorted(current_keys(engine, source_key, origin) - new)
    n = max(
        1,
        -(-len(statements) // MAX_STATEMENTS_PER_BATCH),
        -(-len(entities) // MAX_ENTITIES_PER_BATCH),
        -(-len(gone) // MAX_RETRACTIONS_PER_BATCH),
    )
    reports = []
    for i in range(n):
        rep = load_batch(
            engine,
            store,
            entities=entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
            statements=statements[i * MAX_STATEMENTS_PER_BATCH : (i + 1) * MAX_STATEMENTS_PER_BATCH],
            source_key=source_key,
            raw_sha256s=sorted(set(raw_sha256s)),
            retrieved_at=retrieved_at,
            origin=origin,
            retract_keys=gone[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
            parameters=dict(parameters),
        )
        reports.append(rep.as_dict())
    keys = (
        "entities_inserted",
        "entities_unchanged",
        "entities_conflicting",
        "statements_inserted",
        "statements_new_revision",
        "statements_unchanged",
        "statements_stale",
        "statements_conflicting",
        "retractions_inserted",
    )
    rejected: Counter[str] = Counter()
    sample: list[Any] = []
    for r in reports:
        rejected.update(r.get("rejected_counts") or {})
        sample.extend((r.get("rejected") or [])[: 20 - len(sample)])
    return {
        **{k: sum(int(r.get(k) or 0) for r in reports) for k in keys},
        "batches": len(reports),
        "load_ids": [r.get("load_id") for r in reports],
        "rejected_counts": dict(rejected),
        "rejected_sample": sample,
        "properties": props,
        "retracted": len(gone),
    }


def kg_available(engine: Any) -> bool:
    if engine is None:
        return False
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


__all__ = [
    "CAUSAL_MARKERS",
    "CAUSAL_ROOT",
    "KIND_BY_PREFIX",
    "RO_URL",
    "HealthSourceError",
    "OboStanza",
    "Relation",
    "RelationIndex",
    "claim_relation",
    "curie",
    "data_version",
    "exact_synonyms",
    "digest",
    "drift",
    "kg_id",
    "load_kg",
    "load_predicates",
    "parse_obo",
    "prefix",
    "relationship",
    "unquote",
]
