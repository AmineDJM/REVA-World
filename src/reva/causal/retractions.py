"""Corrected reasons for retractions written before the reasons were derived (audit2 N1, migration 0018).

Until ``reva.causal.model/3`` every dataset-claim retraction and every KG statement retraction of ``retract_keys``
was stored as ``removed_at_source``, whatever happened. The rows are append-only and are never edited:
:func:`annotate_retraction_reasons` appends one ``kg.retraction_annotation`` per such row whose reason the archive
contradicts, with the evidence:

* the retracted row's raw artifact (``raw_sha256``) is one of the raw artifacts of the load that retracted it —
  the provider file was byte-identical, so the source still had the row: the retraction was REVA's
  reclassification (``reclassified_by_reva``), not a removal at the source;
* otherwise nothing is annotated (the row is counted as ``unverified``): REVA does not guess.

Idempotent (one annotation per retraction and corrected reason; the database refuses an annotation that does not
restate the row's origin and recorded reason).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import Engine, text

ANNOTATOR = "reva.causal.retractions/1"
NOTE = (
    "audit2 N1: the provider file was byte-identical in the retracting load (same raw sha256), so the source still "
    "had this row; the retraction followed a change of REVA's connector or mapping, not a removal at the source."
)
CHUNK = 2_000

_CLAIMS_SQL = """
SELECT r.id, r.origin, r.reason, r.claim_key AS record_key, c.id AS row_id, c.raw_sha256, c.source_key, c.dataset,
       c.source_revision, c.load_id AS created_load_id, cl.recorded_at AS created_at,
       cl.parameters->>'relations_sha256' AS created_relations_sha256,
       l.id AS retracting_load_id, l.recorded_at AS retracted_at, l.raw_sha256s AS retracting_raws,
       l.parameters->>'dataset_revision' AS retracting_revision, l.parameters->>'connector' AS retracting_connector,
       l.parameters->>'relations_sha256' AS retracting_relations_sha256, l.loader_version AS retracting_loader
  FROM kg.causal_claim_retraction r
  JOIN kg.causal_claim c ON c.id = r.claim_row_id
  JOIN kg.causal_load cl ON cl.id = c.load_id
  JOIN kg.causal_load l ON l.id = r.load_id
 WHERE r.reason = 'removed_at_source' {origin}
   AND NOT EXISTS (SELECT 1 FROM kg.retraction_annotation a
                    WHERE a.target = 'causal_claim_retraction' AND a.retraction_id = r.id)
 ORDER BY r.id"""

_STATEMENTS_SQL = """
SELECT r.id, r.origin, r.reason, r.statement_key AS record_key, s.id AS row_id, s.raw_sha256, s.source_key,
       NULL AS dataset, s.source_revision, s.load_id AS created_load_id, cl.recorded_at AS created_at,
       NULL AS created_relations_sha256,
       l.id AS retracting_load_id, l.recorded_at AS retracted_at, l.raw_sha256s AS retracting_raws,
       r.source_revision AS retracting_revision, l.parameters->>'connector' AS retracting_connector,
       NULL AS retracting_relations_sha256, l.loader_version AS retracting_loader,
       l.parameters->>'reason' AS retracting_note
  FROM kg.statement_retraction r
  JOIN kg.statement s ON s.id = r.statement_row_id
  JOIN kg.load cl ON cl.id = s.load_id
  JOIN kg.load l ON l.id = r.load_id
 WHERE r.reason = 'removed_at_source' {origin}
   AND NOT EXISTS (SELECT 1 FROM kg.retraction_annotation a
                    WHERE a.target = 'statement_retraction' AND a.retraction_id = r.id)
 ORDER BY r.id"""


def _evidence(r: Any) -> dict[str, Any]:
    ev = {
        "basis": "raw_unchanged",
        "raw_sha256": r["raw_sha256"],
        "raw_in_retracting_load": True,
        "retracted_row_id": r["row_id"],
        "source_key": r["source_key"],
        "dataset": r["dataset"],
        "source_revision_from": r["source_revision"],
        "source_revision_to": r["retracting_revision"],
        "created_load_id": r["created_load_id"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "retracting_load_id": r["retracting_load_id"],
        "retracted_at": r["retracted_at"].isoformat() if r["retracted_at"] else None,
        "retracting_loader": r["retracting_loader"],
        "retracting_connector": r["retracting_connector"],
        "relations_sha256_from": r["created_relations_sha256"],
        "relations_sha256_to": r["retracting_relations_sha256"],
    }
    if r.get("retracting_note"):
        ev["retracting_note"] = r["retracting_note"]
    return {k: v for k, v in ev.items() if v is not None}


def annotate_retraction_reasons(engine: Engine, *, origin: str | None = None, apply: bool = True) -> dict[str, Any]:
    """Append corrected-reason annotations for ``removed_at_source`` retractions the archive contradicts."""
    if origin is not None and origin not in ("live", "test_fixture"):
        raise ValueError("origin must be live or test_fixture")
    o_sql = "AND r.origin = :o" if origin else ""
    out: dict[str, Any] = {"apply": apply, "annotator": ANNOTATOR}
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('reva:causal:retraction-annotate', 0))"))
        for target, sql in (("causal_claim_retraction", _CLAIMS_SQL), ("statement_retraction", _STATEMENTS_SQL)):
            rows = conn.execute(text(sql.format(origin=o_sql)), {"o": origin}).mappings().all()
            todo: list[list[Any]] = []
            unverified: Counter[str] = Counter()
            by_source: Counter[str] = Counter()
            for r in rows:
                if r["raw_sha256"] and r["raw_sha256"] in (r["retracting_raws"] or []):
                    todo.append([r["id"], r["origin"], r["reason"], "reclassified_by_reva", _evidence(r)])
                    by_source[f"{r['source_key']}/{r['dataset'] or '-'}"] += 1
                else:
                    unverified[r["source_key"]] += 1
            inserted = 0
            if apply:
                for i in range(0, len(todo), CHUNK):
                    inserted += int(
                        conn.execute(
                            text("""INSERT INTO kg.retraction_annotation(target, retraction_id, origin, recorded_reason,
                                        corrected_reason, evidence, note, annotator)
                                    SELECT :t, (x->>0)::bigint, x->>1, x->>2, x->>3, x->4, :note, :who
                                      FROM jsonb_array_elements(CAST(:rows AS jsonb)) x
                                    ON CONFLICT (target, retraction_id, corrected_reason) DO NOTHING"""),
                            {"t": target, "note": NOTE, "who": ANNOTATOR, "rows": json.dumps(todo[i : i + CHUNK])},
                        ).rowcount
                    )
            out[target] = {
                "scanned": len(rows),
                "reclassified_by_reva": len(todo),
                "inserted": inserted,
                "unverified": dict(unverified),
                "by_source": dict(sorted(by_source.items())),
            }
    return out
