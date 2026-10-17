"""Registered atlas table versions read outside the atlas snapshot (agent V8ASK).

An atlas concept's declared series (``source_order``) may sit in a table registered *after* the current atlas
snapshot (``eia.spot_d``, Brent daily, registered after snapshot ``717e8452…``). The forecast registry already
reads such tables by their latest registered version (:mod:`reva.evaluation.registry.data`); this adapter does the
same for a geography-free period question when the snapshot has nothing, and every cell carries the table
version it came from (dataset key, artifact sha256, registration time, licence of the version) plus
``outside_snapshot: true`` so the answer says where the value was read. Nothing is written; versions are pinned
at or before the request time.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reva.nlqa.ports import AtlasCell

MAX_CELLS = 5000


class RegisteredTablesAdapter:
    name = "atlas registered table versions (evidence.dataset, outside the snapshot)"

    def __init__(self, engine: Any, settings: Any = None, store: Any = None, *, origin: str = "live") -> None:
        self.engine = engine
        self.settings = settings
        self.origin = origin
        self._store = store
        self._cache: Any = None
        self._lock = threading.Lock()

    def _frames(self) -> Any:
        with self._lock:
            if self._cache is None:
                from reva.evaluation.registry.data import FrameCache

                if self._store is None:
                    from reva.adapters.objects.factory import build_store

                    self._store = build_store(self.settings)
                self._cache = FrameCache(self._store, size=4)
            return self._cache

    def latest(self, dataset_key: str) -> dict[str, Any] | None:
        from reva.evaluation.registry.data import latest_version

        with self.engine.connect() as conn:
            return latest_version(conn, dataset_key, origin=self.origin, cutoff=datetime.now(UTC))

    def period_cells(
        self, concept_id: str, series: Sequence[tuple[str, str, str]], from_year: int | None, to_year: int | None
    ) -> tuple[list[AtlasCell], list[dict[str, Any]]]:
        """Cells of the declared series ``(source, dataset, indicator)`` in their latest registered table version,
        for the years asked (no year → the latest year with a value). Returns ``(cells, pins)``."""
        import polars as pl

        cells: list[AtlasCell] = []
        pins: list[dict[str, Any]] = []
        for source, dataset, indicator in series:
            pin = self.latest(f"atlas.{source}.{dataset}")
            if pin is None:
                continue
            frame = self._frames().get(pin["artifact_sha256"])
            f = frame.filter(pl.col("indicator") == indicator)
            if f.is_empty():
                continue
            f = f.with_columns(pl.col("period_start").cast(pl.Utf8).str.slice(0, 4).cast(pl.Int32).alias("_y"))
            lo, hi = from_year, to_year
            if lo is None and hi is None:
                top = f.filter(pl.col("value").is_not_null())["_y"].max()
                if top is None:
                    continue
                lo = hi = int(str(top))
            f = f.filter((pl.col("_y") >= (lo if lo is not None else -9999)) & (pl.col("_y") <= (hi or 9999)))
            f = f.sort(["period_start", "period_end"]).head(MAX_CELLS)
            if f.is_empty():
                continue
            pins.append(pin)
            for r in f.iter_rows(named=True):
                if r.get("value") is None and r.get("value_text") is None:
                    continue
                cells.append(_cell(r, concept_id, pin))
        return cells, pins


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _cell(r: Mapping[str, Any], concept_id: str, pin: Mapping[str, Any]) -> AtlasCell:
    flags = _json(r.get("flags")) or {}
    sel = _json(r.get("selector"))
    return AtlasCell(
        source=str(r["source"]),
        dataset=str(r["dataset"]),
        indicator=str(r["indicator"]),
        iso3=str(r.get("iso3") or ""),
        period_start=str(r["period_start"])[:10],
        period_end=str(r["period_end"])[:10],
        value=None if r.get("value") is None else str(r["value"]),
        value_text=r.get("value_text"),
        unit=r.get("unit"),
        concept_id=concept_id,
        entity_qid=r.get("entity_qid"),
        flags=flags if isinstance(flags, Mapping) else {},
        raw_sha256=r.get("raw_sha256"),
        selector=json.dumps(sel, sort_keys=True, ensure_ascii=False) if isinstance(sel, Mapping) else sel,
        provenance={
            k: v
            for k, v in {
                "table_artifact_sha256": pin.get("artifact_sha256"),
                "dataset_key": pin.get("dataset_key"),
                "dataset_registered_at": pin.get("recorded_at"),
                "outside_snapshot": True,
                "provider_geo": r.get("provider_geo"),
                "rights": pin.get("rights"),
            }.items()
            if v
        },
    )
