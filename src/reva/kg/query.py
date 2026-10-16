"""Read-only queries over the world knowledge graph (current view ``kg.statement_current``).

Every function takes an open SQLAlchemy ``Connection`` (the API's ``reva_api`` read transaction) and returns
plain dicts. Every statement carries its provenance (source key, source statement id, source revision, raw
artifact sha256, retrieval and recording times, rank, the statement's own references) and a verification
label: Wikidata statements are *community-edited, not verified* by REVA.

Honesty rules (docs/roadmap/v3_world_graph.md)
----------------------------------------------
* A path is not a cause: ``paths`` says "connected via <property label>" for each hop. Causal wording appears
  only for properties whose statements a source asserts as causal (``CAUSAL_PIDS``: has cause P828, has effect
  P1542 and their immediate/contributing variants), and then as "asserted by <source>", never as REVA's
  conclusion.
* Results are bounded and say so (``truncated`` flags, skipped hubs, limits), never silently cut.
* Verification labels (:func:`verification_of`): ``community_edited_not_verified`` (Wikidata),
  ``derived`` (the statement's qualifiers declare a ``derivation``: computed from sourced inputs, e.g. the
  Cliopatria territorial overlay, not reported by a source), else ``source_reported_not_verified``.
* ``origin``: every query takes an optional origin filter (``live`` / ``test_fixture``; ``None`` = both, the
  caller then says when fixture rows are mixed in — the API defaults to ``live`` in production).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Connection, text

from reva.kernel.errors import ValidationProblem
from reva.kg.model import CAUSAL_PIDS, PID_RE, QID_RE, KgTime, _Reject, parse_time

Direction = Literal["out", "in", "both"]

MAX_HOPS = 5
DEFAULT_HUB_LIMIT = 2_000
#: Upper bound of a caller-chosen hub limit (review V3, SEC-1): the hub test counts up to ``hub_limit + 1``
#: rows per candidate node, so the bound caps the cost of one level.
MAX_HUB_LIMIT = 5_000
#: Wall-clock budget of one path search (checked between levels and hub-test batches); exceeding it ends the
#: search with ``truncated`` and the reason.
PATH_TIME_BUDGET_S = 20.0
DEGREE_BATCH = 2_000
MAX_PATHS = 20
MAX_FRONTIER = 5_000
MAX_EDGES_PER_LEVEL = 50_000
MAX_NEIGHBOURS = 1_000
MAX_TIMELINE = 2_000
MAX_SEARCH = 100
#: Class-membership properties ("instance of", "subclass of") connect almost everything to almost everything
#: through classes such as "human" or "country"; they are excluded from paths unless explicitly allowed.
DEFAULT_PATH_EXCLUDED = ("P31", "P279")
COMMUNITY_EDITED_SOURCES = frozenset({"wikidata"})


def is_community_edited(source_key: str | None) -> bool:
    """Wikidata and every ``wikidata.*`` sub-key (fixtures, harvest variants) is community-edited."""
    return bool(source_key) and any(source_key == s or source_key.startswith(s + ".") for s in COMMUNITY_EDITED_SOURCES)


PATH_NOTE = (
    "A path is a chain of sourced statements that connect two entities; it is not a causal explanation. "
    "Each hop cites its statement. Causal wording appears only where a source itself asserts a cause or "
    "effect (has cause / has effect), and is then attributed to that source."
)
COMMUNITY_NOTE = "Wikidata statements are community-edited and are not verified by REVA."
ORIGINS = ("live", "test_fixture")
REPO_ROOT = Path(__file__).resolve().parents[3]
COUNTRY_QIDS = REPO_ROOT / "registry" / "entities" / "country_qids.json"


def _check_origin(origin: str | None) -> str | None:
    if origin is not None and origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture", origin=str(origin)[:32])
    return origin


def _origin_sql(origin: str | None, alias: str = "s") -> str:
    return "" if origin is None else f" AND {alias}.origin = :origin"


@lru_cache(maxsize=4)
def _country_qids_cached(path: str, mtime: float) -> dict[str, str]:
    doc = json.loads(Path(path).read_text())
    out: dict[str, str] = {}
    for code, e in (doc.get("countries") or {}).items():
        q = e.get("qid") if isinstance(e, Mapping) else e
        if isinstance(code, str) and isinstance(q, str) and QID_RE.match(q) and q.startswith("Q"):
            out[code] = q
    return out


def country_qid_registry(path: Path = COUNTRY_QIDS) -> dict[str, str]:
    """ISO3 → Wikidata QID from WDHARVEST's reviewed ``registry/entities/country_qids.json`` (crosswalk
    entity → QID); empty when the file is absent or unreadable."""
    try:
        return _country_qids_cached(str(path), path.stat().st_mtime)
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class TimeWindow:
    """Inclusive window as PostgreSQL date literals (``0044-01-01 BC``; ``None`` = open side). They are SQL
    parameters only; outputs render them with :func:`astro_iso`."""

    lo: str | None = None
    hi: str | None = None
    include_undated: bool = True


_DATE_RE = re.compile(r"^-?[0-9]{1,7}(-[0-9]{2}(-[0-9]{2})?)?$")


def window(start: str | None, end: str | None, *, include_undated: bool = True) -> TimeWindow | None:
    """Build a window from ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD`` strings (astronomical years; ``-0043`` is
    44 BCE). The start side uses the start of its period, the end side the end of its period."""
    if start is None and end is None:
        return None
    out: list[str | None] = []
    for v, side in ((start, 0), (end, 1)):
        if v is None:
            out.append(None)
            continue
        if not isinstance(v, str) or not _DATE_RE.match(v.strip()):
            raise ValidationProblem("time bound must be YYYY, YYYY-MM or YYYY-MM-DD", value=str(v)[:32])
        body = v.strip().lstrip("-")
        prec = {0: 9, 1: 10, 2: 11}[body.count("-")]
        try:
            b = parse_time(KgTime(v.strip(), prec))
        except _Reject as e:
            raise ValidationProblem("invalid time bound", value=v[:32], reason=e.reason) from e
        if b[side] is None:
            raise ValidationProblem("time bound outside the supported date range", value=v[:32])
        out.append(b[side])
    w = TimeWindow(out[0], out[1], include_undated)
    if w.lo and w.hi and date_key(w.lo) > date_key(w.hi):
        raise ValidationProblem("time window start is after its end")
    return w


# ---------------------------------------------------------------------------------------------- dates
# ``kg.statement`` keeps its derived bounds as SQL ``date``s, which reach back to 4713 BCE. psycopg loads a
# ``date`` into ``datetime.date`` and cannot represent years before 1 ("date too small"), so no query here
# selects a ``date`` column: it renders it with :func:`_date_sql` (DateStyle-independent text with an era
# marker) and Python turns that into an astronomical ISO string with :func:`astro_iso`. Range filtering and
# ordering stay in SQL on the ``date`` values themselves, which handle BCE dates correctly.
_PG_DATE_RE = re.compile(r"^([0-9]{4,7})-([0-9]{2})-([0-9]{2})(?: (AD|BC))?$")


def _date_sql(expr: str, alias: str) -> str:
    """``SELECT`` item rendering a SQL ``date`` as ``YYYY-MM-DD AD|BC`` (``to_char``: independent of DateStyle)."""
    return f"to_char({expr}, 'YYYY-MM-DD BC') AS {alias}"


def date_key(pg: str) -> tuple[int, int, int]:
    """(astronomical year, month, day) of a PostgreSQL date literal (``0044-03-15 BC``, ``1975-01-01`` or
    ``1975-01-01 AD``): the one sort key for dates read from or sent to SQL (year 0 = 1 BCE)."""
    mt = _PG_DATE_RE.match(pg.strip())
    if not mt:
        raise ValueError(f"not a PostgreSQL date literal: {pg[:32]!r}")
    y = int(mt.group(1))
    return (1 - y if mt.group(4) == "BC" else y, int(mt.group(2)), int(mt.group(3)))


def astro_iso(pg: str | None) -> str | None:
    """ISO 8601 text of a PostgreSQL date literal in astronomical year numbering, rendered like the atlas
    (``reva.atlas.tables.period_text``): ``0044-03-15 BC`` -> ``-0043-03-15``, ``0001-01-01 BC`` ->
    ``0000-01-01``, years above 9999 with a ``+`` sign."""
    if pg is None:
        return None
    y, m, d = date_key(pg)
    year = f"-{-y:04d}" if y < 0 else (f"+{y}" if y > 9999 else f"{y:04d}")
    return f"{year}-{m:02d}-{d:02d}"


def _check_qid(q: str) -> str:
    if not isinstance(q, str) or not QID_RE.match(q):
        raise ValidationProblem(
            "invalid entity id (expected a Wikidata Q-id or a provider-scoped key)", qid=str(q)[:64]
        )
    return q


def _check_pids(pids: Iterable[str] | None) -> list[str] | None:
    if pids is None:
        return None
    out = sorted(set(pids))
    if len(out) > 200 or not all(isinstance(p, str) and PID_RE.match(p) for p in out):
        raise ValidationProblem("invalid property list (max 200 property ids)")
    return out


def _time_sql(w: TimeWindow | None, alias: str = "s") -> tuple[str, dict[str, Any]]:
    if w is None:
        return "", {}
    dated = f"({alias}.valid_lo IS NOT NULL OR {alias}.valid_hi IS NOT NULL)"
    overlap = (
        f"daterange({alias}.valid_lo, {alias}.valid_hi, '[]') && "
        "daterange(CAST(:w_lo AS date), CAST(:w_hi AS date), '[]')"
    )
    cond = f"({dated} AND {overlap})"
    if w.include_undated:
        cond = f"({cond} OR ({alias}.valid_lo IS NULL AND {alias}.valid_hi IS NULL))"
    return " AND " + cond, {"w_lo": w.lo, "w_hi": w.hi}


# ---------------------------------------------------------------------------------------------- labels
def labels_for(conn: Connection, qids: Iterable[str], langs: Sequence[str] = ("en", "fr")) -> dict[str, dict[str, str]]:
    qs = sorted(set(qids))
    if not qs:
        return {}
    out: dict[str, dict[str, str]] = {}
    for qid, lang, label, origin in conn.execute(
        text("""SELECT e.qid, l.lang, l.label, e.origin FROM kg.entity_current e
                  JOIN kg.entity_label l ON l.entity_id = e.id
                 WHERE e.qid = ANY(:q) AND NOT l.is_alias AND l.lang = ANY(:langs)
                 ORDER BY e.qid, (e.origin = 'live') DESC"""),
        {"q": qs, "langs": list(langs)},
    ).all():
        out.setdefault(qid, {}).setdefault(lang, label)
        _ = origin
    return out


def kinds_for(conn: Connection, qids: Iterable[str]) -> dict[str, str]:
    """Entity kind per QID from the current snapshots (live preferred); absent when no snapshot has one."""
    qs = sorted(set(qids))
    if not qs:
        return {}
    out: dict[str, str] = {}
    for qid, kind in conn.execute(
        text("""SELECT e.qid, e.kind FROM kg.entity_current e
                 WHERE e.qid = ANY(:q) AND e.kind IS NOT NULL ORDER BY e.qid, (e.origin = 'live') DESC"""),
        {"q": qs},
    ).all():
        out.setdefault(qid, kind)
    return out


def property_info(conn: Connection, pids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ps = sorted(set(pids))
    if not ps:
        return {}
    return {
        r["pid"]: dict(r)
        for r in conn.execute(
            text("""SELECT pid, value_kind, reva_predicate, inverse_name, label_en, label_fr, inverse_label_en,
                           inverse_label_fr FROM kg.property_current WHERE pid = ANY(:p)"""),
            {"p": ps},
        ).mappings()
    }


def _display(qid: str, labels: Mapping[str, Mapping[str, str]], lang: str = "en") -> str:
    lab = labels.get(qid, {})
    return lab.get(lang) or lab.get("en") or lab.get("fr") or qid


def _time_out(iso: str | None, prec: int | None) -> dict[str, Any] | None:
    return None if iso is None else {"time": iso, "precision": prec}


def derivation_of(qualifiers: Any) -> tuple[str, str | None] | None:
    """``(derivation, method)`` when a statement's qualifiers declare that REVA (or a source pipeline) computed
    it rather than a source reporting it, e.g. the Cliopatria territorial overlay
    (``derivation: computed_spatial_overlay``, ``method: reva.kg.overlay/1``); ``None`` otherwise."""
    if not isinstance(qualifiers, Mapping):
        return None
    d = qualifiers.get("derivation")
    if isinstance(d, Mapping):  # {"method": ..., ...}
        name, method = d.get("kind") or d.get("method") or "derived", d.get("method") or d.get("version")
    elif d is None or d is False or d == "":
        return None
    else:
        name, method = d, qualifiers.get("method")
    return str(name)[:200], (None if method is None else str(method)[:200])


def verification_of(source_key: str | None, qualifiers: Any = None) -> tuple[str, str | None, str | None]:
    """``(verification, derivation, derivation_method)`` of a statement: Wikidata (``wikidata``/``wikidata.*``
    keys) is ``community_edited_not_verified`` whatever its qualifiers say; a statement whose qualifiers declare a
    derivation is ``derived`` (computed, not reported: see the qualifiers for method, inputs and caveats); any
    other is ``source_reported_not_verified``."""
    if is_community_edited(source_key):
        return "community_edited_not_verified", None, None
    der = derivation_of(qualifiers)
    if der is not None:
        return "derived", der[0], der[1]
    return "source_reported_not_verified", None, None


def _provenance(r: Mapping[str, Any]) -> dict[str, Any]:
    verification, derivation, method = verification_of(r["source_key"], r.get("qualifiers"))
    return {
        "source_key": r["source_key"],
        "statement_id": r["statement_id"],
        "statement_key": r["statement_key"],
        "source_revision": r["source_revision"],
        "raw_sha256": r["raw_sha256"],
        "retrieved_at": r["retrieved_at"],
        "recorded_at": r["recorded_at"],
        "load_id": r["load_id"],
        "origin": r["origin"],
        "rank": r["rank"],
        "references": r["refs"],
        "verification": verification,
        "derivation": derivation,
        "derivation_method": method,
    }


def _ref(qid: str, labels: Mapping[str, Mapping[str, str]], kinds: Mapping[str, str] | None) -> dict[str, Any]:
    return {"qid": qid, "label": _display(qid, labels), "kind": (kinds or {}).get(qid)}


def _statement_out(
    r: Mapping[str, Any],
    labels: Mapping[str, Mapping[str, str]],
    props: Mapping[str, Any],
    kinds: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    p = props.get(r["property_pid"], {})
    return {
        "row_id": r["id"],
        "subject": _ref(r["subject_qid"], labels, kinds),
        "property": {
            "pid": r["property_pid"],
            "label_en": p.get("label_en"),
            "label_fr": p.get("label_fr"),
            "reva_predicate": p.get("reva_predicate"),
            "causal_assertion": r["property_pid"] in CAUSAL_PIDS,
        },
        "object": None if r["object_qid"] is None else _ref(r["object_qid"], labels, kinds),
        "value": r["value"],
        "start": _time_out(r["start_time"], r["start_precision"]),
        "end": _time_out(r["end_time"], r["end_precision"]),
        "point_in_time": _time_out(r["pit_time"], r["pit_precision"]),
        "time_calendars": r["time_calendars"],
        "valid_from": astro_iso(r["valid_lo"]),
        "valid_to": astro_iso(r["valid_hi"]),
        "qualifiers": r["qualifiers"],
        "provenance": _provenance(r),
    }


#: Statement columns; the derived ``date`` bounds come back as ``YYYY-MM-DD AD|BC`` text (see :func:`_date_sql`).
_ST_SELECT = f"""s.id, s.statement_key, s.statement_id, s.origin, s.subject_qid, s.property_pid, s.object_qid,
  s.value, s.start_time, s.start_precision, s.end_time, s.end_precision, s.pit_time, s.pit_precision,
  s.time_calendars, {_date_sql("s.valid_lo", "valid_lo")}, {_date_sql("s.valid_hi", "valid_hi")},
  {_date_sql("s.value_time_lo", "value_time_lo")}, {_date_sql("s.value_time_hi", "value_time_hi")}, s.qualifiers,
  s.rank, s.refs, s.source_key, s.source_revision, s.raw_sha256, s.retrieved_at, s.recorded_at, s.load_id"""


# ---------------------------------------------------------------------------------------------- entity
def exists(conn: Connection, qid: str) -> bool:
    """Is ``qid`` known to the graph (a loaded snapshot or a statement endpoint)?"""
    _check_qid(qid)
    return bool(
        conn.execute(
            text("""SELECT EXISTS (SELECT 1 FROM kg.entity WHERE qid = :q)
                       OR EXISTS (SELECT 1 FROM kg.statement WHERE subject_qid = :q)
                       OR EXISTS (SELECT 1 FROM kg.statement WHERE object_qid = :q)"""),
            {"q": qid},
        ).scalar_one()
    )


def entity(conn: Connection, qid: str, *, origin: str | None = None) -> dict[str, Any] | None:
    """The current snapshot of an entity (live preferred over fixture), with its provenance and degree."""
    _check_qid(qid)
    _check_origin(origin)
    row = (
        conn.execute(
            text(f"""SELECT e.*, l.raw_sha256s FROM kg.entity_current e JOIN kg.load l ON l.id = e.load_id
                     WHERE e.qid = :q{_origin_sql(origin, "e")} ORDER BY (e.origin = 'live') DESC LIMIT 1"""),
            {"q": qid, "origin": origin},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        # Known only as a statement endpoint (no snapshot loaded)?
        seen = conn.execute(
            text("""SELECT EXISTS (SELECT 1 FROM kg.statement WHERE subject_qid = :q)
                       OR EXISTS (SELECT 1 FROM kg.statement WHERE object_qid = :q)"""),
            {"q": qid},
        ).scalar_one()
        if not seen:
            return None
        return {
            "qid": qid,
            "snapshot": None,
            "labels": {},
            "aliases": {},
            "descriptions": {},
            "kind": None,
            "sitelinks": None,
            "provenance": None,
            **_degree(conn, qid),
        }
    labels: dict[str, str] = {}
    for lang, lab in conn.execute(
        text("SELECT lang, label FROM kg.entity_label WHERE entity_id = :i AND NOT is_alias ORDER BY lang"),
        {"i": row["id"]},
    ).all():
        labels[lang] = lab
    return {
        "qid": qid,
        "snapshot": row["id"],
        "kind": row["kind"],
        "labels": labels,
        "descriptions": row["descriptions"],
        "aliases": row["aliases"],
        "sitelinks": row["sitelinks"],
        "provenance": {
            "source_key": row["source_key"],
            "source_revision": row["source_revision"],
            "content_sha256": row["content_sha256"],
            "retrieved_at": row["retrieved_at"],
            "recorded_at": row["recorded_at"],
            "load_id": row["load_id"],
            "raw_sha256s": list(row["raw_sha256s"]),
            "origin": row["origin"],
            "verification": "community_edited_not_verified"
            if is_community_edited(row["source_key"])
            else "source_reported_not_verified",
        },
        **_degree(conn, qid),
    }


def _degree(conn: Connection, qid: str, cap: int = 100_000) -> dict[str, Any]:
    out_n, in_n = conn.execute(
        text("""SELECT (SELECT count(*) FROM (SELECT 1 FROM kg.statement_current s WHERE s.subject_qid = :q
                                                LIMIT :cap) a),
                       (SELECT count(*) FROM (SELECT 1 FROM kg.statement_current s WHERE s.object_qid = :q
                                                LIMIT :cap) b)"""),
        {"q": qid, "cap": cap},
    ).one()
    return {"statements_out": int(out_n), "statements_in": int(in_n), "degree_capped_at": cap}


# ---------------------------------------------------------------------------------------------- neighbours
def neighbours(
    conn: Connection,
    qid: str,
    *,
    direction: Direction = "both",
    properties: Iterable[str] | None = None,
    kind: str | None = None,
    time_window: TimeWindow | None = None,
    include_literals: bool = False,
    limit: int = 100,
    after_id: int | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    """Statements touching ``qid`` (outgoing: ``qid`` is the subject; incoming: it is the object), keyset
    paginated by statement row id."""
    _check_qid(qid)
    _check_origin(origin)
    pids = _check_pids(properties)
    if direction not in ("out", "in", "both"):
        raise ValidationProblem("direction must be out, in or both")
    limit = max(1, min(int(limit), MAX_NEIGHBOURS))
    conds = []
    if direction in ("out", "both"):
        conds.append("s.subject_qid = :q" + ("" if include_literals else " AND s.object_qid IS NOT NULL"))
    if direction in ("in", "both"):
        conds.append("s.object_qid = :q")
    tsql, tparams = _time_sql(time_window)
    kind_sql = ""
    if kind is not None:
        kind_sql = """ AND EXISTS (SELECT 1 FROM kg.entity_current e WHERE e.kind = :kind
                        AND e.qid = CASE WHEN s.subject_qid = :q THEN s.object_qid ELSE s.subject_qid END)"""
    rows = [
        dict(r)
        for r in conn.execute(
            text(f"""SELECT {_ST_SELECT} FROM kg.statement_current s
                     WHERE (({") OR (".join(conds)}))
                       AND (CAST(:pids AS text[]) IS NULL OR s.property_pid = ANY(:pids))
                       AND (CAST(:after AS bigint) IS NULL OR s.id > :after)
                       {tsql}{kind_sql}{_origin_sql(origin)}
                     ORDER BY s.id LIMIT :lim"""),
            {"q": qid, "pids": pids, "after": after_id, "lim": limit + 1, "kind": kind, "origin": origin, **tparams},
        ).mappings()
    ]
    more = len(rows) > limit
    rows = rows[:limit]
    qids = {qid} | {r["subject_qid"] for r in rows} | {r["object_qid"] for r in rows if r["object_qid"]}
    labels = labels_for(conn, qids)
    kinds = kinds_for(conn, qids)
    props = property_info(conn, {r["property_pid"] for r in rows})
    edges = []
    for r in rows:
        e = _statement_out(r, labels, props, kinds)
        e["direction"] = "out" if r["subject_qid"] == qid else "in"
        edges.append(e)
    return {
        "qid": qid,
        "label": _display(qid, labels),
        "edges": edges,
        "next_after_id": rows[-1]["id"] if more and rows else None,
        "origins": sorted({r["origin"] for r in rows}),
    }


# ---------------------------------------------------------------------------------------------- timeline
def timeline(
    conn: Connection,
    qid: str,
    *,
    include_incoming: bool = True,
    time_window: TimeWindow | None = None,
    limit: int = 500,
    origin: str | None = None,
) -> dict[str, Any]:
    """Dated statements about ``qid`` sorted by date: statements with start/end/point-in-time qualifiers and
    time-valued statements (e.g. inception). Undated statements are not part of a timeline."""
    _check_qid(qid)
    _check_origin(origin)
    limit = max(1, min(int(limit), MAX_TIMELINE))
    tsql, tparams = "", {}
    if time_window is not None:
        win = "daterange(CAST(:w_lo AS date), CAST(:w_hi AS date), '[]')"
        tsql = (
            " AND (((s.valid_lo IS NOT NULL OR s.valid_hi IS NOT NULL)"
            f" AND daterange(s.valid_lo, s.valid_hi, '[]') && {win})"
            " OR (s.valid_lo IS NULL AND s.valid_hi IS NULL AND s.value_time_lo IS NOT NULL"
            f" AND daterange(s.value_time_lo, s.value_time_hi, '[]') && {win}))"
        )
        tparams = {"w_lo": time_window.lo, "w_hi": time_window.hi}
    who = "(s.subject_qid = :q OR s.object_qid = :q)" if include_incoming else "s.subject_qid = :q"
    # ordered in SQL on the date itself (correct across the era boundary); only rendered as text
    sort_sql = "COALESCE(s.valid_lo, s.value_time_lo, s.valid_hi)"
    rows = [
        dict(r)
        for r in conn.execute(
            text(f"""SELECT {_ST_SELECT}, {_date_sql(sort_sql, "sort_date")}
                       FROM kg.statement_current s
                      WHERE {who}
                        AND (s.valid_lo IS NOT NULL OR s.valid_hi IS NOT NULL OR s.value_time_lo IS NOT NULL)
                        {tsql}{_origin_sql(origin)}
                      ORDER BY {sort_sql}, s.id LIMIT :lim"""),
            {"q": qid, "lim": limit + 1, "origin": origin, **tparams},
        ).mappings()
    ]
    truncated = len(rows) > limit
    rows = rows[:limit]
    qids = {qid} | {r["subject_qid"] for r in rows} | {r["object_qid"] for r in rows if r["object_qid"]}
    labels = labels_for(conn, qids)
    kinds = kinds_for(conn, qids)
    props = property_info(conn, {r["property_pid"] for r in rows})
    events = []
    for r in rows:
        e = _statement_out(r, labels, props, kinds)
        e["direction"] = "out" if r["subject_qid"] == qid else "in"
        e["sort_date"] = astro_iso(r["sort_date"])
        e["dated_by"] = "validity_qualifiers" if (r["valid_lo"] or r["valid_hi"]) else "time_value"
        events.append(e)
    return {
        "qid": qid,
        "label": _display(qid, labels),
        "events": events,
        "truncated": truncated,
        "origins": sorted({r["origin"] for r in rows}),
    }


# ---------------------------------------------------------------------------------------------- search
#: Matched labels returned per hit (the entity's best matching label first).
MAX_MATCHED_PER_HIT = 10


def search(
    conn: Connection,
    q: str,
    *,
    langs: Sequence[str] = ("fr", "en"),
    kind: str | None = None,
    limit: int = 20,
    origin: str | None = None,
) -> dict[str, Any]:
    """Label/alias search (case-insensitive): exact, then prefix, then substring (substring needs >= 3
    characters and uses the trigram index when pg_trgm is installed).

    Ranking is per *entity* (``DISTINCT ON`` its best-matching label), so an entity with many matching
    labels or aliases never pushes others out; ``truncated`` says when more entities match than ``limit``."""
    q = (q or "").strip()
    if not 1 <= len(q) <= 200:
        raise ValidationProblem("search text must have 1..200 characters")
    if (
        not langs
        or len(langs) > 10
        or not all(isinstance(lang, str) and re.match(r"^[a-z]{2,3}(-[a-z0-9]{1,8}){0,3}$", lang) for lang in langs)
    ):
        raise ValidationProblem("invalid language list")
    _check_origin(origin)
    limit = max(1, min(int(limit), MAX_SEARCH))
    ql = q.lower()
    esc = ql.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = ("%" + esc + "%") if len(ql) >= 3 else (esc + "%")
    rows = conn.execute(
        text(rf"""WITH m AS (
                   SELECT e.id AS entity_id, e.qid, e.kind, e.sitelinks, e.origin, l.lang, l.label, l.is_alias,
                          CASE WHEN lower(l.label) = :ql THEN 0 WHEN lower(l.label) LIKE :prefix ESCAPE '\' THEN 1
                               ELSE 2 END AS match_rank
                     FROM kg.entity_label l
                     JOIN kg.entity e ON e.id = l.entity_id
                    WHERE lower(l.label) LIKE :pat ESCAPE '\' AND l.lang = ANY(:langs)
                      AND (CAST(:kind AS text) IS NULL OR e.kind = :kind){_origin_sql(origin, "e")}
                      AND NOT EXISTS (SELECT 1 FROM kg.entity n WHERE n.qid = e.qid AND n.origin = e.origin
                                         AND n.id > e.id)),
                 best AS (
                   SELECT DISTINCT ON (qid) qid, entity_id, kind, sitelinks, origin, match_rank, is_alias
                     FROM m ORDER BY qid, (origin = 'live') DESC, match_rank, is_alias)
                 SELECT b.qid, b.kind, b.sitelinks, b.origin, b.match_rank,
                        (SELECT jsonb_agg(jsonb_build_object('lang', x.lang, 'label', x.label, 'alias', x.is_alias)
                                          ORDER BY x.match_rank, x.is_alias, x.lang, x.label)
                           FROM (SELECT * FROM m WHERE m.entity_id = b.entity_id
                                  ORDER BY m.match_rank, m.is_alias, m.lang, m.label LIMIT {MAX_MATCHED_PER_HIT}) x)
                          AS matched
                   FROM best b
                  ORDER BY b.match_rank, b.is_alias, b.sitelinks DESC NULLS LAST, b.qid
                  LIMIT :lim"""),
        {
            "ql": ql,
            "prefix": esc + "%",
            "pat": pattern,
            "langs": list(langs),
            "kind": kind,
            "lim": limit + 1,
            "origin": origin,
        },
    ).mappings()
    hits: dict[str, dict[str, Any]] = {}
    truncated = False
    for r in rows:
        if len(hits) >= limit:
            truncated = True
            break
        hits[r["qid"]] = {
            "qid": r["qid"],
            "kind": r["kind"],
            "sitelinks": r["sitelinks"],
            "origin": r["origin"],
            "match": ("exact", "prefix", "substring")[r["match_rank"]],
            "matched": list(r["matched"] or []),
        }
    labels = labels_for(conn, hits.keys())
    for qid, h in hits.items():
        h["labels"] = labels.get(qid, {})
    return {
        "query": q,
        "results": list(hits.values()),
        "mode": "substring" if len(ql) >= 3 else "prefix",
        "truncated": truncated,
    }


ISO3_RE = re.compile(r"^[A-Z]{3}$")


def iso3_lookup(conn: Connection, iso3: str, *, origin: str | None = None, limit: int = 20) -> dict[str, Any]:
    """ISO 3166-1 alpha-3 → entity. The reviewed ``registry/entities/country_qids.json`` decides when it lists
    the code (``match = "iso3_registry"``); otherwise the KG's ISO alpha-3 statements (Wikidata P298,
    ``match = "iso3_statement"``) — several items may hold one code (PSE, YUG, ATA on Wikidata): they are
    all returned and ``ambiguous`` is set, never silently resolved."""
    if not isinstance(iso3, str) or not ISO3_RE.match(iso3):
        raise ValidationProblem("iso3 must be three upper-case letters", iso3=str(iso3)[:8])
    _check_origin(origin)
    reg = country_qid_registry().get(iso3)
    rows: list[dict[str, Any]] = []
    if reg is not None:
        rows = [{"qid": reg, "rank": None, "statement_origin": None, "source": "registry/entities/country_qids.json"}]
    else:
        rows = [
            {**dict(r), "source": "kg:P298"}
            for r in conn.execute(
                text(f"""SELECT s.subject_qid AS qid,
                                CASE WHEN bool_or(s.rank = 'preferred') THEN 'preferred' ELSE 'normal' END AS rank,
                                min(s.origin) AS statement_origin
                           FROM kg.statement_current s
                          WHERE s.property_pid = 'P298' AND s.value->>'value' = :iso3{_origin_sql(origin)}
                          GROUP BY s.subject_qid ORDER BY bool_or(s.rank = 'preferred') DESC, s.subject_qid
                          LIMIT :lim"""),
                {"iso3": iso3, "origin": origin, "lim": limit + 1},
            ).mappings()
        ]
    truncated = len(rows) > limit
    rows = rows[:limit]
    qids = [r["qid"] for r in rows]
    labels = labels_for(conn, qids)
    ents = {
        r["qid"]: dict(r)
        for r in conn.execute(
            text(f"""SELECT DISTINCT ON (e.qid) e.qid, e.kind, e.sitelinks, e.origin FROM kg.entity_current e
                      WHERE e.qid = ANY(:q){_origin_sql(origin, "e")} ORDER BY e.qid, (e.origin = 'live') DESC"""),
            {"q": qids, "origin": origin},
        ).mappings()
    }
    results = []
    for r in rows:
        e = ents.get(r["qid"], {})
        results.append(
            {
                "qid": r["qid"],
                "kind": e.get("kind"),
                "sitelinks": e.get("sitelinks"),
                "origin": e.get("origin") or r.get("statement_origin") or "live",
                "match": "iso3_registry" if r["source"].startswith("registry") else "iso3_statement",
                "matched": [{"lang": "iso3", "label": iso3, "alias": False}],
                "labels": labels.get(r["qid"], {}),
                "rank": r.get("rank"),
            }
        )
    return {
        "query": iso3,
        "results": results,
        "mode": "iso3",
        "truncated": truncated,
        "ambiguous": len(results) > 1,
        "iso3_source": "registry/entities/country_qids.json" if reg is not None else "kg P298 statements",
    }


# ---------------------------------------------------------------------------------------------- paths
@dataclass
class _Side:
    parent: dict[str, tuple[str, dict[str, Any]] | None]
    frontier: set[str]
    depth: int = 0


@dataclass
class PathSearch:
    """Bounds and diagnostics of one path search (returned to the caller, never hidden)."""

    max_hops: int
    hub_limit: int
    allowed_properties: list[str] | None
    excluded_properties: list[str]
    time_window: TimeWindow | None
    levels_expanded: int = 0
    nodes_visited: int = 0
    edges_scanned: int = 0
    hubs_skipped: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)


def _edges_for(
    conn: Connection,
    frontier: Sequence[str],
    pids: list[str] | None,
    excluded: list[str],
    w: TimeWindow | None,
    cap: int,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    tsql, tparams = _time_sql(w)
    return [
        dict(r)
        for r in conn.execute(
            text(f"""SELECT {_ST_SELECT} FROM kg.statement_current s
                      WHERE (s.subject_qid = ANY(:f) OR s.object_qid = ANY(:f)) AND s.object_qid IS NOT NULL
                        AND s.subject_qid <> s.object_qid
                        AND (CAST(:pids AS text[]) IS NULL OR s.property_pid = ANY(:pids))
                        AND NOT (s.property_pid = ANY(:excl))
                        {tsql}{_origin_sql(origin)}
                      LIMIT :cap"""),
            {"f": list(frontier), "pids": pids, "excl": excluded, "cap": cap, "origin": origin, **tparams},
        ).mappings()
    ]


def _degrees(conn: Connection, qids: Sequence[str], cap: int, deadline: float | None = None) -> dict[str, int]:
    """Entity-valued statement count per node, counted on stored rows and capped at ``cap + 1`` (a hub test,
    not an exact degree). Counted in batches of :data:`DEGREE_BATCH`; once ``deadline`` (``time.monotonic``)
    has passed, the remaining nodes are left out (the caller treats untested nodes as not traversable)."""
    out: dict[str, int] = {}
    for i in range(0, len(qids), DEGREE_BATCH):
        if deadline is not None and i and time.monotonic() > deadline:
            break
        out.update(_degree_batch(conn, qids[i : i + DEGREE_BATCH], cap))
    return out


def _degree_batch(conn: Connection, qids: Sequence[str], cap: int) -> dict[str, int]:
    if not qids:
        return {}
    return {
        q: int(n)
        for q, n in conn.execute(
            text("""SELECT q, (SELECT count(*) FROM (SELECT 1 FROM kg.statement s
                                                       WHERE s.subject_qid = q AND s.object_qid IS NOT NULL
                                                       LIMIT :cap) a)
                            + (SELECT count(*) FROM (SELECT 1 FROM kg.statement s WHERE s.object_qid = q
                                                       LIMIT :cap) b)
                      FROM unnest(CAST(:qs AS text[])) AS q"""),
            {"qs": list(qids), "cap": cap + 1},
        ).all()
    }


def paths(
    conn: Connection,
    from_qid: str,
    to_qid: str,
    *,
    max_hops: int = 4,
    allowed_properties: Iterable[str] | None = None,
    time_window: TimeWindow | None = None,
    hub_limit: int = DEFAULT_HUB_LIMIT,
    allow_hubs: Iterable[str] = (),
    max_paths: int = 5,
    max_frontier: int = MAX_FRONTIER,
    max_edges_per_level: int = MAX_EDGES_PER_LEVEL,
    origin: str | None = None,
    time_budget_s: float | None = PATH_TIME_BUDGET_S,
) -> dict[str, Any]:
    """Shortest connections between two entities (bidirectional breadth-first search over entity-valued
    statements, either direction), bounded:

    * ``max_hops`` <= 5; nodes with more than ``hub_limit`` entity-valued statements are not traversed
      (skipped, and reported) unless they are an endpoint or listed in ``allow_hubs``;
    * ``allowed_properties`` restricts the hop properties; without it, "instance of"/"subclass of" are
      excluded (``DEFAULT_PATH_EXCLUDED``);
    * ``time_window``: every hop's statement must overlap the window (undated statements are allowed and
      flagged unless ``include_undated`` is false);
    * per-level frontier/edge caps and a wall-clock budget (``time_budget_s``, checked between levels and
      hub-test batches); hitting one sets ``truncated`` with the reason.

    Returns up to ``max_paths`` shortest paths (all of the same, minimal length), each hop with its property
    labels, direction, dates and its statement's provenance.
    """
    _check_qid(from_qid)
    _check_qid(to_qid)
    if not 1 <= int(max_hops) <= MAX_HOPS:
        raise ValidationProblem(f"max_hops must be between 1 and {MAX_HOPS}")
    if not 1 <= int(hub_limit) <= MAX_HUB_LIMIT:
        raise ValidationProblem(f"hub_limit must be between 1 and {MAX_HUB_LIMIT}")
    max_paths = max(1, min(int(max_paths), MAX_PATHS))
    _check_origin(origin)
    deadline = None if time_budget_s is None else time.monotonic() + float(time_budget_s)
    pids = _check_pids(allowed_properties)
    excluded = [p for p in DEFAULT_PATH_EXCLUDED if pids is None or p not in pids]
    allowed_hubs = {_check_qid(h) for h in allow_hubs}
    diag = PathSearch(int(max_hops), int(hub_limit), pids, excluded, time_window)
    result: dict[str, Any] = {"from": from_qid, "to": to_qid, "paths": [], "note": PATH_NOTE}
    if from_qid == to_qid:
        raise ValidationProblem("from and to are the same entity")
    # Many small index probes: JIT compilation (triggered by the ANY(array) cost estimates) only adds latency.
    conn.exec_driver_sql("SET LOCAL jit = off")
    fwd = _Side({from_qid: None}, {from_qid})
    bwd = _Side({to_qid: None}, {to_qid})
    endpoints = {from_qid, to_qid}
    meetings: list[str] = []
    while fwd.depth + bwd.depth < diag.max_hops and fwd.frontier and bwd.frontier:
        if deadline is not None and diag.levels_expanded and time.monotonic() > deadline:
            diag.truncated = True
            diag.truncation_reasons.append(
                f"time budget of {time_budget_s:g} s exhausted after {diag.levels_expanded} level(s)"
            )
            break
        side, other = (fwd, bwd) if len(fwd.frontier) <= len(bwd.frontier) else (bwd, fwd)
        frontier = sorted(side.frontier)
        if len(frontier) > max_frontier:
            diag.truncated = True
            diag.truncation_reasons.append(f"frontier of {len(frontier)} nodes cut to {max_frontier}")
            frontier = frontier[:max_frontier]
        edges = _edges_for(conn, frontier, pids, excluded, time_window, max_edges_per_level + 1, origin)
        if len(edges) > max_edges_per_level:
            diag.truncated = True
            diag.truncation_reasons.append(f"more than {max_edges_per_level} edges at level {diag.levels_expanded + 1}")
            edges = edges[:max_edges_per_level]
        diag.levels_expanded += 1
        diag.edges_scanned += len(edges)
        fset = set(frontier)
        candidates: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for e in edges:
            for here, there in ((e["subject_qid"], e["object_qid"]), (e["object_qid"], e["subject_qid"])):
                if here in fset and there not in side.parent:
                    candidates.setdefault(there, []).append((here, e))
        # hub test on newly reached nodes (endpoints and explicitly allowed hubs are always traversable)
        test = [q for q in candidates if q not in endpoints and q not in allowed_hubs]
        deg = _degrees(conn, test, diag.hub_limit, deadline)
        untested = {q for q in test if q not in deg}
        if untested:
            diag.truncated = True
            diag.truncation_reasons.append(
                f"time budget exhausted during the hub test: {len(untested)} reached node(s) not traversed"
            )
        nxt: set[str] = set()
        for there, via in sorted(candidates.items()):
            if there in untested:
                continue
            if deg.get(there, 0) > diag.hub_limit:
                diag.hubs_skipped[there] = deg[there]
                continue
            here, e = min(via, key=lambda x: x[1]["id"])
            side.parent[there] = (here, e)
            nxt.add(there)
            if there in other.parent:
                meetings.append(there)
        side.frontier = nxt
        side.depth += 1
        diag.nodes_visited = len(fwd.parent) + len(bwd.parent)
        if meetings:
            break

    found_paths = []
    seen = set()
    for m in meetings[:1000]:  # bounded: path assembly is cheap but meetings can be many
        hops_f: list[tuple[str, str, dict[str, Any]]] = []
        cur = m
        while fwd.parent.get(cur) is not None:
            prev, e = fwd.parent[cur]  # type: ignore[misc]
            hops_f.append((prev, cur, e))
            cur = prev
        hops_f.reverse()
        hops_b: list[tuple[str, str, dict[str, Any]]] = []
        cur = m
        while bwd.parent.get(cur) is not None:
            nxt_q, e = bwd.parent[cur]  # type: ignore[misc]
            hops_b.append((cur, nxt_q, e))
            cur = nxt_q
        hops = hops_f + hops_b
        sig = tuple(h[2]["id"] for h in hops)
        if sig in seen:
            continue
        seen.add(sig)
        found_paths.append(hops)
    # Meeting nodes of one level can sit at different depths on the other side: keep the shortest only.
    if found_paths:
        shortest = min(len(p) for p in found_paths)
        found_paths = [p for p in found_paths if len(p) == shortest]
    if len(found_paths) > max_paths:
        diag.truncation_reasons.append(f"{len(found_paths)} shortest paths found; first {max_paths} returned")
        found_paths = found_paths[:max_paths]
    result["found"] = bool(found_paths)
    if not found_paths:
        result["not_found_reason"] = (
            f"no connection within {diag.max_hops} hops under the given filters"
            + (" (search truncated: a connection may exist beyond the bounds)" if diag.truncated else "")
            + (f"; {len(diag.hubs_skipped)} hub node(s) were not traversed" if diag.hubs_skipped else "")
        )
    return _finish(conn, result, found_paths, diag)


def _finish(
    conn: Connection, result: dict[str, Any], found: list[list[tuple[str, str, dict[str, Any]]]], diag: PathSearch
) -> dict[str, Any]:
    qids = {result["from"], result["to"]} | {q for p in found for h in p for q in (h[0], h[1])}
    qids |= set(list(diag.hubs_skipped)[:50])
    labels = labels_for(conn, qids)
    kinds = kinds_for(conn, qids)
    props = property_info(conn, {h[2]["property_pid"] for p in found for h in p})
    out_paths = []
    for p in found:
        hops = []
        for a, b, e in p:
            forward = e["subject_qid"] == a
            info = props.get(e["property_pid"], {})
            label_en = info.get("label_en") or e["property_pid"]
            hop = _statement_out(e, labels, props, kinds)
            hop["from"] = _ref(a, labels, kinds)
            hop["to"] = _ref(b, labels, kinds)
            hop["direction"] = "forward" if forward else "inverse"
            if forward:
                hop["relation_label_en"] = label_en
                hop["relation_label_fr"] = info.get("label_fr")
            else:
                hop["relation_label_en"] = (
                    info.get("inverse_label_en")
                    or (info["inverse_name"].replace("_", " ") if info.get("inverse_name") else None)
                    or f"is the object of '{label_en}' for"
                )
                hop["relation_label_fr"] = info.get("inverse_label_fr")
            hop["dated"] = e["valid_lo"] is not None or e["valid_hi"] is not None
            hop["time_status"] = (
                ("within_window" if hop["dated"] else "undated")
                if diag.time_window is not None
                else ("dated" if hop["dated"] else "undated")
            )
            src = e["source_key"]
            subj, obj = _display(e["subject_qid"], labels), _display(e["object_qid"], labels)
            if e["property_pid"] in CAUSAL_PIDS:
                hop["wording"] = (
                    f"{subj} — {label_en} — {obj}: a causal relation asserted by {src} "
                    f"(statement {e['statement_id'] or e['statement_key']}), not established by REVA"
                )
                hop["causal_assertion"] = {"asserted_by": src, "statement_key": e["statement_key"]}
            else:
                hop["wording"] = (
                    f"{_display(a, labels)} is connected via '{hop['relation_label_en']}' to {_display(b, labels)}"
                )
                hop["causal_assertion"] = None
            hops.append(hop)
        out_paths.append(
            {
                "length": len(hops),
                "hops": hops,
                "wording": f"{_display(result['from'], labels)} is connected to {_display(result['to'], labels)} "
                f"via {len(hops)} sourced statement(s); a connection is not a cause.",
                "origins": sorted({h["provenance"]["origin"] for h in hops}),
            }
        )
    result["paths"] = out_paths
    result["from_label"] = _display(result["from"], labels)
    result["to_label"] = _display(result["to"], labels)
    d = dict(diag.__dict__)
    tw = diag.time_window
    d["time_window"] = (
        None if tw is None else {"lo": astro_iso(tw.lo), "hi": astro_iso(tw.hi), "include_undated": tw.include_undated}
    )
    d["hubs_skipped"] = [
        {"qid": q, "label": _display(q, labels), "degree_at_least": n} for q, n in list(diag.hubs_skipped.items())[:50]
    ]
    d["hubs_skipped_count"] = len(diag.hubs_skipped)
    result["search"] = d
    if any(is_community_edited(h["provenance"]["source_key"]) for p in out_paths for h in p["hops"]):
        result["community_note"] = COMMUNITY_NOTE
    return result
