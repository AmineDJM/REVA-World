"""Published JSON contracts are valid, mutually consistent and match what the code produces."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import text

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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CONTRACT MISMATCH: kernel UncertaintyKind has 'probability' but observation.schema.json "
        "uncertainty.kind enum does not; align the enums (or document the restriction)"
    ),
)
def test_kernel_uncertainty_kinds_match_observation_contract() -> None:
    unc = load("observation.schema.json")["properties"]["uncertainty"]
    assert set(unc["properties"]["kind"]["enum"]) == {k.value for k in UncertaintyKind}


def _helper() -> Any:
    name = "reva_h2_isolated_db"
    if name not in sys.modules:
        path = REPO / "tests" / "fixtures" / "adversarial" / "isolated_db.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[dict[str, Any]]]:
    """Observation records exported from the DB after a real fixture ingestion."""
    from reva.evidence.runner import FixtureHttp, run_connector

    with _helper().isolated_db(tmp_path_factory) as d:
        for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
            c = builtin_connectors()[name]
            asyncio.run(run_connector(c, FixtureHttp(name), d.store, d.worker, persist_cursor=False))
        with d.api.connect() as c:
            rows = (
                c.execute(
                    text(
                        """SELECT o.*, a.retrieved_at, a.raw_sha256 FROM evidence.observation o
                   JOIN evidence.acquisition a ON a.id = o.acquisition_id ORDER BY o.logical_key"""
                    )
                )
                .mappings()
                .all()
            )
        yield [export(dict(r)) for r in rows]


def _iso(v: Any) -> Any:
    return v.isoformat() if v is not None else None


def export(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "source_id": str(r["source_id"]),
        "connector_version_id": str(r["connector_version_id"]),
        "acquisition_id": str(r["acquisition_id"]),
        "subject_id": str(r["subject_id"]),
        "concept_version_id": str(r["concept_version_id"]),
        "support_id": str(r["support_id"]),
        "logical_key": r["logical_key"],
        "source_revision_id": r["source_revision_id"],
        "value": r["value"],
        "uncertainty": r["uncertainty"],
        "observed_at": _iso(r["observed_at"]),
        "source_published_at": _iso(r["source_published_at"]),
        "source_available_at": _iso(r["source_available_at"]),
        "availability_basis": r["availability_basis"],
        "retrieved_at": _iso(r["retrieved_at"]),
        "source_flags": r["source_flags"],
        "raw_selector": r["raw_selector"],
        "provenance_activity_id": str(r["provenance_activity_id"]),
        "raw_sha256": r["raw_sha256"],
    }


@pytest.mark.db
def test_exported_observation_values_and_identity_fields_conform(exported: list[dict[str, Any]]) -> None:
    assert len(exported) > 200
    kinds = {o["value"]["kind"] for o in exported}
    assert kinds == {"decimal", "missing"}  # both shapes are exercised (Comtrade DZA is Missing)
    schema = load("observation.schema.json")
    sub = {**schema, "required": [k for k in schema["required"] if k != "uncertainty"]}
    sub["properties"] = {k: v for k, v in schema["properties"].items() if k != "uncertainty"}
    v = Draft202012Validator(sub, format_checker=FormatChecker())
    bad = {}
    for o in exported:
        errs = [e.message for e in v.iter_errors({k: x for k, x in o.items() if k != "uncertainty"})]
        if errs:
            bad[o["logical_key"]] = errs[:3]
    assert bad == {}


@pytest.mark.db
@pytest.mark.xfail(
    strict=True,
    reason=(
        "CONTRACT MISMATCH: stored observation uncertainty is UncertaintySpec.model_dump_json() and "
        "includes level/lower/upper/conditioning, which observation.schema.json forbids "
        "(uncertainty.additionalProperties=false); either add these optional fields to the schema or "
        "export with exclude of unset/None fields"
    ),
)
def test_exported_observation_records_validate_against_contract(exported: list[dict[str, Any]]) -> None:
    bad = {
        o["logical_key"]: errors("observation.schema.json", o)[:3]
        for o in exported
        if errors("observation.schema.json", o)
    }
    assert bad == {}


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
