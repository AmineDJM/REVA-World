"""Shared plumbing of the health/education atlas sources (agent A-HEALTHEDU).

Used by :mod:`reva.adapters.connectors.atlas_who` (WHO Global Health Observatory) and
:mod:`reva.adapters.connectors.atlas_uis` (UNESCO Institute for Statistics), both run through
ATLASCORE's single atlas ingest entry point (:mod:`reva.atlas.ingest`, budget-metered; the modules
never build an HTTP client).

Flow of one run (:func:`fetch_archive_register`):

1. every planned item is fetched through ``run.get`` and its raw bytes archived at once with
   ``run.archive`` (an ``evidence.acquisition`` of the source's connector version), whatever their
   content — the archive is the record of what the provider served;
2. each archived body is read back from the artifact store (never the in-memory copy) and checked
   structurally by the source (``check(item, body)``; drift raises ``SourceResponseError``);
3. only if **every** item passed, the archived bytes are parsed into the ATLASCORE long schema and
   registered with ``run.register``. A partial run registers nothing (a table version missing
   indicators would otherwise replace the complete one in the atlas snapshot): it raises a typed
   ``ValidationProblem`` listing the failed items.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from reva.kernel.errors import SourceResponseError, ValidationProblem

#: Maximum length of a flags / selector JSON object (``reva.atlas.tables.MAX_TEXT``).
MAX_FLAGS_CHARS = 4000


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


async def fetch_archive_register(run: Any, source: Any, *, dataset: str) -> dict[str, Any]:
    """Run one source object (``items()``, ``follow_ups(key, body)``, ``check(item, body)``,
    ``atlas_frame(bodies, raws)``, ``GEO_SCHEME``, ``RIGHTS``) inside an ``AtlasRun``."""
    if not run.wants(dataset):
        return {"dataset": dataset, "skipped": True}
    queue = list(source.items())
    bodies: dict[str, bytes] = {}
    raws: dict[str, str] = {}
    failed: list[dict[str, str]] = []
    while queue:
        item = queue.pop(0)
        raw = await run.get(item.url, key=item.key, max_bytes=item.max_bytes, params=dict(item.params))
        sha = run.archive(raw)
        body = run.store.get("raw", sha)
        raws[item.key] = sha
        bodies[item.key] = body
        try:
            source.check(item, body)
        except SourceResponseError as e:
            failed.append({"item": item.key, "error": e.message[:300], "raw_sha256": sha})
            continue
        queue.extend(source.follow_ups(item, body))
    if failed:
        raise ValidationProblem(
            "atlas run blocked: provider response drift; raw bytes archived, nothing registered",
            source=run.spec.name,
            failed_items=failed,
        )
    frame, note, extra = source.atlas_frame(bodies, raws)
    bodies.clear()
    out = run.register(
        dataset=dataset,
        frame=frame,
        raw_sha256s=sorted(set(raws.values())),
        coverage_note=note,
        geo_scheme=source.GEO_SCHEME,
        rights=dict(source.RIGHTS),
    )
    return {"dataset": dataset, "items": len(raws), "atlas": out, **extra}


__all__ = ["MAX_FLAGS_CHARS", "canon", "fetch_archive_register", "fit_flags"]
