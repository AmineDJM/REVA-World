"""Materialised link store (``kg.link_materialisation`` / ``kg.link_row``, migration 0012; agent LINKS2).

One materialisation = every link derivable from one atlas snapshot plus the KG as loaded up to a watermark
(highest ``kg.load`` id), written by the worker in one transaction (complete or absent), read by the API:

* KG kinds (Wikidata P37/P463/P47/P530/colonies/succession..., derived on the whole KG) and dataset KG kinds
  (CShapes borders and colonial rule, UCDP sides, Cliopatria overlay) are stored in full;
* atlas dyadic kinds (IMF IMTS, UN DESA migrant stock, BACI) and the computed UNGA
  vote-agreement index (per decade and issue) keep, per (country, period, facet) and direction, the top
  ``top_k`` partners (Natural Earth borders and LINKS3's small physical-network kinds — cables, 2014 air
  routes, launch services — are stored in full) — the union over both endpoints, so every country's top-``top_k`` is complete. Full
  histories of one pair are read from the atlas snapshot on demand (:mod:`reva.links.service`).

Entry point (worker credentials; throwaway or deployment database)::

    uv run python -m reva.links.store materialise [--atlas-origin live|test_fixture] [--atlas-snapshot SHA]
                                                  [--kg-origin live|test_fixture] [--top-k 10]
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from reva.links.derive import DerivedSet, KgIndex, assert_all_evidenced, derive_all, merge_links
from reva.links.model import Evidence, Link, Stmt, Temporal
from reva.links.taxonomy import LINK_TYPES_FILE

log = logging.getLogger("reva.links.store")

METHOD_VERSION = "links-materialise/1"
DEFAULT_TOP_K = 10
INSERT_BATCH = 5_000
#: KG statements read for a materialisation (bounded: refused beyond, never silently cut)
MAX_KG_ROWS = 5_000_000
KG_LINK_PROPERTIES = (
    "P47", "P530", "P37", "P2936", "P38", "P35", "P463", "P1891", "P710", "P1344", "P607",
    "P1365", "P1366", "P155", "P156", "P17", "P361", "glottolog:spoken_in", "untc:party", "untc:former_party",
)  # fmt: skip
LANGUAGE_GENEALOGY = (
    "P279", "P361", "glottolog:parent", "glottolog:in_group", "glottolog:family", "glottolog:wikidata_item",
)  # fmt: skip


class MaterialisationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------- helpers
def taxonomy_sha256() -> str:
    """sha256 of the link taxonomy; LINKS3: the explicit geography mapping hops are part of it (a changed hop
    changes which atlas rows become links, so an unchanged-inputs materialisation must not be reused)."""
    from reva.links.geo_hops import HOPS_FILE

    body = LINK_TYPES_FILE.read_bytes()
    if HOPS_FILE.exists():
        body += b"\n# geo_mapping_hops.yaml\n" + HOPS_FILE.read_bytes()
    return hashlib.sha256(body).hexdigest()


def node_iso3(node: str, qid_iso3: Mapping[str, str]) -> str | None:
    if node.startswith("iso3:"):
        return node[5:]
    return qid_iso3.get(node)


def link_facet(lk: Link) -> str:
    if lk.link_type == "unga_vote_agreement":
        return f"issue={lk.basis.get('issue', 'all')}"
    return str(lk.basis.get("facet") or "")


def _origin(evidence: Sequence[Evidence]) -> str:
    os_ = {e.origin or "unknown" for e in evidence}
    if os_ == {"live"}:
        return "live"
    if os_ == {"test_fixture"}:
        return "test_fixture"
    if "unknown" in os_ and len(os_) == 1:
        return "unknown"
    return "mixed"


def link_row(lk: Link, mid: int, qid_iso3: Mapping[str, str]) -> dict[str, Any]:
    t = lk.temporal
    weight_num = float(lk.weight) if lk.weight is not None else None
    return {
        "materialisation_id": mid,
        "link_type": lk.link_type,
        "facet": link_facet(lk),
        "subject": lk.subject,
        "object": lk.object,
        "subject_iso3": node_iso3(lk.subject, qid_iso3),
        "object_iso3": node_iso3(lk.object, qid_iso3),
        "directed": lk.directed,
        "year_lo": t.first_year,
        "year_hi": t.last_year,
        "temporal": t.to_json(),
        "weight": lk.weight,
        "weight_num": weight_num,
        "unit": lk.unit if lk.weight is not None else None,
        "via": list(lk.via),
        "evidence": [e.to_json() for e in lk.evidence],
        "basis": dict(lk.basis),
        "origin": _origin(lk.evidence),
    }


def temporal_from_json(d: Mapping[str, Any]) -> Temporal:
    """Read a stored ``temporal`` column (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, signed years)."""
    return Temporal.from_json(d)


def link_from_row(r: Mapping[str, Any]) -> Link:
    ev = tuple(Evidence(**e) for e in r["evidence"])
    return Link(
        r["subject"],
        r["object"],
        r["link_type"],
        bool(r["directed"]),
        temporal_from_json(r["temporal"]),
        ev,
        via=tuple(r.get("via") or ()),
        weight=r.get("weight"),
        unit=r.get("unit"),
        basis=r.get("basis") or {},
    )


# ---------------------------------------------------------------------------------------------- KG read
def kg_watermark(conn: Connection, origin: str | None) -> tuple[int | None, Any]:
    row = conn.execute(
        text(
            "SELECT max(id) AS id, max(recorded_at) AS at FROM kg.load WHERE (CAST(:o AS text) IS NULL OR origin = :o)"
        ),
        {"o": origin},
    ).one()
    return row.id, row.at


def read_kg_for_links(
    conn: Connection, origin: str | None, watermark: int | None, max_rows: int = MAX_KG_ROWS
) -> tuple[list[Stmt], dict[str, Any]]:
    """Every current statement the derivations use (link properties, dataset properties, language genealogy,
    classification context of the nodes reached), up to the KG watermark."""
    from reva.links.dataset_kg import DATASET_PROPERTIES
    from reva.links.paths import _ST_SELECT, KgNeighbourhood, row_to_stmt

    props = [*KG_LINK_PROPERTIES, *DATASET_PROPERTIES, "P298"]
    out: dict[str, Stmt] = {}
    last = 0
    sql = (
        _ST_SELECT.replace("SELECT ", "SELECT id, ", 1)
        + """ WHERE property_pid = ANY(:p)
           AND (CAST(:o AS text) IS NULL OR origin = :o) AND load_id <= :w AND id > :last ORDER BY id LIMIT 50000"""
    )
    while True:
        rows = conn.execute(text(sql), {"p": props, "o": origin, "w": watermark or 0, "last": last}).mappings().all()
        if not rows:
            break
        for r in rows:
            s = row_to_stmt(dict(r))
            out[s.statement_id] = s
        last = int(rows[-1]["id"])
        if len(out) > max_rows:
            raise MaterialisationError(f"more than {max_rows} KG statements to derive links from; raise the bound")
    nb = KgNeighbourhood(conn, origin=origin, max_total_rows=max_rows)
    langs = sorted({s.object for s in out.values() if s.pid in ("P37", "P2936") and s.object})
    gen, reached_l, trunc = nb.expand(langs, 6, LANGUAGE_GENEALOGY) if langs else ([], set(), set())
    for s in gen:
        out.setdefault(s.statement_id, s)
    nodes = {s.subject for s in out.values()} | {s.object for s in out.values() if s.object} | reached_l
    for s in nb.context(nodes):
        out.setdefault(s.statement_id, s)
    return list(out.values()), {"statements": len(out), "language_genealogy_truncated": sorted(trunc)[:50]}


# ---------------------------------------------------------------------------------------------- materialise
@dataclass
class MaterialisationResult:
    id: int
    link_count: int
    report: dict[str, Any]
    seconds: float

    def to_json(self) -> dict[str, Any]:
        return {
            "materialisation_id": self.id,
            "link_count": self.link_count,
            "seconds": round(self.seconds, 1),
            "report": self.report,
        }


def _crosswalk_keys() -> list[str]:
    from reva.atlas.tables import known_country_keys

    return sorted(known_country_keys())


def derive_everything(
    statements: Sequence[Stmt],
    *,
    iso3_to_qid: Mapping[str, str],
    atlas_reader: Any | None = None,
    top_k: int = DEFAULT_TOP_K,
    log_fn: Callable[[str], None] = log.info,
) -> tuple[list[Link], dict[str, Any]]:
    """All link kinds from KG statements (+ an :class:`~reva.links.atlas_links.AtlasLinkReader`)."""
    from reva.links.dataset_kg import derive_dataset_links

    qid_iso3 = {q: c for c, q in iso3_to_qid.items()}

    def node_of(iso3: str) -> str:
        return iso3_to_qid.get(iso3) or f"iso3:{iso3}"

    index = KgIndex(statements)
    t0 = time.monotonic()
    ds: DerivedSet = derive_all(index, countries=set(iso3_to_qid.values()))
    report: dict[str, Any] = {"kg": ds.report()}
    links: list[Link] = list(ds.links)
    log_fn(f"kg derivations: {len(links)} links in {time.monotonic() - t0:.1f}s")
    dsl = derive_dataset_links(index, node_of=node_of, qid_iso3=qid_iso3)
    report["kg_dataset"] = {k: v.to_json() for k, v in dsl.items()}
    for d in dsl.values():
        links.extend(d.links)
    log_fn(f"dataset KG derivations: {sum(len(d.links) for d in dsl.values())} links")
    if atlas_reader is not None:
        atlas_rep: dict[str, Any] = {}
        for lt in atlas_reader.dyadic_types():
            t1 = time.monotonic()
            d = atlas_reader.dyadic(lt, top_k=top_k)
            links.extend(d.links)
            atlas_rep[lt.id] = d.to_json()
            log_fn(f"atlas {lt.id}: {len(d.links)} links in {time.monotonic() - t1:.1f}s")
        t1 = time.monotonic()
        v = atlas_reader.votes(grain="decade", top_k=top_k)
        links.extend(v.links)
        atlas_rep["unga_vote_agreement"] = v.to_json()
        log_fn(f"atlas unga_vote_agreement: {len(v.links)} links in {time.monotonic() - t1:.1f}s")
        report["atlas"] = atlas_rep
    links = merge_links(links)
    assert_all_evidenced(links)
    report["counts"] = dict(sorted(Counter(lk.link_type for lk in links).items()))
    return links, report


def _insert(conn: Connection, rows: Iterable[dict[str, Any]]) -> int:
    sql = text(
        """INSERT INTO kg.link_row (materialisation_id, link_type, facet, subject, object, subject_iso3, object_iso3,
                directed, year_lo, year_hi, temporal, weight, weight_num, unit, via, evidence, basis, origin)
           SELECT materialisation_id, link_type, facet, subject, object, subject_iso3, object_iso3, directed,
                  year_lo, year_hi, temporal, weight, weight_num, unit, via, evidence, basis, origin
             FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(
                  materialisation_id bigint, link_type text, facet text, subject text, object text,
                  subject_iso3 text, object_iso3 text, directed boolean, year_lo integer, year_hi integer,
                  temporal jsonb, weight text, weight_num double precision, unit text, via text[], evidence jsonb,
                  basis jsonb, origin text)"""
    )
    n = 0
    batch: list[dict[str, Any]] = []
    for r in rows:
        batch.append(r)
        if len(batch) >= INSERT_BATCH:
            conn.execute(sql, {"rows": json.dumps(batch, default=str)})
            n += len(batch)
            batch = []
    if batch:
        conn.execute(sql, {"rows": json.dumps(batch, default=str)})
        n += len(batch)
    return n


def materialise(
    engine: Engine,
    store: Any,
    *,
    atlas_origin: str | None = None,
    atlas_sha256: str | None = None,
    kg_origin: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    with_atlas: bool = True,
    duckdb_memory_limit: str = "3GB",
    duckdb_temp_dir: str | None = None,
    log_fn: Callable[[str], None] = log.info,
) -> MaterialisationResult:
    """Derive and store every link of (atlas snapshot, KG watermark) — worker engine, one transaction."""
    from reva.links.paths import KgNeighbourhood

    if not 1 <= top_k <= 100:
        raise MaterialisationError("top_k must be within 1..100")
    t0 = time.monotonic()
    snap = None
    with engine.connect() as conn:
        if with_atlas:
            from reva.atlas.snapshot import SnapshotUnavailable, resolve_snapshot

            try:
                snap = resolve_snapshot(conn, store, origin=atlas_origin, sha256=atlas_sha256)
            except SnapshotUnavailable:
                if atlas_sha256 is not None:
                    raise
                snap = None
                log_fn("no atlas snapshot: KG kinds only")
        wm_id, wm_at = kg_watermark(conn, kg_origin)
        stmts, kg_rep = read_kg_for_links(conn, kg_origin, wm_id)
        nb = KgNeighbourhood(conn, origin=kg_origin)
        iso3_to_qid = nb.iso3_to_qid(_crosswalk_keys())
        ambiguous = dict(nb.ambiguous_iso3)
    log_fn(f"KG: {len(stmts)} statements up to load {wm_id}; {len(iso3_to_qid)} countries mapped to QIDs")
    reader = None
    con = None
    if snap is not None:
        import duckdb

        from reva.atlas.query import build_snapshot_db
        from reva.links.atlas_links import AtlasLinkReader

        path = build_snapshot_db(snap, store)
        con = duckdb.connect(str(path), read_only=True)
        con.execute(f"SET memory_limit = '{duckdb_memory_limit}'")
        if duckdb_temp_dir:
            con.execute(f"SET temp_directory = '{duckdb_temp_dir}'")

        def rows(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
            assert con is not None
            res = con.execute(sql, list(params))
            names = [d[0] for d in res.description]
            return [dict(zip(names, r, strict=True)) for r in res.fetchall()]

        from reva.links.geo_hops import hop_evidence_from_statements

        hop_ev = hop_evidence_from_statements(stmts)
        reader = AtlasLinkReader(
            rows,
            snapshot_sha256=snap.sha256,
            origin=snap.origin,
            node_of=lambda c: iso3_to_qid.get(c) or f"iso3:{c}",
            hop_evidence=hop_ev,
        )
        log_fn(f"mapping hops with KG evidence: {sorted(hop_ev) or 'none'}")
    try:
        links, report = derive_everything(
            stmts, iso3_to_qid=iso3_to_qid, atlas_reader=reader, top_k=top_k, log_fn=log_fn
        )
    finally:
        if con is not None:
            con.close()
    report["kg_read"] = kg_rep
    report["ambiguous_iso3"] = ambiguous
    if reader is not None:
        report["mapping_hops"] = {c: e.statement_id for c, e in sorted(reader.hop_evidence.items())}
    qid_iso3 = {q: c for c, q in iso3_to_qid.items()}
    params = {
        "counts": report["counts"],
        "top_k": top_k,
        "stored_in_full": "KG kinds, land_border and atlas kinds marked stored_in_full (submarine_cable, "
        "air_routes_2014, launch_service); other atlas kinds keep the union of both endpoints' top_k per period and facet",
        "min_common_rollcalls": 10,
    }
    with engine.begin() as conn:
        mid = conn.execute(
            text(
                """INSERT INTO kg.link_materialisation (method_version, taxonomy_sha256, atlas_snapshot_sha256,
                       atlas_origin, kg_origin, kg_load_watermark, kg_recorded_watermark, parameters, report, link_count)
                   VALUES (:mv, :ts, :sha, :ao, :ko, :wm, :wat, CAST(:p AS jsonb), CAST(:r AS jsonb), :n) RETURNING id"""
            ),
            {
                "mv": METHOD_VERSION,
                "ts": taxonomy_sha256(),
                "sha": snap.sha256 if snap else None,
                "ao": snap.origin if snap else None,
                "ko": kg_origin,
                "wm": wm_id,
                "wat": wm_at,
                "p": json.dumps(params),
                "r": json.dumps(report, default=str),
                "n": len(links),
            },
        ).scalar_one()
        n = _insert(conn, (link_row(lk, mid, qid_iso3) for lk in links))
    secs = time.monotonic() - t0
    log_fn(f"materialisation {mid}: {n} links stored in {secs:.1f}s")
    return MaterialisationResult(int(mid), n, report, secs)


# ---------------------------------------------------------------------------------------------- reads
MAT_COLUMNS = (
    "id, method_version, taxonomy_sha256, atlas_snapshot_sha256, atlas_origin, kg_origin, kg_load_watermark, "
    "kg_recorded_watermark, parameters, link_count, recorded_at"
)


def latest_materialisation(
    conn: Connection, *, atlas_origin: str | None = None, atlas_sha256: str | None = None, live_only: bool = False
) -> dict[str, Any] | None:
    """The latest materialisation (optionally for one atlas snapshot / origin). ``live_only`` (production):
    a materialisation built from a fixture snapshot or fixture KG is never served. A pruned materialisation
    (``kg.link_prune``, ``reva links prune``; its link rows are gone) is never returned."""
    try:
        row = (
            conn.execute(
                text(
                    f"""SELECT {MAT_COLUMNS} FROM kg.link_materialisation
                         WHERE (CAST(:sha AS text) IS NULL OR atlas_snapshot_sha256 = :sha)
                           AND (CAST(:ao AS text) IS NULL OR atlas_origin = :ao)
                           AND (NOT :live OR (coalesce(atlas_origin, 'live') = 'live' AND kg_origin = 'live'))
                           AND NOT EXISTS (SELECT 1 FROM kg.link_prune p
                                            WHERE p.materialisation_id = kg.link_materialisation.id)
                         ORDER BY id DESC LIMIT 1"""
                ),
                {"sha": atlas_sha256, "ao": atlas_origin, "live": live_only},
            )
            .mappings()
            .one_or_none()
        )
    except Exception as e:  # the store table is absent before migration 0012
        if "link_materialisation" in str(e):
            conn.rollback()
            return None
        raise
    if row is None:
        return None
    d = dict(row)
    d["recorded_at"] = d["recorded_at"].isoformat() if d.get("recorded_at") else None
    d["kg_recorded_watermark"] = d["kg_recorded_watermark"].isoformat() if d.get("kg_recorded_watermark") else None
    return d


_ROW_COLS = (
    "link_type, facet, subject, object, subject_iso3, object_iso3, directed, year_lo, year_hi, temporal, weight, "
    "weight_num, unit, via, evidence, basis, origin"
)


def country_rows(
    conn: Connection,
    mid: int,
    iso3: str,
    *,
    kinds: Sequence[str] | None,
    facets: Sequence[str],
    year: int | None,
    latest_period_kinds: Sequence[str],
    limit: int,
    cap: int = 20_000,
) -> list[dict[str, Any]]:
    """Top ``limit`` rows per (kind, facet, direction) touching ``iso3``. Kinds in ``latest_period_kinds``
    (snapshot / decade kinds) keep only their latest period when no ``year`` is given."""
    rows = conn.execute(
        text(
            f"""
        WITH x AS (
          SELECT {_ROW_COLS},
                 CASE WHEN NOT directed THEN 'both' WHEN subject_iso3 = :c THEN 'outgoing' ELSE 'incoming' END AS dir
            FROM kg.link_row
           WHERE materialisation_id = :m AND (subject_iso3 = :c OR object_iso3 = :c)
             AND (CAST(:kinds AS text[]) IS NULL OR link_type = ANY(CAST(:kinds AS text[])))
             AND (facet = '' OR facet = ANY(:facets) OR facet LIKE 'source=%')
             AND (CAST(:y AS integer) IS NULL OR ((year_lo IS NULL OR year_lo <= :y) AND (year_hi IS NULL OR year_hi >= :y)))
           LIMIT :cap),
        latest AS (SELECT link_type, facet, dir, max(year_hi) AS yh FROM x
                    WHERE CAST(:y AS integer) IS NULL AND link_type = ANY(:lp) GROUP BY link_type, facet, dir),
        y AS (SELECT x.* FROM x LEFT JOIN latest l USING (link_type, facet, dir)
               WHERE l.yh IS NULL OR x.year_hi = l.yh),
        r AS (SELECT y.*,
                     row_number() OVER (PARTITION BY link_type, facet, dir
                                        ORDER BY weight_num DESC NULLS LAST, year_hi DESC NULLS FIRST, subject, object) AS rn,
                     count(*) OVER (PARTITION BY link_type, facet, dir) AS total
                FROM y)
        SELECT * FROM r WHERE rn <= :n ORDER BY link_type, facet, dir, rn"""
        ),
        {
            "m": mid,
            "c": iso3,
            "kinds": list(kinds) if kinds else None,
            "facets": list(facets),
            "y": year,
            "lp": list(latest_period_kinds),
            "n": limit,
            "cap": cap,
        },
    ).mappings()
    return [dict(r) for r in rows]


def pair_rows(
    conn: Connection,
    mid: int,
    a: str,
    b: str,
    *,
    kinds: Sequence[str] | None,
    exclude_kinds: Sequence[str] = (),
    year_lo: int | None = None,
    year_hi: int | None = None,
    cap: int = 5_000,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            f"""SELECT {_ROW_COLS} FROM kg.link_row
                 WHERE materialisation_id = :m
                   AND ((subject_iso3 = :a AND object_iso3 = :b) OR (subject_iso3 = :b AND object_iso3 = :a))
                   AND (CAST(:kinds AS text[]) IS NULL OR link_type = ANY(CAST(:kinds AS text[])))
                   AND NOT (link_type = ANY(:ex))
                   AND (CAST(:lo AS integer) IS NULL OR year_hi IS NULL OR year_hi >= :lo)
                   AND (CAST(:hi AS integer) IS NULL OR year_lo IS NULL OR year_lo <= :hi)
                 ORDER BY link_type, facet, year_lo NULLS FIRST, year_hi
                 LIMIT :cap"""
        ),
        {
            "m": mid,
            "a": a,
            "b": b,
            "kinds": list(kinds) if kinds else None,
            "ex": list(exclude_kinds),
            "lo": year_lo,
            "hi": year_hi,
            "cap": cap + 1,
        },
    ).mappings()
    return [dict(r) for r in rows]


def rows_touching(conn: Connection, mid: int, nodes: Iterable[str], cap: int = 20_000) -> tuple[list[Link], bool]:
    """Stored links incident to ``nodes`` (path engine); ``True`` when the cap cut the read."""
    ns = sorted(set(nodes))
    rows = (
        conn.execute(
            text(
                f"""SELECT {_ROW_COLS} FROM kg.link_row WHERE materialisation_id = :m AND (subject = ANY(:n) OR object = ANY(:n))
                 LIMIT :cap"""
            ),
            {"m": mid, "n": ns, "cap": cap + 1},
        )
        .mappings()
        .all()
    )
    return [link_from_row(dict(r)) for r in rows[:cap]], len(rows) > cap


# ---------------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="reva.links.store", description="Materialise the link store (worker role)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("materialise")
    m.add_argument("--atlas-origin", choices=("live", "test_fixture"))
    m.add_argument("--atlas-snapshot")
    m.add_argument("--kg-origin", choices=("live", "test_fixture"))
    m.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    m.add_argument("--no-atlas", action="store_true")
    m.add_argument("--duckdb-memory-limit", default="3GB")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import load_settings

    settings = load_settings()
    engine = make_engine("worker", settings)
    res = materialise(
        engine,
        build_store(settings),
        atlas_origin=args.atlas_origin,
        atlas_sha256=args.atlas_snapshot,
        kg_origin=args.kg_origin,
        top_k=args.top_k,
        with_atlas=not args.no_atlas,
        duckdb_memory_limit=args.duckdb_memory_limit,
        log_fn=lambda s: print(s, flush=True),
    )
    out = res.to_json()
    out["report"] = {"counts": res.report.get("counts")}
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def node_of_iso3(conn: Connection, mid: int, iso3: str) -> str | None:
    row = conn.execute(
        text(
            """(SELECT subject FROM kg.link_row WHERE materialisation_id = :m AND subject_iso3 = :c LIMIT 1)
               UNION ALL (SELECT object FROM kg.link_row WHERE materialisation_id = :m AND object_iso3 = :c LIMIT 1)
               LIMIT 1"""
        ),
        {"m": mid, "c": iso3},
    ).first()
    return str(row[0]) if row else None


def predecessors(
    conn: Connection, mid: int, node: str, depth: int = 2, cap: int = 50
) -> dict[str, list[dict[str, Any]]]:
    """Predecessor states of ``node`` through stored ``successor_of`` links (≤ ``depth`` steps): {predecessor:
    [the successor_of rows of the chain]}. Used by the pair view to show links of a country's predecessors
    (Portugal ← Portuguese Empire), each with the succession evidence."""
    out: dict[str, list[dict[str, Any]]] = {}
    frontier = {node: []}  # type: dict[str, list[dict[str, Any]]]
    for _ in range(depth):
        nxt: dict[str, list[dict[str, Any]]] = {}
        for n, chain in frontier.items():
            rows = conn.execute(
                text(
                    f"""SELECT {_ROW_COLS} FROM kg.link_row WHERE materialisation_id = :m AND link_type = 'successor_of'
                         AND subject = :n LIMIT :cap"""
                ),
                {"m": mid, "n": n, "cap": cap},
            ).mappings()
            for r in rows:
                p = r["object"]
                if p == node or p in out:
                    continue
                out[p] = [*chain, dict(r)]
                nxt[p] = out[p]
        frontier = nxt
        if len(out) >= cap:
            break
    return out


def rows_between_nodes(
    conn: Connection, mid: int, a: Sequence[str], b: Sequence[str], cap: int = 2_000
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            f"""SELECT {_ROW_COLS} FROM kg.link_row WHERE materialisation_id = :m AND link_type <> 'successor_of'
                 AND ((subject = ANY(:a) AND object = ANY(:b)) OR (subject = ANY(:b) AND object = ANY(:a))) LIMIT :cap"""
        ),
        {"m": mid, "a": list(a), "b": list(b), "cap": cap},
    ).mappings()
    return [dict(r) for r in rows]
