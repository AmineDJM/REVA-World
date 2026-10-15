"""Atlas queries: DuckDB over the Parquet tables pinned by one atlas snapshot.

For a snapshot (:mod:`reva.atlas.snapshot`) the pinned Parquet artifacts are streamed from the
artifact store, **re-hashed against their pinned sha256** and checked for the atlas schema,
then loaded once into a per-snapshot DuckDB file (``atlas-<snapshot sha>-v2.duckdb`` in the
cache directory, read-only afterwards). Each distinct (source, dataset, indicator) gets an
integer series id; cells are sorted by (country, series, period) so country reads touch few
row groups and series filters are integer comparisons. Every answer names its snapshot.

Views (catalog concepts from :mod:`reva.atlas.catalog`):

* **best-available cell** per (concept, iso3, year): among the concept's ``source_order``
  series, the first one (lowest rank) that *reports* a value (numeric or text) for that
  calendar-year cell. It always names its source, dataset, indicator, provider code, unit,
  flags, raw artifact sha256 + selector and table artifact, and how many declared sources
  report the same cell. No blending, no imputation; a cell no source reports is absent.
* **per-source values**: every candidate series' cells (all periods, missing-flagged rows too).
* **coverage**: per concept (and per source) countries/cells/years; per country for a concept.
* **country dossier**: every concept of every domain — latest best value + annual series.
* **map**: best values of one concept for one year, all countries.

"Year" means a calendar-year cell (``period_start`` = Jan 1, ``period_end`` = Jan 1 of the next
year). Sub-annual or multi-year cells appear in per-source views only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from reva.atlas.catalog import Catalog
from reva.atlas.snapshot import AtlasSnapshot
from reva.atlas.tables import ATLAS_ARROW_SCHEMA, NAMESPACE, known_country_keys
from reva.kernel.errors import IntegrityFailure, ValidationProblem
from reva.ports.storage import ArtifactStore

DB_FORMAT = "atlas-duckdb/3"
DB_SUFFIX = "-v3.duckdb"
KEEP_SNAPSHOT_DBS = 2

DDL = """
CREATE TABLE series (sid INTEGER PRIMARY KEY, source VARCHAR NOT NULL, dataset VARCHAR NOT NULL,
                     indicator VARCHAR NOT NULL);
CREATE TABLE cells (sid INTEGER NOT NULL, iso3 VARCHAR, year INTEGER, has_value BOOLEAN NOT NULL,
                    concept_hint VARCHAR, provider_geo VARCHAR NOT NULL, entity_qid VARCHAR,
                    period_start DATE NOT NULL, period_end DATE NOT NULL, value VARCHAR, value_text VARCHAR,
                    unit VARCHAR NOT NULL, flags VARCHAR NOT NULL, raw_sha256 VARCHAR NOT NULL,
                    selector VARCHAR NOT NULL, table_sha VARCHAR NOT NULL);
"""
#: Full cell columns selected from ``cells x``.
CELL_SELECT = (
    "x.sid, x.iso3, x.year, x.concept_hint, x.provider_geo, x.entity_qid, x.period_start, x.period_end, x.value, "
    "x.value_text, x.unit, x.flags, x.raw_sha256, x.selector, x.table_sha"
)


def cache_dir() -> Path:
    d = Path(os.environ.get("REVA_ATLAS_CACHE_DIR") or Path(tempfile.gettempdir()) / "reva-atlas-cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch_verified(store: ArtifactStore, sha: str, dest: Path) -> None:
    h = hashlib.sha256()
    with open(dest, "wb") as out:
        for chunk in store.open(NAMESPACE, sha):
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != sha:
        dest.unlink(missing_ok=True)
        raise IntegrityFailure("pinned atlas table bytes do not match their sha256", sha256=sha)
    schema = pq.read_schema(dest)
    if not schema.equals(ATLAS_ARROW_SCHEMA, check_metadata=False):
        raise IntegrityFailure("pinned atlas table does not have the atlas schema", sha256=sha)


def build_snapshot_db(snapshot: AtlasSnapshot, store: ArtifactStore, directory: Path | None = None) -> Path:
    """Materialise (once, under a file lock) the DuckDB file of a snapshot; returns its path."""
    d = directory or cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    final = d / f"atlas-{snapshot.sha256}{DB_SUFFIX}"
    if final.exists():
        return final
    with open(d / f"atlas-{snapshot.sha256}.lock", "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        if final.exists():
            return final
        work = Path(tempfile.mkdtemp(prefix=f"build-{snapshot.sha256[:12]}-", dir=d))
        try:
            files = []
            for t in snapshot.tables:
                p = work / f"{t['artifact_sha256']}.parquet"
                _fetch_verified(store, t["artifact_sha256"], p)
                files.append(str(p))
            tmp_db = work / "atlas.duckdb"
            con = duckdb.connect(str(tmp_db))
            try:
                con.execute(f"SET temp_directory = '{work / 'spill'}'")
                con.execute(DDL)
                if files:
                    listing = "[" + ",".join("'" + f.replace("'", "''") + "'" for f in files) + "]"
                    con.execute(
                        f"""CREATE TEMP VIEW raw AS
                            SELECT *, regexp_extract(filename, '([0-9a-f]{{64}})\\.parquet$', 1) AS table_sha
                            FROM read_parquet({listing}, filename = true)"""
                    )
                    con.execute(
                        """INSERT INTO series
                           SELECT CAST(row_number() OVER (ORDER BY source, dataset, indicator) AS INTEGER),
                                  source, dataset, indicator
                           FROM (SELECT DISTINCT source, dataset, indicator FROM raw)"""
                    )
                    con.execute(
                        """INSERT INTO cells
                           SELECT s.sid, r.iso3,
                                  CASE WHEN month(r.period_start) = 1 AND day(r.period_start) = 1
                                        AND r.period_end = CAST(r.period_start + INTERVAL 1 YEAR AS DATE)
                                       THEN CAST(year(r.period_start) AS INTEGER) END,
                                  (r.value IS NOT NULL OR r.value_text IS NOT NULL),
                                  r.concept_id, r.provider_geo, r.entity_qid, r.period_start, r.period_end, r.value,
                                  r.value_text, r.unit, r.flags, r.raw_sha256, r.selector, r.table_sha
                           FROM raw r JOIN series s USING (source, dataset, indicator)
                           ORDER BY r.iso3 NULLS LAST, s.sid, r.period_start"""
                    )
                # the same cells ordered by series first: series/year filters (maps, coverage) prune row groups
                con.execute("CREATE TABLE cells_s AS SELECT * FROM cells ORDER BY sid, year, iso3")
                n = con.execute("SELECT count(*) FROM cells").fetchone()[0]  # type: ignore[index]
                expected = sum(int(t["row_count"]) for t in snapshot.tables)
                if n != expected:
                    raise IntegrityFailure("atlas snapshot row count mismatch", rows=n, expected=expected)
                con.execute(
                    "CREATE TABLE meta AS SELECT ? AS format, ? AS snapshot_sha256, ? AS origin, ? AS cutoff",
                    [DB_FORMAT, snapshot.sha256, snapshot.origin, snapshot.cutoff.isoformat()],
                )
                con.execute("CHECKPOINT")
            finally:
                con.close()
            os.replace(tmp_db, final)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _evict(d, keep=final)
    return final


def _evict(d: Path, keep: Path) -> None:
    dbs = sorted(d.glob("atlas-*.duckdb"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in dbs[KEEP_SNAPSHOT_DBS:]:
        if p != keep:
            p.unlink(missing_ok=True)


@lru_cache(maxsize=65536)
def _json_text(v: str) -> Any:
    try:
        return json.loads(v)
    except ValueError:  # registered tables were validated; never expected
        return {"unparsed": v}


def _json(v: str | None) -> Any:
    if v is None:
        return None
    return _json_text(v)  # shared, read-only: callers never mutate parsed flags/selectors


class AtlasQuery:
    """Read-only queries over one snapshot's DuckDB file (thread-safe: a cursor per call)."""

    def __init__(self, snapshot: AtlasSnapshot, store: ArtifactStore, directory: Path | None = None):
        self.snapshot = snapshot
        self.path = build_snapshot_db(snapshot, store, directory)
        self._con = duckdb.connect(str(self.path), read_only=True)
        self._lock = threading.Lock()
        self.tables = {t["artifact_sha256"]: t for t in snapshot.tables}
        self._memo: dict[Any, Any] = {}
        rows = self._rows("SELECT sid, source, dataset, indicator FROM series")
        self.series_of: dict[int, tuple[str, str, str]] = {
            r["sid"]: (r["source"], r["dataset"], r["indicator"]) for r in rows
        }
        self.sid_of = {v: k for k, v in self.series_of.items()}

    def close(self) -> None:
        self._con.close()

    # ------------------------------------------------------------------ plumbing
    def _rows(
        self,
        sql: str,
        params: Sequence[Any] = (),
        series: list[tuple[Any, ...]] | None = None,
        extra: dict[str, pa.Table] | None = None,
    ) -> list[dict]:
        with self._lock:
            cur = self._con.cursor()
        try:
            concept_names: list[str] = []
            if series is not None:
                tbl, concept_names = _series_table(series)
                cur.register("cs", tbl)
            for name, tbl in (extra or {}).items():
                cur.register(name, tbl)
            res = cur.execute(sql, list(params))
            names = [d[0] for d in res.description]
            out = [dict(zip(names, r, strict=True)) for r in res.fetchall()]
            if "cidx" in names:  # integer concept index (fast partitioning) -> concept id
                for r in out:
                    r["concept_id"] = concept_names[r.pop("cidx")]
            return out
        finally:
            cur.close()

    def _concept_series(self, catalog: Catalog, concept_ids: Iterable[str]) -> list[tuple[str, int, int]]:
        """(concept_id, rank, sid) for the catalog series present in this snapshot."""
        out = []
        for cid, rank, src, ds, ind in catalog.series_rows(concept_ids):
            sid = self.sid_of.get((src, ds, ind))
            if sid is not None:
                out.append((cid, rank, sid))
        return out

    @staticmethod
    def _sid_filter(series: list[tuple[str, int, int]]) -> str:
        """Explicit integer filter for zone-map pruning on small series sets; large sets rely on
        the join with the registered series table (a long IN list costs more than it prunes)."""
        sids = sorted({int(s[2]) for s in series})
        if len(sids) > 64:
            return "TRUE"
        return "x.sid IN (" + ",".join(str(x) for x in sids) + ")"

    def _series_key(self, sid: int) -> tuple[str, str, str, str]:
        src, ds, ind = self.series_of[sid]
        return src, ds, ind, f"{src}.{ds}.{ind}"

    def _cell(self, r: dict[str, Any]) -> dict[str, Any]:
        t = self.tables.get(r["table_sha"], {})
        src, ds, ind, key = self._series_key(r["sid"])
        out = {
            "value": r["value"],
            "value_text": r["value_text"],
            "unit": r["unit"],
            "source": src,
            "dataset": ds,
            "indicator": ind,
            "series": key,
            "provider_geo": r["provider_geo"],
            "entity_qid": r["entity_qid"],
            "period_start": r["period_start"].isoformat(),
            "period_end": r["period_end"].isoformat(),
            "flags": _json(r["flags"]),
            "provenance": {
                "raw_sha256": r["raw_sha256"],
                "selector": _json(r["selector"]),
                "table_artifact_sha256": r["table_sha"],
                "dataset_key": t.get("dataset_key"),
                "dataset_id": t.get("dataset_id"),
            },
        }
        if r.get("year") is not None:
            out["year"] = int(r["year"])
        if "rank" in r:
            out["source_rank"] = int(r["rank"])
        if "n_sources" in r:
            out["sources_reporting"] = int(r["n_sources"])
        return out

    # ------------------------------------------------------------------ best-available
    def _best_sql(self, series: list[tuple[str, int, int]], where: list[str], cols: str, table: str) -> str:
        """Best cell per (concept, iso3, year): the row of the lowest source rank reporting it (ranks
        are unique per concept; (sid, iso3, period) is unique), with the number of declared sources
        reporting the cell. Partitions on the integer concept index."""
        return f"""
            SELECT s.cidx, s.rank, {cols},
                   count(*) OVER (PARTITION BY s.cidx, x.iso3, x.year) AS n_sources
            FROM {table} x JOIN cs s ON x.sid = s.sid
            WHERE {self._sid_filter(series)} AND {" AND ".join(where)}
            QUALIFY row_number() OVER (PARTITION BY s.cidx, x.iso3, x.year ORDER BY s.rank) = 1"""

    def best_cells(
        self, catalog: Catalog, concept_ids: Iterable[str], *, iso3: str | None = None, year: int | None = None
    ) -> list[dict[str, Any]]:
        """Best-available calendar-year cells (concept, iso3, year) per the concepts' source order."""
        series = self._concept_series(catalog, concept_ids)
        if not series:
            return []
        where, params = ["x.iso3 IS NOT NULL", "x.year IS NOT NULL", "x.has_value"], []
        if iso3 is not None:
            where.append("x.iso3 = ?")
            params.append(iso3)
        if year is not None:
            where.append("x.year = ?")
            params.append(year)
        table = "cells" if iso3 is not None else "cells_s"
        rows = self._rows(self._best_sql(series, where, CELL_SELECT, table), params, series)
        rows.sort(key=lambda r: (r["concept_id"], r["iso3"], r["year"]))
        return [{"concept_id": r["concept_id"], "iso3": r["iso3"], **self._cell(r)} for r in rows]

    def map_values(self, catalog: Catalog, concept_id: str, year: int) -> dict[str, Any]:
        _need(catalog, concept_id)
        cells = self.best_cells(catalog, [concept_id], year=year)
        have = {c["iso3"] for c in cells}
        return {
            "concept": catalog.concepts[concept_id].as_dict(),
            "year": year,
            "values": cells,
            "countries_with_value": len(have),
            "countries_without_value": sorted(known_country_keys() - have),
            "by_source": _count_by(cells, "series"),
        }

    def series(self, catalog: Catalog, concept_id: str, iso3: str) -> dict[str, Any]:
        """Best annual series + every candidate source's cells (all periods) for one country."""
        _need(catalog, concept_id)
        best = self.best_cells(catalog, [concept_id], iso3=iso3)
        series = self._concept_series(catalog, [concept_id])
        rank_of = {sid: rank for _, rank, sid in series}
        cond = f"({self._sid_filter(series)} OR x.concept_hint = ?)" if series else "x.concept_hint = ?"
        rows = self._rows(
            f"SELECT {CELL_SELECT} FROM cells x WHERE x.iso3 = ? AND {cond} ORDER BY x.sid, x.period_start",
            [iso3, concept_id],
        )
        per_source: dict[str, dict[str, Any]] = {}
        for r in sorted(rows, key=lambda r: (r["sid"] not in rank_of, rank_of.get(r["sid"], 0), r["sid"])):
            c = self._cell(r)
            rank = rank_of.get(r["sid"])
            entry = per_source.setdefault(
                c["series"],
                {
                    "series": c["series"],
                    "source": c["source"],
                    "dataset": c["dataset"],
                    "indicator": c["indicator"],
                    "source_rank": rank,
                    "in_source_order": rank is not None,
                    "cells": [],
                },
            )
            entry["cells"].append(c)
        declared = [s.key for s in catalog.concepts[concept_id].source_order]
        return {
            "concept": catalog.concepts[concept_id].as_dict(),
            "iso3": iso3,
            "best": best,
            "per_source": list(per_source.values()),
            "sources_without_data": [k for k in declared if k not in per_source],
        }

    # ------------------------------------------------------------------ coverage
    def coverage(self, catalog: Catalog, domain: str | None = None) -> list[dict[str, Any]]:
        concepts = [c.id for c in catalog.by_domain(domain)]
        key = ("coverage", domain, tuple(sorted(catalog.files.items())), tuple(concepts))
        if key in self._memo:
            return self._memo[key]  # type: ignore[no-any-return]
        series = self._concept_series(catalog, concepts)
        stats: dict[str, dict[str, Any]] = {
            cid: {
                "concept_id": cid,
                "domain": catalog.concepts[cid].domain,
                "countries": 0,
                "cells": 0,
                "year_min": None,
                "year_max": None,
                "by_source": [],
            }
            for cid in concepts
        }
        per_sid: dict[int, dict[str, Any]] = {}
        if series:
            best = self._rows(
                f"""WITH b AS (SELECT s.cidx, x.iso3, x.year FROM cells_s x JOIN cs s ON x.sid = s.sid
                               WHERE {self._sid_filter(series)} AND x.iso3 IS NOT NULL AND x.year IS NOT NULL
                                 AND x.has_value GROUP BY ALL)
                   SELECT cidx, count(DISTINCT iso3) AS countries, count(*) AS cells,
                          min(year) AS year_min, max(year) AS year_max FROM b GROUP BY cidx""",
                (),
                series,
            )
            for r in best:
                stats[r["concept_id"]].update({k: r[k] for k in ("countries", "cells", "year_min", "year_max")})
            for r in self._rows(
                f"""SELECT x.sid, count(DISTINCT x.iso3) AS countries, count(*) AS cells,
                           min(x.year) AS year_min, max(x.year) AS year_max
                    FROM cells_s x WHERE {self._sid_filter(series)} AND x.iso3 IS NOT NULL AND x.has_value
                    GROUP BY x.sid"""
            ):
                per_sid[r["sid"]] = r
        for cid in concepts:
            for rank, s in enumerate(catalog.concepts[cid].source_order):
                r = per_sid.get(self.sid_of.get((s.source, s.dataset, s.indicator), -1), {})
                stats[cid]["by_source"].append(
                    {
                        "series": s.key,
                        "source_rank": rank,
                        "countries": int(r.get("countries", 0)),
                        "cells": int(r.get("cells", 0)),
                        "year_min": r.get("year_min"),
                        "year_max": r.get("year_max"),
                    }
                )
        out = [stats[c] for c in concepts]
        self._memo[key] = out
        return out

    def coverage_by_country(self, catalog: Catalog, concept_id: str) -> list[dict[str, Any]]:
        _need(catalog, concept_id)
        series = self._concept_series(catalog, [concept_id])
        rows = (
            self._rows(
                f"""WITH b AS (SELECT x.iso3, x.year, min(s.rank) AS rank FROM cells_s x JOIN cs s ON x.sid = s.sid
                               WHERE {self._sid_filter(series)} AND x.iso3 IS NOT NULL AND x.year IS NOT NULL
                                 AND x.has_value GROUP BY ALL)
                   SELECT iso3, count(*) AS years, min(year) AS year_min, max(year) AS year_max,
                          list(DISTINCT rank ORDER BY rank) AS ranks
                   FROM b GROUP BY iso3 ORDER BY iso3""",
                (),
                series,
            )
            if series
            else []
        )
        order = [s.key for s in catalog.concepts[concept_id].source_order]
        have = {r["iso3"] for r in rows}
        out = [
            {
                "iso3": r["iso3"],
                "years": int(r["years"]),
                "year_min": r["year_min"],
                "year_max": r["year_max"],
                "best_sources_used": [order[i] for i in r["ranks"]],
            }
            for r in rows
        ]
        out.extend(
            {"iso3": k, "years": 0, "year_min": None, "year_max": None, "best_sources_used": []}
            for k in sorted(known_country_keys() - have)
        )
        return out

    # ------------------------------------------------------------------ dossier
    def dossier(
        self, catalog: Catalog, iso3: str, *, domain: str | None = None, include_series: bool = True
    ) -> dict[str, Any]:
        """Every concept (of every domain, or one): latest best-available cell + annual series."""
        concepts = sorted(catalog.by_domain(domain), key=lambda c: (c.domain, c.subdomain or "", c.id))
        series = self._concept_series(catalog, [c.id for c in concepts])
        where = ["x.iso3 = ?", "x.year IS NOT NULL", "x.has_value"]
        latest: dict[str, dict[str, Any]] = {}
        points: dict[str, list[dict[str, Any]]] = {}
        if series:
            light = "x.sid, x.iso3, x.year, x.value, x.value_text, x.flags"
            for r in self._rows(self._best_sql(series, where, light, "cells"), [iso3], series):
                points.setdefault(r["concept_id"], []).append(r)
            if points:  # full cells (provenance) only for each concept's latest year
                last = [max(p, key=lambda r: r["year"]) for p in points.values()]
                keys = pa.table(
                    {
                        "k": pa.array(list(range(len(last))), pa.int32()),
                        "sid": pa.array([r["sid"] for r in last], pa.int32()),
                        "year": pa.array([r["year"] for r in last], pa.int32()),
                        "rank": pa.array([r["rank"] for r in last], pa.int32()),
                        "n_sources": pa.array([r["n_sources"] for r in last], pa.int64()),
                    }
                )
                full = self._rows(
                    f"""SELECT k.k, k.rank, k.n_sources, {CELL_SELECT}
                        FROM cells x JOIN k ON x.sid = k.sid AND x.year = k.year WHERE x.iso3 = ?""",
                    [iso3],
                    extra={"k": keys},
                )
                latest = {last[r.pop("k")]["concept_id"]: self._cell(r) for r in full}
        domains: dict[str, dict[str, Any]] = {}
        for c in concepts:
            d = domains.setdefault(c.domain, {"domain": c.domain, "concepts": [], "without_data": []})
            pts = points.get(c.id)
            if not pts:
                d["without_data"].append(c.id)
                continue
            pts.sort(key=lambda r: r["year"])
            item: dict[str, Any] = {
                "concept_id": c.id,
                "labels": c.labels,
                "unit": c.unit,
                "subdomain": c.subdomain,
                "latest": latest[c.id],
                "years": len(pts),
                "year_min": pts[0]["year"],
                "year_max": pts[-1]["year"],
                "sources_used": sorted({self._series_key(r["sid"])[3] for r in pts}),
            }
            if include_series:
                item["series"] = [
                    {
                        "year": r["year"],
                        "value": r["value"],
                        "value_text": r["value_text"],
                        "series": self._series_key(r["sid"])[3],
                        "flags": _json(r["flags"]),
                    }
                    for r in pts
                ]
            d["concepts"].append(item)
        other = self._rows("SELECT count(*) AS n FROM cells WHERE iso3 = ? AND year IS NULL AND has_value", [iso3])
        return {
            "iso3": iso3,
            "domains": list(domains.values()),
            "concepts_with_data": sum(len(d["concepts"]) for d in domains.values()),
            "concepts_without_data": sum(len(d["without_data"]) for d in domains.values()),
            "non_annual_cells": int(other[0]["n"]),
        }


def _series_table(series: list[tuple[Any, ...]]) -> tuple[pa.Table, list[str]]:
    """Registered ``cs`` table (cidx, rank, sid) and the concept id of each cidx."""
    names: list[str] = []
    index: dict[str, int] = {}
    cidx = []
    for cid, _, _ in series:
        if cid not in index:
            index[cid] = len(names)
            names.append(cid)
        cidx.append(index[cid])
    return (
        pa.table(
            {
                "cidx": pa.array(cidx, pa.int32()),
                "rank": pa.array([s[1] for s in series], pa.int32()),
                "sid": pa.array([s[2] for s in series], pa.int32()),
            }
        ),
        names,
    )


def _need(catalog: Catalog, concept_id: str) -> None:
    if concept_id not in catalog.concepts:
        raise ValidationProblem("unknown atlas concept", concept=concept_id[:200])


def _count_by(cells: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cells:
        out[c[key]] = out.get(c[key], 0) + 1
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- process cache
_QUERIES: OrderedDict[str, AtlasQuery] = OrderedDict()
_QLOCK = threading.Lock()


def query_for(snapshot: AtlasSnapshot, store: ArtifactStore, directory: Path | None = None) -> AtlasQuery:
    """Process-wide cached :class:`AtlasQuery` per snapshot (at most 2 open)."""
    key = f"{directory or ''}|{snapshot.sha256}"
    with _QLOCK:
        q = _QUERIES.get(key)
        if q is not None:
            _QUERIES.move_to_end(key)
            return q
    q = AtlasQuery(snapshot, store, directory)
    with _QLOCK:
        if key in _QUERIES:
            q.close()
            return _QUERIES[key]
        _QUERIES[key] = q
        while len(_QUERIES) > 2:
            _, old = _QUERIES.popitem(last=False)
            old.close()
    return q
