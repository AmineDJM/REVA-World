"""DataQualityEvaluator V1: dimension-level rules against an explicit data contract.

Hard blockers follow architecture §20.2: unknown/changed units or support,
unexpected categories, incomplete expected keys, negative counts, missing
cell-type classification. Outliers and provisional flags are review signals
(warnings/info), never automatic deletions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from reva.autonomy.common import ArtifactIO, report
from reva.ports.autonomy import ArtifactRef, Finding, ValidationReport

EVALUATOR = "data-quality:dimension-rules@1"


def _f(code: str, severity: str, message: str) -> Finding:
    return Finding(code=code, severity=severity, message=message, evidence=())  # type: ignore[arg-type]


def contract_for(manifest: dict[str, Any], registry: Any = None) -> dict[str, Any]:
    """Data contract for a validated eurostat_jsonstat manifest.

    The *semantic* expectations (concept version, unit, aggregation, population
    universe, temporal support) and the subject provider come from the registry
    concept's semantic profile and the template's subject binding — never from the
    manifest's own ``semantics`` block, so a manifest that lies about its support
    or unit fails (review R4-01). Only the requested structure (subjects, years,
    pinned categories) is taken from the manifest.
    """
    from reva.autonomy.binding import SUBJECT, expected_semantics
    from reva.kernel.errors import ValidationProblem
    from reva.ontology.registry import load_registry

    rp = manifest["request_parameters"]
    reg = registry or load_registry()
    try:
        concept = reg.concept(str(rp["semantics"]["concept_id"]))
    except KeyError:
        raise ValidationProblem("manifest concept is not registered: no data contract can be derived",
                                concept=str(rp["semantics"]["concept_id"])[:100]) from None
    sem = expected_semantics(concept)
    res = rp["resolution_dimensions"]
    dims = {d: {"categories": list(rp["pinned_categories"][d]), "totals": list(rp["total_categories"].get(d, []))}
            for d in res}
    expected = len(rp["time"]) * len(rp["filters"][rp["subject"]["dimension"]])
    for d in res:
        expected *= len(dims[d]["categories"])
    return {
        "schema": "reva-data-contract/1",
        "concept_id": sem["concept_id"], "concept_version": sem["concept_version"],
        "unit_ref": sem["unit_ref"], "aggregation": sem["aggregation"],
        "population_universe": sem["population_universe"], "temporal_support": sem["temporal_support"],
        "subject_provider": SUBJECT["alias_provider"],
        "basis": "registry_semantic_profile",
        "subjects": list(rp["filters"][rp["subject"]["dimension"]]),
        "years": list(rp["time"]),
        "dimensions": dims,
        "expected_cells": expected,
        "non_negative": True,
    }


def evaluate_batch(batch: dict[str, Any], contract: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    obs: list[dict[str, Any]] = list(batch.get("observations") or [])
    f: list[Finding] = []
    detail: dict[str, Any] = {"cells": len(obs)}
    wrong_concept = [o for o in obs if (o["concept_id"], o["concept_version"])
                     != (contract["concept_id"], contract["concept_version"])]
    if wrong_concept:
        f.append(_f("concept_mismatch", "blocker", f"{len(wrong_concept)} cells map to another concept"))
    units = Counter(o["value"].get("unit_ref") for o in obs if o["value"].get("kind") == "decimal")
    if set(units) - {contract["unit_ref"]}:
        f.append(_f("unit_mismatch", "blocker", f"units {sorted(map(str, units))} != {contract['unit_ref']}"))
    mmdd = contract["temporal_support"].split(":", 1)[1] if ":" in contract["temporal_support"] else ""
    bad_support = 0
    bad_time = 0
    years: set[int] = set()
    for o in obs:
        s = o["support"]
        universe = s["population"]["universe"]
        if s["aggregation"] != contract["aggregation"] or universe != contract["population_universe"]:
            bad_support += 1
        t = s["temporal"]
        start = str(t.get("start") or "")
        if t.get("kind") != "instant" or start[5:] != mmdd:
            bad_time += 1
        if start[:4].isdigit():
            years.add(int(start[:4]))
    if bad_support:
        f.append(_f("support_mismatch", "blocker", f"{bad_support} cells with aggregation/universe off contract"))
    if bad_time:
        f.append(_f("temporal_support_mismatch", "blocker",
                    f"{bad_time} cells not on the {contract['temporal_support']} point support"))
    if years - set(contract["years"]):
        f.append(_f("unexpected_period", "blocker", f"periods {sorted(years - set(contract['years']))}"))
    subj = {(o["subject_provider"], o["subject_key"]) for o in obs}
    if any(p != contract["subject_provider"] or k not in contract["subjects"] for p, k in subj):
        f.append(_f("unexpected_subject", "blocker", f"subjects {sorted(subj)} outside contract"))
    per_dim: dict[str, Any] = {}
    for dim, spec in contract["dimensions"].items():
        seen = Counter(o["support"]["resolution"].get(dim) for o in obs)
        allowed = set(spec["categories"])
        unexpected = sorted(str(c) for c in seen if c not in allowed)
        absent = sorted(c for c in allowed if c not in seen)
        missing_by_cat: dict[str, int] = defaultdict(int)
        for o in obs:
            if o["value"].get("kind") == "missing":
                missing_by_cat[str(o["support"]["resolution"].get(dim))] += 1
        per_dim[dim] = {"categories_seen": len(seen), "unexpected": unexpected, "absent": absent,
                        "missing_values_by_category": dict(missing_by_cat), "totals": spec["totals"]}
        if unexpected:
            f.append(_f("unexpected_category", "blocker", f"{dim}: unexpected categories {unexpected[:10]}"))
        if absent:
            f.append(_f("missing_category", "blocker", f"{dim}: expected categories absent {absent[:10]}"))
        if missing_by_cat:
            f.append(_f("missing_values", "warning",
                        f"{dim}: {sum(missing_by_cat.values())} Missing cells in {len(missing_by_cat)} categories"))
    detail["dimensions"] = per_dim
    if len(obs) != int(contract["expected_cells"]):
        f.append(_f("incomplete", "blocker", f"{len(obs)} cells, contract expects {contract['expected_cells']}"))
    if contract.get("non_negative"):
        neg = sum(1 for o in obs if o["value"].get("kind") == "decimal" and Decimal(o["value"]["value"]) < 0)
        if neg:
            f.append(_f("negative_count", "blocker", f"{neg} negative counts"))
    no_type = sum(1 for o in obs if o.get("source_flags", {}).get("cell_type") not in ("leaf", "subtotal", "total"))
    if no_type:
        f.append(_f("cell_type_missing", "blocker", f"{no_type} cells lack leaf/subtotal/total classification"))
    cell_types = Counter(o.get("source_flags", {}).get("cell_type") for o in obs)
    status = Counter(o.get("source_flags", {}).get("status") or "" for o in obs)
    detail["cell_types"] = dict(cell_types)
    detail["status_flags"] = {k or "(none)": v for k, v in status.items()}
    flagged = {k: v for k, v in status.items() if k}
    if flagged:
        f.append(_f("status_flags_present", "info", f"status flags preserved: {dict(sorted(flagged.items()))}"))
    return f, detail


class DimensionRuleQualityEvaluator:
    def __init__(self, io_: ArtifactIO) -> None:
        self.io = io_

    def evaluate(self, observation_batch: ArtifactRef, data_contract: ArtifactRef) -> ValidationReport:
        batch = self.io.get_json(observation_batch)
        contract = self.io.get_json(data_contract)
        findings, detail = evaluate_batch(batch, contract)
        outcome = "fail" if any(x.severity == "blocker" for x in findings) else "pass"
        ev = self.io.put_json({"schema": "reva-quality-report/1", "evaluator": EVALUATOR,
                               "batch": observation_batch.sha256, "contract": data_contract.sha256,
                               "outcome": outcome, "detail": detail,
                               "findings": [{"code": x.code, "severity": x.severity, "message": x.message}
                                            for x in findings]}, "evaluation")
        return report(outcome, findings, ev, EVALUATOR, {"cells": detail["cells"]})
