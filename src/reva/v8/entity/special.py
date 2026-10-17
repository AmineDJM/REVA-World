"""Fiches of non-KG objects the console links to (``v8Links.entity('question'|'effect'|'source', id)``): a forecast
question (sealed forecasts, status, outcome), an effect estimate (attributed wording, interval, scope) and a source
(registry record, licence, counts of what it backs)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection, text

from reva.atlas.snapshot import normalise_rights
from reva.v8.entity import claims_part as CL
from reva.v8.entity import common as C
from reva.v8.entity import forecast_part as F
from reva.v8.entity.resolve import NotResolved, Resolved


def _identity(
    res: Resolved, label: str, kind_label: str, description: str | None, source: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "qid": None, "kind": res.kind, "kg_kind": None, "kind_label_fr": kind_label, "label": label, "label_fr": label,
        "label_en": None, "description": description, "definition": None, "definition_source": None, "aliases": [],
        "ids": [{"scheme": kind_label, "value": res.id, "url": None, "source": None, "property": None, "label_fr": "identifiant",
                 "ref": None}],
        "anchor": False, "anchor_note": None, "source": source, "sitelinks": None, "snapshot_loaded": True,
        "community_edited": False, "degree": {"statements_out": 0, "statements_in": 0, "capped_at": 0}, "iso3": None,
    }  # fmt: skip


def fill(
    b: dict[str, Any], conn: Connection, store_factory: Callable[[], Any], res: Resolved, origin: str | None
) -> None:
    if res.special == "question":
        _question(b, conn, store_factory(), res, origin)
    elif res.special == "effect":
        _effect(b, conn, res, origin)
    elif res.special == "source":
        _source(b, conn, res)


def _question(b: dict[str, Any], conn: Connection, store: Any, res: Resolved, origin: str | None) -> None:
    if not res.id.startswith("fq_"):
        raise NotResolved("not a forecast question id", id=res.id)
    fc = F.forecasts(conn, store, origin, question_id=res.id, wait_s=60.0)
    qs = fc["questions"]
    if not qs:
        raise NotResolved("unknown forecast question", id=res.id)
    found = qs[0]
    b["identity"] = _identity(res, found.get("label_fr") or res.id, "Question de prévision", found.get("text_fr"), None)
    b["forecasts"] = {**fc, "questions": qs, "n_total": len(qs), "n_pending": sum(q["status"] == "pending" for q in qs),
                      "n_resolved": sum(q["status"] == "resolved" for q in qs), "families": [str(found.get("family"))]}  # fmt: skip
    q = qs[0] if qs else None
    if q:
        b["headline"] = [
            {"key": "status", "label_fr": "Statut", "value": None, "value_text": q["judged_fr"], "unit": None, "period": q["reference_period"],
             "kind": "judged", "ref": None, "source": None, "missing": None, "points": [], "sub": None, "link": None},
            {"key": "geo", "label_fr": "Zone", "value": None, "value_text": q.get("geo_label_fr") or q.get("geo"), "unit": None, "period": None,
             "kind": "relation", "ref": C.ref("country", q["geo"], q.get("geo_label_fr") or q["geo"]) if q.get("geo") and len(str(q["geo"])) == 3 else None,
             "source": None, "missing": None, "points": [], "sub": None, "link": None},
            {"key": "n", "label_fr": "Prévisions scellées", "value": str(len(q["forecasts"])), "value_text": None, "unit": None, "period": None,
             "kind": "count", "ref": None, "source": None, "missing": None, "points": [], "sub": None, "link": None},
        ]  # fmt: skip
        if q["outcome"] is None and q["status"] != "pending":
            b["missing"].append(
                {
                    "section": "forecasts",
                    "what_fr": "valeur réalisée",
                    "reason_code": q["status"],
                    "reason_fr": q["judged_fr"],
                }
            )


def _effect(b: dict[str, Any], conn: Connection, res: Resolved, origin: str | None) -> None:
    from reva.effects import query as eq

    e = eq.get_estimate(conn, res.id, origin=origin or "any")
    if e is None:
        raise NotResolved("unknown effect estimate", id=res.id)
    item = CL._effect(e, "estimate")
    b["identity"] = _identity(
        res, f"{item['cause_fr']} → {item['effect_fr']}", "Estimation d’effet", item["wording_fr"], item["source"]
    )
    b["effects"] = {"items": [item], "n": 1, "n_country_scope": None, "note_fr": CL.EFFECT_NOTE_FR}
    b["headline"] = [
        {"key": "point", "label_fr": "Estimation ponctuelle", "value": item["point"], "value_text": None if item["point"] else "non publiée",
         "unit": item["unit"], "period": None, "kind": "estimate", "ref": None, "source": item["source"],
         "missing": None if item["point"] else {"code": "not_reported", "reason_fr": "la source ne publie pas de valeur ponctuelle"},
         "points": [], "sub": f"intervalle {item['ci_low']} – {item['ci_high']}" if item["ci_low"] and item["ci_high"] else None, "link": None},
    ]  # fmt: skip


def _source(b: dict[str, Any], conn: Connection, res: Resolved) -> None:
    row = (
        conn.execute(
            text("""SELECT src.source_key, src.title, src.provider_url, src.lineage_family,
                           COALESCE(m.rights, src.rights) AS rights
                      FROM evidence.source src
                      LEFT JOIN LATERAL (SELECT v.rights FROM evidence.source_metadata_version v
                                          WHERE v.source_id = src.id ORDER BY v.version DESC LIMIT 1) m ON true
                     WHERE src.source_key = :k"""),
            {"k": res.id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotResolved("unknown source", id=res.id)
    rights = normalise_rights(row["rights"], basis="source_registry") if row["rights"] else None
    src = {"source": row["source_key"], "label": row["title"], "dataset": None, "statement_key": None, "series": None, "property": None,
           "raw_sha256": None, "selector": None, "retrieved_at": None, "period": None, "licence": C.licence_of(rights),
           "verification": None, "origin": None, "href": row["provider_url"]}  # fmt: skip
    b["identity"] = _identity(res, row["title"], "Source", row["provider_url"], src)
    n_ds = conn.execute(
        text("SELECT count(DISTINCT dataset_key) FROM evidence.dataset WHERE dataset_key LIKE :p"),
        {"p": f"atlas.{res.id}.%"},
    ).scalar_one()
    b["headline"] = [
        {"key": "licence", "label_fr": "Licence", "value": None, "value_text": (rights or {}).get("licence") or "non déclarée",
         "unit": None, "period": None, "kind": "literal", "ref": None, "source": src, "missing": None, "points": [], "sub": None, "link": None},
        {"key": "tables", "label_fr": "Tables atlas chargées", "value": str(n_ds), "value_text": None, "unit": None, "period": None,
         "kind": "count", "ref": None, "source": None, "missing": None, "points": [], "sub": None, "link": None},
    ]  # fmt: skip
