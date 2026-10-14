"""Published JSON contracts are valid, mutually consistent and match what the code produces."""

from __future__ import annotations

import asyncio
import json
import typing
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from reva.adapters.connectors.registry import builtin_connectors
from reva.kernel.uncertainty import UncertaintyKind, UncertaintyStatus
from reva.kernel.values import MissingReason
from reva.ontology.registry import load_registry
from reva.ports.connectors import ConnectorSpec

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
OWN_SCHEMAS = ("scenario.schema.json", "observation.schema.json", "connector.schema.json")


def load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text())


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load(name), format_checker=FormatChecker())


def errors(name: str, instance: Any) -> list[str]:
    return [f"/{'/'.join(map(str, e.path))}: {e.message}" for e in validator(name).iter_errors(instance)]


# =========================================================================== schema validity
@pytest.mark.parametrize("path", sorted(CONTRACTS.glob("*.schema.json")), ids=lambda p: p.name)
def test_every_schema_is_valid_for_its_declared_draft(path: Path) -> None:
    schema = json.loads(path.read_text())
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)


@pytest.mark.parametrize("name", OWN_SCHEMAS)
def test_reva_schemas_are_draft_2020_12_and_closed(name: str) -> None:
    schema = load(name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)
    assert schema.get("additionalProperties") is False  # unknown fields are rejected, not ignored
    assert set(schema["required"]) <= set(schema["properties"])


# =========================================================================== scenario
def test_scenario_example_validates() -> None:
    example = load("scenario.example.json")
    assert errors("scenario.schema.json", example) == []


def test_scenario_example_round_trips_through_spec_validator() -> None:
    from reva.kernel.errors import ValidationProblem
    from reva.simulation.spec import validate_scenario_spec

    example = load("scenario.example.json")
    spec = validate_scenario_spec(example, allow_fixture_baseline=True)
    assert spec.as_json() == example or errors("scenario.schema.json", spec.as_json()) == []
    # The illustrative fixture baseline is never accepted as a real baseline.
    with pytest.raises(ValidationProblem):
        validate_scenario_spec(example)
    # Canonical bytes are deterministic and the digest is stable.
    assert spec.canonical_bytes() == validate_scenario_spec(example, allow_fixture_baseline=True).canonical_bytes()
    assert json.loads(spec.canonical_bytes()) == spec.as_json()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(unexpected="x"),
        lambda d: d.pop("baseline_release_id"),
        lambda d: d.update(countries=["FRA", "GBR"]),
        lambda d: d["intervention"].update(import_multiplier=-1),
        lambda d: d.update(missing_input_policy="zero_fill"),
    ],
)
def test_scenario_schema_rejects_invalid_variants(mutate: Any) -> None:
    example = load("scenario.example.json")
    mutate(example)
    assert errors("scenario.schema.json", example) != []


# =========================================================================== observation
def test_kernel_missing_reasons_match_observation_contract() -> None:
    schema = load("observation.schema.json")
    missing = next(
        v for v in schema["properties"]["value"]["oneOf"] if v["properties"]["kind"].get("const") == "missing"
    )
    assert set(missing["properties"]["reason"]["enum"]) == {r.value for r in MissingReason}


def test_kernel_uncertainty_statuses_match_observation_contract() -> None:
    unc = load("observation.schema.json")["properties"]["uncertainty"]
    assert set(unc["properties"]["status"]["enum"]) == {s.value for s in UncertaintyStatus}


def test_observation_contract_restricts_kernel_kinds_only_by_probability() -> None:
    """D-8 resolved: observation records (source reports) never carry the kernel kind
    'probability' (a model-output type); the schema documents the restriction and the
    exporter refuses it (tests/contracts/test_observation_export_properties.py)."""
    kind = load("observation.schema.json")["properties"]["uncertainty"]["properties"]["kind"]
    assert set(kind["enum"]) == {k.value for k in UncertaintyKind} - {"probability"}
    assert "probability" in kind["description"]


# Exported observation records (real ingestion of captured fixtures → reva.evidence.export →
# full Draft 2020-12 + format validation) are covered in tests/contracts/test_observation_export.py.


# =========================================================================== appendix pins
ANNOTATIONS = frozenset({"$comment", "description", "title", "examples"})
APPENDIX_DIGESTS = {  # sha256 of canonical JSON, annotation keywords removed (binding appendices)
    "scenario.schema.json": "dd598b55635b50ee48264c5ab7f395ea66afdb57a6e73e0074d9fc91fe43ed2d",
    "observation.schema.json": "4619edd9425dd3a78c3f454ed01fd0fe050e450b7bd0560763ea6c5de77c4c74",
    "connector.schema.json": "0e83f9df3b3cb69563b76b0f49c25b7f89ec7b879be571f22e7a2316ed22254b",
}


def _strip_annotations(o: Any, parent: str | None = None) -> Any:
    if isinstance(o, dict):
        return {k: _strip_annotations(v, k) for k, v in o.items() if not (k in ANNOTATIONS and parent != "properties")}
    if isinstance(o, list):
        return [_strip_annotations(v) for v in o]
    return o


@pytest.mark.parametrize("name", sorted(APPENDIX_DIGESTS))
def test_schema_validation_semantics_equal_the_binding_appendix(name: str) -> None:
    """Annotations may be added; any change to validation keywords needs a version bump + ADR."""
    import hashlib

    canon = json.dumps(_strip_annotations(load(name)), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(canon.encode()).hexdigest() == APPENDIX_DIGESTS[name]


# =========================================================================== connector manifests
def manifest_from_spec(spec: ConnectorSpec, eligibility_scope: list[str]) -> dict[str, Any]:
    """The declarative manifest view of a built-in connector spec."""
    return {
        "schema_version": "1.0.0",
        "name": spec.name,
        "version": spec.version,
        "template": spec.template,
        "source_key": spec.source_key,
        "allowed_hosts": list(spec.allowed_hosts),
        "dataset": ",".join(spec.dataset_ids),
        "request_parameters": {"geography": list(spec.geography), "field_mappings": spec.field_mappings},
        "mapping_version": spec.parser_version,
        "rights_ref": spec.licence,
        "lineage_family": spec.lineage_family,
        "max_response_bytes": spec.max_response_bytes,
        "max_pages": spec.max_pages,
        "cadence": {"minimum_interval_seconds": spec.cadence_seconds, "release_aware": spec.release_aware},
        "eligibility_scope": eligibility_scope,
        "generation_mode": "declarative_only",
    }


@pytest.mark.parametrize("name", sorted(builtin_connectors()))
def test_builtin_connector_specs_map_onto_manifest_contract(name: str) -> None:
    reg = load_registry()
    spec = builtin_connectors()[name].describe()
    scope = sorted({m["target"]["concept_id"] for m in reg.mappings if m["connector"]["name"] == spec.name})
    manifest = manifest_from_spec(spec, scope)
    assert errors("connector.schema.json", manifest) == []


def test_connector_contract_and_spec_model_agree_on_bounds_and_templates() -> None:
    schema = load("connector.schema.json")["properties"]
    template_literal = ConnectorSpec.model_fields["template"].annotation
    assert set(schema["template"]["enum"]) == set(typing.get_args(template_literal))
    meta = {type(m).__name__: m for m in ConnectorSpec.model_fields["max_response_bytes"].metadata}
    assert schema["max_response_bytes"]["maximum"] == meta["Le"].le
    assert schema["max_response_bytes"]["minimum"] == meta["Ge"].ge
    pages = {type(m).__name__: m for m in ConnectorSpec.model_fields["max_pages"].metadata}
    assert schema["max_pages"]["maximum"] == pages["Le"].le
    cad = {type(m).__name__: m for m in ConnectorSpec.model_fields["cadence_seconds"].metadata}
    assert schema["cadence"]["properties"]["minimum_interval_seconds"]["minimum"] == cad["Ge"].ge


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(allowed_hosts=["http://evil"]),
        lambda m: m.update(max_response_bytes=10**9),
        lambda m: m.update(generation_mode="llm_generated_code"),
        lambda m: m.update(template="arbitrary_python"),
        lambda m: m["cadence"].update(minimum_interval_seconds=1),
        lambda m: m.update(code="import os; os.system('id')"),
    ],
)
def test_connector_contract_rejects_unsafe_manifests(mutate: Any) -> None:
    spec = builtin_connectors()["worldbank.wdi.indicators"].describe()
    m = manifest_from_spec(spec, ["demography.population.wdi_midyear_total"])
    mutate(m)
    assert errors("connector.schema.json", m) != []


def test_generated_eurostat_manifest_validates_with_formats(tmp_path: Path) -> None:
    """The autonomy generator's real output (captured Eurostat catalogue, offline) satisfies the contract."""
    import uuid
    from datetime import UTC, datetime

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.autonomy.common import ArtifactIO, default_budget, make_context
    from reva.autonomy.discovery import EurostatCatalogueDiscoverer
    from reva.autonomy.fixtures import BankHttp, bank_from_dir
    from reva.autonomy.generator import DeclarativeConnectorGenerator
    from reva.autonomy.mapping import RegistryOntologyMapper
    from reva.ports.autonomy import KnowledgeGap

    io = ArtifactIO(FilesystemArtifactStore(tmp_path / "objects"))
    ctx = make_context(io, "evidence", datetime.now(UTC), {"schema": "test"})
    gap = KnowledgeGap(
        gap_id=uuid.uuid4(),
        requirement_id="R-POP-JAN-AGESEX",
        concept_version="demography.population.jan1_age_sex@1.0.0",
        geography=("FRA",),
        temporal_support={"years": [2023]},
        reason="unsupported: no eligible connector",
        priority=1.0,
        priority_method="test",
        context=ctx,
    )

    async def pipeline() -> dict[str, Any]:
        bank = bank_from_dir(io, "eurostat.demo_pjan.fr")
        cands = await EurostatCatalogueDiscoverer(io, BankHttp(io, bank)).discover(gap, default_budget())
        mapping = RegistryOntologyMapper(io).assess(cands[0], gap.context)[0]
        cp = DeclarativeConnectorGenerator(io).generate(cands[0], mapping, ("eurostat_jsonstat",))
        manifest: dict[str, Any] = io.get_json(cp.manifest)
        return manifest

    manifest = asyncio.run(pipeline())
    assert errors("connector.schema.json", manifest) == []
    assert manifest["generation_mode"] == "declarative_only"


# =========================================================================== registry
def test_registry_loads_and_mappings_target_registered_concepts() -> None:
    reg = load_registry()
    versions = {(c.concept_id, c.version) for c in reg.concepts}
    assert reg.mappings
    for m in reg.mappings:
        t = m["target"]
        assert (t["concept_id"], t["version"]) in versions, m["mapping_key"]


def test_registry_mappings_reference_known_connectors() -> None:
    reg = load_registry()
    known = {(s.describe().name, s.describe().version) for s in builtin_connectors().values()}
    for m in reg.mappings:
        c = (m["connector"]["name"], m["connector"]["version"])
        assert c in known or m["connector"]["name"].startswith("eurostat"), c


def test_connector_concepts_are_registered() -> None:
    from reva.adapters.connectors.comtrade import FLOW_CONCEPT
    from reva.adapters.connectors.worldbank import INDICATORS

    ids = {c.concept_id for c in load_registry().concepts}
    used = (
        set(FLOW_CONCEPT.values())
        | {v["concept"] for v in INDICATORS.values()}
        | {
            "trade.hs2709.net_weight.world_total",
            "predicate.trade_flow",
            "role.reporter",
            "role.partner",
            "role.product",
        }
    )
    assert used <= ids, used - ids


def test_predicate_signatures_reference_registered_roles_and_kinds() -> None:
    reg = load_registry()
    by_id = {c.concept_id: c for c in reg.concepts}
    entity_kinds = {e["kind"] for e in reg.entities}
    for c in reg.concepts:
        if c.kind != "predicate":
            continue
        sig = (c.semantic_profile.signature if c.semantic_profile else None) or {}
        assert sig.get("roles"), c.concept_id
        for role, spec in sig["roles"].items():
            assert role in by_id and by_id[role].kind == "role", role
            for k in spec["entity_kinds"]:
                assert k in by_id and by_id[k].kind == "entity_type", k
            assert spec.get("min", 0) <= spec.get("max", 10**9)
    assert entity_kinds <= {c.concept_id for c in reg.concepts if c.kind == "entity_type"}


def test_quantity_concepts_carry_semantic_profiles_with_distinct_population_semantics() -> None:
    reg = load_registry()
    for c in reg.concepts:
        if c.kind == "quantity":
            assert c.semantic_profile is not None, c.concept_id
            assert c.semantic_profile.temporal_support and c.semantic_profile.aggregation
    pops = [c for c in reg.concepts if c.concept_id.startswith("demography.population")]
    if len(pops) > 1:  # January vs mid-year population are not the same concept
        assert len(
            {(p.semantic_profile.temporal_support, p.semantic_profile.aggregation) for p in pops if p.semantic_profile}
        ) == len(pops)


def test_selection_policy_references_registered_concepts() -> None:
    reg = load_registry()
    ids = {c.concept_id for c in reg.concepts}
    sources = {s["source_key"] for s in reg.sources}
    for concept, prefs in reg.policies["selection"]["preferences"].items():
        assert concept in ids, concept
        assert set(prefs) <= sources, (concept, prefs)


def test_ontology_manifest_is_deterministic() -> None:
    from reva.kernel.ids import logical_hash

    assert logical_hash(load_registry().manifest()) == logical_hash(load_registry().manifest())
