"""Atlas reads for the registry: table versions (``evidence.dataset``) and their verified frames.

The registry never takes an atlas snapshot: it pins, per question family, the latest version of the declared
table recorded at or before the issue time (same selection rule as the atlas snapshot), and at resolution reads
the versions recorded after the seal, in order.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from threading import Lock
from typing import Any

import polars as pl
from sqlalchemy import text

from reva.atlas.tables import ATLAS_DATASET_CONCEPT, read_atlas_table
from reva.ports.storage import ArtifactStore

_VERSIONS_SQL = """
SELECT d.id::text AS dataset_id, d.dataset_key, d.artifact_sha256, d.manifest_sha256, d.parser_version,
       d.row_count, d.recorded_at, a.rights
FROM evidence.dataset d LEFT JOIN meta.artifact a ON a.sha256 = d.artifact_sha256
WHERE d.concept_id = :c AND d.origin = :o AND d.dataset_key = :k
"""


def _pin(r: Any) -> dict[str, Any]:
    return {
        "dataset_key": r["dataset_key"],
        "dataset_id": r["dataset_id"],
        "artifact_sha256": r["artifact_sha256"],
        "manifest_sha256": r["manifest_sha256"],
        "parser_version": r["parser_version"],
        "row_count": int(r["row_count"]),
        "recorded_at": r["recorded_at"].isoformat(),
        "rights": r["rights"],
    }


def latest_version(conn: Any, dataset_key: str, *, origin: str, cutoff: datetime) -> dict[str, Any] | None:
    r = (
        conn.execute(
            text(_VERSIONS_SQL + " AND d.recorded_at <= :cut ORDER BY d.recorded_at DESC, d.id DESC LIMIT 1"),
            {"c": ATLAS_DATASET_CONCEPT, "o": origin, "k": dataset_key, "cut": cutoff},
        )
        .mappings()
        .first()
    )
    return _pin(r) if r else None


def versions_between(
    conn: Any, dataset_key: str, *, origin: str, after: datetime, until: datetime
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(_VERSIONS_SQL + " AND d.recorded_at > :a AND d.recorded_at <= :u ORDER BY d.recorded_at, d.id"),
        {"c": ATLAS_DATASET_CONCEPT, "o": origin, "k": dataset_key, "a": after, "u": until},
    ).mappings()
    return [_pin(r) for r in rows]


class FrameCache:
    """Small LRU of verified atlas frames keyed by artifact sha256 (immutable content)."""

    def __init__(self, store: ArtifactStore, size: int = 6) -> None:
        self.store = store
        self.size = size
        self._d: OrderedDict[str, pl.DataFrame] = OrderedDict()
        self._lock = Lock()

    def get(self, sha: str) -> pl.DataFrame:
        with self._lock:
            if sha in self._d:
                self._d.move_to_end(sha)
                return self._d[sha]
        frame = read_atlas_table(self.store, sha)
        with self._lock:
            self._d[sha] = frame
            while len(self._d) > self.size:
                self._d.popitem(last=False)
        return frame
