"""ConnectorGenerator V1: pure declarative manifest generation for approved templates.

Deterministic: the same candidate metadata + mapping produce byte-identical
manifests. Only the approved ``eurostat_jsonstat`` template is supported; any
other template, an unapproved provider or a non-inspected candidate is refused.
No code is generated (``generation_mode: declarative_only``). Hosts come from the
pinned promotion policy's template entry, never from fetched metadata.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from reva.adapters.connectors.eurostat_template import (
    EUROSTAT_FLAGS,
    TEMPLATE_HOSTS,
    TEMPLATE_ID,
    canonical_logical_key_template,
    template_digest,
    validate_manifest,
)
from reva.autonomy.binding import connector_name, expected_semantics, registered_source, template_entry
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
                 policy: PinnedPolicy | None = None,
                 version_resolver: Callable[[dict[str, Any]], str] | None = None) -> None:
        """``version_resolver(manifest) -> version``: content-derived connector version
        (``jobs.resolve_connector_version``): identical content keeps its registered
        version, changed content gets a new semver patch (review R4-03)."""
        self.io = io_
        self.registry = registry or load_registry()
        self.policy = policy or promotion_policy()
        self.version_resolver = version_resolver

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
        if self.version_resolver is not None:
            manifest["version"] = str(self.version_resolver(manifest))
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
        sem = expected_semantics(concept)
        order: list[str] = list(inspection["dimension_order"])
        cats: dict[str, list[str]] = inspection["categories"]
        if len(order) != len(inspection["size"]):
            raise ValidationProblem("inspection dimension order and size disagree")
        geo = list((meta.get("geography") or {}).get("eurostat_geo") or [])
        years = [int(y) for y in meta.get("years") or []]
        resolution = [d for d in order if d not in STRUCTURAL_DIMS]
        units = cats.get("unit") or []
        exact = mapping.relation == "exact" and not mapping.review_required
        # Source and lineage family come from the approved template's registered source.
        source_key = str((template_entry(self.policy, TEMPLATE_ID) or {}).get("source_key") or "")
        source = registered_source(self.registry, source_key)
        if source is None:
            raise PolicyDenied("approved template names no registered source in the pinned policy")
        manifest = self._manifest(candidate, inspection, mm, hosts, concept_id, version, sem, order, cats, geo,
                                  years, resolution, units, exact, source)
        manifest["request_parameters"]["logical_key_template"] = canonical_logical_key_template(manifest)
        return manifest

    @staticmethod
    def _manifest(candidate: SourceCandidate, inspection: dict[str, Any], mm: dict[str, Any], hosts: list[str],
                  concept_id: str, version: str, sem: dict[str, str], order: list[str],
                  cats: dict[str, list[str]], geo: list[str], years: list[int], resolution: list[str],
                  units: list[str], exact: bool, source: dict[str, Any]) -> dict[str, Any]:
        name = connector_name(candidate.dataset_id, geo)
        return {
            "schema_version": "1.0.0",
            "name": name,
            "version": "1.0.0",
            "template": TEMPLATE_ID,
            "source_key": source["source_key"],
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
                "semantics": {**sem, "concept_id": concept_id, "concept_version": version,
                              "unit_category": units[0] if len(units) == 1 else ""},
                "subject": {"dimension": "geo", "alias_provider": "eurostat_geo"},
                "resolution_dimensions": resolution,
                "total_categories": {d: [c for c in cats[d] if c in EUROSTAT_TOTAL_CODES] for d in resolution},
                "special_categories": {d: {c: EUROSTAT_SPECIAL_CODES[c] for c in cats[d]
                                           if c in EUROSTAT_SPECIAL_CODES} for d in resolution},
                "logical_key_template": "",  # derived by the reviewed template code (build_manifest)
                "licence": str((source.get("rights") or {}).get("licence", "")),
                "inspection_fingerprint": inspection.get("structure_fingerprint"),
                "dataset_label": str(inspection.get("label", ""))[:200],
            },
            "mapping_version": f"{mm['mapping_key']}@{mm['version']}",
            "rights_ref": "registry/sources/eurostat.yaml#rights",
            "lineage_family": str(source.get("lineage_family") or ""),
            "max_response_bytes": min(5_000_000, max(1_000_000, 20 * int(inspection.get("bytes") or 0))),
            "max_pages": 1,
            "cadence": {"minimum_interval_seconds": DEFAULT_CADENCE_SECONDS, "release_aware": True},
            "eligibility_scope": [f"{concept_id}@{version}:{s}" for s in (mm.get("scope") or {}).get("subjects", [])]
            if exact else [],
            "generation_mode": "declarative_only",
        }
