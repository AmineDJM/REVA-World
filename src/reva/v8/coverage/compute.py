"""Compute the coverage aggregates (``reva v8 coverage refresh``): read-only on the database and the object store.

What it reads:

* atlas tables: the latest live version of every ``evidence.dataset`` with ``concept_id = 'atlas.table'`` (row count,
  manifest coverage, rights recorded with the artifact), and the Parquet artifact itself, columns ``iso3`` and
  ``period_start`` only, to count rows per country and the first / latest year per country;
* the atlas catalog (themes of the concepts citing each table) and the declared atlas sources (connector name,
  datasets it should produce, refresh cadence, licence);
* live acquisitions per connector (last fetch, versions) — "ingest runs";
* the knowledge graph: current entities by kind and source, current statements by source, KG loads, and the
  entities attached to a country by a ``located_in``-family property whose object is the country's Wikidata item or an
  ISO 3166-2 code of that country (identifier prefix, never a name).

What it writes: one compact JSON document (``var/v8/coverage/coverage.json`` by default, atomically replaced). Nothing
is written to the database. Counts are what is *stored*; they say nothing about how much of the phenomenon exists.
"""

from __future__ import annotations

import io
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import Engine, text

from reva.v8.coverage.domains import DomainRegistry, country_qid_map, load_domains, load_places

SCHEMA = "reva-v8-coverage/1"
DEFAULT_DIR = Path(__file__).resolve().parents[4] / "var" / "v8" / "coverage"
HIST_FROM = 1900  # rows per year from this year on; earlier rows are counted in one "before" bucket

TABLES_SQL = """
SELECT DISTINCT ON (d.dataset_key)
       d.dataset_key, d.subject_key, d.artifact_sha256, d.row_count, d.coverage, d.parser_version,
       d.recorded_at, a.rights
FROM evidence.dataset d LEFT JOIN meta.artifact a ON a.sha256 = d.artifact_sha256
WHERE d.concept_id = 'atlas.table' AND d.origin = :o
ORDER BY d.dataset_key, d.recorded_at DESC, d.id DESC
"""
VERSIONS_SQL = """
SELECT dataset_key, count(*) AS versions, min(recorded_at) AS first_recorded_at
FROM evidence.dataset WHERE concept_id = 'atlas.table' AND origin = :o GROUP BY 1
"""
CONNECTORS_SQL = """
SELECT cv.name, array_agg(DISTINCT cv.version) AS versions, max(a.retrieved_at) AS last_retrieved_at,
       count(*) AS acquisitions
FROM evidence.acquisition a JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
WHERE a.origin = :o GROUP BY 1
"""
KG_KINDS_SQL = """
SELECT kind, source_key, count(*) AS n, max(recorded_at) AS last_recorded_at
FROM kg.entity_current WHERE origin = :o GROUP BY 1, 2
"""
KG_STATEMENTS_SQL = "SELECT source_key, count(*) AS n FROM kg.statement_current WHERE origin = :o GROUP BY 1"
KG_LOADS_SQL = """
SELECT source_key, count(*) AS loads, max(recorded_at) AS last_load_at
FROM kg.load WHERE origin = :o GROUP BY 1
"""
KG_PLACES_SQL = """
WITH m(place, iso3) AS (SELECT * FROM unnest(CAST(:places AS text[]), CAST(:iso3s AS text[])))
SELECT e.kind, e.source_key, m.iso3, count(DISTINCT s.subject_qid) AS n
FROM kg.statement_current s
JOIN m ON m.place = CASE WHEN s.object_qid LIKE 'iso3166_2:%' THEN 'iso2:' || split_part(substr(s.object_qid, 11), '-', 1)
                         ELSE s.object_qid END
JOIN kg.entity_current e ON e.qid = s.subject_qid AND e.origin = :o
WHERE s.origin = :o AND s.property_pid = ANY(CAST(:pids AS text[]))
GROUP BY 1, 2, 3
"""


def licence_class(rights: dict[str, Any] | None) -> str:
    """open | non_commercial | restricted | unknown (from the normalised rights, review V3b LIC-1)."""
    if not rights:
        return "unknown"
    if rights.get("non_commercial"):
        return "non_commercial"
    if rights.get("restricted"):
        return "restricted"
    if rights.get("open_licence") is True:
        return "open"
    return "unknown"


def table_rights(raw: Any, source: str) -> dict[str, Any] | None:
    from reva.atlas.query import source_spec_rights, with_required_notice
    from reva.atlas.snapshot import normalise_rights

    r = normalise_rights(raw, basis="table_artifact") if raw else None
    if r is None:
        r = source_spec_rights(source)
    return with_required_notice(r, source)


def per_country(frame: pl.DataFrame, today: date) -> tuple[dict[str, list[Any]], dict[int, int], int, int, int | None]:
    """Rows per ISO3 as ``[rows, first_year, latest_past_year|None, future_rows]``, rows per year (>= HIST_FROM),
    rows before HIST_FROM, rows with no ISO3 (aggregates, regions, grid cells), latest past year of the table."""
    yr = pl.col("period_start").dt.year()
    future = pl.col("period_start") > pl.lit(today)
    f = frame.with_columns(yr.alias("y"), future.alias("fut"))
    g = (
        f.filter(pl.col("iso3").is_not_null())
        .group_by("iso3")
        .agg(
            pl.len().alias("n"),
            pl.col("y").min().alias("y0"),
            pl.col("y").filter(~pl.col("fut")).max().alias("y1"),
            pl.col("fut").sum().alias("nf"),
        )
    )
    countries = {str(r["iso3"]): [int(r["n"]), r["y0"], r["y1"], int(r["nf"])] for r in g.iter_rows(named=True)}
    h = f.group_by("y").agg(pl.len().alias("n"))
    hist: dict[int, int] = {}
    before = 0
    for r in h.iter_rows(named=True):
        if r["y"] is None:
            continue
        if r["y"] >= HIST_FROM:
            hist[int(r["y"])] = int(r["n"])
        else:
            before += int(r["n"])
    unmapped = int(f.filter(pl.col("iso3").is_null()).height)
    lp = f.filter(~pl.col("fut")).select(pl.col("y").max()).item()
    return countries, hist, before, unmapped, (int(lp) if lp is not None else None)


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v is not None else None)


def compute(
    engine: Engine,
    store: Any,
    *,
    origin: str = "live",
    today: date | None = None,
    log: Callable[[str], None] = lambda _m: None,
    kg: bool = True,
    tables_filter: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """The full coverage document (see module docstring). ``tables_filter`` restricts the atlas pass (tests)."""
    t0 = time.monotonic()
    today = today or datetime.now(UTC).date()
    reg = load_domains()
    places = load_places()
    known = {p.iso3 for p in places}
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout = 600000"))
        tables = [dict(r) for r in conn.execute(text(TABLES_SQL), {"o": origin}).mappings()]
        versions = {r["dataset_key"]: dict(r) for r in conn.execute(text(VERSIONS_SQL), {"o": origin}).mappings()}
        connectors = {r["name"]: dict(r) for r in conn.execute(text(CONNECTORS_SQL), {"o": origin}).mappings()}
        kg_doc = _kg(conn, reg, origin, log) if kg else None
        db_time = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        conn.rollback()
    log(f"{len(tables)} atlas tables, {len(connectors)} connectors with live acquisitions")

    themes_of, catalog_concepts = _catalog(reg)
    specs = _specs()
    by_source_specs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    connectors_of_table: dict[str, list[str]] = defaultdict(list)
    for s in specs:
        by_source_specs[s["atlas_source"]].append(s)
        for ds in s["datasets"]:
            connectors_of_table[f"atlas.{s['atlas_source']}.{ds}"].append(s["name"])

    out_tables: list[dict[str, Any]] = []
    table_countries: dict[str, dict[str, list[Any]]] = {}
    cells: dict[str, dict[str, list[Any]]] = defaultdict(dict)
    hist: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    hist_before: dict[str, int] = defaultdict(int)
    for i, t in enumerate(sorted(tables, key=lambda x: x["dataset_key"])):
        key = str(t["dataset_key"])
        if tables_filter is not None and not tables_filter(key):
            continue
        _, source, dataset = key.split(".", 2)
        cov = t["coverage"] or {}
        conns = sorted(connectors_of_table.get(key, ())) or [x["name"] for x in by_source_specs.get(source, [])]
        domain, basis = reg.domain_of_table(key, source, themes_of.get((source, dataset), ()), conns)
        rights = table_rights(t["rights"], source)
        try:
            frame = pl.read_parquet(
                io.BytesIO(store.get("normalised", t["artifact_sha256"])), columns=["iso3", "period_start"]
            )
            countries, h, before, unmapped, latest = per_country(frame, today)
            read_error = None
        except Exception as e:  # an unreadable artifact is reported, never counted as empty
            countries, h, before, unmapped, latest = {}, {}, 0, 0, None
            read_error = f"{type(e).__name__}: {str(e)[:200]}"
        unknown_iso = sorted(set(countries) - known)
        table_countries[key] = countries
        for iso3, (n, y0, y1, nf) in countries.items():
            c = cells[domain].get(iso3)
            if c is None:
                cells[domain][iso3] = [n, 1, y0, y1, nf]
            else:
                c[0] += n
                c[1] += 1
                c[2] = _min(c[2], y0)
                c[3] = _max(c[3], y1)
                c[4] += nf
        for y, n in h.items():
            hist[domain][y] += n
        hist_before[domain] += before
        last_fetch = max(
            (
                connectors[c]["last_retrieved_at"]
                for c in conns
                if c in connectors and connectors[c]["last_retrieved_at"]
            ),
            default=None,
        )
        cadence = min((s["cadence_seconds"] for s in specs if s["name"] in conns), default=None)
        out_tables.append(
            {
                "key": key,
                "source": source,
                "dataset": dataset,
                "domain": domain,
                "domain_basis": basis,
                "rows": int(t["row_count"]),
                "indicators": cov.get("indicators"),
                "countries": len(countries),
                "numeric_cells": cov.get("numeric_cells"),
                "text_cells": cov.get("text_cells"),
                "missing_cells": cov.get("missing_cells"),
                "rows_without_country": unmapped,
                "unknown_iso3": unknown_iso[:20],
                "period_min": cov.get("period_min"),
                "period_max": cov.get("period_max"),
                "latest_past_year": latest,
                "future_rows": sum(v[3] for v in countries.values()),
                "recorded_at": _iso(t["recorded_at"]),
                "first_recorded_at": _iso((versions.get(key) or {}).get("first_recorded_at")),
                "versions": int((versions.get(key) or {}).get("versions") or 1),
                "last_fetch_at": _iso(last_fetch),
                "cadence_seconds": cadence,
                "connectors": conns,
                "parser_version": t["parser_version"],
                "licence": _rights_out(rights),
                "licence_class": licence_class(rights),
                "coverage_note": (str(cov.get("coverage_note"))[:400] if cov.get("coverage_note") else None),
                "read_error": read_error,
            }
        )
        if i % 40 == 0:
            log(f"  {i + 1}/{len(tables)} {key}")

    loaded = {t["key"]: t for t in out_tables}
    declared = []
    for s in specs:
        conn_row = connectors.get(s["name"])
        expected = [f"atlas.{s['atlas_source']}.{ds}" for ds in s["datasets"]]
        doms = [loaded[k]["domain"] for k in expected if k in loaded]
        cdom: str | None = reg.by_connector.get(s["name"]) or reg.by_source.get(s["atlas_source"])
        if cdom is None and doms:
            cdom = max(set(doms), key=doms.count)
        declared.append(
            {
                **s,
                "tables_expected": expected,
                "tables_loaded": [k for k in expected if k in loaded],
                "acquired": conn_row is not None,
                "versions_acquired": sorted(conn_row["versions"]) if conn_row else [],
                "last_retrieved_at": _iso(conn_row["last_retrieved_at"]) if conn_row else None,
                "domain": cdom or reg.other.id,
            }
        )

    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "computed_at": datetime.now(UTC).isoformat(),
        "db_time": _iso(db_time),
        "today": today.isoformat(),
        "origin": origin,
        "domains_sha256": reg.sha256,
        "hist_from": HIST_FROM,
        "domains": [
            {"id": d.id, "fr": d.fr, "en": d.en, "themes": list(d.themes), "kg_kinds": list(d.kg_kinds)}
            for d in (*reg.domains, reg.other)
        ],
        "places": [
            {
                "iso3": p.iso3,
                "label": p.label,
                "label_fr": p.label_fr,
                "region": p.region,
                "subregion": p.subregion,
                "placement": p.placement,
                **({"placement_basis": p.placement_basis} if p.placement_basis else {}),
            }
            for p in places
        ],
        "regions": dict(reg.regions),
        "subregions": dict(reg.subregions),
        "tables": out_tables,
        "table_countries": table_countries,
        "cells": {d: dict(sorted(v.items())) for d, v in cells.items()},
        "hist": {d: {str(y): n for y, n in sorted(v.items())} for d, v in hist.items()},
        "hist_before": dict(hist_before),
        "catalog": catalog_by_domain(catalog_concepts, {t["key"] for t in out_tables}),
        "connectors": declared,
        "kg": kg_doc,
        "elapsed_s": None,
    }
    doc["elapsed_s"] = round(time.monotonic() - t0, 1)
    return doc


def _min(a: Any, b: Any) -> Any:
    return b if a is None else (a if b is None else min(a, b))


def _max(a: Any, b: Any) -> Any:
    return b if a is None else (a if b is None else max(a, b))


def _rights_out(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if not r:
        return None
    keep = ("licence", "licence_url", "attribution", "commercial_use", "non_commercial", "share_alike")
    keep2 = ("no_derivatives", "open_licence", "restricted", "basis", "restriction", "required_notice")
    return {k: r.get(k) for k in (*keep, *keep2) if r.get(k) is not None}


def _catalog(reg: DomainRegistry) -> tuple[dict[tuple[str, str], list[str]], list[tuple[str, str, str, list[str]]]]:
    """Themes of the catalog concepts citing each (source, dataset), and every concept as
    ``(concept_id, domain, label, tables cited by its source order)``."""
    from reva.atlas.catalog import cached_catalog

    cat = cached_catalog()
    themes_of: dict[tuple[str, str], list[str]] = defaultdict(list)
    concepts: list[tuple[str, str, str, list[str]]] = []
    for c in cat.concepts.values():
        dom = reg.by_theme.get(c.theme or "", reg.other.id)
        for s in c.source_order:
            if c.theme:
                themes_of[(s.source, s.dataset)].append(c.theme)
        label = c.labels.get("fr") or c.labels.get("en") or c.id
        concepts.append((c.id, dom, label, sorted({f"atlas.{s.source}.{s.dataset}" for s in c.source_order})))
    return themes_of, concepts


def catalog_by_domain(
    concepts: list[tuple[str, str, str, list[str]]], loaded: set[str], examples: int = 40
) -> dict[str, dict[str, Any]]:
    """Per domain: catalog concepts, how many cite at least one loaded table, and examples of concepts none of whose
    cited tables is loaded (declared in the catalog, no data stored)."""
    out: dict[str, dict[str, Any]] = {}
    for cid, dom, label, tabs in sorted(concepts):
        d = out.setdefault(dom, {"concepts": 0, "with_loaded_table": 0, "without_loaded_table": 0, "examples": []})
        d["concepts"] += 1
        if any(t in loaded for t in tabs):
            d["with_loaded_table"] += 1
        else:
            d["without_loaded_table"] += 1
            if len(d["examples"]) < examples:
                d["examples"].append({"id": cid, "label": label, "tables": tabs[:4]})
    return out


def _specs() -> list[dict[str, Any]]:
    from reva.atlas.sources import atlas_sources

    out = []
    for name, e in sorted(atlas_sources().items()):
        sp = e.spec
        out.append(
            {
                "name": name,
                "version": sp.version,
                "atlas_source": sp.atlas_source,
                "source_key": sp.source_key,
                "provider": sp.provider[:200],
                "licence": sp.licence[:300],
                "datasets": list(sp.datasets),
                "cadence_seconds": int(sp.cadence_seconds),
            }
        )
    return out


def _kg(conn: Any, reg: DomainRegistry, origin: str, log: Callable[[str], None]) -> dict[str, Any]:
    from reva.kg.families import load_registry

    t = time.monotonic()
    kinds = [dict(r) for r in conn.execute(text(KG_KINDS_SQL), {"o": origin}).mappings()]
    loads = {r["source_key"]: dict(r) for r in conn.execute(text(KG_LOADS_SQL), {"o": origin}).mappings()}
    log(f"KG kinds/loads in {time.monotonic() - t:.1f}s")
    t = time.monotonic()
    stmts = {r["source_key"]: int(r["n"]) for r in conn.execute(text(KG_STATEMENTS_SQL), {"o": origin}).mappings()}
    log(f"KG statements by source in {time.monotonic() - t:.1f}s")
    pids = sorted(p for p, d in load_registry().properties_for(["located_in"]) if d == "out")
    qmap = country_qid_map()
    places = [p for p in load_places()]
    keys: list[str] = []
    isos: list[str] = []
    for q, iso3 in qmap.items():
        keys.append(q)
        isos.append(iso3)
    for p in places:
        if p.iso2:
            keys.append(f"iso2:{p.iso2}")
            isos.append(p.iso3)
    t = time.monotonic()
    rows = conn.execute(text(KG_PLACES_SQL), {"places": keys, "iso3s": isos, "pids": pids, "o": origin}).mappings()
    by_country: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_kind_country: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        dom = reg.domain_of_kind(str(r["kind"]), str(r["source_key"]))
        by_country[dom][str(r["iso3"])] += int(r["n"])
        kc = by_kind_country[str(r["kind"])]
        kc[str(r["iso3"])] = kc.get(str(r["iso3"]), 0) + int(r["n"])
    log(f"KG entities by country in {time.monotonic() - t:.1f}s")
    by_kind: dict[str, dict[str, Any]] = {}
    for r in kinds:
        k = by_kind.setdefault(
            str(r["kind"]),
            {
                "kind": r["kind"],
                "domain": reg.domain_of_kind(str(r["kind"])),
                "entities": 0,
                "sources": {},
                "last": None,
                "by_domain": {},
            },
        )
        sd = reg.domain_of_kind(str(r["kind"]), str(r["source_key"]))
        k["by_domain"][sd] = k["by_domain"].get(sd, 0) + int(r["n"])
        k["entities"] += int(r["n"])
        k["sources"][str(r["source_key"])] = int(r["n"])
        k["last"] = _iso(max(x for x in (r["last_recorded_at"], _dt(k["last"])) if x is not None))
    sources = sorted(
        {*stmts, *loads, *(s for k in by_kind.values() for s in k["sources"])},
    )
    return {
        "located_in_properties": pids,
        "located_in_note": "entities attached to a country by a located_in-family property whose object is the "
        "country's Wikidata item or one of its ISO 3166-2 codes; entities placed only in a city, an ADM1 entity "
        "without an ISO code or by coordinates are not counted",
        "kinds": sorted(by_kind.values(), key=lambda k: -k["entities"]),
        "sources": [
            {
                "source_key": s,
                "statements": stmts.get(s, 0),
                "entities": sum(k["sources"].get(s, 0) for k in by_kind.values()),
                "loads": int((loads.get(s) or {}).get("loads") or 0),
                "last_load_at": _iso((loads.get(s) or {}).get("last_load_at")),
            }
            for s in sources
        ],
        "cells": {d: dict(sorted(v.items())) for d, v in by_country.items()},
        "kind_countries": by_kind_country,
    }


def _dt(v: Any) -> datetime | None:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def write(doc: dict[str, Any], directory: Path = DEFAULT_DIR) -> Path:
    """Atomically write ``coverage.json`` (compact) into ``directory``; returns its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "coverage.json"
    tmp = directory / f".coverage.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path
