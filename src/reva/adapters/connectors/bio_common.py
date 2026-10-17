"""Shared helpers of the V4BIO connectors (tree of life, genes, proteins, genomes; agent V4BIO).

Not an atlas module (no ``atlas_`` prefix): ``atlas_bio_taxonomy`` and ``atlas_bio_molecular`` import it. It holds
the predicate allow-list loader (``registry/kg/predicates_bio.yaml``), the bounded KG loader (register predicates,
retract what a new full build no longer states, load in batches), typed literal values and small parsing guards.
Nothing here fetches: connectors fetch only through ``run.get`` (budget-metered, archived).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import SourceResponseError

REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_bio.yaml"
SCOPE_FILE = REPO_ROOT / "registry" / "bio" / "taxon_scope.yaml"
DECIMAL_RE = re.compile(r"^[0-9]{1,20}$")
DAY = 86400

#: KG entity kinds of this layer (roadmap V4 section 1).
KIND_TAXON = "taxon"
KIND_GENE = "gene"
KIND_PROTEIN = "protein"
KIND_CHROMOSOME = "chromosome"
KIND_ASSEMBLY = "genome_assembly"


class BioSourceError(SourceResponseError):
    pass


def err(msg: str, kind: str = "schema_drift") -> BioSourceError:
    return BioSourceError(msg, retryable=False, kind=kind)


def digest(*modules: Any, files: Sequence[Path] = ()) -> str:
    """sha256 over the modules' source and the registry files they read (code_digest of an atlas source)."""
    h = hashlib.sha256()
    for m in modules:
        h.update(inspect.getsource(m).encode())
    for f in (PREDICATES_FILE, *files):
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def json_body(raw: Any, what: str) -> Any:
    try:
        return json.loads(raw.body)
    except (ValueError, UnicodeDecodeError) as e:
        raise err(f"{what}: response is not JSON") from e


# ============================================================================================== literals
def count_value(n: int, unit: str) -> dict[str, Any]:
    """A count as a KG quantity (decimal string; never a filled-in zero: callers only emit counts the source gave)."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise err(f"count is not a non-negative integer: {n!r}")
    return {"type": "quantity", "amount": str(n), "unit": unit}


def string_value(s: str) -> dict[str, Any]:
    return {"type": "string", "value": s}


def external_id(s: str) -> dict[str, Any]:
    return {"type": "external_id", "value": s}


def text_value(s: str, lang: str) -> dict[str, Any]:
    return {"type": "monolingualtext", "text": s, "language": lang}


def day_time(iso_day: str) -> Any:
    """A ``KgTime`` at day precision from ``YYYY-MM-DD`` (or a longer ISO timestamp)."""
    from reva.kg.model import KgTime

    d = str(iso_day)[:10]
    if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", d):
        raise err(f"not an ISO day: {iso_day!r}")
    return KgTime(f"{d}T00:00:00Z", 11)


def opt_list(v: Any) -> list[str] | None:
    """A run option given as a list of strings or a comma-separated CLI string (None when absent)."""
    if v is None:
        return None
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x) for x in v]


def as_int(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not DECIMAL_RE.match(str(v)):
        raise err(f"{what}: not a non-negative integer: {v!r}")
    return int(v)


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
    """``KgProperty`` objects of ``predicates_bio.yaml`` (only pids starting with one of ``prefixes`` if given)."""
    from reva.kg.model import KgProperty

    return [
        KgProperty(**{k: p[k] for k in _PRED_KEYS if k in p})
        for p in predicate_docs(path)
        if not prefixes or any(p["pid"].startswith(x) for x in prefixes)
    ]


def current_keys(engine: Any, source_key: str, origin: str, prefix: str) -> set[str]:
    from sqlalchemy import text

    like = f"{source_key}:{prefix}".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT statement_key FROM kg.statement_current
                        WHERE source_key = :s AND origin = :o AND statement_key LIKE :p"""),
                {"s": source_key, "o": origin, "p": like},
            ).scalars()
        )


def current_entities(engine: Any, origin: str, like: str) -> dict[str, str]:
    """``qid -> kind`` of current KG entities whose qid matches the SQL LIKE pattern (e.g. ``gbif:%``)."""
    from sqlalchemy import text

    with engine.connect() as c:
        return {
            r.qid: r.kind
            for r in c.execute(
                text("SELECT qid, kind FROM kg.entity_current WHERE origin = :o AND qid LIKE :p"),
                {"o": origin, "p": like},
            )
        }


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
    retract_prefix: str | None,
) -> dict[str, Any]:
    """Register the predicates, then load in bounded batches. Current statements of this source whose key starts
    with ``<source_key>:<retract_prefix>`` and that this build no longer has are retracted (``None``: no
    retraction -- a partial run never retracts)."""
    from reva.kg.model import (
        MAX_ENTITIES_PER_BATCH,
        MAX_RETRACTIONS_PER_BATCH,
        MAX_STATEMENTS_PER_BATCH,
        load_batch,
        register_properties,
        statement_key,
    )

    props = register_properties(engine, load_predicates(prefixes)).__dict__
    retract: list[str] = []
    if retract_prefix is not None:
        new = {statement_key(s, source_key) for s in statements}
        retract = sorted(current_keys(engine, source_key, origin, retract_prefix) - new)
    n = max(
        1,
        -(-len(statements) // MAX_STATEMENTS_PER_BATCH),
        -(-len(entities) // MAX_ENTITIES_PER_BATCH),
        -(-len(retract) // MAX_RETRACTIONS_PER_BATCH),
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
            retract_keys=retract[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
            parameters=dict(parameters),
        )
        reports.append(rep.as_dict())
    keys = (
        "entities_inserted",
        "entities_unchanged",
        "entities_conflicting",
        "labels_inserted",
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
        "retracted_keys_sample": retract[:20],
    }


def raw_bytes_total(raws: Sequence[Any]) -> int:
    return sum(len(r.body) for r in raws)
