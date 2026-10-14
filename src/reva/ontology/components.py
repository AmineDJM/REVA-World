"""Component manifests: independently versioned schemas attached to entities.

A component manifest declares name, semantic version, applicable entity kinds,
a JSON Schema (Draft 2020-12), concepts, support requirements, privacy class
and named validation rules. Manifests live in
``registry/components/<name>.v<major>.schema.json``; the manifest metadata is
the ``x-reva-component`` annotation of the schema document.

Versioning (architecture §3.3): published versions are immutable. An additive
optional field is a minor change; changed meaning (definitions), units,
required fields or category definitions require a major version and an
explicit migration. Existing snapshots keep their original schemas.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from enum import IntEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict

from reva.kernel.errors import ImmutableRecordViolation, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.records import ComponentManifest
from reva.ontology.registry import REGISTRY_ROOT
from reva.ontology.units import currency_basis_issues, is_currency_unit, is_known_unit

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
COMPONENT_METADATA_KEY = "x-reva-component"
COMPONENTS_DIR = REGISTRY_ROOT / "components"
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_FILE = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)\.v(?P<major>\d+)\.schema\.json$")

# Published component versions and their manifest digests. A published entry is
# immutable: changing the schema file of a listed version fails verification;
# publish a new version instead (see classify_change for the required bump).
PUBLISHED_COMPONENT_DIGESTS: Mapping[str, str] = {
    "demographics@1.0.0": "efbd8fabbc4678371ad9cabfbc8a9da31c3e0cfa90b3c983215942fa00578ad5",
    "display_geometry@1.0.0": "6aeb397849e6f49425e155119dac5a031934fe86dc446d25bb7a80aefc3a270c",
    "economy@1.0.0": "4c2cf98e608e252cea32c201bbb54a6607e43e0a9447031d95b0207f4cf90dbe",
    "trade_crude@1.0.0": "232abc1bc5665c47929b68b1ba526002c64a7ed5a5cd2d15d8dd0cf583119e60",
}

# Entity kinds referenced by component manifests that are not yet registered
# concepts in registry/concepts (requested from the registry owner).
REQUESTED_REGISTRY_CONCEPTS: frozenset[str] = frozenset()


def parse_semver(v: str) -> tuple[int, int, int]:
    m = _SEMVER.match(v)
    if not m:
        raise ValidationProblem(f"not a semantic version: {v!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def component_key(m: ComponentManifest) -> str:
    """``demographics.v1`` style key (name + major version)."""
    return f"{m.name}.v{parse_semver(m.version)[0]}"


def version_key(m: ComponentManifest) -> str:
    return f"{m.name}@{m.version}"


def manifest_digest(m: ComponentManifest) -> str:
    return logical_hash(m.model_dump(mode="json"))


# --------------------------------------------------------------------------- loading


def manifest_from_schema_document(doc: Mapping[str, Any]) -> ComponentManifest:
    """Build and check a manifest from a schema document with ``x-reva-component``."""
    meta = doc.get(COMPONENT_METADATA_KEY)
    if not isinstance(meta, Mapping):
        raise ValidationProblem(f"schema document lacks {COMPONENT_METADATA_KEY}")
    if doc.get("$schema") != SCHEMA_DIALECT:
        raise ValidationProblem(f"component schemas must declare $schema {SCHEMA_DIALECT}")
    schema = {k: v for k, v in doc.items() if k != COMPONENT_METADATA_KEY}
    manifest = ComponentManifest(
        name=meta["name"],
        version=meta["version"],
        applicable_entity_kinds=tuple(meta["applicable_entity_kinds"]),
        json_schema=schema,
        concepts=tuple(meta["concepts"]),
        support_requirements=dict(meta.get("support_requirements") or {}),
        privacy_class=meta["privacy_class"],
        validation_rules=tuple(meta.get("validation_rules") or ()),
    )
    check_manifest(manifest)
    return manifest


def check_manifest(m: ComponentManifest) -> None:
    """Structural checks: semver, Draft 2020-12 schema, known rule names, $id version."""
    parse_semver(m.version)
    if not m.applicable_entity_kinds:
        raise ValidationProblem(f"{m.name}: no applicable entity kinds")
    if not m.concepts:
        raise ValidationProblem(f"{m.name}: no concepts")
    for c in m.concepts:
        if "@" not in c:
            raise ValidationProblem(f"{m.name}: concept {c!r} must be pinned as concept_id@version")
    try:
        Draft202012Validator.check_schema(m.json_schema)
    except SchemaError as e:
        raise ValidationProblem(f"{m.name}: invalid Draft 2020-12 schema: {e.message}") from e
    unknown = [r for r in m.validation_rules if r not in VALIDATION_RULES]
    if unknown:
        raise ValidationProblem(f"{m.name}: unknown validation rules {unknown}")
    sid = m.json_schema.get("$id")
    if sid is not None and sid != f"urn:reva:component:{m.name}:{m.version}":
        raise ValidationProblem(f"{m.name}: $id {sid} does not match name/version")


def load_component_manifests(root: Path | None = None) -> dict[str, ComponentManifest]:
    """All manifests keyed ``name@version``; the file name must match name and major."""
    root = root or COMPONENTS_DIR
    out: dict[str, ComponentManifest] = {}
    for p in sorted(root.glob("*.schema.json")):
        fm = _FILE.match(p.name)
        if not fm:
            raise ValidationProblem(f"component schema file name {p.name} must be <name>.v<major>.schema.json")
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        m = manifest_from_schema_document(doc)
        if m.name != fm.group("name") or parse_semver(m.version)[0] != int(fm.group("major")):
            raise ValidationProblem(f"{p.name}: file name does not match {m.name}@{m.version}")
        k = version_key(m)
        if k in out:
            raise ValidationProblem(f"duplicate component version {k}")
        out[k] = m
    return out


def verify_published(manifests: Mapping[str, ComponentManifest],
                     pinned: Mapping[str, str] = PUBLISHED_COMPONENT_DIGESTS) -> None:
    """Every pinned published version must exist and be byte-for-byte (logically) unchanged."""
    for key, digest in pinned.items():
        m = manifests.get(key)
        if m is None:
            raise ImmutableRecordViolation(f"published component {key} was removed", component=key)
        actual = manifest_digest(m)
        if actual != digest:
            raise ImmutableRecordViolation(
                f"published component {key} changed (digest {actual} != pinned {digest}); publish a new version",
                component=key)


# --------------------------------------------------------------------------- instance validation

RuleFn = Callable[[Mapping[str, Any]], list[str]]


def _walk(obj: Any, path: str = "") -> Iterable[tuple[str, Mapping[str, Any]]]:
    if isinstance(obj, Mapping):
        yield path, obj
        for k, v in obj.items():
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}/{i}")


_GENDER_KEYS = frozenset({"gender", "gender_identity"})


def _rule_no_gender(inst: Mapping[str, Any]) -> list[str]:
    return [f"{p or '/'}: field {k!r} is not part of recorded sex and is not collected"
            for p, d in _walk(inst) for k in d if k in _GENDER_KEYS]


def _rule_age(inst: Mapping[str, Any]) -> list[str]:
    age = inst.get("age")
    if not isinstance(age, Mapping):
        return []
    kind, errs = age.get("kind"), []
    has = {k for k in ("years", "lower_bound_years", "upper_bound_years") if k in age}
    expected = {
        "single_year": {"years"},
        "band": {"lower_bound_years", "upper_bound_years"},
        "open_ended": {"lower_bound_years"},
        "unknown": set(),
    }.get(str(kind))
    if expected is not None and has != expected:
        errs.append(f"/age: kind {kind} requires exactly {sorted(expected)}, got {sorted(has)} (no imputation)")
    if kind == "band" and has == expected and age["upper_bound_years"] < age["lower_bound_years"]:
        errs.append("/age: band upper bound below lower bound")
    return errs


def _rule_units(inst: Mapping[str, Any]) -> list[str]:
    return [f"{p}: unit_ref {d['unit_ref']!r} is not registered" for p, d in _walk(inst)
            if "unit_ref" in d and not (isinstance(d["unit_ref"], str) and is_known_unit(d["unit_ref"]))]


def _rule_currency(inst: Mapping[str, Any]) -> list[str]:
    errs: list[str] = []
    for p, d in _walk(inst):
        u = d.get("unit_ref")
        if isinstance(u, str) and is_known_unit(u) and is_currency_unit(u):
            errs.extend(f"{p}: {i.message}" for i in currency_basis_issues(u, d.get("basis"), p)
                        if i.severity == "error")
    return errs


def _rule_valuation(inst: Mapping[str, Any]) -> list[str]:
    errs = []
    for key in ("imports_border_value", "exports_border_value"):
        v = inst.get(key)
        if isinstance(v, Mapping) and v.get("kind") == "decimal":
            basis = v.get("basis")
            if not isinstance(basis, Mapping) or not basis.get("valuation"):
                errs.append(f"/{key}: border value without valuation (CIF/FOB/undetermined)")
    return errs


def _rule_display(inst: Mapping[str, Any]) -> list[str]:
    return [] if inst.get("authoritative") is False else ["/authoritative: display geometry must be non-authoritative"]


VALIDATION_RULES: Mapping[str, RuleFn] = {
    "no_gender_identity_fields": _rule_no_gender,
    "age_category_consistent": _rule_age,
    "units_must_be_registered": _rule_units,
    "currency_values_require_basis": _rule_currency,
    "border_values_require_valuation": _rule_valuation,
    "display_geometry_not_authoritative": _rule_display,
}


class ComponentValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    component: str
    schema_errors: tuple[str, ...] = ()
    rule_errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.schema_errors and not self.rule_errors

    def require(self) -> None:
        if not self.ok:
            raise ValidationProblem(f"{self.component} instance invalid",
                                    schema_errors=list(self.schema_errors), rule_errors=list(self.rule_errors))


def validate_component_instance(m: ComponentManifest, instance: Mapping[str, Any]) -> ComponentValidationReport:
    validator = Draft202012Validator(m.json_schema, format_checker=FormatChecker())
    schema_errors = tuple(sorted(f"/{'/'.join(str(x) for x in e.absolute_path)}: {e.message}"
                                 for e in validator.iter_errors(instance)))
    rule_errors: list[str] = []
    for r in m.validation_rules:
        rule_errors.extend(VALIDATION_RULES[r](instance))
    return ComponentValidationReport(component=version_key(m), schema_errors=schema_errors,
                                     rule_errors=tuple(rule_errors))


# --------------------------------------------------------------------------- change classification


class ChangeLevel(IntEnum):
    none = 0
    patch = 1
    minor = 2
    major = 3


class ChangeReason(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    level: ChangeLevel
    path: str
    message: str


class ChangeReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    level: ChangeLevel
    reasons: tuple[ChangeReason, ...] = ()


# Keywords whose change alters what a valid instance is or means.
_MEANING_KEYWORDS = frozenset({
    "type", "enum", "const", "format", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "multipleOf", "minItems", "maxItems", "uniqueItems", "additionalProperties",
    "$ref", "minProperties", "maxProperties", "dependentRequired", "if", "then", "else", "not",
    "prefixItems", "contains", "patternProperties", "propertyNames", "unevaluatedProperties",
    "unevaluatedItems", "x-reva-unit", "x-reva-concept",
})
_DEFINITION_KEYWORDS = frozenset({"description"})
_ANNOTATION_KEYWORDS = frozenset({"title", "examples", "$comment", "default", "deprecated", "readOnly", "writeOnly"})
_COMPOSITION = ("oneOf", "anyOf", "allOf")


def _diff_schema(old: Any, new: Any, path: str, out: list[ChangeReason]) -> None:
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        if old != new:
            out.append(ChangeReason(level=ChangeLevel.major, path=path, message="schema node replaced"))
        return
    old_props: Mapping[str, Any] = old.get("properties", {})
    new_props: Mapping[str, Any] = new.get("properties", {})
    old_req, new_req = set(old.get("required", [])), set(new.get("required", []))
    for name in sorted(set(old_props) - set(new_props)):
        out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/properties/{name}", message="field removed"))
    for name in sorted(set(new_props) - set(old_props)):
        if name in new_req:
            out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/properties/{name}",
                                    message="new required field"))
        else:
            out.append(ChangeReason(level=ChangeLevel.minor, path=f"{path}/properties/{name}",
                                    message="additive optional field"))
    for name in sorted(set(old_props) & set(new_props)):
        _diff_schema(old_props[name], new_props[name], f"{path}/properties/{name}", out)
    for name in sorted((new_req - old_req) & set(old_props)):
        out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/required/{name}",
                                message="existing field became required"))
    for name in sorted((old_req - new_req) & set(new_props)):
        out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/required/{name}",
                                message="required field became optional (consumers may rely on it)"))
    for kw in ("$defs", "definitions"):
        o, n = old.get(kw, {}), new.get(kw, {})
        for name in sorted(set(o) | set(n)):
            if name not in n:
                out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/{kw}/{name}",
                                        message="definition removed"))
            elif name not in o:
                out.append(ChangeReason(level=ChangeLevel.minor, path=f"{path}/{kw}/{name}",
                                        message="definition added"))
            else:
                _diff_schema(o[name], n[name], f"{path}/{kw}/{name}", out)
    for kw in _COMPOSITION:
        o, n = old.get(kw), new.get(kw)
        if o is None and n is None:
            continue
        if o is None or n is None or len(o) != len(n):
            out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/{kw}", message=f"{kw} alternatives changed"))
            continue
        for i, (a, b) in enumerate(zip(o, n, strict=True)):
            _diff_schema(a, b, f"{path}/{kw}/{i}", out)
    if "items" in old or "items" in new:
        if "items" in old and "items" in new:
            _diff_schema(old["items"], new["items"], f"{path}/items", out)
        else:
            out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/items", message="items changed"))
    handled = {"properties", "required", "$defs", "definitions", "items", *_COMPOSITION}
    for kw in sorted((set(old) | set(new)) - handled):
        if old.get(kw) == new.get(kw):
            continue
        if kw in _MEANING_KEYWORDS:
            what = "category definition" if kw in ("enum", "const") else "constraint/unit"
            out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/{kw}", message=f"{what} changed"))
        elif kw in _DEFINITION_KEYWORDS:
            out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/{kw}",
                                    message="definition text changed (meaning change; requires review)"))
        elif kw in _ANNOTATION_KEYWORDS:
            out.append(ChangeReason(level=ChangeLevel.patch, path=f"{path}/{kw}", message="annotation changed"))
        elif kw in ("$id", "$schema"):
            continue  # $id tracks the version; $schema is fixed by check_manifest
        else:
            out.append(ChangeReason(level=ChangeLevel.major, path=f"{path}/{kw}",
                                    message=f"unrecognised keyword {kw} changed (treated as meaning change)"))


def _set_diff(path: str, old: Iterable[str], new: Iterable[str], added: ChangeLevel, removed: ChangeLevel,
              out: list[ChangeReason]) -> None:
    o, n = set(old), set(new)
    for x in sorted(n - o):
        out.append(ChangeReason(level=added, path=f"{path}/{x}", message="added"))
    for x in sorted(o - n):
        out.append(ChangeReason(level=removed, path=f"{path}/{x}", message="removed"))


def classify_change(old: ComponentManifest, new: ComponentManifest) -> ChangeReport:
    """Smallest semantic-version bump that the difference between two manifests requires."""
    if old.name != new.name:
        raise ValidationProblem(f"different components: {old.name} vs {new.name}")
    out: list[ChangeReason] = []
    _diff_schema(old.json_schema, new.json_schema, "#", out)
    _set_diff("applicable_entity_kinds", old.applicable_entity_kinds, new.applicable_entity_kinds,
              ChangeLevel.minor, ChangeLevel.major, out)
    old_c = {c.split("@")[0]: c for c in old.concepts}
    new_c = {c.split("@")[0]: c for c in new.concepts}
    for cid in sorted(set(old_c) | set(new_c)):
        if cid not in new_c:
            out.append(ChangeReason(level=ChangeLevel.major, path=f"concepts/{cid}", message="concept removed"))
        elif cid not in old_c:
            out.append(ChangeReason(level=ChangeLevel.minor, path=f"concepts/{cid}", message="concept added"))
        elif old_c[cid] != new_c[cid]:
            out.append(ChangeReason(level=ChangeLevel.major, path=f"concepts/{cid}",
                                    message=f"concept version changed {old_c[cid]} -> {new_c[cid]}"))
    if old.support_requirements != new.support_requirements:
        out.append(ChangeReason(level=ChangeLevel.major, path="support_requirements", message="support changed"))
    if old.privacy_class != new.privacy_class:
        out.append(ChangeReason(level=ChangeLevel.major, path="privacy_class", message="privacy class changed"))
    _set_diff("validation_rules", old.validation_rules, new.validation_rules, ChangeLevel.major, ChangeLevel.minor,
              out)
    level = max((r.level for r in out), default=ChangeLevel.none)
    return ChangeReport(level=level, reasons=tuple(out))


def check_version_bump(old: ComponentManifest, new: ComponentManifest) -> ChangeReport:
    """Raise unless ``new.version`` is a sufficient bump over ``old.version``."""
    report = classify_change(old, new)
    ov, nv = parse_semver(old.version), parse_semver(new.version)
    if report.level is ChangeLevel.none:
        if nv < ov:
            raise ValidationProblem(f"{new.name}: version went backwards")
        return report
    if nv <= ov:
        raise ImmutableRecordViolation(
            f"{new.name}@{old.version} is published; a {report.level.name} change needs a new version",
            reasons=[r.model_dump(mode="json") for r in report.reasons])
    if nv[0] > ov[0]:
        bump = ChangeLevel.major
    elif nv[1] > ov[1]:
        bump = ChangeLevel.minor
    else:
        bump = ChangeLevel.patch
    if bump < report.level:
        raise ValidationProblem(
            f"{new.name}: {old.version} -> {new.version} is a {bump.name} bump but the change is {report.level.name}",
            reasons=[r.model_dump(mode="json") for r in report.reasons])
    return report


class ComponentCatalog:
    """In-memory catalogue enforcing immutability and version-bump rules."""

    def __init__(self) -> None:
        self._published: dict[str, ComponentManifest] = {}

    def publish(self, m: ComponentManifest) -> str:
        check_manifest(m)
        key = version_key(m)
        existing = self._published.get(key)
        if existing is not None:
            if manifest_digest(existing) != manifest_digest(m):
                raise ImmutableRecordViolation(f"{key} is published and immutable", component=key)
            return key
        prior = [p for p in self._published.values() if p.name == m.name and
                 parse_semver(p.version) < parse_semver(m.version)]
        if prior:
            check_version_bump(max(prior, key=lambda p: parse_semver(p.version)), m)
        self._published[key] = m
        return key

    def get(self, name: str, version: str) -> ComponentManifest:
        return self._published[f"{name}@{version}"]

    def versions(self, name: str) -> list[str]:
        return sorted((m.version for m in self._published.values() if m.name == name), key=parse_semver)
