"""T1 semantic binding: what an auto-promotable manifest MUST be, derived only from
the pinned policy, the registry and reviewed template code — never from the manifest.

A declarative connector manifest is a proposer artefact. Every field that carries
meaning (concept semantics, source, lineage family, name, subject, logical-key
layout, resolution dimensions, totals) is recomputed here from:

* the pinned promotion policy (``approved_templates`` / ``approved_exact_mappings``),
* the registry concept's ``semantic_profile`` and the registered source,
* reviewed template code (``canonical_logical_key_template``),

and compared with the manifest. The gate (``gate.py``), the validator's data
contract (``validator.py``) and the promotion step (``jobs.promote_connector``)
all use this module, so the validator and sandbox can no longer simply agree with
whatever the manifest claims (review R4-01).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, text

from reva.adapters.connectors import eurostat_template as et
from reva.autonomy.policy import PinnedPolicy
from reva.ontology.registry import Registry

STRUCTURAL_DIMS = frozenset({"freq", "unit", "geo", "time"})
TOTAL_CODES = ("TOTAL", "T")
SUBJECT = {"dimension": "geo", "alias_provider": "eurostat_geo"}
MIN_CADENCE_SECONDS = 86_400
MAX_RESPONSE_BYTES = 5_000_000
CONNECTOR_PREFIX = "eurostat."  # namespace of template connectors; never a built-in name
SEMANTIC_FIELDS = ("unit_ref", "aggregation", "population_universe", "temporal_support", "spatial_scheme")


def expected_semantics(concept: Any) -> dict[str, str]:
    """Manifest ``semantics`` fields implied by a registry concept's semantic profile."""
    sp = concept.semantic_profile.model_dump(mode="json") if concept.semantic_profile else {}
    return {
        "concept_id": concept.concept_id,
        "concept_version": concept.version,
        "unit_ref": str(sp.get("unit_ref") or ""),
        "aggregation": str(sp.get("aggregation") or ""),
        "population_universe": str(sp.get("population_universe") or ""),
        "temporal_support": str(sp.get("temporal_support") or ""),
        "spatial_scheme": str(sp.get("spatial_support") or "eurostat_geo_code"),
    }


def connector_name(dataset: str, geo: list[str]) -> str:
    return f"{CONNECTOR_PREFIX}{dataset}.{'-'.join(g.lower() for g in geo)}"


def template_entry(policy: PinnedPolicy, template: str) -> Mapping[str, Any] | None:
    return (policy.content.get("approved_templates") or {}).get(template)


def approved_template_digest(policy: PinnedPolicy, template: str) -> str | None:
    """The template code digest the maintainers reviewed and pinned in the policy."""
    entry = template_entry(policy, template) or {}
    v = entry.get("template_sha256")
    return str(v) if v else None


def approved_entry(policy: PinnedPolicy, template: str, dataset: str, concept_id: str,
                   version: str) -> dict[str, Any] | None:
    for m in policy.content.get("approved_exact_mappings") or []:
        tgt = m.get("target") or {}
        if (m.get("template") == template and m.get("dataset") == dataset
                and tgt.get("concept_id") == concept_id and str(tgt.get("version")) == version):
            return dict(m)
    return None


def registered_source(registry: Registry, source_key: str) -> dict[str, Any] | None:
    return next((dict(s) for s in registry.sources if s.get("source_key") == source_key), None)


def iso3_for_geo(registry: Registry, geo: list[str]) -> list[str]:
    out = []
    for e in registry.entities:
        if any(a.get("provider") == "eurostat_geo" and str(a.get("key")) in geo for a in e.get("aliases", [])):
            out.append(str(e["key"]))
    return sorted(out)


def builtin_connector_names() -> set[str]:
    from reva.adapters.connectors.registry import builtin_connectors

    return set(builtin_connectors(None))


def binding_problems(manifest: Mapping[str, Any], registry: Registry, policy: PinnedPolicy) -> list[str]:
    """Differences between a (schema-valid) manifest and what policy + registry + template code imply."""
    problems: list[str] = []
    template = manifest.get("template")
    tpl = template_entry(policy, str(template))
    if template != et.TEMPLATE_ID or tpl is None:
        return [f"template {template!r} is not an approved declarative template"]
    rp = manifest["request_parameters"]
    sem = rp["semantics"]
    concept_id, version = str(sem.get("concept_id")), str(sem.get("concept_version"))
    try:
        concept = registry.concept(concept_id)
    except KeyError:
        return [f"concept {concept_id} is not registered"]
    if concept.version != version:
        problems.append(f"concept version {version} is not the registered {concept.version}")
    exp = expected_semantics(concept)
    for k in SEMANTIC_FIELDS:
        if sem.get(k) != exp[k]:
            problems.append(f"semantics.{k} {sem.get(k)!r} differs from registry profile {exp[k]!r}")
    # source / lineage family come from the approved template's registered source
    src_key = tpl.get("source_key")
    source = registered_source(registry, str(src_key)) if src_key else None
    if source is None:
        problems.append("approved template has no registered source in the pinned policy")
    else:
        if manifest.get("source_key") != source["source_key"]:
            problems.append(f"source_key {manifest.get('source_key')!r} is not the template's registered source "
                            f"{source['source_key']!r}")
        if manifest.get("lineage_family") != source.get("lineage_family"):
            problems.append(f"lineage_family {manifest.get('lineage_family')!r} differs from the registered "
                            f"{source.get('lineage_family')!r}")
    subj = rp.get("subject") or {}
    if dict(subj) != SUBJECT:
        problems.append(f"subject {subj} differs from the template subject binding {SUBJECT}")
    geo = list((rp.get("filters") or {}).get("geo") or [])
    expected_name = connector_name(str(manifest.get("dataset")), geo)
    if manifest.get("name") != expected_name:
        problems.append(f"connector name {manifest.get('name')!r} is not the derived name {expected_name!r}")
    if str(manifest.get("name")) in builtin_connector_names():
        problems.append(f"connector name {manifest.get('name')!r} collides with a built-in connector")
    order = list(rp["dimension_order"])
    res = [d for d in order if d not in STRUCTURAL_DIMS]
    if list(rp["resolution_dimensions"]) != res:
        problems.append(f"resolution_dimensions {rp['resolution_dimensions']} != structural derivation {res}")
    pinned = rp.get("pinned_categories") or {}
    exp_totals = {d: [c for c in pinned.get(d, []) if c in TOTAL_CODES] for d in res}
    if dict(rp.get("total_categories") or {}) != exp_totals:
        problems.append("total_categories differ from the template's total-code derivation")
    try:
        canonical = et.canonical_logical_key_template(manifest)
    except (KeyError, TypeError) as e:
        canonical = f"<underivable: {type(e).__name__}>"
    if rp.get("logical_key_template") != canonical:
        problems.append(f"logical_key_template {str(rp.get('logical_key_template'))[:120]!r} is not the "
                        f"template-derived {canonical!r}")
    if set(pinned.get("geo") or []) != set(geo):
        problems.append("pinned geo categories differ from the geo filter")
    cadence = int((manifest.get("cadence") or {}).get("minimum_interval_seconds") or 0)
    if cadence < MIN_CADENCE_SECONDS:
        problems.append(f"cadence {cadence}s is below the template minimum {MIN_CADENCE_SECONDS}s")
    if int(manifest.get("max_response_bytes") or 0) > MAX_RESPONSE_BYTES:
        problems.append("max_response_bytes exceeds the template bound")
    concept_key = f"{concept_id}@{version}"
    exp_scope = [f"{concept_key}:{s}" for s in iso3_for_geo(registry, geo)]
    if sorted(manifest.get("eligibility_scope") or []) != exp_scope:
        problems.append(f"eligibility_scope {manifest.get('eligibility_scope')} != registry-derived {exp_scope}")
    return problems


def name_conflicts(conn: Connection, name: str, source_key: str, template: str) -> list[str]:
    """A connector name already registered under another source or template is never reused."""
    rows = conn.execute(text(
        """SELECT cv.version, s.source_key, cv.declared_capabilities->>'template' AS template
           FROM evidence.connector_version cv JOIN evidence.source s ON s.id = cv.source_id
           WHERE cv.name = :n"""), {"n": name}).mappings().all()
    return [f"connector name {name!r} is registered as {r['version']} under source {r['source_key']!r} "
            f"template {r['template']!r}" for r in rows
            if r["source_key"] != source_key or r["template"] != template]
