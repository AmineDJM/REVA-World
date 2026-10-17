"""Items named by causal claims that the KG cannot label yet, and their place in the harvester queue (audit2 N3).

A claim's qualifiers name Wikidata items — who Wikidata says makes the claim (P3680), the nature of the statement
(P5102), its sourcing circumstances (P1480), the part / jurisdiction / exclusion / inclusion / criterion it is
restricted to (P518, P1001, P1011, P1012, P1013) — and its endpoints are items too. Until the harvester has fetched
and the KG has loaded them, REVA shows them as ``Q7747 (libellé non chargé)``, never a bare id and never a label
written by hand. :func:`unlabelled_items` lists them; :func:`queue_for_harvest` puts them at tier 1
(``TIER_KG_CAUSAL``) of the causal harvester's state (``var/kg/harvest_state/causal.causwd.json``), so the next
CAUSWD session fetches them first. Nothing is fetched here (no network).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from reva.causal import grading as gr

#: Qualifiers whose values are items the wording names.
QUALIFIER_ITEM_PIDS = (
    gr.SUPPORTED_BY_PID,
    gr.NATURE_OF_STATEMENT_PID,
    gr.SOURCING_CIRCUMSTANCES_PID,
    *gr.SCOPE_QUALIFIER_PIDS,
)
UNLOADED_EN = "label not loaded"
UNLOADED_FR = "libellé non chargé"


def unloaded_label(qid: str, lang: str = "en") -> str:
    """``Q7747 (label not loaded)`` / ``Q7747 (libellé non chargé)``: an honest stand-in, never a bare id."""
    return f"{qid} ({UNLOADED_FR if lang == 'fr' else UNLOADED_EN})"


def unlabelled_items(conn: Connection, origin: str | None = "live") -> dict[str, dict[str, Any]]:
    """``{qid: {"pids": [...], "claims": n, "group": "qualifier" | "endpoint"}}`` for Wikidata items named by current
    claims (qualifier values of :data:`QUALIFIER_ITEM_PIDS`, cause / effect ids) that have no en/fr label in the KG."""
    o_sql = " AND c.origin = :o" if origin else ""
    rows = conn.execute(
        text(f"""WITH q AS (
                   SELECT DISTINCT c.id, p.pid, COALESCE(v->>'id', v->>'qid', v #>> '{{}}') AS qid, 'qualifier' AS grp
                     FROM kg.causal_claim_current c
                     CROSS JOIN unnest(CAST(:pids AS text[])) p(pid)
                     CROSS JOIN LATERAL jsonb_array_elements(
                         CASE WHEN jsonb_typeof(c.qualifiers->p.pid) = 'array' THEN c.qualifiers->p.pid
                              ELSE '[]'::jsonb END) v
                    WHERE c.statement_key IS NOT NULL{o_sql}
                   UNION ALL
                   SELECT c.id, 'cause', c.cause_id, 'endpoint' FROM kg.causal_claim_current c
                    WHERE c.statement_key IS NOT NULL{o_sql}
                   UNION ALL
                   SELECT c.id, 'effect', c.effect_id, 'endpoint' FROM kg.causal_claim_current c
                    WHERE c.statement_key IS NOT NULL{o_sql})
                 SELECT q.qid, q.grp, array_agg(DISTINCT q.pid) AS pids, count(DISTINCT q.id) AS claims
                   FROM q
                  WHERE q.qid ~ '^Q[1-9][0-9]*$'
                    AND NOT EXISTS (SELECT 1 FROM kg.entity_current e JOIN kg.entity_label l ON l.entity_id = e.id
                                     WHERE e.qid = q.qid AND NOT l.is_alias AND l.lang IN ('en', 'fr'))
                  GROUP BY q.qid, q.grp"""),
        {"pids": list(QUALIFIER_ITEM_PIDS), "o": origin},
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for qid, grp, pids, n in rows:
        cur = out.setdefault(qid, {"pids": [], "claims": 0, "group": grp})
        cur["pids"] = sorted(set(cur["pids"]) | set(pids))
        cur["claims"] = max(cur["claims"], int(n))
        if grp == "qualifier":  # a qualifier value is the more urgent reason
            cur["group"] = "qualifier"
    return dict(sorted(out.items()))


def queue_for_harvest(items: Mapping[str, Mapping[str, Any]], state_path: Path | None = None) -> dict[str, Any]:
    """Put ``items`` at tier 1 of the causal harvester's queue (``State.add``: a better tier moves an item up; an
    item already fetched is reported as waiting for ``reva kg import-harvest``, not queued again)."""
    from reva.causal_harvest.live import STATE_PATH, TIER_KG_CAUSAL, State

    st = State(state_path or STATE_PATH)
    counts = {"items": len(items), "queued_new": 0, "moved_to_tier1": 0, "already_tier1": 0}
    fetched_not_loaded: list[str] = []
    missing_at_source: list[str] = []
    for qid, info in items.items():
        if qid in st.fetched:
            fetched_not_loaded.append(qid)
            continue
        if qid in st.data["missing"] or qid in st.data["redirects"]:
            missing_at_source.append(qid)
            continue
        before = st.frontier.get(qid)
        before_tier = int(before.get("tier", 99)) if before is not None else None
        st.add(qid, tier=TIER_KG_CAUSAL, reason=f"fixa:causal_{info.get('group', 'item')}_without_kg_label")
        if before is None:
            counts["queued_new"] += 1
        elif before_tier is not None and before_tier > TIER_KG_CAUSAL:
            counts["moved_to_tier1"] += 1
        else:
            counts["already_tier1"] += 1
    st.save()
    return {
        **counts,
        "fetched_not_loaded": sorted(fetched_not_loaded),
        "missing_or_redirected_at_source": sorted(missing_at_source),
        "state_path": str(st.path),
        "tier": TIER_KG_CAUSAL,
    }
