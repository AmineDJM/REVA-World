"""V8 change feed: what changed in REVA, read from what the database recorded (agent V8API).

Every item reports recorded facts only (server ``recorded_at``, counts read from the database or from the
per-load counts written by ``reva v8 index build``); nothing is inferred. Kinds:

``ingest`` (atlas tables / datasets registered, grouped per source per 10 minutes, revisions flagged),
``atlas_snapshot``, ``kg_batch`` (``kg.load``), ``causal_claims`` (``kg.causal_load``), ``effects``
(``kg.effect_load`` + retractions), ``forecast_batch`` (sealed issue batches), ``question_resolved`` (one item per
resolved / void question, with the scores of its forecasters and the official forecaster's when one was sealed),
``evaluation`` (back-tests and registry annotations), ``owner_decision`` (open items only the owner can decide,
``registry/coverage/pending.yaml``).
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from reva.v8.search import hrefs

CHANGE_KINDS = (
    "ingest",
    "atlas_snapshot",
    "kg_batch",
    "causal_claims",
    "effects",
    "forecast_batch",
    "question_resolved",
    "evaluation",
    "owner_decision",
)
PER_KIND_MAX = 2000
OFFICIAL_RE = re.compile(r"\.reference_")
ISO3_RE = re.compile(r"^[A-Z]{3}$")
COUNTS_FILE = "load_counts.json"
KIND_FR = {
    "ingest": "Nouvelles données",
    "atlas_snapshot": "Instantané de l'atlas",
    "kg_batch": "Graphe de connaissances",
    "causal_claims": "Affirmations causales",
    "effects": "Estimations d'effet",
    "forecast_batch": "Prévisions scellées",
    "question_resolved": "Questions résolues",
    "evaluation": "Évaluations",
    "owner_decision": "Décisions du propriétaire",
}


@dataclass
class ChangeQuery:
    since: datetime
    until: datetime | None = None
    kinds: Iterable[str] | None = None
    limit: int = 50
    cursor: str | None = None
    origin: str | None = None


def encode_cursor(at: str, id_: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([at, id_]).encode()).decode().rstrip("=")


def decode_cursor(c: str) -> tuple[str, str]:
    raw = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4))
    at, id_ = json.loads(raw)
    if not isinstance(at, str) or not isinstance(id_, str):
        raise ValueError("bad cursor")
    return at, id_


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _num(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float):
        return format(Decimal(repr(v)).normalize(), "f")
    return str(v)


def _fr_int(n: int | None) -> str:
    return "?" if n is None else f"{n:,}".replace(",", " ")


def item(
    kind: str,
    id_: str,
    at: datetime,
    title: str,
    *,
    summary: str | None = None,
    source: str | None = None,
    severity: str = "info",
    counts: Mapping[str, int | None] | None = None,
    refs: list[dict[str, str]] | None = None,
    links: list[dict[str, str]] | None = None,
    origin: str = "live",
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "kind": kind,
        "at": _iso(at),
        "title": title,
        "summary": summary,
        "source": source,
        "severity": severity,
        "counts": {k: v for k, v in (counts or {}).items() if v is not None},
        "refs": refs or [],
        "links": links or [],
        "origin": origin,
        "detail": dict(detail or {}),
    }


# ------------------------------------------------------------------------------------------------ helpers
_SRC_LOCK = threading.Lock()
_SRC_TITLES: dict[str, str] = {}


def source_titles() -> dict[str, str]:
    """atlas short source key and registry source_key → provider title (registry/sources, atlas specs)."""
    with _SRC_LOCK:
        if not _SRC_TITLES:
            try:
                from reva.atlas.sources import atlas_sources

                for e in atlas_sources().values():
                    _SRC_TITLES.setdefault(e.spec.atlas_source, e.spec.provider)
                    _SRC_TITLES.setdefault(e.spec.source_key, e.spec.provider)
            except Exception:  # noqa: S110 - titles are cosmetic; keys are shown instead
                pass
        return _SRC_TITLES


def load_counts(directory: Path | None = None) -> dict[str, Any]:
    """Per-load row counts written by ``reva v8 index build`` (kg loads, causal loads); {} when absent."""
    from reva.v8.search.build import index_dir

    p = (directory or index_dir()) / COUNTS_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, ValueError):
        return {}


def _oc(origin: str | None, col: str = "origin") -> str:
    return f" AND {col} = :origin" if origin else ""


# ------------------------------------------------------------------------------------------------ collectors
def ingests(conn: Connection, since: datetime, until: datetime, origin: str | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            f"""
            WITH f AS (
              SELECT dataset_key, subject_key, origin, min(recorded_at) AS first_at FROM evidence.dataset
              GROUP BY 1, 2, 3
            ), d AS (
              SELECT d0.dataset_key, d0.concept_id, d0.subject_key, d0.row_count, d0.origin, d0.recorded_at,
                     CASE WHEN d0.concept_id = 'atlas.table' THEN split_part(d0.subject_key, '.', 1)
                          ELSE split_part(d0.dataset_key, '.', 1) END AS source,
                     CASE WHEN d0.concept_id = 'atlas.table' THEN d0.subject_key ELSE d0.dataset_key END AS tbl,
                     d0.recorded_at > f.first_at AS revision
              FROM evidence.dataset d0
              JOIN f ON f.dataset_key = d0.dataset_key AND f.subject_key = d0.subject_key AND f.origin = d0.origin
              WHERE d0.recorded_at > :since AND d0.recorded_at <= :until{_oc(origin, "d0.origin")}
            )
            SELECT source, origin,
                   to_timestamp(floor(extract(epoch FROM recorded_at) / 600) * 600) AS bucket,
                   max(recorded_at) AS at, sum(row_count) AS rows, count(DISTINCT tbl) AS tables,
                   array_agg(DISTINCT tbl) AS tbls,
                   array_agg(DISTINCT tbl) FILTER (WHERE revision) AS revised,
                   array_agg(DISTINCT subject_key) FILTER (WHERE concept_id <> 'atlas.table') AS subjects,
                   bool_and(concept_id = 'atlas.table') AS atlas
            FROM d GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT :lim"""
        ),
        {"since": since, "until": until, "origin": origin, "lim": PER_KIND_MAX},
    ).mappings()
    titles = source_titles()
    out = []
    for r in rows:
        tbls = sorted(r["tbls"] or [])
        revised = sorted(r["revised"] or [])
        geos = sorted(s for s in (r["subjects"] or []) if ISO3_RE.match(s))
        src = r["source"]
        name = titles.get(src) or src
        n_new = len(tbls) - len(revised)
        what = []
        if n_new:
            what.append(f"{n_new} table{'s' if n_new > 1 else ''} nouvelle{'s' if n_new > 1 else ''}")
        if revised:
            what.append(f"{len(revised)} révision{'s' if len(revised) > 1 else ''}")
        refs = [{"type": "table", "id": t} for t in tbls[:200]] + [{"type": "source", "id": src}]
        refs += [{"type": "geo", "id": g} for g in geos[:300]]
        links = [{"label": f"Voir {t}", "href": hrefs.series(t)} for t in tbls[:3]] if r["atlas"] else []
        links.append({"label": "Source", "href": hrefs.entity("source", src)})
        out.append(
            item(
                "ingest",
                f"ingest:{src}:{r['origin']}:{_iso(r['bucket'])}",
                r["at"],
                f"{name} : {_fr_int(int(r['rows'] or 0))} lignes ({', '.join(what)})",
                summary=", ".join(tbls[:6]) + (" …" if len(tbls) > 6 else ""),
                source=src,
                counts={"rows": int(r["rows"] or 0), "tables": int(r["tables"]), "revisions": len(revised)},
                refs=refs,
                links=links,
                origin=r["origin"],
                detail={"tables": tbls[:200], "revised_tables": revised[:200], "geos": geos[:300]},
            )
        )
    return out


def snapshots(conn: Connection, since: datetime, until: datetime, origin: str | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """SELECT id::text AS id, parameters, recorded_at FROM meta.activity
               WHERE kind = 'atlas.snapshot' AND recorded_at > :since AND recorded_at <= :until
               ORDER BY recorded_at DESC LIMIT :lim"""
        ),
        {"since": since, "until": until, "lim": PER_KIND_MAX},
    ).mappings()
    out = []
    for r in rows:
        p = r["parameters"] or {}
        org = p.get("origin") or "live"
        if origin and org != origin:
            continue
        n_tables = p.get("tables") if isinstance(p.get("tables"), int) else None
        out.append(
            item(
                "atlas_snapshot",
                f"atlas_snapshot:{r['id']}",
                r["recorded_at"],
                "Nouvel instantané de l'atlas" + (f" : {_fr_int(n_tables)} tables" if n_tables else ""),
                severity="notable",
                counts={"tables": n_tables},
                origin=org,
                detail={k: v for k, v in p.items() if isinstance(v, str | int) and k != "origin"},
            )
        )
    return out


def kg_batches(
    conn: Connection, since: datetime, until: datetime, origin: str | None, counts: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """KG loads grouped per source per 10 minutes (a harvest writes many small loads)."""
    rows = conn.execute(
        text(
            f"""WITH l AS (
                  SELECT id, source_key, origin, recorded_at, loader_version, parameters,
                         cardinality(raw_sha256s) AS n_raw,
                         to_timestamp(floor(extract(epoch FROM recorded_at) / 600) * 600) AS bucket
                  FROM kg.load WHERE recorded_at > :since AND recorded_at <= :until{_oc(origin)}
                  ORDER BY recorded_at DESC LIMIT :lim),
                r AS (SELECT load_id, count(*) AS n FROM kg.statement_retraction
                      WHERE load_id IN (SELECT id FROM l) GROUP BY load_id)
                SELECT l.source_key, l.origin, l.bucket, max(l.recorded_at) AS at,
                       array_agg(l.id ORDER BY l.id) AS ids, sum(l.n_raw) AS n_raw,
                       sum(COALESCE(r.n, 0)) AS retracted,
                       array_agg(DISTINCT l.loader_version) AS versions,
                       (array_agg(l.parameters ORDER BY l.id DESC))[1] AS parameters
                FROM l LEFT JOIN r ON r.load_id = l.id
                GROUP BY 1, 2, 3 ORDER BY 4 DESC"""
        ),
        {"since": since, "until": until, "origin": origin, "lim": PER_KIND_MAX * 10},
    ).mappings()
    kgc = counts.get("kg") or {}
    kg_max = int(counts.get("kg_max_load_id") or 0)
    out = []
    for g in rows:
        src, org, bucket = g["source_key"], g["origin"], g["bucket"]
        ids = [int(x) for x in g["ids"]]
        known = [kgc.get(str(i), {"entities": 0, "statements": 0}) for i in ids if str(i) in kgc or i <= kg_max]
        complete = len(known) == len(ids)
        ent = sum(c.get("entities", 0) for c in known) if complete else None
        stm = sum(c.get("statements", 0) for c in known) if complete else None
        retracted = int(g["retracted"] or 0)
        p = g["parameters"] or {}
        bits = []
        if complete:
            bits.append(f"{_fr_int(ent)} entités, {_fr_int(stm)} énoncés")
        if retracted:
            bits.append(f"{_fr_int(retracted)} retraits")
        n = len(ids)
        out.append(
            item(
                "kg_batch",
                f"kg_batch:{src}:{org}:{ids[0]}-{ids[-1]}",
                g["at"],
                f"Graphe : {n} lot{'s' if n > 1 else ''} {src}" + (f" — {', '.join(bits)}" if bits else ""),
                summary=str(p.get("why") or p.get("revision") or p.get("kg_revision") or "") or None,
                source=src,
                counts={"loads": n, "entities": ent, "statements": stm, "retractions": retracted},
                refs=[{"type": "source", "id": src}] + [{"type": "kg_load", "id": str(i)} for i in ids[:200]],
                links=[{"label": "Source", "href": hrefs.entity("source", src)}],
                origin=org,
                detail={
                    "load_ids": ids[:200],
                    "loader_versions": sorted(str(v) for v in g["versions"] or []),
                    "raw_artifacts": int(g["n_raw"] or 0),
                    "counts": "load_counts"
                    if complete
                    else "not computed yet for every load (run `uv run reva v8 index build`)",
                    "bucket": _iso(bucket),
                },
            )
        )
    return out


CAUSAL_KIND_FR = {
    "dataset": "jeu de données codé",
    "kg_statements": "énoncés du graphe",
    "grading": "notation",
    "candidates": "candidats (en quarantaine)",
}


def causal_loads(
    conn: Connection, since: datetime, until: datetime, origin: str | None, counts: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            f"""WITH l AS (
                  SELECT id, kind, source_key, dataset, origin, recorded_at, parameters FROM kg.causal_load
                  WHERE recorded_at > :since AND recorded_at <= :until{_oc(origin)}
                  ORDER BY recorded_at DESC LIMIT :lim),
                r AS (SELECT load_id, count(*) AS n FROM kg.causal_claim_retraction
                      WHERE load_id IN (SELECT id FROM l) GROUP BY load_id)
                SELECT l.*, COALESCE(r.n, 0) AS retracted FROM l LEFT JOIN r ON r.load_id = l.id
                ORDER BY l.recorded_at DESC"""
        ),
        {"since": since, "until": until, "origin": origin, "lim": PER_KIND_MAX},
    ).mappings()
    cc = counts.get("causal") or {}
    out = []
    for r in rows:
        p = r["parameters"] or {}
        added = cc.get(str(r["id"]), 0 if int(r["id"]) <= int(counts.get("causal_max_load_id") or 0) else None)
        graded = p.get("claims") if r["kind"] == "grading" else None
        bits = []
        if added is not None:
            bits.append(f"{_fr_int(added)} affirmations ajoutées")
        if r["retracted"]:
            bits.append(f"{_fr_int(r['retracted'])} retirées")
        if graded is not None:
            bits.append(f"{_fr_int(graded)} affirmations notées")
        label = r["dataset"] or r["source_key"]
        out.append(
            item(
                "causal_claims",
                f"causal_claims:{r['id']}",
                r["recorded_at"],
                f"Causalité : {CAUSAL_KIND_FR.get(r['kind'], r['kind'])} {label}"
                + (f" — {', '.join(bits)}" if bits else ""),
                source=r["source_key"],
                counts={"added": added, "retracted": int(r["retracted"]), "graded": graded},
                refs=[{"type": "source", "id": r["source_key"]}, {"type": "claim_load", "id": str(r["id"])}],
                origin=r["origin"],
                detail={"load_kind": r["kind"], "dataset": r["dataset"]},
            )
        )
    return out


def effect_changes(conn: Connection, since: datetime, until: datetime, origin: str | None) -> list[dict[str, Any]]:
    loads: list[Any] = list(
        conn.execute(
            text(
                f"""SELECT l.id, l.kind, l.source_key, l.dataset, l.origin, l.recorded_at,
                       array_agg(e.estimate_key ORDER BY e.estimate_key) FILTER (WHERE e.id IS NOT NULL) AS keys
                FROM kg.effect_load l LEFT JOIN kg.effect_estimate e ON e.load_id = l.id
                WHERE l.recorded_at > :since AND l.recorded_at <= :until{_oc(origin, "l.origin")}
                GROUP BY l.id ORDER BY l.recorded_at DESC LIMIT :lim"""
            ),
            {"since": since, "until": until, "origin": origin, "lim": PER_KIND_MAX},
        ).mappings()
    )
    ret: dict[int, list[tuple[str, str]]] = {}
    if loads:
        for lid, k, why in conn.execute(
            text("SELECT load_id, estimate_key, reason FROM kg.effect_estimate_retraction WHERE load_id = ANY(:l)"),
            {"l": [int(r["id"]) for r in loads]},
        ):
            ret.setdefault(int(lid), []).append((k, why))
    out = []
    for r in loads:
        keys = list(r["keys"] or [])
        retired = ret.get(int(r["id"]), [])
        bits = []
        if keys:
            bits.append(f"{len(keys)} ajoutée{'s' if len(keys) > 1 else ''}")
        if retired:
            bits.append(f"{len(retired)} retirée{'s' if len(retired) > 1 else ''}")
        out.append(
            item(
                "effects",
                f"effects:{r['id']}",
                r["recorded_at"],
                f"Estimations d'effet ({r['dataset']}) : {', '.join(bits) or 'aucune ligne'}",
                summary="Nombres attribués à leur source ; REVA n'établit pas ces effets.",
                source=r["source_key"],
                severity="notable",
                counts={"added": len(keys), "retired": len(retired)},
                refs=[{"type": "estimate", "id": k} for k in keys[:200]]
                + [{"type": "estimate", "id": k} for k, _ in retired[:200]]
                + [{"type": "source", "id": r["source_key"]}],
                links=[{"label": k, "href": hrefs.entity("effect", k)} for k in keys[:3]],
                origin=r["origin"],
                detail={
                    "load_kind": r["kind"],
                    "retired": [{"estimate_key": k, "reason": why} for k, why in retired[:200]],
                },
            )
        )
    return out


def _activities(
    conn: Connection, kinds: list[str], since: datetime, until: datetime, *, like: str | None = None
) -> list[Any]:
    cond = "kind = ANY(:kinds)" + (" OR kind LIKE :like" if like else "")
    return list(
        conn.execute(
            text(
                f"""SELECT id::text AS id, kind, parameters, recorded_at FROM meta.activity
                    WHERE ({cond}) AND recorded_at > :since AND recorded_at <= :until
                    ORDER BY recorded_at DESC LIMIT :lim"""
            ),
            {"kinds": kinds, "like": like, "since": since, "until": until, "lim": PER_KIND_MAX},
        ).mappings()
    )


ISSUE_KINDS = ["evaluation.forecast_registry_issue", "evaluation.health_registry_issue"]
RESOLVE_KINDS = ["evaluation.forecast_registry_resolve"]


def forecast_batches(conn: Connection, since: datetime, until: datetime, origin: str | None) -> list[dict[str, Any]]:
    from reva.evaluation.registry import read as R

    meta = {b["batch_sha256"]: b for b in R.batches(conn, origin)}
    out = []
    for r in _activities(conn, ISSUE_KINDS, since, until):
        p = r["parameters"] or {}
        org = p.get("origin") or "live"
        if origin and org != origin:
            continue
        sha = p.get("batch_sha256") or p.get("manifest_sha256") or ""
        b = meta.get(sha) or {}
        n_q = b.get("n_questions") if b else p.get("questions")
        health = r["kind"] == "evaluation.health_registry_issue"
        what = "santé" if health else (str(p.get("cycle") or b.get("cycle") or ""))
        title = f"Prévisions scellées ({what}) : {_fr_int(n_q)} questions" if n_q else f"Prévisions scellées ({what})"
        if p.get("ensemble"):
            title += " — ensemble REVA"
        models = [m.get("model") for m in b.get("models", [])] if b else []
        out.append(
            item(
                "forecast_batch",
                f"forecast_batch:{sha[:16] or r['id']}",
                r["recorded_at"],
                title,
                summary=str(p.get("label") or "")[:300] or None,
                source="reva.forecast_registry",
                severity="notable",
                counts={"questions": n_q, "models": len(models) or None},
                refs=[{"type": "batch", "id": sha}] if sha else [],
                links=[{"label": "Scores", "href": hrefs.scoreboard()}],
                origin=org,
                detail={
                    "batch_sha256": sha,
                    "cycle": p.get("cycle") or b.get("cycle"),
                    "registry": "health" if health else "forecast",
                    "models": models,
                    "supplement_of": b.get("supplement_of") if b else None,
                },
            )
        )
    return out


def _score_values(s: Mapping[str, Any]) -> dict[str, str]:
    keys = ("abs_error", "crps", "log_score", "brier", "interval_score", "covered", "point")
    return {k: v for k in keys if (v := _num(s.get(k))) is not None}


def resolutions(
    conn: Connection, since: datetime, until: datetime, origin: str | None, store: Any
) -> list[dict[str, Any]]:
    from reva.evaluation.registry import read as R

    oc = " AND e.task_scope->>'origin' = :origin" if origin else ""
    rows = conn.execute(
        text(
            f"""SELECT e.task_scope, e.metrics, v.model_key, e.recorded_at FROM model.evaluation e
                JOIN model.version v ON v.id = e.model_version_id
                WHERE e.metrics->>'kind' = 'registry_score' AND e.task_scope->>'kind' = 'forecast_registry'
                  AND e.recorded_at > :since AND e.recorded_at <= :until{oc}
                ORDER BY e.recorded_at DESC"""
        ),
        {"since": since, "until": until, "origin": origin},
    ).mappings()
    by_res: dict[str, dict[str, Any]] = {}
    for r in rows:
        m, ts = r["metrics"], r["task_scope"]
        sha = m.get("resolution_sha256") or ""
        g = by_res.setdefault(
            sha,
            {
                "at": r["recorded_at"],
                "batch": ts.get("batch_sha256"),
                "origin": ts.get("origin") or "live",
                "qids": list(ts.get("question_ids") or []),
                "scores": {},
            },
        )
        for s in m.get("scores") or []:
            g["scores"].setdefault(s.get("question_id"), {})[m.get("model") or r["model_key"]] = _score_values(s)
    out = []
    for sha, g in by_res.items():
        outcomes: dict[str, Any] = {}
        questions: dict[str, Any] = {}
        if store is not None and sha:
            try:
                doc = R.CACHE.get(store, sha)
                outcomes = {o["question_id"]: o for o in doc.get("outcomes", [])}
                if g["batch"]:
                    man = R.CACHE.get(store, g["batch"])
                    questions = {q["question_id"]: q for q in man.get("questions", [])}
            except Exception:  # noqa: S110 - manifests unreadable: the item still says what was recorded
                pass
        for qid in g["qids"]:
            o = outcomes.get(qid) or {}
            q = questions.get(qid) or {}
            sc = g["scores"].get(qid) or {}
            official = {k: v for k, v in sc.items() if OFFICIAL_RE.search(k)}
            status = o.get("status") or ("resolved" if sc else "unknown")
            label = q.get("label_fr") or q.get("text_fr") or qid
            where = " ".join(
                x for x in (q.get("geo_label_fr") or q.get("geo"), str(q.get("reference_period") or "")) if x
            )
            if status == "resolved":
                outcome = f" : résultat {_num(o.get('value'))}" if o.get("value") is not None else ""
                title = f"Question résolue — {label}" + (f" ({where})" if where else "") + outcome
            elif status == "void":
                title = f"Question annulée — {label}" + (f" ({where})" if where else "")
            else:
                title = f"Résolution enregistrée — {label}"
            refs = [{"type": "question", "id": qid}]
            if q.get("family"):
                refs.append({"type": "family", "id": q["family"]})
            out.append(
                item(
                    "question_resolved",
                    f"question_resolved:{qid}:{sha[:12]}",
                    g["at"],
                    title,
                    summary=(o.get("reason") or None) if status == "void" else None,
                    source="reva.forecast_registry",
                    severity="notable",
                    counts={"forecasters_scored": len(sc)},
                    refs=refs,
                    links=[
                        {"label": "Question", "href": hrefs.entity("question", qid)},
                        {"label": "Scores", "href": hrefs.scoreboard(q.get("family"))},
                    ],
                    origin=g["origin"],
                    detail={
                        "status": status,
                        "outcome": _num(o.get("value")),
                        "scores": sc,
                        "official": official or "absent",
                        "resolution_sha256": sha,
                        "batch_sha256": g["batch"],
                    },
                )
            )
    return out


EVAL_FR = {
    "evaluation.forecast_registry_annotate": "Annotation du registre de prévisions",
    "evaluation.forecast_registry_backtest": "Rétro-test (registre de prévisions)",
    "evaluation.health_registry_backtest": "Rétro-test santé",
    "evaluation.conflict_backtest": "Rétro-test conflits",
    "evaluation.finance_backtest": "Rétro-test finance",
}


def evaluations(conn: Connection, since: datetime, until: datetime, origin: str | None) -> list[dict[str, Any]]:
    out = []
    for r in _activities(conn, [], since, until, like="evaluation.%"):
        if r["kind"] in ISSUE_KINDS or r["kind"] in RESOLVE_KINDS:
            continue
        p = r["parameters"] or {}
        org = p.get("origin") or "live"
        if origin and org != origin:
            continue
        what = p.get("family") or p.get("kind") or ""
        out.append(
            item(
                "evaluation",
                f"evaluation:{r['id']}",
                r["recorded_at"],
                f"{EVAL_FR.get(r['kind'], r['kind'])}" + (f" : {what}" if what else ""),
                summary=str(p.get("label") or "")[:300] or None,
                source="reva.evaluation",
                refs=[{"type": "family", "id": str(p["family"])}] if p.get("family") else [],
                origin=org,
                detail={"activity_kind": r["kind"]},
            )
        )
    return out


def owner_decisions(since: datetime, until: datetime) -> list[dict[str, Any]]:
    try:
        from reva.v8.coverage.pending import load_pending

        reg = load_pending()
    except Exception:  # the registry is another agent's; an unreadable file yields no items
        return []
    upd = getattr(reg, "updated", None)
    try:
        d = upd if isinstance(upd, date) else date.fromisoformat(str(upd))
    except ValueError:
        return []
    at = datetime.combine(d, time(0, 0), tzinfo=UTC)
    if not (since < at <= until):
        return []
    out = []
    for it in reg.items:
        if it.type != "decision" or it.status not in ("open", "blocked"):
            continue
        refs = [{"type": "source", "id": s} for s in it.unlocks.atlas_sources] if it.unlocks else []
        out.append(
            item(
                "owner_decision",
                f"owner_decision:{it.id}",
                at,
                f"Décision du propriétaire : {it.title}",
                summary=it.action,
                severity="action",
                refs=refs,
                links=[{"label": "Couverture", "href": "/v8/couverture"}],
                detail={"decision_kind": it.kind, "status": it.status, "registry": "registry/coverage/pending.yaml"},
            )
        )
    return out


# ------------------------------------------------------------------------------------------------ feed
def read_changes(
    conn: Connection, q: ChangeQuery, *, store: Any = None, counts_dir: Path | None = None
) -> dict[str, Any]:
    until = q.until or datetime.now(UTC)
    kinds = [k for k in (q.kinds or CHANGE_KINDS) if k in CHANGE_KINDS]
    counts = load_counts(counts_dir)
    conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
    collectors: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "ingest": lambda: ingests(conn, q.since, until, q.origin),
        "atlas_snapshot": lambda: snapshots(conn, q.since, until, q.origin),
        "kg_batch": lambda: kg_batches(conn, q.since, until, q.origin, counts),
        "causal_claims": lambda: causal_loads(conn, q.since, until, q.origin, counts),
        "effects": lambda: effect_changes(conn, q.since, until, q.origin),
        "forecast_batch": lambda: forecast_batches(conn, q.since, until, q.origin),
        "question_resolved": lambda: resolutions(conn, q.since, until, q.origin, store),
        "evaluation": lambda: evaluations(conn, q.since, until, q.origin),
        "owner_decision": lambda: owner_decisions(q.since, until),
    }
    items: list[dict[str, Any]] = []
    per_kind: dict[str, int] = {}
    for k in kinds:
        got = collectors[k]()
        per_kind[k] = len(got)
        items.extend(got)
    items.sort(key=lambda x: (x["at"], x["id"]), reverse=True)
    if q.cursor:
        c_at, c_id = decode_cursor(q.cursor)
        items = [x for x in items if (x["at"], x["id"]) < (c_at, c_id)]
    page = items[: q.limit]
    nxt = encode_cursor(page[-1]["at"], page[-1]["id"]) if len(items) > q.limit and page else None
    return {
        "since": _iso(q.since),
        "until": _iso(until),
        "items": page,
        "counts": per_kind,
        "kind_labels": {k: KIND_FR[k] for k in kinds},
        "next_cursor": nxt,
    }


def default_since(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=7)
