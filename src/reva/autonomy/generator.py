"""ConnectorGenerator V1: pure declarative manifest generation for approved templates.

Deterministic: the same candidate metadata + mapping produce byte-identical
manifests. Only the approved ``eurostat_jsonstat`` template is supported; any
other template, an unapproved provider or a non-inspected candidate is refused.
No code is generated (``generation_mode: declarative_only``). Hosts come from the
pinned promotion policy's template entry, never from fetched metadata.
"""

from __future__ import annotations

from typing import Any

from reva.adapters.connectors.eurostat_template import (
    EUROSTAT_FLAGS,
    TEMPLATE_HOSTS,
    TEMPLATE_ID,
    template_digest,
    validate_manifest,
)
from reva.autonomy.common import ArtifactIO, stable_id
from reva.autonomy.mapping import STRUCTURAL_DIMS, TEMPLATE_FOR_PROVIDER
from reva.autonomy.policy import PinnedPolicy, promotion_policy
from reva.kernel.errors import PolicyDenied, ValidationProblem
from reva.ontology.registry import Registry, load_registry
from reva.ports.autonomy import ConnectorProposal, MappingProposal, SourceCandidate

EUROSTAT_TOTAL_CODES = ("TOTAL", "T")
EUROSTAT_SPECIAL_CODES = {"UNK": "unknown", "Y_OPEN": "open_ended_top", "NAP": "not_applicable"}
DEFAULT_CADENCE_SECONDS = 7 * 86400  # weekly metadata/update check


class DeclarativeConnectorGenerator:
    def __init__(self, io_: ArtifactIO, *, registry: Registry | None = None,
                 policy: PinnedPolicy | None = None) -> None:
        self.io = io_
        self.registry = registry or load_registry()
        self.policy = policy or promotion_policy()

    def generate(self, candidate: SourceCandidate, mapping: MappingProposal,
                 approved_template_ids: tuple[str, ...]) -> ConnectorProposal:
        template = TEMPLATE_FOR_PROVIDER.get(candidate.provider)
        policy_templates = self.policy.content.get("approved_templates") or {}
        if template is None:
            raise PolicyDenied(f"no approved template for provider {candidate.provider!r}")
        if template not in approved_template_ids or template not in policy_templates:
            raise PolicyDenied(f"template {template!r} is not approved; refusing to generate",
                               approved=list(approved_template_ids))
        if template != TEMPLATE_ID:
            raise PolicyDenied(f"template {template!r} has no declarative generator")
        if mapping.source_metadata.sha256 != candidate.metadata.sha256:
            raise ValidationProblem("mapping proposal does not belong to this candidate")
        meta = self.io.get_json(candidate.metadata)
        inspection = meta.get("inspection")
        if not inspection:
            raise ValidationProblem("candidate must be inspected before a manifest can be generated")
        mm = self.io.get_json(mapping.mapping_manifest)
        manifest = self.build_manifest(candidate, meta, inspection, mm, mapping,
                                       list(policy_templates[template].get("allowed_hosts") or []))
        validate_manifest(manifest)
        ref = self.io.put_json(manifest, "models")
        return ConnectorProposal(
            proposal_id=stable_id("connector-proposal", ref.sha256),
            candidate=candidate,
            template_id=template,
            template_digest=template_digest(),
            manifest=ref,
            mapping=mapping,
        )

    def build_manifest(self, candidate: SourceCandidate, meta: dict[str, Any], inspection: dict[str, Any],
                       mm: dict[str, Any], mapping: MappingProposal, policy_hosts: list[str]) -> dict[str, Any]:
        hosts = sorted(set(policy_hosts) & set(TEMPLATE_HOSTS))
        if not hosts:
            raise PolicyDenied("approved template has no allowed hosts in the pinned policy")
        concept_id, version = mapping.target_concept_version.split("@", 1)
        concept = self.registry.concept(concept_id)
        sp = concept.semantic_profile.model_dump(mode="json") if concept.semantic_profile else {}
        order: list[str] = list(inspection["dimension_order"])
        cats: dict[str, list[str]] = inspection["categories"]
        sizes = dict(zip(order, inspection["size"], strict=True))
        geo = list((meta.get("geography") or {}).get("eurostat_geo") or [])
        years = [int(y) for y in meta.get("years") or []]
        resolution = [d for d in order if d not in STRUCTURAL_DIMS]
        key_dims = ["geo", *sorted(resolution, key=lambda d: (sizes.get(d, 0), d)), "time"]
        units = cats.get("unit") or []
        exact = mapping.relation == "exact" and not mapping.review_required
        source = next(s for s in self.registry.sources if s["source_key"] == "eurostat.dissemination")
        name = f"eurostat.{candidate.dataset_id}.{'-'.join(g.lower() for g in geo)}"
        return {
            "schema_version": "1.0.0",
            "name": name,
            "version": "1.0.0",
            "template": TEMPLATE_ID,
            "source_key": "eurostat.dissemination",
            "allowed_hosts": hosts,
            "dataset": candidate.dataset_id,
            "request_parameters": {
                "filters": {"geo": geo},
                "time": years,
                "lang": "EN",
                "dimension_order": order,
                "pinned_categories": {d: list(cats[d]) for d in order if d != "time"},
                "status_vocabulary": sorted(EUROSTAT_FLAGS),
                "observed_status_labels": dict(inspection.get("status_labels") or {}),
                "semantics": {
                    "concept_id": concept_id,
                    "concept_version": version,
                    "unit_ref": sp.get("unit_ref") or "",
                    "unit_category": units[0] if len(units) == 1 else "",
                    "aggregation": sp.get("aggregation") or "",
                    "population_universe": sp.get("population_universe") or "",
                    "temporal_support": sp.get("temporal_support") or "",
                    "spatial_scheme": sp.get("spatial_support") or "eurostat_geo_code",
                },
                "subject": {"dimension": "geo", "alias_provider": "eurostat_geo"},
                "resolution_dimensions": resolution,
                "total_categories": {d: [c for c in cats[d] if c in EUROSTAT_TOTAL_CODES] for d in resolution},
                "special_categories": {d: {c: EUROSTAT_SPECIAL_CODES[c] for c in cats[d]
                                           if c in EUROSTAT_SPECIAL_CODES} for d in resolution},
                "logical_key_template": "eurostat:{dataset}:" + ":".join("{%s}" % d for d in key_dims),
                "licence": str((source.get("rights") or {}).get("licence", "")),
                "inspection_fingerprint": inspection.get("structure_fingerprint"),
                "dataset_label": str(inspection.get("label", ""))[:200],
            },
            "mapping_version": f"{mm['mapping_key']}@{mm['version']}",
            "rights_ref": "registry/sources/eurostat.yaml#rights",
            "lineage_family": str(source.get("lineage_family", "eurostat")),
            "max_response_bytes": min(5_000_000, max(1_000_000, 20 * int(inspection.get("bytes") or 0))),
            "max_pages": 1,
            "cadence": {"minimum_interval_seconds": DEFAULT_CADENCE_SECONDS, "release_aware": True},
            "eligibility_scope": [f"{concept_id}@{version}:{s}" for s in (mm.get("scope") or {}).get("subjects", [])]
            if exact else [],
            "generation_mode": "declarative_only",
        }
