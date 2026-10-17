"""Watch-rule evaluation against the change feed (and, for thresholds, the latest observed atlas value).

* ``new_data`` — series: an ``ingest`` touching a table of the item (its table, or a table of the concept's source
  order / headline series); entity: a ``kg_batch`` of the record's source, or an ``ingest`` naming its ISO3.
* ``revision`` — series: an ``ingest`` that re-registered one of those tables (a new version of a table that existed).
* ``resolution`` — forecast: a ``question_resolved`` item for the question.
* ``threshold`` — series: the latest **observed** value (projections never count) compared with ``op value``; fires
  only when the comparison holds, with the value, period and source. No observed value → ``not_evaluable`` with the
  reason (never treated as 0).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, text

from reva.v8.search import hrefs

OPS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}
RULE_FR = {"new_data": "Nouvelle donnée", "revision": "Révision", "resolution": "Résolution", "threshold": "Seuil"}


def series_tables(item: Mapping[str, Any]) -> set[str]:
    """Atlas tables (``source.dataset``) that can carry this series item."""
    out: set[str] = set()
    if item.get("table"):
        t = str(item["table"])
        out.add(t[6:] if t.startswith("atlas.") else t)
    concept = str(item.get("concept") or "")
    try:
        if item.get("headline"):
            from reva.atlas.source_order import cached_headlines

            h = cached_headlines().concepts.get(concept)
            for hs in h.series if h else ():
                out.add(f"{hs.series.source}.{hs.series.dataset}")
        else:
            from reva.atlas.catalog import cached_catalog

            c = cached_catalog().concepts.get(concept)
            for s in c.source_order if c else ():
                out.add(f"{s.source}.{s.dataset}")
    except Exception:  # noqa: S110 - registries unreadable: only the explicit table is watched
        pass
    return out


def entity_sources(conn: Connection, items: list[Mapping[str, Any]]) -> dict[str, str]:
    qids = sorted({str(i["id"]) for i in items if i.get("kind") == "entity"})
    if not qids:
        return {}
    rows = conn.execute(
        text(
            """SELECT DISTINCT ON (qid) qid, source_key FROM kg.entity WHERE qid = ANY(:q)
               ORDER BY qid, id DESC"""
        ),
        {"q": qids},
    )
    return {q: s for q, s in rows}


def _refs(change: Mapping[str, Any], typ: str) -> set[str]:
    return {r["id"] for r in change.get("refs", []) if r.get("type") == typ}


def _alert(rule: Mapping[str, Any], item: Mapping[str, Any] | None, change: Mapping[str, Any] | None, msg: str,
           *, status: str = "fired", observed: dict[str, Any] | None = None, reason: str | None = None,
           at: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "rule_id": rule["id"],
        "item_uid": rule["item_uid"],
        "kind": rule["kind"],
        "at": at or (change or {}).get("at"),
        "message": msg,
        "change": dict(change) if change else None,
        "observed": observed,
        "status": status,
        "reason": reason,
        "item_label": (item or {}).get("label"),
    }


# ------------------------------------------------------------------------------------------------ thresholds
_OBS_LOCK = threading.Lock()
_OBS: dict[tuple[str, str, str, bool], dict[str, Any] | None] = {}


def _warm_query(snapshot_sha: str) -> Any | None:
    """The process' already-open atlas query for this snapshot, or None (never opens or builds one here)."""
    from reva.atlas import query as aq

    with aq._QLOCK:
        for key, q in aq._QUERIES.items():
            if key.endswith(f"|{snapshot_sha}"):
                return q
    return None


def latest_observed(q: Any, concept: str, geo: str, headline: bool) -> dict[str, Any] | None:
    """Latest observed (non-projection) value of one country series, with period and source; None if none."""
    key = (q.snapshot.sha256, concept, geo, headline)
    with _OBS_LOCK:
        if key in _OBS:
            return _OBS[key]
    if headline:
        from reva.atlas.source_order import cached_headlines

        cells = q.headline_series(cached_headlines(), concept, geo, projections="exclude")["best"]
    else:
        from reva.atlas.catalog import cached_catalog

        cells = q.series(cached_catalog(), concept, geo)["best"]
    obs = [
        c
        for c in cells
        if c.get("value") not in (None, "")
        and not c.get("projection")
        and (c.get("flags") or {}).get("estimate_kind") != "projection"
    ]
    out = None
    if obs:
        c = max(obs, key=lambda c: str(c.get("period_start") or c.get("year") or ""))
        by = c.get("answered_by") or {}
        out = {
            "value": str(c["value"]),
            "period": str(c.get("year") or c.get("period_start")),
            "source": by.get("series") or c.get("series") or c.get("source"),
            "unit": c.get("unit"),
            "licence": ((c.get("rights") or {}).get("licence") or "")[:200] or None,
        }
    with _OBS_LOCK:
        _OBS[key] = out
    return out


def _threshold(rule: Mapping[str, Any], item: Mapping[str, Any], atlas: Callable[[], Any | None]) -> dict[str, Any]:
    if item.get("kind") != "series":
        return _alert(rule, item, None, "Règle de seuil sans objet", status="not_evaluable", reason="not_a_series")
    q = atlas()
    label = item.get("label") or f"{item.get('concept')} · {item.get('geo')}"
    if q is None:
        return _alert(
            rule,
            item,
            None,
            f"Seuil non évalué : {label}",
            status="not_evaluable",
            reason="atlas_query_not_ready (fichier de requête de l'atlas non ouvert par ce serveur)",
        )
    try:
        obs = latest_observed(q, str(item["concept"]), str(item["geo"]), bool(item.get("headline")))
    except Exception as e:  # unknown concept / country: reported, never guessed
        return _alert(
            rule, item, None, f"Seuil non évalué : {label}", status="not_evaluable", reason=f"{type(e).__name__}"
        )
    if obs is None:
        return _alert(
            rule,
            item,
            None,
            f"Seuil non évalué : {label} — aucune valeur observée",
            status="not_evaluable",
            reason="no_observed_value",
        )
    try:
        holds = OPS[str(rule["op"])](Decimal(obs["value"]), Decimal(str(rule["value"])))
    except (InvalidOperation, KeyError):
        return _alert(rule, item, None, f"Seuil non évalué : {label}", status="not_evaluable", reason="not_numeric")
    status = "fired" if holds else "not_fired"
    msg = f"{label} : {obs['value']} ({obs['period']}, {obs['source']}) {rule['op']} {rule['value']}"
    return _alert(rule, item, None, ("Seuil franchi — " if holds else "Seuil non franchi — ") + msg,
                  status=status, observed=obs)  # fmt: skip


# ------------------------------------------------------------------------------------------------ evaluate
def evaluate(
    conn: Connection,
    board: Mapping[str, Any],
    changes: list[Mapping[str, Any]],
    *,
    atlas: Callable[[], Any | None] = lambda: None,
) -> list[dict[str, Any]]:
    items = {i["uid"]: i for i in board.get("items", [])}
    ent_src = entity_sources(conn, list(items.values()))
    out: list[dict[str, Any]] = []
    for rule in board.get("rules", []):
        item = items.get(rule["item_uid"])
        if item is None:
            out.append(_alert(rule, None, None, "Élément retiré du tableau", status="not_evaluable",
                              reason="item_missing"))  # fmt: skip
            continue
        label = item.get("label") or item.get("id") or item.get("questionId") or ""
        kind = rule["kind"]
        if kind == "threshold":
            out.append(_threshold(rule, item, atlas))
            continue
        fired = 0
        for ch in changes:
            ck = ch.get("kind")
            hit = False
            if item["kind"] == "series" and ck == "ingest":
                tables = series_tables(item)
                if kind == "new_data":
                    hit = bool(tables & _refs(ch, "table"))
                elif kind == "revision":
                    hit = bool(tables & set((ch.get("detail") or {}).get("revised_tables") or []))
            elif item["kind"] == "entity" and kind == "new_data":
                ident = str(item["id"])
                if ck == "kg_batch" and ent_src.get(ident) and ent_src[ident] in _refs(ch, "source"):
                    hit = True
                elif ck == "ingest" and ident in _refs(ch, "geo"):
                    hit = True
            elif item["kind"] == "forecast" and kind == "resolution" and ck == "question_resolved":
                hit = str(item["questionId"]) in _refs(ch, "question")
            if hit:
                fired += 1
                out.append(_alert(rule, item, ch, f"{RULE_FR[kind]} — {label} : {ch.get('title')}"))
        if not fired:
            continue
    out.sort(key=lambda a: str(a.get("at") or ""), reverse=True)
    return out


def item_href(item: Mapping[str, Any]) -> str | None:
    k = item.get("kind")
    if k == "entity":
        return hrefs.entity(str(item["entityKind"]), str(item["id"]))
    if k == "series":
        params = {"concept": str(item["concept"]), "geo": str(item["geo"])}
        return hrefs.series(str(item.get("table") or "headline"), params)
    if k == "forecast":
        return hrefs.entity("question", str(item["questionId"]))
    if k == "decision":
        return hrefs.decision(str(item["ref"]))
    return None
