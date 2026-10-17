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

"Year" view: a calendar-year cell (``[Jan 1, Jan 1 next year)``), or a non-calendar cell spanning
150..1097 days (semester, fiscal year, survey or 3-year window) attached to its provider's
``flags.label_year`` or to the year of its last day; every answer shows its true period and
``period_basis``. Years use astronomical numbering (year 0 = 1 BCE, -9999 = 10000 BCE; same as the KG). Periods
are returned as ISO 8601 text (``-9999-01-01``). Sub-annual or multi-year cells appear in per-source
views only.
"""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import os
import re
import shutil
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from reva.atlas.catalog import Catalog, Series
from reva.atlas.snapshot import AtlasSnapshot, normalise_rights
from reva.atlas.source_order import CellRule, HeadlineConcept, HeadlineRegistry, Operand
from reva.atlas.tables import ATLAS_ARROW_SCHEMA, NAMESPACE, known_country_keys
from reva.kernel.errors import IntegrityFailure, RevaError, ValidationProblem
from reva.ports.storage import ArtifactStore

DB_FORMAT = "atlas-duckdb/6"  # /4: year view attaches non-calendar cells (ATLAS-2), period_basis column
DB_SUFFIX = "-v6.duckdb"
#: Non-calendar cells attach to a year view when they are a point in time (a 1-day "as of" period:
#: GeoNames, Natural Earth, Factbook, WBL, Constitute) or span 150..1097 days (semesters, 12-month
#: fiscal years, survey and 3-year windows): to ``flags.label_year`` when the provider labels the cell
#: with a year inside the period, else to the year containing the period's last day. Monthly/quarterly
#: and longer windows stay in per-source views only and are never reported "absent".
ATTACH_MIN_DAYS = 150
ATTACH_MAX_DAYS = 1097
KEEP_SNAPSHOT_DBS = 2
#: File naming the snapshot whose query file eviction never removes (the latest live one; review V3b SEC-3).
PIN_FILE = "live-snapshot.pin"


class SnapshotNotMaterialised(RevaError):
    """The query file of a snapshot that is not the latest one does not exist: it is never built inside a
    request (review V3b SEC-3: a read-scope ``?snapshot=`` must not force a multi-GB build and evict the live
    file). Query files are built by the worker (``atlas_snapshot`` job with ``warm``) or for the latest
    snapshot."""

    code = "snapshot_not_materialised"
    http_status = 409


DDL = """
CREATE TABLE series (sid INTEGER PRIMARY KEY, source VARCHAR NOT NULL, dataset VARCHAR NOT NULL,
                     indicator VARCHAR NOT NULL);
CREATE TABLE cells (sid INTEGER NOT NULL, iso3 VARCHAR, year INTEGER, has_value BOOLEAN NOT NULL,
                    concept_hint VARCHAR, provider_geo VARCHAR NOT NULL, entity_qid VARCHAR,
                    period_start DATE NOT NULL, period_end DATE NOT NULL, value VARCHAR, value_text VARCHAR,
                    unit VARCHAR NOT NULL, flags VARCHAR NOT NULL, raw_sha256 VARCHAR NOT NULL,
                    selector VARCHAR NOT NULL, table_sha VARCHAR NOT NULL, period_basis VARCHAR NOT NULL);
CREATE TABLE cells_s AS SELECT * FROM cells LIMIT 0;
"""


#: Full cell columns selected from ``cells x``.
def iso_date_sql(col: str) -> str:
    """ISO 8601 text of a DATE in astronomical years (``-9999-01-01`` = 10000 BCE; DuckDB's own
    VARCHAR form is ``10000-01-01 (BC)`` and Python dates stop at year 1)."""
    return (
        f"printf('%s%04d-%02d-%02d', CASE WHEN year({col}) < 0 THEN '-' ELSE '' END, abs(year({col})), "
        f"month({col}), day({col}))"
    )


#: Full cell columns selected from ``cells x`` (periods as astronomical ISO text: BCE-safe).
CELL_SELECT = (
    "x.sid, x.iso3, x.year, x.concept_hint, x.provider_geo, x.entity_qid, "
    f"{iso_date_sql('x.period_start')} AS period_start, {iso_date_sql('x.period_end')} AS period_end, x.value, "
    "x.value_text, x.unit, x.flags, x.raw_sha256, x.selector, x.table_sha, x.period_basis"
)

#: Year attachment of every cell (see ATTACH_*): calendar years; else label_year or the last day's year.
CELLS_SELECT = f"""
WITH pre AS (
  SELECT s.sid, r.*,
         (month(r.period_start) = 1 AND day(r.period_start) = 1
          AND r.period_end = CAST(r.period_start + INTERVAL 1 YEAR AS DATE)) AS cal,
         datediff('day', r.period_start, r.period_end) AS span,
         TRY_CAST(json_extract_string(r.flags, '$.label_year') AS INTEGER) AS label_year,
         CAST(year(CAST(r.period_end - INTERVAL 1 DAY AS DATE)) AS INTEGER) AS last_year
  FROM raw r JOIN series s USING (source, dataset, indicator)),
att AS (
  SELECT *,
         CASE WHEN cal THEN CAST(year(period_start) AS INTEGER)
              WHEN span <= 1 OR span BETWEEN {ATTACH_MIN_DAYS} AND {ATTACH_MAX_DAYS} THEN
                CASE WHEN label_year BETWEEN year(period_start) AND last_year THEN label_year ELSE last_year END
         END AS attach,
         CASE WHEN cal THEN 'calendar_year' WHEN span <= 1 THEN 'point_in_time' WHEN span < 365 THEN 'sub_annual'
              WHEN span <= 366 THEN 'twelve_month_window' ELSE 'multi_year' END AS basis
  FROM pre),
ranked AS (
  SELECT *, row_number() OVER (PARTITION BY sid, coalesce(iso3, '#' || provider_geo), attach
                               ORDER BY cal DESC, (value IS NOT NULL OR value_text IS NOT NULL) DESC,
                                        period_end DESC, period_start DESC) AS rn
  FROM att)
SELECT sid, iso3, CAST(CASE WHEN attach IS NOT NULL AND rn = 1 THEN attach END AS INTEGER) AS year,
       (value IS NOT NULL OR value_text IS NOT NULL) AS has_value, concept_id AS concept_hint, provider_geo,
       entity_qid, period_start, period_end, value, value_text, unit, flags, raw_sha256, selector, table_sha,
       basis AS period_basis
FROM ranked
"""


def cache_dir() -> Path:
    from reva.atlas.cachedir import cache_dir as _private

    return _private()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_db(final: Path, snapshot: AtlasSnapshot) -> None:
    """ATLAS-1: a cached query file is used only if its bytes match the digest recorded when it was
    built and its meta names this snapshot and format; otherwise both files are removed and the read
    is refused (the next read rebuilds from the verified Parquet artifacts)."""
    side = final.with_name(final.name + ".sha256")
    try:
        want = json.loads(side.read_text())
        ok = (
            want.get("format") == DB_FORMAT
            and want.get("snapshot_sha256") == snapshot.sha256
            and want.get("sha256") == _file_sha256(final)
        )
        if ok:
            con = duckdb.connect(str(final), read_only=True)
            try:
                meta = con.execute("SELECT format, snapshot_sha256 FROM meta").fetchone()
            finally:
                con.close()
            ok = meta == (DB_FORMAT, snapshot.sha256)
    except (OSError, ValueError, duckdb.Error):
        ok = False
    if not ok:
        final.unlink(missing_ok=True)
        side.unlink(missing_ok=True)
        raise IntegrityFailure("cached atlas query file failed verification; removed", path=final.name)


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


def build_snapshot_db(
    snapshot: AtlasSnapshot, store: ArtifactStore, directory: Path | None = None, *, build: bool = True
) -> Path:
    """Materialise (once, under a file lock) the DuckDB file of a snapshot; returns its path.

    ``build=False`` (API reads of a snapshot that is not the latest): an existing verified file is used,
    a missing one raises :class:`SnapshotNotMaterialised` instead of being built in the request."""
    from reva.atlas.cachedir import private_dir

    d = private_dir(directory) if directory is not None else cache_dir()
    final = d / f"atlas-{snapshot.sha256}{DB_SUFFIX}"
    if final.exists():
        _verify_db(final, snapshot)
        return final
    if not build:
        raise SnapshotNotMaterialised(
            "the query file of this atlas snapshot is not materialised; only the latest snapshot (or one whose "
            "query file already exists) can be read",
            snapshot=snapshot.sha256,
        )
    lock_fd = os.open(d / f"atlas-{snapshot.sha256}.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        if final.exists():
            _verify_db(final, snapshot)
            return final
        work = Path(tempfile.mkdtemp(prefix=f"build-{snapshot.sha256[:12]}-", dir=d))
        try:
            tmp_db = work / "atlas.duckdb"
            _build_db(snapshot, store, work, tmp_db)
            digest = {"format": DB_FORMAT, "snapshot_sha256": snapshot.sha256, "sha256": _file_sha256(tmp_db)}
            side_tmp = work / "atlas.duckdb.sha256"
            side_tmp.write_text(json.dumps(digest))
            os.replace(side_tmp, final.with_name(final.name + ".sha256"))
            os.replace(tmp_db, final)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _evict(d, keep=final)
    return final


def _build_db(snapshot: AtlasSnapshot, store: ArtifactStore, work: Path, tmp_db: Path) -> None:
    """Load the pinned tables one at a time (bounded memory and spill; ~5 M rows per table at most).

    The series table is built first over every file (three columns only). Each table's cells are then
    computed (year attachment, per series) into a temporary table and appended to ``cells`` (sorted by
    country) and ``cells_s`` (sorted by series). Tables are loaded in (source, dataset) order, so
    ``cells_s`` is globally ordered by series id (ids follow that order); ``cells`` is ordered by
    country within each table's segment (zone maps still prune). Each Parquet copy is deleted as soon
    as it is loaded. Memory: ``REVA_ATLAS_BUILD_MEMORY`` (default 3GB), threads ``REVA_ATLAS_BUILD_THREADS``
    (default 2); spill goes to the private work directory."""
    tables = sorted(snapshot.tables, key=lambda t: str(t["dataset_key"]))
    files: list[Path] = []
    for t in tables:
        p = work / f"{t['artifact_sha256']}.parquet"
        _fetch_verified(store, t["artifact_sha256"], p)
        files.append(p)
    con = duckdb.connect(str(tmp_db))
    try:
        mem = os.environ.get("REVA_ATLAS_BUILD_MEMORY", "3GB")
        threads = int(os.environ.get("REVA_ATLAS_BUILD_THREADS", "2"))
        if not re.fullmatch(r"[0-9]+(\.[0-9]+)?\s*[KMGT]?i?B", mem):
            raise ValidationProblem("REVA_ATLAS_BUILD_MEMORY must look like 3GB", value=mem[:20])
        con.execute(f"SET memory_limit = '{mem}'")
        con.execute(f"SET threads = {max(1, threads)}")
        con.execute("SET preserve_insertion_order = true")
        con.execute(f"SET temp_directory = '{work / 'spill'}'")
        con.execute(DDL)
        if files:
            listing = "[" + ",".join("'" + str(f).replace("'", "''") + "'" for f in files) + "]"
            con.execute(
                f"""INSERT INTO series
                    SELECT CAST(row_number() OVER (ORDER BY source, dataset, indicator) AS INTEGER),
                           source, dataset, indicator
                    FROM (SELECT DISTINCT source, dataset, indicator FROM read_parquet({listing}))"""
            )
        for f in files:
            q = "'" + str(f).replace("'", "''") + "'"
            con.execute(
                f"""CREATE OR REPLACE TEMP VIEW raw AS
                    SELECT *, regexp_extract(filename, '([0-9a-f]{{64}})\\.parquet$', 1) AS table_sha
                    FROM read_parquet({q}, filename = true)"""
            )
            con.execute(f"CREATE OR REPLACE TEMP TABLE t AS {CELLS_SELECT}")
            con.execute("INSERT INTO cells SELECT * FROM t ORDER BY iso3 NULLS LAST, sid, period_start")
            con.execute("INSERT INTO cells_s SELECT * FROM t ORDER BY sid, year, iso3")
            con.execute("DROP TABLE t")
            con.execute("DROP VIEW raw")
            f.unlink(missing_ok=True)
        n = con.execute("SELECT count(*) FROM cells").fetchone()[0]  # type: ignore[index]
        n_s = con.execute("SELECT count(*) FROM cells_s").fetchone()[0]  # type: ignore[index]
        expected = sum(int(t["row_count"]) for t in snapshot.tables)
        if n != expected or n_s != expected:
            raise IntegrityFailure("atlas snapshot row count mismatch", rows=n, expected=expected)
        con.execute(
            "CREATE TABLE meta AS SELECT ? AS format, ? AS snapshot_sha256, ? AS origin, ? AS cutoff",
            [DB_FORMAT, snapshot.sha256, snapshot.origin, snapshot.cutoff.isoformat()],
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()


def pin_snapshot(sha256: str, directory: Path | None = None) -> None:
    """Record ``sha256`` as the live snapshot whose query file :func:`_evict` never removes (SEC-3)."""
    from reva.atlas.cachedir import private_dir

    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValidationProblem("not a snapshot sha256")
    d = private_dir(directory) if directory is not None else cache_dir()
    pin = d / PIN_FILE
    try:
        if pin.read_text().strip() == sha256:
            return
    except OSError:
        pass
    tmp = d / f".{PIN_FILE}.{os.getpid()}.{threading.get_ident()}"
    tmp.write_text(sha256)
    os.replace(tmp, pin)


def _pinned(d: Path) -> str | None:
    try:
        v = (d / PIN_FILE).read_text().strip()
    except OSError:
        return None
    return v if re.fullmatch(r"[0-9a-f]{64}", v) else None


def _evict(d: Path, keep: Path) -> None:
    pinned = _pinned(d)
    dbs = sorted(d.glob("atlas-*.duckdb"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in dbs[KEEP_SNAPSHOT_DBS:]:
        if p == keep or (pinned is not None and p.name == f"atlas-{pinned}{DB_SUFFIX}"):
            continue  # the file just built, and the live snapshot's file, are never evicted
        p.unlink(missing_ok=True)
        p.with_name(p.name + ".sha256").unlink(missing_ok=True)


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

    def __init__(
        self, snapshot: AtlasSnapshot, store: ArtifactStore, directory: Path | None = None, *, build: bool = True
    ):
        self.snapshot = snapshot
        self.path = build_snapshot_db(snapshot, store, directory, build=build)
        self._con = duckdb.connect(str(self.path), read_only=True)
        self._lock = threading.Lock()
        self.tables = {t["artifact_sha256"]: t for t in snapshot.tables}
        # LIC-1: rights recorded with each pinned table artifact (manifest / meta.artifact)
        self._table_rights = {
            sha: with_required_notice(
                normalise_rights(t.get("rights"), basis="table_artifact"), _table_source(t.get("dataset_key"))
            )
            for sha, t in self.tables.items()
        }
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
    ) -> list[dict[str, Any]]:
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

    def _light_source(self, r: dict[str, Any]) -> dict[str, Any]:
        src, ds, _ind, key = self._series_key(r["sid"])
        return {"source": src, "dataset": ds, "series": key}

    def rights_of(self, table_sha: str | None, source: str) -> dict[str, Any] | None:
        """LIC-1: the rights of the table a cell comes from (recorded with the table artifact), else the
        licence its atlas source spec declares (``basis: atlas_source_spec``); ``None`` when neither is known."""
        r = self._table_rights.get(table_sha or "")
        return r if r is not None else source_spec_rights(source)

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
            "period_start": r["period_start"],  # ISO text, astronomical years (BCE-safe)
            "period_end": r["period_end"],
            "period_basis": r.get("period_basis"),
            "flags": _json(r["flags"]),
            "provenance": {
                "raw_sha256": r["raw_sha256"],
                "selector": _json(r["selector"]),
                "table_artifact_sha256": r["table_sha"],
                "dataset_key": t.get("dataset_key"),
                "dataset_id": t.get("dataset_id"),
            },
            "rights": self.rights_of(r["table_sha"], src),
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
        where, params = ["x.iso3 IS NOT NULL", "x.year IS NOT NULL", "x.has_value"], list[Any]()
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

    def _other_period_countries(self, sids: Iterable[int], year: int) -> set[str]:
        """Countries with a value-bearing cell overlapping calendar ``year`` that the year view does not
        attach to it (monthly/quarterly cells, long windows): reported apart, never "absent" (ATLAS-2)."""
        ids = sorted({int(x) for x in sids})
        if not ids:
            return set()
        rows = self._rows(
            f"""SELECT DISTINCT x.iso3 FROM cells_s x
                WHERE x.sid IN ({",".join(str(i) for i in ids)}) AND x.iso3 IS NOT NULL AND x.has_value
                  AND (x.year IS NULL OR x.year <> ?)
                  AND x.period_start < make_date(?, 1, 1) AND x.period_end > make_date(?, 1, 1)""",
            [year, year + 1, year],
        )
        return {r["iso3"] for r in rows}

    def map_values(self, catalog: Catalog, concept_id: str, year: int) -> dict[str, Any]:
        _need(catalog, concept_id)
        cells = self.best_cells(catalog, [concept_id], year=year)
        have = {c["iso3"] for c in cells}
        other = self._other_period_countries((s[2] for s in self._concept_series(catalog, [concept_id])), year) - have
        return {
            "concept": catalog.concepts[concept_id].as_dict(),
            "year": year,
            "values": cells,
            "countries_with_value": len(have),
            "countries_without_value": sorted(known_country_keys() - have - other),
            "countries_with_other_period_cells": sorted(other),
            "by_source": _count_by(cells, "series"),
            "licences": licence_summary(cells),
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
            "licences": licence_summary([*best, *(c for e in per_source.values() for c in e["cells"])]),
        }

    # ------------------------------------------------------------------ one table (KG measured_by bridge)
    def table_cells(
        self,
        catalog: Catalog,
        source: str,
        dataset: str,
        *,
        provider_geo: str | None = None,
        iso3: str | None = None,
        indicators: Sequence[str] = (),
        indicator_contains: str | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        """Cells of one atlas table ``<source>.<dataset>`` selected by what a KG ``measured_by`` statement names
        (provider geography, ISO3, exact indicators or an indicator substring), every period as published.

        Per indicator: cell count, geographies, first/last period; cells (sorted by indicator then period) only when
        a geography is given (``provider_geo`` or ``iso3``), at most ``limit`` in all (``truncated``). A table that
        is not in this snapshot is ``in_snapshot: false`` (absent, never empty-as-zero). ``concepts`` lists the
        catalog concepts whose source order reads this table."""
        sids = [
            sid
            for sid, (src, ds, ind) in self.series_of.items()
            if src == source
            and ds == dataset
            and (not indicators or ind in indicators)
            and (indicator_contains is None or indicator_contains in ind)
        ]
        in_snapshot = any(src == source and ds == dataset for src, ds, _ in self.series_of.values())
        concepts = sorted({cid for cid, _r, src, ds, _i in catalog.series_rows() if src == source and ds == dataset})
        out: dict[str, Any] = {
            "table": f"{source}.{dataset}",
            "source": source,
            "dataset": dataset,
            "in_snapshot": in_snapshot,
            "filters": {
                "provider_geo": provider_geo,
                "iso3": iso3,
                "indicators": list(indicators),
                "indicator_contains": indicator_contains,
            },
            "concepts": concepts,
            "indicators": [],
            "cells": [],
            "truncated": False,
            "licences": [],
        }
        if not sids:
            return out
        where = [f"x.sid IN ({','.join(str(int(x)) for x in sorted(sids))})"]
        params: list[Any] = []
        if provider_geo is not None:
            where.append("x.provider_geo = ?")
            params.append(provider_geo)
        if iso3 is not None:
            where.append("x.iso3 = ?")
            params.append(iso3)
        cond = " AND ".join(where)
        summary = self._rows(
            f"""SELECT x.sid, count(*) AS n_cells, count(*) FILTER (WHERE x.has_value) AS n_values,
                       count(DISTINCT x.provider_geo) AS n_geos, count(DISTINCT x.iso3) AS n_countries,
                       {iso_date_sql("min(x.period_start)")} AS period_first,
                       {iso_date_sql("max(x.period_end)")} AS period_last, min(x.unit) AS unit
                FROM cells_s x WHERE {cond} GROUP BY x.sid ORDER BY x.sid""",
            params,
        )
        for r in summary:
            _src, _ds, ind, key = self._series_key(r["sid"])
            out["indicators"].append(
                {
                    "indicator": ind,
                    "series": key,
                    "cells": int(r["n_cells"]),
                    "values": int(r["n_values"]),
                    "geographies": int(r["n_geos"]),
                    "countries": int(r["n_countries"]),
                    "period_first": r["period_first"],
                    "period_last": r["period_last"],
                    "unit": r["unit"],
                }
            )
        if provider_geo is None and iso3 is None:
            sample = self._rows(f"SELECT DISTINCT x.table_sha FROM cells_s x WHERE {cond} LIMIT 20", params)
            out["licences"] = licence_summary({"source": source, "rights": self.rights_of(t["table_sha"], source)}
                                              for t in sample)  # fmt: skip
            return out
        rows = self._rows(
            f"SELECT {CELL_SELECT} FROM cells_s x WHERE {cond} ORDER BY x.sid, x.period_start, x.period_end LIMIT ?",
            [*params, int(limit) + 1],
        )
        out["truncated"] = len(rows) > limit
        out["cells"] = [self._cell(r) for r in rows[:limit]]
        out["licences"] = licence_summary(out["cells"])
        return out

    # ------------------------------------------------------------------ coverage
    def coverage(self, catalog: Catalog, domain: str | None = None, theme: str | None = None) -> list[dict[str, Any]]:
        concepts = [c.id for c in catalog.by_domain(domain, theme)]
        key = ("coverage", domain, theme, tuple(sorted(catalog.files.items())), tuple(concepts))
        if key in self._memo:
            return self._memo[key]  # type: ignore[no-any-return]
        series = self._concept_series(catalog, concepts)
        stats: dict[str, dict[str, Any]] = {
            cid: {
                "concept_id": cid,
                "domain": catalog.concepts[cid].domain,
                "theme": catalog.concepts[cid].theme,
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
        self,
        catalog: Catalog,
        iso3: str,
        *,
        domain: str | None = None,
        include_series: bool = True,
        theme: str | None = None,
    ) -> dict[str, Any]:
        """Every concept (of every domain, or one; optionally one theme): latest best-available cell
        + annual series."""
        concepts = sorted(catalog.by_domain(domain, theme), key=lambda c: (c.domain, c.subdomain or "", c.id))
        series = self._concept_series(catalog, [c.id for c in concepts])
        where = ["x.iso3 = ?", "x.year IS NOT NULL", "x.has_value"]
        latest: dict[str, dict[str, Any]] = {}
        points: dict[str, list[dict[str, Any]]] = {}
        if series:
            light = (
                "x.sid, x.iso3, x.year, x.value, x.value_text, x.flags, x.unit, x.period_basis, x.table_sha, "
                f"{iso_date_sql('x.period_start')} AS period_start, {iso_date_sql('x.period_end')} AS period_end"
            )
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
                "theme": c.theme,
                "latest": latest[c.id],
                "years": len(pts),
                "year_min": pts[0]["year"],
                "year_max": pts[-1]["year"],
                "sources_used": sorted({self._series_key(r["sid"])[3] for r in pts}),
                "licences": licence_summary(
                    [
                        {**self._light_source(r), "rights": self.rights_of(r["table_sha"], self.series_of[r["sid"]][0])}
                        for r in pts
                    ]
                ),
            }
            if include_series:
                item["series"] = [
                    {
                        "year": r["year"],
                        "value": r["value"],
                        "value_text": r["value_text"],
                        "unit": r["unit"],  # ATLAS-3: a series may switch currency or unit across years
                        "period_start": r["period_start"],
                        "period_end": r["period_end"],
                        "period_basis": r["period_basis"],
                        "series": self._series_key(r["sid"])[3],
                        "flags": _json(r["flags"]),
                    }
                    for r in pts
                ]
            d["concepts"].append(item)
        other = self._rows("SELECT count(*) AS n FROM cells WHERE iso3 = ? AND year IS NULL AND has_value", [iso3])
        return {
            "iso3": iso3,
            "licences": _merge_licences(c.get("licences") or [] for d in domains.values() for c in d["concepts"]),
            "domains": list(domains.values()),
            "concepts_with_data": sum(len(d["concepts"]) for d in domains.values()),
            "concepts_without_data": sum(len(d["without_data"]) for d in domains.values()),
            "non_annual_cells": int(other[0]["n"]),
        }

    # ------------------------------------------------------------------ headline concepts (ATLASOPS)
    def _resolve(
        self, specs: Sequence[_Spec], *, iso3: str | None = None, year: int | None = None, include: bool = False
    ) -> dict[str, dict[tuple[str, int], dict[str, Any]]]:
        """Per spec and (iso3, year): ranks with an eligible value, projection-only ranks (own flags or a
        registry cell rule), unit mismatches, ranks not equivalent for the country, the values of every
        eligible rank, and the best cell: the lowest-rank eligible actual value, else (projections allowed or
        ``include``) the lowest-rank projection. One DuckDB pass for every spec."""
        hs: list[tuple[int, int, int, str | None, bool]] = []
        hr: list[tuple[int, int, int, int | None, str | None, str | None]] = []
        hx: list[tuple[int, int, str]] = []
        for i, sp in enumerate(specs):
            for rank, (ser, _scale, unit, _j) in enumerate(sp.series):
                sid = self.sid_of.get((ser.source, ser.dataset, ser.indicator))
                if sid is None:
                    continue
                hs.append((i, rank, sid, unit, sp.allow_projections or include))
                for ri, rule in enumerate(sp.rules[rank] if sp.rules else ()):
                    hr.append((i, rank, ri, rule.year_after, rule.footnote_matches, rule.unless_flag))
                for c in sorted(sp.excluded[rank] if sp.excluded else {}):
                    hx.append((i, rank, c))
        out: dict[str, dict[tuple[str, int], dict[str, Any]]] = {sp.key: {} for sp in specs}
        if not hs:
            return out
        tbl = pa.table(
            {
                "ci": pa.array([h[0] for h in hs], pa.int32()),
                "rank": pa.array([h[1] for h in hs], pa.int32()),
                "sid": pa.array([h[2] for h in hs], pa.int32()),
                "unit": pa.array([h[3] for h in hs], pa.string()),
                "allow": pa.array([h[4] for h in hs], pa.bool_()),
            }
        )
        rules = pa.table(
            {
                "ci": pa.array([h[0] for h in hr], pa.int32()),
                "rank": pa.array([h[1] for h in hr], pa.int32()),
                "ri": pa.array([h[2] for h in hr], pa.int32()),
                "year_after": pa.array([h[3] for h in hr], pa.int32()),
                "footnote": pa.array([h[4] for h in hr], pa.string()),
                "unless_flag": pa.array([h[5] for h in hr], pa.string()),
            }
        )
        excl = pa.table(
            {
                "ci": pa.array([h[0] for h in hx], pa.int32()),
                "rank": pa.array([h[1] for h in hx], pa.int32()),
                "iso3": pa.array([h[2] for h in hx], pa.string()),
            }
        )
        where, params = ["x.iso3 IS NOT NULL", "x.year IS NOT NULL", "x.has_value"], list[Any]()
        if iso3 is not None:
            where.append("x.iso3 = ?")
            params.append(iso3)
        if year is not None:
            where.append("x.year = ?")
            params.append(year)
        sids = sorted({h[2] for h in hs})
        sidf = "TRUE" if len(sids) > 64 else "x.sid IN (" + ",".join(str(x) for x in sids) + ")"
        table = "cells" if iso3 is not None else "cells_s"
        ok = "num AND NOT noneq AND NOT bad_unit AND (NOT isproj OR allow)"
        rule_sql = (
            """,
            m AS (SELECT c0.ci, c0.rank, c0.iso3, c0.year, min(r.ri) AS ri
                    FROM c0 JOIN hr r ON r.ci = c0.ci AND r.rank = c0.rank
                   WHERE (r.year_after IS NULL OR c0.year > r.year_after)
                     AND (r.footnote IS NULL
                          OR regexp_matches(coalesce(json_extract_string(c0.flags, '$.footnote'), ''), r.footnote))
                     AND (r.unless_flag IS NULL OR json_extract_string(c0.flags, '$.' || r.unless_flag) IS NULL)
                   GROUP BY ALL)"""
            if hr
            else ", m AS (SELECT ci, rank, iso3, year, CAST(NULL AS INTEGER) AS ri FROM c0 WHERE FALSE)"
        )
        sql = f"""
            WITH c0 AS (
              SELECT h.ci, h.rank, h.allow, x.iso3, x.year, x.flags,
                     {PROJECTION_SQL} AS own_proj,
                     (h.unit IS NOT NULL AND x.unit <> h.unit) AS bad_unit,
                     (x.value IS NOT NULL) AS num,
                     {{'sid': x.sid, 'value': x.value, 'value_text': x.value_text, 'unit': x.unit,
                       'flags': x.flags, 'provider_geo': x.provider_geo, 'entity_qid': x.entity_qid,
                       'period_start': {iso_date_sql("x.period_start")}, 'period_end': {iso_date_sql("x.period_end")},
                       'raw_sha256': x.raw_sha256, 'selector': x.selector, 'table_sha': x.table_sha,
                       'period_basis': x.period_basis}} AS cell
              FROM {table} x JOIN hs h ON x.sid = h.sid
              WHERE {sidf} AND {" AND ".join(where)}){rule_sql},
            c AS (
              SELECT c0.*, m.ri, (hx.iso3 IS NOT NULL) AS noneq, (c0.own_proj OR m.ri IS NOT NULL) AS isproj,
                     c0.rank + CASE WHEN c0.own_proj OR m.ri IS NOT NULL THEN 1000 ELSE 0 END AS sortkey
              FROM c0 LEFT JOIN m ON m.ci = c0.ci AND m.rank = c0.rank AND m.iso3 = c0.iso3 AND m.year = c0.year
                      LEFT JOIN hx ON hx.ci = c0.ci AND hx.rank = c0.rank AND hx.iso3 = c0.iso3)
            SELECT ci, iso3, year,
                   list(rank ORDER BY rank) FILTER (WHERE {ok}) AS ok_ranks,
                   list(rank ORDER BY rank) FILTER (WHERE own_proj AND NOT allow AND NOT noneq) AS projected_ranks,
                   list({{'rank': rank, 'ri': ri}} ORDER BY rank)
                     FILTER (WHERE ri IS NOT NULL AND NOT own_proj AND NOT allow AND NOT noneq) AS rule_ranks,
                   list(rank ORDER BY rank) FILTER (WHERE bad_unit AND NOT (isproj AND NOT allow) AND NOT noneq)
                     AS unit_ranks,
                   list(rank ORDER BY rank) FILTER (WHERE noneq) AS noneq_ranks,
                   list({{'rank': rank, 'value': cell.value, 'proj': isproj}} ORDER BY rank) FILTER (WHERE {ok})
                     AS ok_values,
                   arg_min(cell, sortkey) FILTER (WHERE {ok}) AS best,
                   arg_min(rank, sortkey) FILTER (WHERE {ok}) AS best_rank,
                   arg_min(ri, sortkey) FILTER (WHERE {ok}) AS best_rule,
                   arg_min(isproj, sortkey) FILTER (WHERE {ok}) AS best_proj
            FROM c GROUP BY ci, iso3, year"""
        for r in self._rows(sql, params, extra={"hs": tbl, "hr": rules, "hx": excl}):
            out[specs[r.pop("ci")].key][(r["iso3"], int(r["year"]))] = r
        return out

    def _consulted(self, sp: _Spec, row: dict[str, Any] | None, iso3: str | None = None) -> list[dict[str, Any]]:
        ok = set((row or {}).get("ok_ranks") or [])
        proj = set((row or {}).get("projected_ranks") or [])
        ruled = {int(x["rank"]): int(x["ri"]) for x in (row or {}).get("rule_ranks") or []}
        unit = set((row or {}).get("unit_ranks") or [])
        noneq = set((row or {}).get("noneq_ranks") or [])
        best = (row or {}).get("best_rank")
        out = []
        for rank, (ser, _s, _u, _j) in enumerate(sp.series):
            extra: dict[str, Any] = {}
            excluded = sp.excluded[rank] if sp.excluded else {}
            if iso3 is not None and iso3 in excluded:
                st = "not_equivalent_for_country"
                extra["reason"] = excluded[iso3]
            elif (ser.source, ser.dataset, ser.indicator) not in self.sid_of:
                st = "series_not_in_snapshot"
            elif best is not None and rank == best:
                st = "answered"
            elif rank in ok:
                st = "also_reported"
            elif rank in noneq:
                st = "not_equivalent_for_country"
            elif rank in proj:
                st = "projection_only"
            elif rank in ruled:
                rule = sp.rules[rank][ruled[rank]]
                st = rule.estimate_kind  # projection_upstream | nowcast | extrapolated
                extra["rule"] = rule.label.get("en")
            elif rank in unit:
                st = "unit_mismatch"
            else:
                st = "no_value"
            out.append({"series": ser.key, "source_rank": rank, "status": st, **extra})
        return out

    def _reported_cell(self, sp: _Spec, unit: str | None, key: tuple[str, int], row: dict[str, Any]) -> dict[str, Any]:
        rank = int(row["best_rank"])
        ser, scale, _u, just = sp.series[rank]
        c = self._cell({**row["best"], "year": key[1]})
        flags = c["flags"]
        if row.get("best_rule") is not None and sp.rules:
            rule = sp.rules[rank][int(row["best_rule"])]
            # never mutate the shared parsed flags: a copy with the rule that marks the cell (HL-2 / LAB-1)
            flags = {
                **(flags if isinstance(flags, dict) else {}),
                "estimate_kind": rule.estimate_kind,
                "estimate_rule": {
                    "label": dict(rule.label),
                    "upstream": rule.upstream,
                    "basis": rule.basis,
                    "registry": "registry/atlas/source_order",
                },
            }
        return {
            "concept_id": sp.key,
            "iso3": key[0],
            "year": key[1],
            "value": _scaled(c["value"], scale),
            # the concept unit only when it is one fixed unit; "named per cell" units stay the cell's own
            "unit": c["unit"] if (unit is None or "per cell" in unit) else unit,
            "method": "reported",
            "projection": bool(row.get("best_proj")),
            "answered_by": {
                "series": ser.key,
                "source": ser.source,
                "dataset": ser.dataset,
                "indicator": ser.indicator,
                "source_rank": rank,
                "justification": just,
            },
            "reported": {"value": c["value"], "unit": c["unit"], "scale": str(scale)},
            "provider_geo": c["provider_geo"],
            "period_start": c["period_start"],
            "period_end": c["period_end"],
            "period_basis": c.get("period_basis"),
            "flags": flags,
            "provenance": c["provenance"],
            "rights": c["rights"],
            "consulted": self._consulted(sp, row, key[0]),
            "derived": None,
        }

    def _headline(
        self,
        reg: HeadlineRegistry,
        concept_ids: Iterable[str],
        *,
        iso3: str | None = None,
        year: int | None = None,
        include_projections: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Best-available headline cells (reported, else derived) per concept and (iso3, year).

        Without a year filter every change of answering series inside one country's series is flagged on the
        cell where it happens (``source_break``; review V3b HL-1)."""
        concepts = [reg.concepts[c] for c in concept_ids]
        specs: dict[str, _Spec] = {c.id: _spec_of(c) for c in concepts}
        alts = {c.id: _alternatives(c, reg) for c in concepts}
        for c in concepts:
            for a, b in alts[c.id]:
                for o in (a, b):
                    sp = _operand_spec(o, reg)
                    specs.setdefault(sp.key, sp)
        res = self._resolve(list(specs.values()), iso3=iso3, year=year, include=include_projections)
        out: dict[str, dict[str, Any]] = {}
        for c in concepts:
            sp = specs[c.id]
            rows = res[c.id]
            cells = {k: self._reported_cell(sp, c.unit, k, r) for k, r in rows.items() if r["best_rank"] is not None}
            if c.derived:
                for i, (a, b) in enumerate(alts[c.id]):
                    sa, sb = specs[_operand_spec(a, reg).key], specs[_operand_spec(b, reg).key]
                    ra, rb = res[sa.key], res[sb.key]
                    for k in sorted(set(ra) & set(rb)):
                        if k in cells or ra[k]["best_rank"] is None or rb[k]["best_rank"] is None:
                            continue
                        d = self._derived_cell(reg, c, i, (a, sa), (b, sb), k, ra[k], rb[k], rows.get(k))
                        if d is not None:
                            cells[k] = d
            if year is None:
                _mark_source_breaks(cells, rows, sp)
            out[c.id] = {"concept": c, "spec": sp, "cells": cells, "rows": rows}
        return out

    def _derived_cell(
        self,
        reg: HeadlineRegistry,
        c: HeadlineConcept,
        alt: int,
        a: tuple[Operand, _Spec],
        b: tuple[Operand, _Spec],
        key: tuple[str, int],
        ra: dict[str, Any],
        rb: dict[str, Any],
        own: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        assert c.derived is not None
        unit_of = {o.concept: reg.concepts[o.concept].unit for o, _ in (a, b) if o.concept}
        ca = self._reported_cell(a[1], unit_of.get(a[0].concept or ""), key, ra)
        cb = self._reported_cell(b[1], unit_of.get(b[0].concept or ""), key, rb)
        try:
            va, vb = Decimal(ca["value"]), Decimal(cb["value"])
        except InvalidOperation:  # pragma: no cover - registered values are strict decimals
            return None
        if c.derived.formula == "ratio":
            if vb <= 0:
                return None
            v = Context(prec=DERIVED_PRECISION).divide(va, vb)
            formula, precision = f"{a[0].label} / {b[0].label}", f"{DERIVED_PRECISION} significant digits"
        else:
            v, formula, precision = va - vb, f"{a[0].label} - {b[0].label}", "exact"
        v = v * c.derived.scale
        consulted = self._consulted(_spec_of(c), own, key[0])
        consulted.append({"series": f"derived: {formula}", "source_rank": len(c.series), "status": "answered"})
        return {
            "concept_id": c.id,
            "iso3": key[0],
            "year": key[1],
            "value": _dec_text(v),
            "unit": c.unit,
            "method": "derived",
            "projection": bool(ca.get("projection") or cb.get("projection")),
            "answered_by": None,
            "reported": None,
            "provider_geo": None,
            "period_start": ca["period_start"],
            "period_end": ca["period_end"],
            "period_basis": ca.get("period_basis"),
            "flags": _derived_flags(ca["flags"], cb["flags"]),
            "provenance": None,
            "rights": next((x for x in (ca["rights"], cb["rights"]) if x and x.get("restricted")), ca["rights"]),
            "consulted": consulted,
            "derived": {
                "formula": c.derived.formula,
                "expression": formula,
                "alternative": alt,
                "precision": precision,
                "justification": c.derived.justification,
                "inputs": [ca, cb],
            },
        }

    @staticmethod
    def _answer_key(cell: dict[str, Any]) -> str:
        return "derived" if cell["method"] == "derived" else cell["answered_by"]["series"]

    def headline_map(
        self, reg: HeadlineRegistry, concept_id: str, year: int, *, projections: str = "exclude"
    ) -> dict[str, Any]:
        """``projections="include"``: cells that are projections (own flags or a registry cell rule) may answer
        where no actual value exists, flagged (``projection: true``, ``flags.estimate_kind``)."""
        _need_headline(reg, concept_id)
        h = self._headline(reg, [concept_id], year=year, include_projections=_include(projections))[concept_id]
        cells = sorted(h["cells"].values(), key=lambda x: x["iso3"])
        have = {x["iso3"] for x in cells}
        sids = [
            self.sid_of[k]
            for ser, _sc, _u, _j in h["spec"].series
            if (k := (ser.source, ser.dataset, ser.indicator)) in self.sid_of
        ]
        other = self._other_period_countries(sids, year) - have
        without = [
            {"iso3": k, "consulted": self._consulted(h["spec"], h["rows"].get((k, year)), k)}
            for k in sorted(known_country_keys() - have - other)
        ]
        return {
            "concept": reg.concepts[concept_id].as_dict(),
            "year": year,
            "projections": projections,
            "values": cells,
            "countries_with_value": len(have),
            "countries_without_value": without,
            "countries_with_other_period_cells": sorted(other),
            "by_source": _count(self._answer_key(x) for x in cells),
            "projection_values": sum(1 for x in cells if x.get("projection")),
            "licences": licence_summary(_answer_sources(cells)),
        }

    def headline_series(
        self, reg: HeadlineRegistry, concept_id: str, iso3: str, *, projections: str = "exclude"
    ) -> dict[str, Any]:
        _need_headline(reg, concept_id)
        h = self._headline(reg, [concept_id], iso3=iso3, include_projections=_include(projections))[concept_id]
        cells = sorted(h["cells"].values(), key=lambda x: x["year"])
        per: dict[int, list[int]] = {}
        for (_, y), r in h["rows"].items():
            for rank in r.get("ok_ranks") or []:
                per.setdefault(int(rank), []).append(y)
        by_series = []
        for rank, (ser, _s, _u, _j) in enumerate(h["spec"].series):
            ys = sorted(per.get(rank, []))
            by_series.append(
                {
                    "series": ser.key,
                    "source_rank": rank,
                    "in_snapshot": (ser.source, ser.dataset, ser.indicator) in self.sid_of,
                    "years": len(ys),
                    "year_min": ys[0] if ys else None,
                    "year_max": ys[-1] if ys else None,
                    "years_answered": sum(
                        1 for x in cells if x["answered_by"] and x["answered_by"]["series"] == ser.key
                    ),
                }
            )
        return {
            "concept": reg.concepts[concept_id].as_dict(),
            "iso3": iso3,
            "projections": projections,
            "best": cells,
            "by_series": by_series,
            "derived_years": sum(1 for x in cells if x["method"] == "derived"),
            "sources_without_data": [b["series"] for b in by_series if b["years"] == 0],
            "source_breaks": [x["source_break"] for x in cells if x.get("source_break")],
            "projection_years": [x["year"] for x in cells if x.get("projection")],
            "licences": licence_summary(_answer_sources(cells)),
        }

    def headline_dossier(
        self,
        reg: HeadlineRegistry,
        iso3: str,
        *,
        theme: str | None = None,
        include_series: bool = True,
        projections: str = "exclude",
    ) -> dict[str, Any]:
        """Every headline concept for one country: latest best value (who answered, who was
        consulted and had nothing) + the best annual series (source breaks flagged)."""
        concepts = sorted(reg.by_theme(theme), key=lambda c: (c.theme, c.id))
        hs = (
            self._headline(reg, [c.id for c in concepts], iso3=iso3, include_projections=_include(projections))
            if concepts
            else {}
        )
        items, without = [], []
        for c in concepts:
            h = hs[c.id]
            pts = sorted(h["cells"].values(), key=lambda x: x["year"])
            if not pts:
                without.append(
                    {"concept_id": c.id, "theme": c.theme, "consulted": self._consulted(h["spec"], None, iso3)}
                )
                continue
            item: dict[str, Any] = {
                "concept_id": c.id,
                "theme": c.theme,
                "labels": c.labels,
                "unit": c.unit,
                "latest": pts[-1],
                "years": len(pts),
                "year_min": pts[0]["year"],
                "year_max": pts[-1]["year"],
                "sources_used": sorted({self._answer_key(x) for x in pts}),
                "source_breaks": [x["source_break"] for x in pts if x.get("source_break")],
                "licences": licence_summary(_answer_sources(pts)),
            }
            if include_series:
                item["series"] = [
                    {
                        "year": x["year"],
                        "value": x["value"],
                        "unit": x["unit"],
                        "period_start": x["period_start"],
                        "period_end": x["period_end"],
                        "method": x["method"],
                        "series": self._answer_key(x),
                        **({"projection": True} if x.get("projection") else {}),
                        **({"source_break": x["source_break"]} if x.get("source_break") else {}),
                    }
                    for x in pts
                ]
            items.append(item)
        return {
            "concepts": items,
            "without_data": without,
            "projections": projections,
            "licences": _merge_licences(i["licences"] for i in items),
        }

    def headline_coverage(self, reg: HeadlineRegistry, theme: str | None = None) -> list[dict[str, Any]]:
        concepts = sorted(reg.by_theme(theme), key=lambda c: (c.theme, c.id))
        key = ("headline_coverage", theme, reg.fingerprint())
        if key in self._memo:
            return self._memo[key]  # type: ignore[no-any-return]
        hs = self._headline(reg, [c.id for c in concepts]) if concepts else {}
        out = []
        known = known_country_keys()
        for c in concepts:
            cells = list(hs[c.id]["cells"].values())
            latest: dict[str, dict[str, Any]] = {}
            for x in cells:
                if x["iso3"] not in latest or x["year"] > latest[x["iso3"]]["year"]:
                    latest[x["iso3"]] = x
            answered: dict[str, set[str]] = {}
            for x in cells:
                answered.setdefault(self._answer_key(x), set()).add(x["iso3"])
            years = [x["year"] for x in cells]
            out.append(
                {
                    "concept_id": c.id,
                    "theme": c.theme,
                    "countries": len(latest),
                    "cells": len(cells),
                    "year_min": min(years) if years else None,
                    "year_max": max(years) if years else None,
                    "derived_cells": sum(1 for x in cells if x["method"] == "derived"),
                    "latest_year_answered_by": _count(self._answer_key(x) for x in latest.values()),
                    "countries_answered_by": {k: len(v) for k, v in sorted(answered.items())},
                    "series_in_snapshot": [
                        s.key
                        for s in c.series
                        if (s.series.source, s.series.dataset, s.series.indicator) in self.sid_of
                    ],
                    "series_not_in_snapshot": [
                        s.key
                        for s in c.series
                        if (s.series.source, s.series.dataset, s.series.indicator) not in self.sid_of
                    ],
                    "countries_without_value": sorted(known - set(latest)),
                }
            )
        self._memo[key] = out
        return out


# --------------------------------------------------------------------------- headline concepts (ATLASOPS)
#: A cell is a projection when its provider says so: WEO/ILO-style estimate_kind, a truthy
#: ``projection`` flag (WPP/WHO booleans, ILO text) or the WDI's ``period_after_vintage``.
PROJECTION_SQL = (
    "(coalesce(json_extract_string(x.flags, '$.estimate_kind'), '') = 'projection' "
    "OR coalesce(json_extract_string(x.flags, '$.projection'), 'false') NOT IN ('false', '') "
    "OR coalesce(json_extract_string(x.flags, '$.period_after_vintage'), 'false') = 'true')"
)
DERIVED_PRECISION = 12  # significant digits of a derived ratio (an exact formula, rounded once)


def _table_source(dataset_key: Any) -> str:
    """``atlas.<source>.<dataset>`` -> ``<source>``."""
    parts = str(dataset_key or "").split(".")
    return parts[1] if len(parts) >= 3 else ""


def required_notices(licences: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """The distinct notices that must be shown with an answer (one per source), from its licence summary."""
    seen: dict[str, dict[str, str]] = {}
    for x in licences:
        n = x.get("required_notice")
        if n and str(x.get("source")) not in seen:
            seen[str(x.get("source"))] = {"source": str(x.get("source")), "notice": str(n)}
    return sorted(seen.values(), key=lambda d: d["source"])


def _dec_text(d: Decimal) -> str:
    """A finite Decimal as a strict decimal string (no exponent, no trailing zeros)."""
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    t = format(d.normalize(), "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _scaled(value: str, scale: Decimal) -> str:
    return value if scale == 1 else _dec_text(Decimal(value) * scale)


@dataclass(frozen=True)
class _Spec:
    """One concept (or derived operand) to resolve: ordered (series, scale, reported unit), with each
    series' registry cell rules and the countries it is not equivalent for (review V3b HL-1/HL-2)."""

    key: str
    series: tuple[tuple[Series, Decimal, str | None, str | None], ...]  # (series, scale, unit, justification)
    allow_projections: bool = False
    rules: tuple[tuple[CellRule, ...], ...] = ()  # per rank
    excluded: tuple[dict[str, str], ...] = ()  # per rank: iso3 -> reason


def _spec_of(c: HeadlineConcept) -> _Spec:
    return _Spec(
        c.id,
        tuple((s.series, s.scale, s.reported_unit, s.justification) for s in c.series),
        c.projections == "allow",
        tuple(s.cell_rules for s in c.series),
        tuple(s.excluded_countries() for s in c.series),
    )


def _include(projections: str) -> bool:
    if projections not in ("exclude", "include"):
        raise ValidationProblem("projections must be exclude or include", projections=str(projections)[:20])
    return projections == "include"


def _answer_sources(cells: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """(source, rights) of headline cells, derived cells through their two inputs."""
    out: list[dict[str, Any]] = []
    for x in cells:
        if x.get("method") == "derived" and x.get("derived"):
            out.extend(
                {"source": i["answered_by"]["source"], "rights": i.get("rights")} for i in x["derived"]["inputs"]
            )
        elif x.get("answered_by"):
            out.append({"source": x["answered_by"]["source"], "rights": x.get("rights")})
    return out


def _mark_source_breaks(cells: dict[tuple[str, int], dict[str, Any]], rows: dict[Any, Any], sp: _Spec) -> None:
    """HL-1: where the answering series of a country's headline series changes from one answered year to the
    next, the later cell gets ``source_break`` (from, to, years) with both series' values in the last year
    both report (``overlap``), so a level shift between definitions is visible and never read as a change."""
    by_iso: dict[str, list[dict[str, Any]]] = {}
    for (iso3, _y), cell in cells.items():
        by_iso.setdefault(iso3, []).append(cell)
    years_of: dict[str, list[int]] = {}
    for iso3, y in rows:
        years_of.setdefault(iso3, []).append(int(y))
    rank_of = {ser.key: r for r, (ser, _sc, _u, _j) in enumerate(sp.series)}
    for iso3, pts in by_iso.items():
        pts.sort(key=lambda x: x["year"])
        for prev, cur in itertools.pairwise(pts):
            fa, fb = AtlasQuery._answer_key(prev), AtlasQuery._answer_key(cur)
            if fa == fb:
                continue
            brk: dict[str, Any] = {
                "from": fa,
                "to": fb,
                "from_year": prev["year"],
                "year": cur["year"],
                "overlap": None,
                "note": "the answering series changes here: values on either side come from different sources "
                "(and possibly definitions); compare them only through the overlap year",
            }
            ra, rb = rank_of.get(fa), rank_of.get(fb)
            if ra is not None and rb is not None:
                for y in sorted((y for y in years_of.get(iso3, ()) if y <= prev["year"]), reverse=True):
                    vals = {int(v["rank"]): v for v in rows[(iso3, y)].get("ok_values") or []}
                    if ra in vals and rb in vals:
                        va = Decimal(_scaled(vals[ra]["value"], sp.series[ra][1]))
                        vb = Decimal(_scaled(vals[rb]["value"], sp.series[rb][1]))
                        brk["overlap"] = {
                            "year": y,
                            "from_value": _dec_text(va),
                            "to_value": _dec_text(vb),
                            "ratio": _dec_text(Context(prec=6).divide(vb, va)) if va != 0 else None,
                        }
                        break
            cur["source_break"] = brk


def _alternatives(c: HeadlineConcept, reg: HeadlineRegistry) -> list[tuple[Operand, Operand]]:
    """Operand pairs of a derived concept. With ``same_source`` (the default; review ATLAS-6: no value
    combines two providers) a pair naming headline concepts is expanded into the pairs of their listed
    series that come from the same provider (``source``), in the first operand's order."""
    if c.derived is None:
        return []
    out: list[tuple[Operand, Operand]] = []
    for a, b in c.derived.alternatives:
        if not c.derived.same_source or (a.concept is None and b.concept is None):
            if c.derived.same_source and a.series is not None and b.series is not None:
                if a.series.source != b.series.source:
                    continue  # refused at load time as well
            out.append((a, b))
            continue

        def series_of(o: Operand) -> list[Operand]:
            if o.concept is None:
                return [o]
            return [
                Operand(series=s.series, scale=s.scale, reported_unit=s.reported_unit)
                for s in reg.concepts[o.concept].series
            ]

        for x in series_of(a):
            for y in series_of(b):
                if x.series is not None and y.series is not None and x.series.source == y.series.source:
                    out.append((x, y))
    return out


def _derived_flags(fa: Any, fb: Any) -> dict[str, Any]:
    """Flags of a derived cell: both inputs' flags, and any honesty-relevant flag lifted to the top."""
    out: dict[str, Any] = {"inputs_flags": [fa or {}, fb or {}]}
    for k in ("estimate_kind", "projection", "estimate", "provider_flag", "estimate_type", "period_basis"):
        vals = [f.get(k) for f in (fa or {}, fb or {}) if isinstance(f, dict) and f.get(k) not in (None, False, "")]
        if vals:
            out[k] = vals[0] if len(vals) == 1 or vals[0] == vals[1] else vals
    return out


def _operand_spec(o: Operand, reg: HeadlineRegistry) -> _Spec:
    if o.concept is not None:
        return _spec_of(reg.concepts[o.concept])
    assert o.series is not None
    # a series operand keeps the cell rules / country exceptions its headline listings declare (a derived
    # density never turns a WDI population projection into an actual)
    rules: tuple[CellRule, ...] = ()
    excluded: dict[str, str] = {}
    for c in reg.concepts.values():
        for s in c.series:
            if s.key == o.series.key:
                rules = rules or s.cell_rules
                excluded.update(s.excluded_countries())
    return _Spec(f"series:{o.series.key}", ((o.series, o.scale, o.reported_unit, None),), False, (rules,), (excluded,))


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


def _need_headline(reg: HeadlineRegistry, concept_id: str) -> None:
    if concept_id not in reg.concepts:
        raise ValidationProblem("unknown headline concept", concept=concept_id[:200])


def _count(keys: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in keys:
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _count_by(cells: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cells:
        out[c[key]] = out.get(c[key], 0) + 1
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- rights (review V3b LIC-1)
_SPEC_RIGHTS: dict[str, dict[str, Any] | None] = {}
_SPEC_LOCK = threading.Lock()


def source_spec_rights(source: str) -> dict[str, Any] | None:
    """Rights from the atlas source spec(s) declaring ``source`` (the licence every connector run records
    by default); the most restrictive when several specs share the table key. Cached per process."""
    with _SPEC_LOCK:
        if source in _SPEC_RIGHTS:
            return _SPEC_RIGHTS[source]
    try:
        from reva.atlas.sources import atlas_sources

        specs = [e.spec for e in atlas_sources().values() if e.spec.atlas_source == source]
    except Exception:  # pragma: no cover - a broken connector module never breaks a read
        specs = []
    cands = [
        x
        for sp in sorted(specs, key=lambda sp: sp.name)
        if (x := normalise_rights({"licence": sp.licence, "provider": sp.provider}, basis="atlas_source_spec"))
    ]
    out = with_required_notice(next((x for x in cands if x["restricted"]), cands[0] if cands else None), source)
    with _SPEC_LOCK:
        _SPEC_RIGHTS[source] = out
    return out


_NOTICES: dict[str, dict[str, str]] = {}


def registry_notices() -> dict[str, str]:
    """Atlas source key -> the notice its source registry entry (``registry/sources/*.yaml`` ``rights.
    required_notice``) says must accompany any presentation of the data. Cached per process; empty on error."""
    with _SPEC_LOCK:
        if "v" in _NOTICES:
            return _NOTICES["v"]
    out: dict[str, str] = {}
    try:
        import yaml

        from reva.atlas.sources import atlas_sources

        root = Path(__file__).resolve().parents[3] / "registry" / "sources"
        by_key: dict[str, str] = {}
        for f in sorted(root.glob("*.yaml")):
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            notice = (doc.get("rights") or {}).get("required_notice") if isinstance(doc, dict) else None
            if isinstance(notice, str) and notice.strip() and doc.get("source_key"):
                by_key[str(doc["source_key"])] = " ".join(notice.split())
        for e in atlas_sources().values() if by_key else ():
            if e.spec.source_key in by_key:
                out.setdefault(e.spec.atlas_source, by_key[e.spec.source_key])
    except Exception:  # pragma: no cover - a broken registry file never breaks a read
        out = {}
    with _SPEC_LOCK:
        _NOTICES["v"] = out
    return out


def with_required_notice(rights: dict[str, Any] | None, source: str) -> dict[str, Any] | None:
    """``rights`` plus the source registry's required notice when the recorded rights carry none."""
    if rights is None or rights.get("required_notice"):
        return rights
    notice = registry_notices().get(source)
    return {**rights, "required_notice": notice} if notice else rights


def licence_summary(cells: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct rights of the sources behind ``cells`` (one entry per source and licence), restricted first:
    what an answer must show next to its values (non-commercial, share-alike, no-derivatives, not open)."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for c in cells:
        r = c.get("rights")
        if not r:
            continue
        k = (str(c.get("source")), str(r.get("licence")))
        if k not in seen:
            seen[k] = {"source": c.get("source"), **r}
    return sorted(seen.values(), key=lambda x: (not x.get("restricted"), str(x["source"]), str(x["licence"])))


def _merge_licences(groups: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for g in groups:
        for x in g:
            seen.setdefault((str(x.get("source")), str(x.get("licence"))), x)
    return sorted(seen.values(), key=lambda x: (not x.get("restricted"), str(x["source"]), str(x["licence"])))


# --------------------------------------------------------------------------- process cache
_QUERIES: OrderedDict[str, AtlasQuery] = OrderedDict()
_QLOCK = threading.Lock()


def query_for(
    snapshot: AtlasSnapshot, store: ArtifactStore, directory: Path | None = None, *, build: bool = True
) -> AtlasQuery:
    """Process-wide cached :class:`AtlasQuery` per snapshot (at most 2 open). ``build=False``: never build a
    missing query file (:class:`SnapshotNotMaterialised`; review V3b SEC-3)."""
    key = f"{directory or ''}|{snapshot.sha256}"
    with _QLOCK:
        q = _QUERIES.get(key)
        if q is not None:
            _QUERIES.move_to_end(key)
            return q
    q = AtlasQuery(snapshot, store, directory, build=build)
    with _QLOCK:
        if key in _QUERIES:
            q.close()
            return _QUERIES[key]
        _QUERIES[key] = q
        while len(_QUERIES) > 2:
            _, old = _QUERIES.popitem(last=False)
            old.close()
    return q


def request_snapshot(
    conn: Any, store: ArtifactStore, *, origin: str | None = None, sha256: str | None = None
) -> tuple[AtlasSnapshot, bool]:
    """The snapshot an API request asks for and whether its query file may be built in the request (review V3b
    SEC-3): only the latest snapshot (of ``origin``) may be built, and the latest live one is pinned against
    eviction; any other recorded snapshot is served only if its query file already exists. Call
    ``query_for(snap, store, build=...)`` *after* releasing ``conn`` (a build can take minutes)."""
    from reva.atlas.snapshot import latest_snapshot_ref, resolve_snapshot

    snap = resolve_snapshot(conn, store, origin=origin, sha256=sha256)
    if sha256 is None:
        is_latest = True
    else:
        ref = latest_snapshot_ref(conn, origin)
        is_latest = ref is not None and ref["sha"] == snap.sha256
    if is_latest and snap.origin == "live":
        try:
            pin_snapshot(snap.sha256)
        except OSError:  # pragma: no cover - a read-only cache dir never fails a read
            pass
    return snap, is_latest
