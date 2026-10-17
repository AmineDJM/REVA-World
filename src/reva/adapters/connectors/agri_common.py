"""Shared helpers of the V6AGRI food-systems connectors (not an atlas module: it declares no source).

``atlas_usda_psd`` (USDA FAS PSD balance sheets), ``atlas_usda_wasde`` (WASDE monthly vintages),
``atlas_fao_tm`` (FAOSTAT detailed trade matrix, bounded) and ``atlas_ifpri_er`` (IFPRI food export
restrictions -> KG events) share: canonical JSON, the module code digest, a deterministic slug for provider
labels used inside indicator ids (the label itself is always kept verbatim in ``flags``), the agricultural
dataset predicates of ``registry/kg/predicates_agri.yaml`` and a guarded KG load.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PREDICATES_FILE = Path(__file__).resolve().parents[4] / "registry" / "kg" / "predicates_agri.yaml"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical compact JSON (sorted keys) for atlas ``flags`` / ``selector`` cells."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def module_digest(module: Any) -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(module).encode()).hexdigest()


def slug(text: str) -> str:
    """``'World Rice Supply and Use  (Milled Basis)'`` -> ``'world_rice_supply_and_use_milled_basis'``.

    A re-spelling of the provider's own label (lower case, runs of non-alphanumerics -> ``_``) used only to build
    stable indicator ids; never used to match two sources."""
    s = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    if not s:
        raise ValueError(f"empty slug for {text!r}")
    return s


def load_predicates(pids: Iterable[str] | None = None, path: Path = PREDICATES_FILE) -> list[Any]:
    """``KgProperty`` records of ``predicates_agri.yaml`` (all, or the ``pids`` subset; unknown pid -> KeyError)."""
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
    by_pid = {p["pid"]: KgProperty(**{k: p[k] for k in keys if k in p}) for p in doc["predicates"]}
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
    """Register the dataset predicates (+ the named Wikidata PIDs with WDHARVEST's definitions) and load.

    Typed result (``loaded`` / ``blocked`` / ``failed``): the atlas table of the same run stays registered."""
    from sqlalchemy.exc import DBAPIError

    from reva.adapters.connectors.atlas_geopol_common import to_kg_model
    from reva.kernel.errors import RevaError
    from reva.kg.model import load_batch, register_properties

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
    try:
        rep = load_batch(
            engine,
            store,
            entities=ents,
            statements=sts,
            source_key=source_key,
            raw_sha256s=sorted(set(raw_sha256s)),
            retrieved_at=retrieved_at,
            origin=origin,
        )
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": e.details}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}"}
    d = rep.as_dict()
    d.update(status="loaded", statements_built=len(statements), entities_built=len(entities))
    return d


__all__ = ["PREDICATES_FILE", "canon", "guarded_kg_load", "load_predicates", "module_digest", "slug"]
