"""OntologyMapper V1: exact registry/policy match + candidate proposals for anything else.

* ``exact`` is granted only when the *pinned promotion policy* lists an approved
  exact mapping for (template, dataset, target concept/version) whose scope covers
  the candidate geography, and the inspected structure is consistent with it.
  Catalogue text never grants semantics.
* Every other registered concept of the same unit/scale is compared by semantic
  profile (temporal support, population universe, aggregation). Different
  temporal support or universe without an approved transform is ``incompatible``.
  E.g. Eurostat demo_pjan (1 January, usually resident) vs WDI SP.POP.TOTL
  (midyear, de facto) — the tempting false equivalence is rejected explicitly.
* Anything else becomes a reviewable candidate (``review_required=True``).
"""

from __future__ import annotations

from typing import Any

from reva.autonomy.binding import MAPPING_VERSION, inspection_structure_problems, mapping_key
from reva.autonomy.common import ArtifactIO, stable_id
from reva.autonomy.policy import PinnedPolicy, promotion_policy
from reva.ontology.registry import Registry, load_registry
from reva.ports.autonomy import MappingProposal, ScopedContext, SourceCandidate

PROFILE_FIELDS = ("temporal_support", "population_universe", "aggregation", "unit_ref", "scale_type")
STRUCTURAL_DIMS = {"freq", "unit", "geo", "time"}
TEMPLATE_FOR_PROVIDER = {"eurostat": "eurostat_jsonstat"}


def _profile(concept: Any) -> dict[str, Any]:
    sp = concept.semantic_profile.model_dump(mode="json") if concept.semantic_profile else {}
    return {k: sp.get(k) for k in PROFILE_FIELDS}


def compare_profiles(source: dict[str, Any], target: dict[str, Any]) -> tuple[str, list[str]]:
    """Relation of a source with profile ``source`` to a concept with profile ``target``."""
    diffs = [k for k in PROFILE_FIELDS if source.get(k) != target.get(k)]
    if not diffs:
        return "exact", []
    reasons = [f"{k}: {source.get(k)!r} vs {target.get(k)!r}" for k in diffs]
    if {"unit_ref", "scale_type"} & set(diffs):
        return "incompatible", reasons
    if {"temporal_support", "population_universe"} & set(diffs):
        return "incompatible", reasons  # no approved transform between these supports/universes
    return "overlap", reasons


class RegistryOntologyMapper:
    def __init__(self, io_: ArtifactIO, *, registry: Registry | None = None,
                 policy: PinnedPolicy | None = None) -> None:
        self.io = io_
        self.registry = registry or load_registry()
        self.policy = policy or promotion_policy()

    # ------------------------------------------------------------------ helpers
    def _meta(self, candidate: SourceCandidate) -> dict[str, Any]:
        return dict(self.io.get_json(candidate.metadata))

    def _gap_concept(self, meta: dict[str, Any]) -> Any:
        req = next((r for r in self.registry.requirements["requirements"] if r["id"] == meta.get("requirement_id")),
                   None)
        if req is None:
            raise KeyError(f"requirement {meta.get('requirement_id')} not in catalogue")
        return self.registry.concept(req["concept"])

    def _approved_exact(self, candidate: SourceCandidate, concept: Any, geo: list[str]) -> dict[str, Any] | None:
        template = TEMPLATE_FOR_PROVIDER.get(candidate.provider)
        for m in self.policy.content.get("approved_exact_mappings") or []:
            tgt = m.get("target") or {}
            if (m.get("template") == template and m.get("dataset") == candidate.dataset_id
                    and tgt.get("concept_id") == concept.concept_id and str(tgt.get("version")) == concept.version
                    and geo and set(geo) <= set((m.get("scope") or {}).get("geo") or [])):
                return dict(m)
        return None

    @staticmethod
    def _structure_findings(inspection: dict[str, Any] | None) -> list[str]:
        if not inspection:
            return ["candidate was not inspected: no structural evidence"]
        problems = []
        cats = inspection.get("categories") or {}
        if str(inspection.get("jsonstat_version")) != "2.0":
            problems.append("not JSON-stat 2.0")
        if len(cats.get("unit", [])) != 1:
            problems.append(f"expected exactly one unit category, got {cats.get('unit')}")
        if cats.get("freq") != ["A"]:
            problems.append(f"expected annual frequency, got {cats.get('freq')}")
        if not {"geo", "time"} <= set(inspection.get("dimension_order") or []):
            problems.append("geo/time dimensions missing")
        if int(inspection.get("value_count") or 0) == 0:
            problems.append("inspection returned no values")
        return problems

    def _proposal(self, candidate: SourceCandidate, mapping_key: str, concept: Any, relation: str,
                  review: bool, body: dict[str, Any]) -> MappingProposal:
        manifest = {
            "schema": "reva-mapping-proposal/1",
            "mapping_key": mapping_key,
            "version": MAPPING_VERSION,
            "source_ref": {"provider": candidate.provider, "dataset": candidate.dataset_id,
                           "template": TEMPLATE_FOR_PROVIDER.get(candidate.provider),
                           "candidate_metadata_sha256": candidate.metadata.sha256},
            "target": {"concept_id": concept.concept_id, "version": concept.version},
            "relation": relation,
            "review_required": review,
            "policy_version": self.policy.version,
            "policy_sha256": self.policy.sha256,
            **body,
        }
        ref = self.io.put_json(manifest, "models")
        return MappingProposal(
            proposal_id=stable_id("mapping", candidate.metadata.sha256, concept.concept_id, concept.version),
            source_metadata=candidate.metadata,
            target_concept_version=f"{concept.concept_id}@{concept.version}",
            relation=relation,  # type: ignore[arg-type]
            mapping_manifest=ref,
            review_required=review,
        )

    # ------------------------------------------------------------------ protocol
    def propose_mapping(self, candidate: SourceCandidate, context: ScopedContext) -> MappingProposal:
        """Mapping of the candidate to the gap's own concept (the one that may be promoted)."""
        return self.assess(candidate, context)[0]

    def assess(self, candidate: SourceCandidate, context: ScopedContext) -> list[MappingProposal]:
        """[primary mapping to the gap concept, then every comparison with related concepts]."""
        meta = self._meta(candidate)
        concept = self._gap_concept(meta)
        geo = list((meta.get("geography") or {}).get("eurostat_geo") or [])
        iso3 = list((meta.get("geography") or {}).get("iso3") or [])
        inspection = meta.get("inspection")
        problems = self._structure_findings(inspection)
        approved = self._approved_exact(candidate, concept, geo)
        if approved:  # (R6-04) provider structure must be the one the maintainers pinned
            problems += inspection_structure_problems(inspection, approved)
        resolution = [d for d in (inspection or {}).get("dimension_order", []) if d not in STRUCTURAL_DIMS]
        out: list[MappingProposal] = []
        if approved and not problems and candidate.rights_status == "verified" \
                and approved.get("rights") == "verified":
            source_profile = _profile(concept)
            out.append(self._proposal(
                candidate, mapping_key(candidate.dataset_id, concept.concept_id),
                concept, "exact", False,
                {"basis": "approved_exact_mapping", "approved_entry": approved,
                 "scope": {"geo": geo, "subjects": iso3}, "resolution_dimensions": resolution,
                 "source_semantic_profile": source_profile, "structure_problems": []}))
        else:
            reasons = list(problems)
            if not approved:
                reasons.append("no approved exact mapping in the pinned policy for this dataset/concept/scope")
            if candidate.rights_status != "verified":
                reasons.append(f"rights status {candidate.rights_status}")
            source_profile = {}
            out.append(self._proposal(
                candidate, f"{candidate.provider}.{candidate.dataset_id}.{concept.concept_id.split('.')[-1]}",
                concept, "overlap", True,
                {"basis": "candidate_only", "reasons": reasons, "scope": {"geo": geo, "subjects": iso3},
                 "resolution_dimensions": resolution, "structure_problems": problems,
                 "note": "new semantics remain a reviewable proposal (T2)"}))
        if not source_profile:
            return out
        # Compare with every other registered concept of the same unit and scale.
        for other in self.registry.concepts:
            if other.concept_id == concept.concept_id or other.semantic_profile is None:
                continue
            op = _profile(other)
            if (op.get("unit_ref"), op.get("scale_type")) != (source_profile.get("unit_ref"),
                                                               source_profile.get("scale_type")):
                continue
            relation, reasons = compare_profiles(source_profile, op)
            registry_mapping = next(
                (m for m in self.registry.mappings
                 if m["source_ref"].get("dataset") == candidate.dataset_id
                 and m["target"]["concept_id"] == other.concept_id), None)
            if registry_mapping and registry_mapping["mapping_kind"] == "incompatible":
                relation = "incompatible"
            mk = (registry_mapping or {}).get("mapping_key") or \
                f"{candidate.provider}.{candidate.dataset_id}.to_{other.concept_id.split('.')[-1]}"
            out.append(self._proposal(
                candidate, mk, other, relation, relation != "incompatible",
                {"basis": "semantic_profile_comparison", "profile_differences": reasons,
                 "source_semantic_profile": source_profile, "target_semantic_profile": op,
                 "registry_mapping": registry_mapping, "eligibility": "none",
                 "decision": "rejected_false_equivalence" if relation == "incompatible" else "candidate",
                 "scope": {"geo": geo, "subjects": iso3}}))
        return out
