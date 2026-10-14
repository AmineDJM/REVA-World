"""KnowledgeGapDetector V1: requirement catalogue vs a coverage snapshot.

For each requirement (task, concept, geography, period, acceptable freshness,
weight) and each geography:

* ``unsupported`` — no connector version is *eligible* for the concept and
  subject (e.g. France 1 January age × sex population until the Eurostat
  connector is promoted);
* ``missing`` — eligible sources exist but no value for some years (absent, or
  stored as typed Missing, e.g. Algeria crude imports/exports 2020–2024 from the
  Comtrade preview);
* ``stale`` — the newest eligible value is older than the acceptable freshness.

Coverage is reported only relative to this finite catalogue with explicit
numerators/denominators (architecture §20.1).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from reva.autonomy.common import ArtifactIO, stable_id
from reva.cli.seed import entity_id
from reva.ontology.registry import Registry, load_registry
from reva.ports.autonomy import ArtifactRef, KnowledgeGap, ScopedContext

PRIORITY_METHOD = "requirement_weight_x_affected_period_fraction@1"

LATEST_SQL = """
WITH latest AS (
  SELECT o.*, row_number() OVER (PARTITION BY o.source_id, o.logical_key ORDER BY o.revision_no DESC) AS rn
  FROM evidence.observation o
  JOIN evidence.acquisition a ON a.id = o.acquisition_id
  WHERE o.recorded_at <= :cutoff AND a.origin = ANY(:origins)
)
SELECT c.concept_id, l.subject_id, ws.temporal_extent->>'start' AS start, l.value->>'kind' AS kind,
       l.value->>'reason' AS reason, l.recorded_at, s.source_key,
       evidence.is_eligible(l.connector_version_id, l.concept_version_id, l.subject_id, :cutoff) AS eligible
FROM latest l
JOIN ontology.concept_version c ON c.id = l.concept_version_id
JOIN world.support ws ON ws.id = l.support_id
JOIN evidence.source s ON s.id = l.source_id
WHERE l.rn = 1 AND c.concept_id = ANY(:concepts)
  AND NOT EXISTS (SELECT 1 FROM evidence.observation_retraction r WHERE r.observation_id = l.id
                  AND r.recorded_at <= :cutoff)
"""

ELIGIBILITY_SQL = """
SELECT DISTINCT ON (e.connector_version_id, e.concept_version_id)
       c.concept_id, e.decision, e.applicability
FROM evidence.eligibility e JOIN ontology.concept_version c ON c.id = e.concept_version_id
WHERE e.recorded_at <= :cutoff
ORDER BY e.connector_version_id, e.concept_version_id, e.recorded_at DESC, e.id DESC
"""


def build_coverage_snapshot(conn: Connection, cutoff: datetime, origins: Sequence[str],
                            registry: Registry | None = None) -> dict[str, Any]:
    reg = registry or load_registry()
    reqs = reg.requirements["requirements"]
    concepts = sorted({r["concept"] for r in reqs})
    geos = sorted({g for r in reqs for g in r["geography"]})
    by_entity = {str(entity_id(g)): g for g in geos}
    eligible: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(text(ELIGIBILITY_SQL), {"cutoff": cutoff}).mappings():
        if row["decision"] != "eligible":
            continue
        subjects = (row["applicability"] or {}).get("subject_ids")
        targets = geos if subjects is None else [by_entity[s] for s in subjects if s in by_entity]
        eligible[row["concept_id"]].update(targets)
    cells: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in conn.execute(text(LATEST_SQL), {"cutoff": cutoff, "origins": list(origins),
                                               "concepts": concepts}).mappings():
        geo = by_entity.get(str(row["subject_id"]))
        if geo is None or not row["eligible"] or not (row["start"] or "")[:4].isdigit():
            continue
        key = (row["concept_id"], geo, int(row["start"][:4]))
        cell = cells.setdefault(key, {"concept_id": key[0], "geo": geo, "year": key[2], "values": 0,
                                      "missing": 0, "missing_reasons": set(), "sources": set(),
                                      "latest_recorded_at": None})
        cell["sources"].add(row["source_key"])
        if row["kind"] == "missing":
            cell["missing"] += 1
            cell["missing_reasons"].add(row["reason"] or "unknown")
        else:
            cell["values"] += 1
            ts = row["recorded_at"]
            if cell["latest_recorded_at"] is None or ts > cell["latest_recorded_at"]:
                cell["latest_recorded_at"] = ts
    out_cells = []
    for c in sorted(cells.values(), key=lambda c: (c["concept_id"], c["geo"], c["year"])):
        out_cells.append({**c, "status": "value" if c["values"] else "missing",
                          "missing_reasons": sorted(c["missing_reasons"]), "sources": sorted(c["sources"]),
                          "latest_recorded_at": c["latest_recorded_at"].isoformat() if c["latest_recorded_at"]
                          else None})
    return {"schema": "reva-coverage-snapshot/1", "as_of": cutoff.isoformat(), "origins": list(origins),
            "catalogue_version": reg.requirements.get("catalogue_version"),
            "eligible": {k: sorted(v) for k, v in sorted(eligible.items())}, "cells": out_cells}


def _runs(years: Iterable[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for y in sorted(years):
        if out and out[-1][-1] == y - 1:
            out[-1].append(y)
        else:
            out.append([y])
    return out


def classify(requirements: dict[str, Any], snapshot: dict[str, Any], cutoff: datetime
             ) -> list[dict[str, Any]]:
    """Per (requirement, geography, year) status: covered / unsupported / missing / stale."""
    cells = {(c["concept_id"], c["geo"], int(c["year"])): c for c in snapshot["cells"]}
    eligible = {k: set(v) for k, v in snapshot["eligible"].items()}
    out = []
    for r in requirements["requirements"]:
        y0, y1 = int(r["period"][0]), int(r["period"][1])
        for g in r["geography"]:
            for y in range(y0, y1 + 1):
                cell = cells.get((r["concept"], g, y))
                if g not in eligible.get(r["concept"], set()):
                    status, detail = "unsupported", "no eligible connector version for concept and subject"
                elif cell is None or cell["status"] != "value":
                    status = "missing"
                    detail = ("source returned no value (typed Missing: "
                              + ",".join(cell["missing_reasons"]) + ")") if cell else "no observation"
                else:
                    age_days = (cutoff - datetime.fromisoformat(cell["latest_recorded_at"])).days
                    if age_days > int(r["acceptable_freshness_days"]):
                        status, detail = "stale", f"newest eligible value is {age_days} days old"
                    else:
                        status, detail = "covered", ""
                out.append({"requirement_id": r["id"], "task": r["task"], "concept": r["concept"], "geo": g,
                            "year": y, "status": status, "detail": detail, "weight": r["weight"]})
    return out


def coverage_report(requirements: dict[str, Any], snapshot: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
    rows = classify(requirements, snapshot, cutoff)
    per: dict[str, dict[str, Any]] = {}
    for row in rows:
        p = per.setdefault(row["requirement_id"], {"concept": row["concept"], "weight": row["weight"],
                                                    "denominator": 0, "available": 0, "semantically_supported": 0,
                                                    "fresh": 0})
        p["denominator"] += 1
        p["semantically_supported"] += row["status"] != "unsupported"
        p["available"] += row["status"] in ("covered", "stale")
        p["fresh"] += row["status"] == "covered"
    tot_w = sum(p["weight"] * p["denominator"] for p in per.values()) or 1
    return {
        "statement": "coverage of this finite requirement catalogue under its stated thresholds only",
        "catalogue_version": requirements.get("catalogue_version"),
        "per_requirement": per,
        "unweighted": {k: sum(p[k] for p in per.values()) for k in
                       ("denominator", "available", "semantically_supported", "fresh")},
        "weighted_availability": sum(p["weight"] * p["available"] for p in per.values()) / tot_w,
        "weighted_freshness": sum(p["weight"] * p["fresh"] for p in per.values()) / tot_w,
    }


class CatalogueGapDetector:
    def __init__(self, io_: ArtifactIO, registry: Registry | None = None) -> None:
        self.io = io_
        self.registry = registry or load_registry()

    def detect(self, requirements: ArtifactRef, coverage_snapshot: ArtifactRef,
               context: ScopedContext) -> list[KnowledgeGap]:
        reqs = self.io.get_json(requirements)
        snap = self.io.get_json(coverage_snapshot)
        rows = classify(reqs, snap, context.knowledge_cutoff)
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["status"] != "covered":
                grouped[(row["requirement_id"], row["geo"], row["status"])].append(row)
        by_id = {r["id"]: r for r in reqs["requirements"]}
        gaps = []
        for (rid, geo, status), items in sorted(grouped.items()):
            r = by_id[rid]
            concept = self.registry.concept(r["concept"])
            years = sorted(i["year"] for i in items)
            span = int(r["period"][1]) - int(r["period"][0]) + 1
            details = sorted({i["detail"] for i in items})
            gaps.append(KnowledgeGap(
                gap_id=stable_id("gap", str(reqs.get("catalogue_version")), rid, geo, status,
                                 ",".join(map(str, years)), coverage_snapshot.sha256),
                requirement_id=rid,
                concept_version=f"{concept.concept_id}@{concept.version}",
                geography=(geo,),
                temporal_support={"years": years, "runs": _runs(years), "period": list(r["period"]),
                                  "task": r["task"]},
                reason=f"{status}: {'; '.join(details)}",
                priority=float(r["weight"]) * len(years) / span,
                priority_method=PRIORITY_METHOD,
                context=context,
            ))
        return gaps
