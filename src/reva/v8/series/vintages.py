"""Vintages of an atlas series for the Comparer (as first published vs the latest snapshot value).

Only series with a registered vintage table are served: IMF WEO series (``imf.weo.<code>``) map to the WEO vintage
tables (``atlas.vintage_weo.weo_imf`` then ``weo_dbnomics``), whose indicators are ``<code>|v=<vintage>``
(:mod:`reva.adapters.connectors.vintage_common`). Tables are read through :func:`reva.evaluation.vintages.
load_vintage_cells` (read-only; bytes verified against their sha256) and kept per process by artifact.

* ``first``: per period, the value of the earliest vintage (by ``available_at``) that holds a non-projection value
  for it — "as first published".
* a named vintage: that vintage's values as published (its projection cells flagged, never observations).
"""

from __future__ import annotations

import threading
from typing import Any

import polars as pl

#: atlas table -> vintage dataset keys (first wins for a vintage label present in both).
VINTAGE_TABLES: dict[str, tuple[str, ...]] = {
    "imf.weo": ("atlas.vintage_weo.weo_imf", "atlas.vintage_weo.weo_dbnomics"),
}
PROJECTION_KINDS = ("projection",)

_CACHE: dict[tuple[str, ...], pl.DataFrame] = {}
_LOCK = threading.Lock()


def has_vintages(table: str) -> bool:
    return table in VINTAGE_TABLES


def _cells(conn: Any, store: Any, keys: tuple[str, ...]) -> pl.DataFrame:
    from sqlalchemy import text

    from reva.evaluation.vintages import load_vintage_cells

    shas = tuple(
        conn.execute(
            text(
                """SELECT DISTINCT ON (dataset_key) artifact_sha256 FROM evidence.dataset
                   WHERE concept_id = 'atlas.table' AND origin = 'live' AND dataset_key = ANY(:k)
                   ORDER BY dataset_key, recorded_at DESC, id DESC"""
            ),
            {"k": list(keys)},
        ).scalars()
    )
    with _LOCK:
        hit = _CACHE.get(shas)
    if hit is not None:
        return hit
    df = load_vintage_cells(conn, store, dataset_keys=keys)
    with _LOCK:
        _CACHE.clear()  # one set of vintage tables at a time (bounded memory)
        _CACHE[shas] = df
    return df


def vintages_for(conn: Any, store: Any, table: str, measure: str, isos: list[str]) -> dict[str, Any]:
    keys = VINTAGE_TABLES.get(table)
    if not keys:
        return {"available": False, "reason": "aucune table de millésimes enregistrée pour cette série", "vintages": []}
    df = _cells(conn, store, keys)
    if df.height == 0:
        return {"available": False, "reason": "tables de millésimes non chargées", "vintages": []}
    sub = df.filter(
        pl.col("provider_variable").eq(measure) & pl.col("iso3").is_in(isos) & pl.col("value").is_not_null()
    )
    if sub.height == 0:
        return {"available": False, "reason": f"aucun millésime de « {measure} » pour ces pays", "vintages": []}
    # one row per (vintage, iso3, period): the first listed dataset wins
    order = {k: i for i, k in enumerate(keys)}
    sub = (
        sub.with_columns(pl.col("dataset_key").replace_strict(order, default=99).alias("_o"))
        .sort(["vintage", "iso3", "period_start", "_o"])
        .unique(subset=["vintage", "iso3", "period_start"], keep="first", maintain_order=True)
    )
    vint = (
        sub.group_by("vintage")
        .agg(pl.col("available_at").min(), pl.col("dataset_key").first(), pl.len().alias("cells"))
        .sort("available_at", nulls_last=True)
    )
    vintages = [
        {
            "vintage": r["vintage"],
            "available_at": r["available_at"].isoformat() if r["available_at"] is not None else None,
            "dataset": r["dataset_key"],
            "cells": int(r["cells"]),
        }
        for r in vint.iter_rows(named=True)
    ]
    rows = sub.select(
        "vintage", "iso3", "period_start", "period_end", "value", "unit", "estimate_kind", "available_at", "dataset_key"
    ).to_dicts()
    per: dict[str, dict[str, list[dict[str, Any]]]] = {}
    first: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        proj = str(r["estimate_kind"] or "") in PROJECTION_KINDS
        pt = {
            "p": str(r["period_start"].year),
            "ps": r["period_start"].isoformat(),
            "v": r["value"],
            "unit": r["unit"],
            "proj": proj,
            "vintage": r["vintage"],
            "available_at": r["available_at"].isoformat() if r["available_at"] is not None else None,
            "dataset": r["dataset_key"],
        }
        per.setdefault(r["vintage"], {}).setdefault(r["iso3"], []).append(pt)
        if not proj and r["available_at"] is not None:
            cur = first.setdefault(r["iso3"], {}).get(pt["p"])
            if cur is None or pt["available_at"] < cur["available_at"]:
                first[r["iso3"]][pt["p"]] = pt
    return {
        "available": True,
        "reason": None,
        "vintages": vintages,
        "first": {iso: sorted(d.values(), key=lambda x: x["ps"]) for iso, d in first.items()},
        "by_vintage": per,
        "note": "« première publication » : pour chaque période, la valeur non projetée du plus ancien millésime "
        "publié (available_at) ; chaque point nomme son millésime.",
    }
