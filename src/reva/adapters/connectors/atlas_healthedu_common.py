"""Shared plumbing of the health/education atlas connectors (agent A-HEALTHEDU).

Used by :mod:`reva.adapters.connectors.atlas_who` (WHO Global Health Observatory) and
:mod:`reva.adapters.connectors.atlas_uis` (UNESCO Institute for Statistics).

Flow of one run (:func:`run_and_register`):

1. :func:`reva.evidence.runner.run_connector` fetches every planned item (live through the
   budget-metered SafeHttpClient, or from captured fixtures) and archives each raw response as an
   ``evidence.acquisition`` (zero observation batches: atlas cells do not enter the evidence
   observation store). Structural drift found by the connector's ``parse`` quarantines the item.
2. Only if **every** item was archived un-quarantined, the archived bytes are read back from the
   artifact store (never the in-memory copy), parsed into the ATLASCORE long schema and registered
   with :func:`reva.atlas.tables.register_atlas_table`. A partial run registers nothing: a table
   version lacking some indicators would silently replace the previous complete one in the atlas
   snapshot. The result then says ``status: blocked`` and lists the failed items.

The connectors' ``describe()`` returns an :class:`AtlasConnectorSpec`, a subclass of the shared
``ConnectorSpec`` whose ``template`` admits the atlas templates (the shared Literal in
``reva.ports.connectors`` lists only the V1/V2 templates; see the agent report for the requested
additive change there).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from reva.kernel.errors import ValidationProblem
from reva.ports.connectors import ConnectorSpec, FetchOutcome

#: Maximum length of a flags / selector JSON object (``reva.atlas.tables.MAX_TEXT``).
MAX_FLAGS_CHARS = 4000


class AtlasConnectorSpec(ConnectorSpec):
    """``ConnectorSpec`` for the atlas connectors of this agent (same fields, atlas templates)."""

    template: Literal["atlas_who_gho_odata", "atlas_uis_data_api"]  # type: ignore[assignment]


def canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def fit_flags(flags: dict[str, Any], *, long_keys: Sequence[str] = ("comments", "footnotes")) -> str:
    """Canonical flags JSON of at most :data:`MAX_FLAGS_CHARS` characters.

    Long provider notes are shortened (never dropped silently): each text in ``long_keys`` is cut
    progressively and ``notes_truncated: true`` is set. Every other key is kept verbatim."""
    s = canon(flags)
    if len(s) <= MAX_FLAGS_CHARS:
        return s
    out = dict(flags)
    out["notes_truncated"] = True
    for limit in (1500, 600, 250, 100, 40):
        for k in long_keys:
            v = out.get(k)
            if isinstance(v, str):
                out[k] = v[:limit]
            elif isinstance(v, list):
                out[k] = [
                    {kk: (vv[:limit] if isinstance(vv, str) else vv) for kk, vv in f.items()}
                    if isinstance(f, dict)
                    else f
                    for f in v[:10]
                ]
        s = canon(out)
        if len(s) <= MAX_FLAGS_CHARS:
            return s
    for k in long_keys:
        out.pop(k, None)
    out["notes_dropped_too_long"] = True
    s = canon(out)
    if len(s) > MAX_FLAGS_CHARS:
        raise ValidationProblem("atlas flags exceed 4000 characters even without provider notes", keys=sorted(out))
    return s


def register_connector(owner_engine: Any, store: Any, connector: Any, actor: str = "maintainer:a-healthedu") -> str:
    """Register the connector version (and nothing else) like the seed does for built-ins.

    For tests and the throwaway live ingest: the connectors are not in the built-in registry
    (``reva.adapters.connectors.registry`` is lead-owned; see the agent report)."""
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    spec = connector.describe()
    with owner_engine.begin() as conn:
        act = repo.simple_activity(
            "registry.connector_version", "review", actor, {"connector": spec.name, "version": spec.version}
        )
        repo.record_activity(conn, act)
        return str(register_connector_version(conn, store, spec, act.id))


def items_ok(outcomes: Sequence[FetchOutcome], planned: Sequence[str]) -> tuple[bool, list[dict[str, str]]]:
    """Every planned item archived un-quarantined (published or an identical earlier acquisition)."""
    by_key = {o.item_key: o for o in outcomes}
    bad = []
    for key in planned:
        o = by_key.get(key)
        if o is None:
            bad.append({"item": key, "status": "not_run"})
        elif o.status not in ("published", "unchanged") or not o.raw_sha256:
            bad.append({"item": key, "status": o.status, "detail": o.detail[:300]})
    return (not bad), bad


async def run_and_register(
    connector: Any,
    http: Any,
    store: Any,
    worker_engine: Any,
    *,
    budget: Any = None,
) -> dict[str, Any]:
    """Archive every raw response, then parse the archived bytes and register one atlas table.

    Returns ``{"status": "registered"|"blocked", "run": RunReport dict, "atlas": summary|None,
    "failed_items": [...]}``. Never registers a partial table.
    """
    from reva.atlas.tables import register_atlas_table
    from reva.evidence.runner import run_connector
    from reva.ports.connectors import FetchBudget

    spec = connector.describe()
    plan = connector.plan(None, budget or FetchBudget(max_requests=1000))
    rep = await run_connector(
        connector, http, store, worker_engine, budget=budget or FetchBudget(max_requests=1000), persist_cursor=False
    )
    expected = getattr(connector, "expected_item_keys", None)
    planned = list(expected()) if callable(expected) else [i.key for i in plan.items]
    ok, bad = items_ok(rep.outcomes, planned)
    if not ok:
        return {
            "status": "blocked",
            "reason": "items_not_archived",
            "failed_items": bad,
            "run": rep.as_dict(),
            "atlas": None,
        }
    raws = {o.item_key: o.raw_sha256 for o in rep.outcomes if o.raw_sha256}
    origin = getattr(http, "origin", "live")
    bodies = {k: store.get("raw", sha) for k, sha in raws.items()}
    frame, coverage_note, extra = connector.atlas_frame(bodies, raws)
    summary = register_atlas_table(
        worker_engine,
        store,
        source=connector.ATLAS_SOURCE,
        dataset=connector.ATLAS_DATASET,
        frame=frame,
        raw_sha256s=sorted(set(raws.values())),
        parser_version=spec.parser_version,
        origin=origin,
        coverage_note=coverage_note,
        geo_scheme=connector.GEO_SCHEME,
        rights=connector.RIGHTS,
        actor=f"connector:{spec.name}",
    )
    return {"status": "registered", "run": rep.as_dict(), "atlas": summary, "failed_items": [], **extra}


__all__ = [
    "MAX_FLAGS_CHARS",
    "AtlasConnectorSpec",
    "canon",
    "fit_flags",
    "items_ok",
    "register_connector",
    "run_and_register",
]
