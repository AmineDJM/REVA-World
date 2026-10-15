"""Shared helpers of the science & technology atlas connectors (V3, agent A-SCI).

Used by :mod:`atlas_openalex`, :mod:`atlas_nobel` and :mod:`atlas_wipo`, which declare their atlas
sources through ``atlas_source()`` / ``atlas_ingest(run)`` (``reva.atlas.sources`` /
``reva.atlas.ingest``). Nothing here makes a network request or builds an HTTP client.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from typing import Any


def code_digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(m).encode())
    return "sha256:" + h.hexdigest()


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical JSON object text (flags / selector columns)."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame({c: [r[c] for r in rows] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


def trim_registration(summary: Mapping[str, Any]) -> dict[str, Any]:
    """A compact copy of a ``register_atlas_table`` summary for run reports."""
    out = {k: v for k, v in summary.items() if k != "coverage"}
    out["unmapped_provider_geo"] = list(summary.get("unmapped_provider_geo", []))[:50]
    return out


__all__ = ["atlas_frame", "canon", "code_digest", "trim_registration"]
