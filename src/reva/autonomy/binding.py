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

R6 additions:

* the approved exact-mapping entry (dataset, concept, geography scope, verified
  rights) is checked here, so the gate and the promotion step share it (R6-02);
* the *provider structure* is pinned by the policy entry (``structure``): the
  dimension order, the provider unit code(s), fixed categories, a per-dimension
  category vocabulary and the total codes. A provider that switches demo_pjan to
  thousands (``THS``) or adds a ``citizen`` dimension is out of T1 scope (R6-04);
* every remaining manifest field is derived or pinned: licence and rights reference
  from the registered source/policy, special categories from a template constant,
  ``mapping_version`` from the mapping manifest, ``lang``, the status vocabulary,
  ``max_pages``; unknown ``request_parameters`` keys are refused (R6-12);
* identifiers are canonical: upper-case, sorted, unique geo codes and a name that
  joins them with ``.`` (a character no code can contain) (R6-11).
"""

from __future__ import annotations

import re
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
SEMANTIC_KEYS = frozenset({"concept_id", "concept_version", "unit_category", *SEMANTIC_FIELDS})
# Template constants (reviewed code): special category meanings and the fixed request language.
SPECIAL_CODES = {"UNK": "unknown", "Y_OPEN": "open_ended_top", "NAP": "not_applicable"}
TEMPLATE_LANG = "EN"
MAPPING_VERSION = "1.0.0"
REQUEST_PARAMETER_KEYS = frozenset(
    {
        "filters",
        "time",
        "lang",
        "dimension_order",
        "pinned_categories",
        "status_vocabulary",
        "semantics",
        "subject",
        "resolution_dimensions",
        "total_categories",
        "special_categories",
        "logical_key_template",
        "licence",
        "inspection_fingerprint",
    }
)
_GEO_RE = re.compile(r"[A-Z][A-Z0-9_]{1,11}")


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


def canonical_geo(geo: list[str]) -> list[str]:
    """Upper-case, sorted, unique geo codes (the only form a T1 manifest may carry)."""
    return sorted({str(g).upper() for g in geo})


def connector_name(dataset: str, geo: list[str]) -> str:
    """``eurostat.<dataset>.<geo>[.<geo>...]`` over the canonical geo list (R6-11).

    Codes are joined with ``.``, which neither a dataset code nor a geo code can contain, so
    ``["FR-DE"]`` and ``["FR", "DE"]`` (or ``["fr"]`` and ``["FR"]``) never share a name.
    """
    return f"{CONNECTOR_PREFIX}{dataset}." + ".".join(g.lower() for g in canonical_geo(geo))


def mapping_key(dataset: str, concept_id: str) -> str:
    """Key of the exact mapping a template connector for ``dataset`` -> ``concept_id`` records."""
    return f"eurostat.{dataset}.{concept_id.split('.')[-1]}"


def expected_special_categories(pinned: Mapping[str, list[str]], resolution: list[str]) -> dict[str, dict[str, str]]:
    return {d: {c: SPECIAL_CODES[c] for c in pinned.get(d) or [] if c in SPECIAL_CODES} for d in resolution}


def template_entry(policy: PinnedPolicy, template: str) -> Mapping[str, Any] | None:
    return (policy.content.get("approved_templates") or {}).get(template)


def approved_template_digest(policy: PinnedPolicy, template: str) -> str | None:
    """The template code digest the maintainers reviewed and pinned in the policy."""
    entry = template_entry(policy, template) or {}
    v = entry.get("template_sha256")
    return str(v) if v else None


def approved_entry(
    policy: PinnedPolicy, template: str, dataset: str, concept_id: str, version: str
) -> dict[str, Any] | None:
    for m in policy.content.get("approved_exact_mappings") or []:
        tgt = m.get("target") or {}
        if (
            m.get("template") == template
            and m.get("dataset") == dataset
            and tgt.get("concept_id") == concept_id
            and str(tgt.get("version")) == version
        ):
            return dict(m)
    return None


def entry_for(policy: PinnedPolicy, template: str, dataset: str) -> dict[str, Any] | None:
    """The approved exact-mapping entry for (template, dataset), whatever the target."""
    for m in policy.content.get("approved_exact_mappings") or []:
        if m.get("template") == template and m.get("dataset") == dataset:
            return dict(m)
    return None


def inspection_structure_problems(inspection: Mapping[str, Any] | None, entry: Mapping[str, Any]) -> list[str]:
    """Provider structure seen at inspection vs the structure pinned in the approved entry (R6-04)."""
    pins = entry.get("structure") or {}
    if not pins:
        return ["the approved entry pins no provider structure"]
    if not inspection:
        return ["candidate was not inspected"]
    cats = {str(k): [str(c) for c in v] for k, v in (inspection.get("categories") or {}).items()}
    return _structure_problems(list(inspection.get("dimension_order") or []), cats, pins)


def _structure_problems(order: list[str], cats: Mapping[str, list[str]], pins: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    want_order = list(pins.get("dimension_order") or [])
    if order != want_order:
        problems.append(f"dimension order {order} is not the pinned provider structure {want_order}")
    units = list(cats.get("unit") or [])
    allowed_units = list(pins.get("provider_unit_codes") or [])
    if len(units) != 1 or units[0] not in allowed_units:
        problems.append(f"provider unit codes {units} are not the pinned unit code(s) {allowed_units}")
    for dim, fixed in (pins.get("fixed_categories") or {}).items():
        if list(cats.get(dim) or []) != list(fixed):
            problems.append(f"dimension {dim}: categories {cats.get(dim)} are not the pinned {list(fixed)}")
    patterns = dict(pins.get("category_patterns") or {})
    for dim, pattern in patterns.items():
        codes = list(cats.get(dim) or [])
        rx = re.compile(str(pattern))
        bad = [c for c in codes if not rx.fullmatch(c)]
        if not codes or bad or len(set(codes)) != len(codes):
            problems.append(f"dimension {dim}: categories {bad[:10] or codes[:10]} outside the pinned vocabulary")
    resolution = [d for d in order if d not in STRUCTURAL_DIMS]
    if sorted(resolution) != sorted(patterns):
        problems.append(f"resolution dimensions {resolution} are not the pinned vocabularies {sorted(patterns)}")
    for dim, totals in (pins.get("total_codes") or {}).items():
        if not set(totals) <= set(cats.get(dim) or []):
            problems.append(f"dimension {dim}: pinned total codes {list(totals)} absent")
    return problems


def scope_problems(
    manifest: Mapping[str, Any], registry: Registry, policy: PinnedPolicy, entry: Mapping[str, Any] | None
) -> list[str]:
    """Approved entry, geography scope, rights and pinned provider structure (shared by gate + promotion)."""
    if entry is None:
        return ["no approved exact mapping for this template/dataset/concept"]
    problems: list[str] = []
    rp = manifest["request_parameters"]
    geo = list((rp.get("filters") or {}).get("geo") or [])
    if not geo or not set(geo) <= set((entry.get("scope") or {}).get("geo") or []):
        problems.append(f"geography {sorted(map(str, geo))} outside approved scope {entry.get('scope')}")
    tpl = template_entry(policy, str(manifest.get("template"))) or {}
    source = registered_source(registry, str(tpl.get("source_key") or ""))
    if entry.get("rights") != "verified" or not ((source or {}).get("rights") or {}).get("licence"):
        problems.append("rights are not verified (approved entry and registered source licence)")
    pins = entry.get("structure") or {}
    if not pins:
        problems.append("the approved entry pins no provider structure (R6-04): not auto-promotable")
        return problems
    pinned = {str(k): list(v) for k, v in (rp.get("pinned_categories") or {}).items()}
    cats = {**pinned, "unit": [str((rp.get("semantics") or {}).get("unit_category"))]}
    if pinned.get("unit") != cats["unit"]:
        problems.append(f"pinned unit categories {pinned.get('unit')} differ from semantics.unit_category")
    problems += _structure_problems(list(rp["dimension_order"]), cats, pins)
    if set(pinned) != set(rp["dimension_order"]) - {"time"}:
        problems.append("pinned_categories must pin every non-time dimension")
    exp_totals = {d: list(pins.get("total_codes", {}).get(d) or []) for d in rp["resolution_dimensions"]}
    if dict(rp.get("total_categories") or {}) != exp_totals:
        problems.append(f"total_categories {rp.get('total_categories')} are not the pinned total codes {exp_totals}")
    return problems


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


def binding_problems(
    manifest: Mapping[str, Any],
    registry: Registry,
    policy: PinnedPolicy,
    *,
    mapping_manifest: Mapping[str, Any] | None = None,
) -> list[str]:
    """Differences between a (schema-valid) manifest and what policy + registry + template code imply.

    ``mapping_manifest`` (the proposal's mapping document) is checked too when given: exact,
    no review, the derived key/version, this concept and the registry-derived subjects.
    """
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
            problems.append(
                f"source_key {manifest.get('source_key')!r} is not the template's registered source "
                f"{source['source_key']!r}"
            )
        if manifest.get("lineage_family") != source.get("lineage_family"):
            problems.append(
                f"lineage_family {manifest.get('lineage_family')!r} differs from the registered "
                f"{source.get('lineage_family')!r}"
            )
    extra = sorted(set(rp) - REQUEST_PARAMETER_KEYS)
    if extra:
        problems.append(f"request_parameters {extra[:10]} are not template fields (no free manifest fields)")
    if set(sem) != SEMANTIC_KEYS:
        problems.append(f"semantics keys {sorted(sem)} are not the template's {sorted(SEMANTIC_KEYS)}")
    if rp.get("lang", TEMPLATE_LANG) != TEMPLATE_LANG:
        problems.append(f"lang {rp.get('lang')!r} is not the template language {TEMPLATE_LANG!r}")
    if list(rp.get("status_vocabulary") or []) != sorted(et.EUROSTAT_FLAGS):
        problems.append("status_vocabulary is not the reviewed Eurostat flag vocabulary")
    if source is not None and rp.get("licence") != str((source.get("rights") or {}).get("licence", "")):
        problems.append("licence differs from the registered source licence")
    if manifest.get("rights_ref") != tpl.get("rights_ref"):
        problems.append(
            f"rights_ref {str(manifest.get('rights_ref'))[:120]!r} is not the policy's {tpl.get('rights_ref')!r}"
        )
    if int(manifest.get("max_pages") or 0) != 1:
        problems.append("max_pages must be 1 (one filtered cube per request)")
    if not isinstance(manifest.get("version"), str) or not et.SEMVER_RE.fullmatch(manifest["version"]):
        problems.append(f"version {str(manifest.get('version'))[:40]!r} is not a canonical MAJOR.MINOR.PATCH")
    exp_mapping = mapping_key(str(manifest.get("dataset")), concept_id)
    if manifest.get("mapping_version") != f"{exp_mapping}@{MAPPING_VERSION}":
        problems.append(
            f"mapping_version {str(manifest.get('mapping_version'))[:120]!r} is not the derived "
            f"{exp_mapping}@{MAPPING_VERSION}"
        )
    if mapping_manifest is not None:
        mm = mapping_manifest
        if (mm.get("mapping_key"), str(mm.get("version"))) != (exp_mapping, MAPPING_VERSION):
            problems.append("mapping manifest key/version differ from the derived mapping")
        if mm.get("relation") != "exact" or mm.get("review_required"):
            problems.append("mapping manifest is not an approved exact mapping")
        if (mm.get("target") or {}) != {"concept_id": concept_id, "version": version}:
            problems.append("mapping manifest target differs from the manifest concept")
    subj = rp.get("subject") or {}
    if dict(subj) != SUBJECT:
        problems.append(f"subject {subj} differs from the template subject binding {SUBJECT}")
    geo = list((rp.get("filters") or {}).get("geo") or [])
    if set(rp.get("filters") or {}) != {"geo"}:
        problems.append(f"filters {sorted(rp.get('filters') or {})} must filter exactly the subject dimension")
    if geo != canonical_geo(geo) or not all(isinstance(g, str) and _GEO_RE.fullmatch(g) for g in geo):
        problems.append(f"geo filter {geo[:10]} is not canonical (upper-case, sorted, unique codes)")
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
    if dict(rp.get("special_categories") or {}) != expected_special_categories(pinned, res):
        problems.append("special_categories differ from the template's special-code meanings")
    try:
        canonical = et.canonical_logical_key_template(manifest)
    except (KeyError, TypeError) as e:
        canonical = f"<underivable: {type(e).__name__}>"
    if rp.get("logical_key_template") != canonical:
        problems.append(
            f"logical_key_template {str(rp.get('logical_key_template'))[:120]!r} is not the "
            f"template-derived {canonical!r}"
        )
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
    if mapping_manifest is not None:
        subjects = sorted((mapping_manifest.get("scope") or {}).get("subjects") or [])
        derived = iso3_for_geo(registry, geo)
        if subjects != derived or not subjects:
            problems.append(f"mapping subjects {subjects} differ from the registry-derived {derived}")
    problems += scope_problems(
        manifest,
        registry,
        policy,
        approved_entry(policy, str(template), str(manifest.get("dataset")), concept_id, version),
    )
    return problems


def name_conflicts(conn: Connection, name: str, source_key: str, template: str) -> list[str]:
    """A connector name already registered under another source or template is never reused."""
    rows = (
        conn.execute(
            text(
                """SELECT cv.version, s.source_key, cv.declared_capabilities->>'template' AS template
           FROM evidence.connector_version cv JOIN evidence.source s ON s.id = cv.source_id
           WHERE cv.name = :n"""
            ),
            {"n": name},
        )
        .mappings()
        .all()
    )
    return [
        f"connector name {name!r} is registered as {r['version']} under source {r['source_key']!r} "
        f"template {r['template']!r}"
        for r in rows
        if r["source_key"] != source_key or r["template"] != template
    ]
