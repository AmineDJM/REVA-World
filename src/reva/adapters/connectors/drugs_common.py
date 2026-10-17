"""Shared helpers of the V6 medicines connectors (agent V6DRUGS): ChEMBL, UniChem, WHO eEML, openFDA, ClinicalTrials.gov.

Not an atlas module (no ``atlas_`` prefix): the ``atlas_drugs_*`` modules import it. It holds the predicate
allow-list loader (``registry/kg/predicates_drugs.yaml``), the bounded KG loader (register predicates, retract
what a new *complete* build no longer states, load in batches), typed literal values, a few guards and a tiny
stdlib HTML tree used to read the eEML pages. Nothing here fetches: connectors fetch only through ``run.get``
(budget-metered, raw bytes archived).

Rules carried by every connector of the layer (docs/dev/v6_drugs.md):

* identifiers exactly as published (``CHEMBL25``, ``P00533``, ``MONDO:0005148``, ``D003924``, ``DB00572``, UNII);
  never a join on a name;
* an indication or a listing is an association (family ``associated_with``), never a causal claim;
* adverse-event counts are counts of *reports*, never of drug-caused events;
* counts are the provider's own; a count the provider does not return is Missing (no statement), never zero.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import SourceResponseError

REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_drugs.yaml"
DECIMAL_RE = re.compile(r"^[0-9]{1,20}$")

#: KG entity kinds of this layer.
KIND_DRUG = "drug"
KIND_TARGET = "drug_target"
KIND_TRIAL_AGG = "clinical_trial"
KIND_MESH = "mesh_descriptor"
KIND_SUBSTANCE = "drug_substance"
KIND_DISEASE_TERM = "disease_or_phenotype"

#: Caveat repeated in every adverse-event value (openFDA's own wording is quoted by the connector).
REPORTS_NOT_CAUSATION = (
    "Counts of spontaneous adverse-event reports that mention the substance (suspect or concomitant). A report "
    "does not establish that the substance caused the event; counts depend on use, publicity and reporting rules."
)
#: WHOCC terms for ATC codes, quoted verbatim from https://atcddd.fhi.no/copyright_disclaimer/ (2026-10-17).
WHOCC_TERMS = (
    "Use of all or parts of the material requires reference to the WHO Collaborating Centre for Drug Statistics "
    "Methodology.  Copying and distribution for commercial purposes is not allowed.  Changing or manipulating the "
    "material is not allowed."
)
ATC_FLAGS = {
    "licence_noncommercial": True,
    "rights_holder": "WHO Collaborating Centre for Drug Statistics Methodology (WHOCC), Oslo",
    "rights_terms": WHOCC_TERMS,
    "rights_url": "https://atcddd.fhi.no/copyright_disclaimer/",
}


class DrugsSourceError(SourceResponseError):
    pass


def err(msg: str, kind: str = "schema_drift") -> DrugsSourceError:
    return DrugsSourceError(msg, retryable=False, kind=kind)


def digest(*modules: Any, files: Sequence[Path] = ()) -> str:
    """sha256 over the modules' source and the registry files they read (code_digest of an atlas source)."""
    h = hashlib.sha256()
    for m in modules:
        h.update(inspect.getsource(m).encode())
    for f in (PREDICATES_FILE, *files):
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def json_body(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise err(f"{what}: response is not JSON") from e


def as_count(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not DECIMAL_RE.match(str(v)):
        raise err(f"{what}: not a non-negative integer: {v!r}")
    return int(v)


def opt_list(v: Any) -> list[str] | None:
    """A run option given as a list of strings or a comma-separated CLI string (None when absent)."""
    if v is None:
        return None
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x) for x in v]


def chunks(items: Sequence[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield list(items[i : i + n])


# ============================================================================================== literals
def count_value(n: int, unit: str) -> dict[str, Any]:
    """A count as a KG quantity (decimal string; only counts the source gave)."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise err(f"count is not a non-negative integer: {n!r}")
    return {"type": "quantity", "amount": str(n), "unit": unit}


def string_value(s: str) -> dict[str, Any]:
    return {"type": "string", "value": s}


def external_id(s: str) -> dict[str, Any]:
    return {"type": "external_id", "value": s}


def url_value(s: str) -> dict[str, Any]:
    return {"type": "url", "value": s}


def year_value(y: int) -> dict[str, Any]:
    return {"type": "time", "time": f"{y:04d}", "precision": 9}


def day_time(iso_day: str) -> Any:
    from reva.kg.model import KgTime

    d = str(iso_day)[:10]
    if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", d):
        raise err(f"not an ISO day: {iso_day!r}")
    return KgTime(f"{d}T00:00:00Z", 11)


# ============================================================================================== predicates / KG
_PRED_KEYS = (
    "pid",
    "value_kind",
    "reva_predicate",
    "label_en",
    "label_fr",
    "inverse_name",
    "inverse_label_en",
    "inverse_label_fr",
)


def predicate_docs(path: Path = PREDICATES_FILE) -> list[dict[str, Any]]:
    return list(yaml.safe_load(path.read_text(encoding="utf-8"))["predicates"])


def load_predicates(prefixes: Sequence[str] = (), path: Path = PREDICATES_FILE) -> list[Any]:
    """``KgProperty`` objects of ``predicates_drugs.yaml`` (only pids starting with one of ``prefixes`` if given)."""
    from reva.kg.model import KgProperty

    return [
        KgProperty(**{k: p[k] for k in _PRED_KEYS if k in p})
        for p in predicate_docs(path)
        if not prefixes or any(p["pid"].startswith(x) for x in prefixes)
    ]


def kg_available(engine: Any) -> bool:
    if engine is None:
        return False
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


def current_keys(engine: Any, source_key: str, origin: str) -> set[str]:
    from sqlalchemy import text

    with engine.connect() as c:
        return set(
            c.execute(
                text("SELECT statement_key FROM kg.statement_current WHERE source_key = :s AND origin = :o"),
                {"s": source_key, "o": origin},
            ).scalars()
        )


def current_entities(engine: Any, origin: str, like: str, kind: str | None = None) -> dict[str, str]:
    """``qid -> kind`` of current KG entities whose qid matches the SQL LIKE pattern (e.g. ``chembl:%``)."""
    from sqlalchemy import text

    sql = "SELECT qid, kind FROM kg.entity_current WHERE origin = :o AND qid LIKE :p"
    params: dict[str, Any] = {"o": origin, "p": like}
    if kind is not None:
        sql += " AND kind = :k"
        params["k"] = kind
    with engine.connect() as c:
        return {r.qid: r.kind for r in c.execute(text(sql), params)}


def current_literals(engine: Any, origin: str, pid: str) -> list[tuple[str, str]]:
    """``(subject_qid, value)`` of current literal statements of one predicate (identifier joins)."""
    from sqlalchemy import text

    with engine.connect() as c:
        rows = c.execute(
            text(
                """SELECT subject_qid, value ->> 'value' AS v FROM kg.statement_current
                   WHERE origin = :o AND property_pid = :p AND value IS NOT NULL"""
            ),
            {"o": origin, "p": pid},
        )
        return [(r.subject_qid, r.v) for r in rows if r.v]


def current_edges(engine: Any, origin: str, pid: str) -> list[tuple[str, str]]:
    """``(subject_qid, object_qid)`` of current entity-valued statements of one predicate."""
    from sqlalchemy import text

    with engine.connect() as c:
        rows = c.execute(
            text(
                """SELECT subject_qid, object_qid FROM kg.statement_current
                   WHERE origin = :o AND property_pid = :p AND object_qid IS NOT NULL"""
            ),
            {"o": origin, "p": pid},
        )
        return [(r.subject_qid, r.object_qid) for r in rows]


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
    prefixes: Sequence[str],
    retract: bool,
) -> dict[str, Any]:
    """Register the predicates, then load in bounded batches. With ``retract`` (a complete build only), current
    statements of this source that the build no longer has are retracted; a partial run never retracts."""
    from reva.kg.model import (
        MAX_ENTITIES_PER_BATCH,
        MAX_RETRACTIONS_PER_BATCH,
        MAX_STATEMENTS_PER_BATCH,
        load_batch,
        register_properties,
        statement_key,
    )

    props = register_properties(engine, load_predicates(prefixes)).__dict__
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
        "entities_built": len(entities),
        "statements_built": len(statements),
        "batches": len(reports),
        "load_ids": [r.get("load_id") for r in reports],
        "rejected_counts": dict(rejected),
        "rejected_sample": sample,
        "properties": props,
        "retracted": len(gone),
    }


# ============================================================================================== tiny HTML tree
@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = None

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def text(self) -> str:
        """Visible text, whitespace collapsed (entities already decoded by the parser)."""
        out: list[str] = []

        def walk(n: Node | str) -> None:
            if isinstance(n, str):
                out.append(n)
            elif n.tag not in ("script", "style"):
                for c in n.children:
                    walk(c)

        walk(self)
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def iter(self) -> Iterator[Node]:
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        return [n for n in self.iter() if (tag is None or n.tag == tag) and (cls is None or cls in n.classes)]

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        for n in self.iter():
            if (tag is None or n.tag == tag) and (cls is None or cls in n.classes):
                return n
        return None


_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        n = Node(tag, {k: (v or "") for k, v in attrs}, parent=self.cur)
        self.cur.children.append(n)
        if tag not in _VOID:
            self.cur = n

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, parent=self.cur))

    def handle_endtag(self, tag: str) -> None:
        n: Node | None = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:  # unmatched end tags are ignored
            self.cur = n.parent

    def handle_data(self, data: str) -> None:
        self.cur.children.append(data)


def parse_html(body: bytes, what: str) -> Node:
    try:
        text_ = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise err(f"{what}: page is not UTF-8") from e
    b = _TreeBuilder()
    b.feed(text_)
    b.close()
    return b.root


__all__ = [
    "ATC_FLAGS",
    "KIND_DRUG",
    "KIND_TARGET",
    "KIND_TRIAL_AGG",
    "REPORTS_NOT_CAUSATION",
    "DrugsSourceError",
    "Node",
    "as_count",
    "count_value",
    "current_edges",
    "current_entities",
    "current_literals",
    "digest",
    "err",
    "external_id",
    "json_body",
    "kg_available",
    "load_kg",
    "parse_html",
    "string_value",
    "year_value",
]
