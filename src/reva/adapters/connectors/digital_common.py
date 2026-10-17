"""Shared helpers of the V6 digital-world connectors (agent V6DIGITAL; not an atlas module: it declares no source).

``atlas_keepiton`` (Access Now #KeepItOn STOP internet shutdowns) and ``atlas_cfr_cyber`` (Council on Foreign
Relations Cyber Operations Tracker) share: the dataset predicates of ``registry/kg/predicates_digital.yaml``, the
provider-name geography schemes of ``registry/atlas/geo_aliases/v6digital.yaml`` (exact names only, each alias with
its evidence), a splitter for provider cells that list several countries, plain-text extraction of archived HTML
pages (for verbatim definitions) and a guarded KG load. Report: ``docs/dev/v6_digital.md``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from reva.adapters.connectors.agri_common import canon, module_digest, slug

REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_digital.yaml"
_KEYS = (
    "pid",
    "value_kind",
    "reva_predicate",
    "label_en",
    "label_fr",
    "inverse_name",
    "inverse_label_en",
    "inverse_label_fr",
)


def page_text(body: bytes) -> str:
    """Visible text of an HTML page (scripts/styles dropped, tags -> spaces, entities unescaped, spaces collapsed)."""
    t = body.decode("utf-8", errors="replace")
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def quote_between(text: str, start: str, ends: Sequence[str], limit: int = 1500) -> str:
    """The verbatim text from ``start`` (included) up to the first of ``ends`` (excluded); '' when absent."""
    i = text.find(start)
    if i < 0:
        return ""
    j = len(text)
    for e in ends:
        k = text.find(e, i + len(start))
        if 0 <= k < j:
            j = k
    return text[i:j].strip()[:limit]


def split_countries(cell: str, known: Iterable[str], seps: Sequence[str] = (";", ",")) -> list[str]:
    """Split a provider cell naming several countries (``'Burundi; Kenya'``, ``'Israel, United States'``).

    A known name that itself contains a separator (``'Palestine, State of'``, ``'Tanzania, United Republic of'``)
    is kept whole: at each position the longest known name followed by a separator or the end wins; otherwise the
    text up to the next separator is one (unknown) part. Never fuzzy: parts are compared verbatim."""
    names = sorted({k for k in known if k}, key=len, reverse=True)
    s = cell.strip()
    out: list[str] = []
    while s:
        hit = next(
            (n for n in names if s.startswith(n) and (len(s) == len(n) or s[len(n)] in seps)),
            None,
        )
        if hit is None:
            cut = min((s.find(c) for c in seps if c in s), default=-1)
            hit = s if cut < 0 else s[:cut]
        part = hit.strip()
        if part:
            out.append(part)
        s = s[len(hit) :].lstrip()
        if s and s[0] in seps:
            s = s[1:].lstrip()
    return out


def scheme_names(scheme: str) -> set[str]:
    """The provider names of a ``v6digital.yaml`` geography scheme (as written, before upper-casing)."""
    doc = yaml.safe_load((REPO_ROOT / "registry" / "atlas" / "geo_aliases" / "v6digital.yaml").read_text())
    return {str(a["code"]) for a in doc["schemes"][scheme]["aliases"]}


def load_predicates(pids: Iterable[str] | None = None) -> list[Any]:
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(PREDICATES_FILE.read_text(encoding="utf-8"))
    by_pid = {p["pid"]: KgProperty(**{k: p[k] for k in _KEYS if k in p}) for p in doc["predicates"]}
    if pids is None:
        return list(by_pid.values())
    return [by_pid[p] for p in pids]


def guarded_kg_load(
    engine: Any,
    store: Any,
    *,
    pids: Sequence[str],
    wikidata_pids: Sequence[str],
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    source_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
) -> dict[str, Any]:
    """Register the predicates (+ WDHARVEST's definitions of ``wikidata_pids``) and load in bounded batches.

    Typed result (``loaded`` / ``blocked`` / ``failed``): the atlas tables of the same run stay registered."""
    from sqlalchemy.exc import DBAPIError

    from reva.adapters.connectors.atlas_geopol_common import to_kg_model
    from reva.kernel.errors import RevaError
    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_STATEMENTS_PER_BATCH, load_batch, register_properties

    try:
        props = load_predicates(pids)
        if wikidata_pids:
            from reva.adapters.connectors.wikidata import load_allowlist
            from reva.kg.harvest import kg_properties as wikidata_properties

            wd = [p for p in wikidata_properties(load_allowlist()) if p.pid in set(wikidata_pids)]
            if len(wd) != len(set(wikidata_pids)):
                return {"status": "blocked", "reason": f"{list(wikidata_pids)} not all in registry/kg/properties.yaml"}
            props = props + wd
        register_properties(engine, props)
    except (RevaError, DBAPIError, KeyError) as e:
        return {"status": "blocked", "reason": f"KG properties not registrable: {str(getattr(e, 'message', e))[:300]}"}
    ents, sts = to_kg_model(entities, statements, source_key=source_key, source_revision=source_revision)
    totals: dict[str, int] = {}
    rejected: dict[str, int] = {}
    samples: list[Any] = []
    batches = 0
    try:
        chunks: list[tuple[list[Any], list[Any]]] = [
            (ents[i : i + MAX_ENTITIES_PER_BATCH], []) for i in range(0, len(ents), MAX_ENTITIES_PER_BATCH)
        ]
        chunks += [([], sts[i : i + MAX_STATEMENTS_PER_BATCH]) for i in range(0, len(sts), MAX_STATEMENTS_PER_BATCH)]
        for be, bs in chunks:
            rep = load_batch(
                engine,
                store,
                entities=be,
                statements=bs,
                source_key=source_key,
                raw_sha256s=sorted(set(raw_sha256s)),
                retrieved_at=retrieved_at,
                origin=origin,
            )
            batches += 1
            d = rep.as_dict()
            samples.extend(d.get("rejected_sample", [])[: max(0, 20 - len(samples))])
            for k, v in d.items():
                if k != "load_id" and isinstance(v, int) and not isinstance(v, bool):
                    totals[k] = totals.get(k, 0) + v
            for k, v in rep.rejected_counts.items():
                rejected[k] = rejected.get(k, 0) + v
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": e.details, "batches": batches}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}", "batches": batches}
    return {
        "status": "loaded",
        **totals,
        "rejected": rejected,
        "rejected_sample": samples,
        "batches": batches,
        "entities_built": len(entities),
        "statements_built": len(statements),
    }


__all__ = [
    "PREDICATES_FILE",
    "canon",
    "guarded_kg_load",
    "load_predicates",
    "module_digest",
    "page_text",
    "quote_between",
    "scheme_names",
    "slug",
    "split_countries",
]
