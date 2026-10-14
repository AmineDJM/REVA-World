"""Versioned concept mappings and weighted, time-dependent crosswalks.

A mapping is a versioned object: source concept/version (or provider source
reference), target concept/version, relation type, transformation,
applicability, evidence, approval and uncertainty contribution. Only an
approved ``exact`` mapping may be used as identity.

Crosswalks may be many-to-many and time-dependent. Unmapped mass is preserved
as an explicit residual per source code; totals are never forced to balance by
arbitrary assignment, and a residual is never redistributed to targets.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reva.kernel.errors import IncompatibleSemantics, ValidationProblem
from reva.kernel.records import Concept
from reva.kernel.values import Missing, decimal_str
from reva.ontology.validation import CompatibilityReport, Verdict, compare_concepts

_PRECISION = 60
_CLASSIFICATION = r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$"  # "<system>:<revision>", e.g. "HS:H5"


class RelationType(StrEnum):
    exact = "exact"
    narrower = "narrower"
    broader = "broader"
    overlap = "overlap"
    weighted_crosswalk = "weighted_crosswalk"
    approximate_transform = "approximate_transform"
    incompatible = "incompatible"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConceptRef(_Frozen):
    concept_id: str = Field(min_length=1)
    version: str = Field(min_length=1)

    def key(self) -> str:
        return f"{self.concept_id}@{self.version}"


class MappingTransform(_Frozen):
    kind: Literal["identity", "none", "linear", "crosswalk"]
    params: dict[str, Any] = {}

    @model_validator(mode="after")
    def _params(self) -> MappingTransform:
        if self.kind == "linear" and "factor" not in self.params:
            raise ValueError("linear transform requires params.factor")
        if self.kind == "crosswalk" and not {"crosswalk_key", "crosswalk_version"} <= set(self.params):
            raise ValueError("crosswalk transform requires crosswalk_key and crosswalk_version")
        return self


class Applicability(_Frozen):
    subjects: tuple[str, ...] | None = None  # None = not restricted by subject
    year_from: int | None = None  # inclusive
    year_to: int | None = None  # inclusive
    classifications: tuple[str, ...] | None = None
    reason: str | None = None

    def applies(self, *, subject: str | None = None, year: int | None = None,
                classification: str | None = None) -> bool:
        if self.subjects is not None and (subject is None or subject not in self.subjects):
            return False
        if self.year_from is not None or self.year_to is not None:
            if year is None:
                return False
            if self.year_from is not None and year < self.year_from:
                return False
            if self.year_to is not None and year > self.year_to:
                return False
        return not (self.classifications is not None
                    and (classification is None or classification not in self.classifications))


class MappingEvidence(_Frozen):
    kind: Literal["provider_metadata", "methodology_document", "review_note", "statistical_test", "registry_review"]
    ref: str = Field(min_length=1)
    note: str | None = None


class MappingApproval(_Frozen):
    status: Literal["approved", "candidate", "rejected"]
    basis: str = Field(min_length=1)  # e.g. review policy version
    reviewer: str | None = None
    review_activity_id: UUID | None = None


class UncertaintyContribution(_Frozen):
    kind: Literal["none_exact", "unquantified", "relative_sd", "absolute_bounds", "not_applicable"]
    value: str | None = None  # decimal string for relative_sd / "lo,hi" for bounds
    note: str | None = None


class ConceptMapping(_Frozen):
    mapping_key: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_ref: dict[str, Any]
    source_concept: ConceptRef | None = None
    target: ConceptRef
    relation: RelationType
    transform: MappingTransform
    applicability: Applicability
    evidence: tuple[MappingEvidence, ...] = ()
    approval: MappingApproval
    uncertainty: UncertaintyContribution

    @model_validator(mode="after")
    def _coherent(self) -> ConceptMapping:
        r, t, u = self.relation, self.transform.kind, self.uncertainty.kind
        if r is RelationType.exact and (t != "identity" or u != "none_exact"):
            raise ValueError("exact mappings use the identity transform and contribute no uncertainty")
        if r is not RelationType.exact and t == "identity":
            raise ValueError(f"{r.value} mapping cannot use the identity transform")
        if r is RelationType.incompatible and t != "none":
            raise ValueError("incompatible mappings carry no transform")
        if r is RelationType.weighted_crosswalk and t != "crosswalk":
            raise ValueError("weighted_crosswalk mappings must reference a crosswalk")
        if r is RelationType.approximate_transform and u in ("none_exact", "not_applicable"):
            raise ValueError("approximate transforms must declare an uncertainty contribution")
        if r is not RelationType.exact and u == "none_exact":
            raise ValueError(f"{r.value} mapping cannot claim zero uncertainty")
        return self

    @property
    def usable_as_identity(self) -> bool:
        return self.relation is RelationType.exact and self.approval.status == "approved"

    def require_identity(self, *, subject: str | None = None, year: int | None = None,
                         classification: str | None = None) -> ConceptRef:
        """Return the target for identity use or raise ``IncompatibleSemantics``."""
        if self.relation is not RelationType.exact:
            raise IncompatibleSemantics(
                f"mapping {self.mapping_key}@{self.version} is {self.relation.value}; only exact mappings can be "
                "used as identity", mapping_key=self.mapping_key, relation=self.relation.value,
                reason=self.applicability.reason)
        if self.approval.status != "approved":
            raise IncompatibleSemantics(f"mapping {self.mapping_key}@{self.version} is {self.approval.status}",
                                        mapping_key=self.mapping_key)
        if not self.applicability.applies(subject=subject, year=year, classification=classification):
            raise IncompatibleSemantics(
                f"mapping {self.mapping_key}@{self.version} does not apply to subject={subject} year={year} "
                f"classification={classification}", mapping_key=self.mapping_key)
        return self.target


# --------------------------------------------------------------------------- registry adapter

REGISTRY_REVIEW_BASIS = "seed-reviewed-1"


def _applicability_from_registry(raw: Mapping[str, Any]) -> Applicability:
    years = raw.get("years")
    yf = yt = None
    if years:
        if len(years) == 1:
            yf = yt = int(years[0])
        elif len(years) == 2:
            yf, yt = int(years[0]), int(years[1])
        else:
            raise ValidationProblem(f"applicability.years must be [year] or [from, to]: {years}")
    subjects = raw.get("subjects")
    classes = raw.get("classifications")
    return Applicability(
        subjects=tuple(subjects) if subjects is not None else None,
        year_from=yf,
        year_to=yt,
        classifications=tuple(classes) if classes is not None else None,
        reason=raw.get("reason"),
    )


def mapping_from_registry(raw: Mapping[str, Any]) -> ConceptMapping:
    """Typed view of a ``registry/mappings/*.yaml`` entry.

    The registry YAML does not (yet) carry evidence/uncertainty fields; the
    approval mirrors the seed policy (exact and incompatible relations are
    reviewed at seed; others remain candidates), evidence records that review.
    Explicit ``approval``/``evidence``/``uncertainty``/``transform`` keys in the
    YAML take precedence.
    """
    relation = RelationType(raw["mapping_kind"])
    default_transform = {"kind": "identity"} if relation is RelationType.exact else {"kind": "none"}
    default_unc = {
        RelationType.exact: {"kind": "none_exact"},
        RelationType.incompatible: {"kind": "not_applicable"},
    }.get(relation, {"kind": "unquantified"})
    default_status = "approved" if relation in (RelationType.exact, RelationType.incompatible) else "candidate"
    approval = raw.get("approval") or {"status": default_status, "basis": REGISTRY_REVIEW_BASIS}
    evidence = raw.get("evidence") or [{
        "kind": "registry_review",
        "ref": f"registry/mappings:{raw['mapping_key']}@{raw['version']}",
        "note": (raw.get("applicability") or {}).get("reason"),
    }]
    src_concept = raw.get("source_concept")
    return ConceptMapping(
        mapping_key=raw["mapping_key"],
        version=str(raw["version"]),
        source_ref=dict(raw.get("source_ref") or {}),
        source_concept=ConceptRef.model_validate(src_concept) if src_concept else None,
        target=ConceptRef(concept_id=raw["target"]["concept_id"], version=str(raw["target"]["version"])),
        relation=relation,
        transform=MappingTransform.model_validate(raw.get("transform") or default_transform),
        applicability=_applicability_from_registry(raw.get("applicability") or {}),
        evidence=tuple(MappingEvidence.model_validate(e) for e in evidence),
        approval=MappingApproval.model_validate(approval),
        uncertainty=UncertaintyContribution.model_validate(raw.get("uncertainty") or default_unc),
    )


def load_registry_mappings(registry: Any) -> tuple[ConceptMapping, ...]:
    """All mappings of a loaded :class:`reva.ontology.registry.Registry`, typed."""
    out = tuple(mapping_from_registry(m) for m in registry.mappings)
    keys = [(m.mapping_key, m.version) for m in out]
    if len(keys) != len(set(keys)):
        raise ValidationProblem("duplicate mapping versions")
    return out


def find_mapping(mappings: Iterable[ConceptMapping], mapping_key: str, version: str | None = None) -> ConceptMapping:
    found = [m for m in mappings if m.mapping_key == mapping_key and (version is None or m.version == version)]
    if not found:
        raise KeyError(mapping_key)
    return max(found, key=lambda m: tuple(int(x) for x in m.version.split(".") if x.isdigit()))


class RelationAssessment(_Frozen):
    """Whether two concept versions may be related by identity.

    ``relation`` is ``exact`` only for a fully compatible pair, ``incompatible``
    when any semantic dimension differs, and ``None`` when information is
    missing (blocked): the relation is then undetermined, never assumed.
    Narrower/broader/overlap need a human/evidence judgement and are not
    derived automatically.
    """

    source: str
    target: str
    relation: RelationType | None
    report: CompatibilityReport


def assess_identity(source: Concept, target: Concept) -> RelationAssessment:
    report = compare_concepts(source, target)
    relation = {
        Verdict.compatible: RelationType.exact,
        Verdict.incompatible: RelationType.incompatible,
        Verdict.blocked: None,
    }[report.verdict]
    return RelationAssessment(source=report.left, target=report.right, relation=relation, report=report)


def check_mapping_against_concepts(mapping: ConceptMapping, source: Concept, target: Concept) -> CompatibilityReport:
    """A declared ``exact`` mapping between two concepts must not contradict their semantics."""
    if mapping.target.key() != f"{target.concept_id}@{target.version}":
        raise ValidationProblem(f"mapping {mapping.mapping_key} targets {mapping.target.key()}, not {target.concept_id}")
    report = compare_concepts(source, target)
    if mapping.relation is RelationType.exact and report.verdict is not Verdict.compatible:
        raise IncompatibleSemantics(
            f"mapping {mapping.mapping_key} declared exact but concepts differ: " + "; ".join(report.reasons))
    return report


# --------------------------------------------------------------------------- weighted crosswalks


def _dec(v: Decimal | str | int) -> Decimal:
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation as e:
        raise ValidationProblem(f"not a decimal: {v!r}") from e
    if not d.is_finite():
        raise ValidationProblem("non-finite values are forbidden")
    return d


class CrosswalkLink(_Frozen):
    source_code: str = Field(min_length=1)
    target_code: str = Field(min_length=1)
    weight: str  # decimal string in (0, 1]: share of the source code's mass
    valid_from_year: int | None = None  # inclusive
    valid_to_year: int | None = None  # inclusive

    @field_validator("weight")
    @classmethod
    def _w(cls, v: str) -> str:
        d = _dec(v)
        if not (Decimal(0) < d <= Decimal(1)):
            raise ValueError("crosswalk weight must be in (0, 1]")
        return v

    @model_validator(mode="after")
    def _years(self) -> CrosswalkLink:
        if self.valid_from_year is not None and self.valid_to_year is not None and \
                self.valid_to_year < self.valid_from_year:
            raise ValueError("valid_to_year before valid_from_year")
        return self

    def active(self, year: int) -> bool:
        return (self.valid_from_year is None or year >= self.valid_from_year) and (
            self.valid_to_year is None or year <= self.valid_to_year)


class WeightedCrosswalk(_Frozen):
    """A versioned many-to-many, time-dependent classification crosswalk.

    For every source code and year, active weights sum to at most 1. Weights
    summing to less than 1 leave an explicit, preserved residual.
    """

    crosswalk_key: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_classification: str = Field(pattern=_CLASSIFICATION)
    target_classification: str = Field(pattern=_CLASSIFICATION)
    links: tuple[CrosswalkLink, ...] = Field(min_length=1)
    evidence: tuple[MappingEvidence, ...] = Field(min_length=1)
    approval: MappingApproval

    @model_validator(mode="after")
    def _weights_bounded(self) -> WeightedCrosswalk:
        by_src: dict[str, list[CrosswalkLink]] = {}
        seen: set[tuple[str, str, int | None, int | None]] = set()
        for lk in self.links:
            k = (lk.source_code, lk.target_code, lk.valid_from_year, lk.valid_to_year)
            if k in seen:
                raise ValueError(f"duplicate crosswalk link {k}")
            seen.add(k)
            by_src.setdefault(lk.source_code, []).append(lk)
        for code, links in by_src.items():
            probes = {y for lk in links for y in (lk.valid_from_year, lk.valid_to_year) if y is not None} or {0}
            for y in probes:
                total = sum((_dec(lk.weight) for lk in links if lk.active(y)), Decimal(0))
                if total > 1:
                    raise ValueError(f"weights for {code} sum to {total} > 1 in {y}: mass would be created")
        return self

    def apply(self, values: Mapping[str, Decimal | str | Missing], year: int, *, unit_ref: str,
              require_approved: bool = True) -> CrosswalkResult:
        """Map extensive source-classified values to target codes for ``year``.

        * mapped share  = value x weight per active link,
        * residual      = value - sum(mapped shares) per source code,
        * unmatched     = source codes with no active link (whole value is residual),
        * missing inputs propagate: targets fed by a Missing source are flagged incomplete.
        Conservation ``total_input == total_mapped + total_residual`` holds exactly.
        """
        if require_approved and self.approval.status != "approved":
            raise IncompatibleSemantics(f"crosswalk {self.crosswalk_key}@{self.version} is {self.approval.status}")
        targets: dict[str, Decimal] = {}
        residual: dict[str, Decimal] = {}
        unmatched: list[str] = []
        missing_sources: list[str] = []
        incomplete: set[str] = set()
        total_in = Decimal(0)
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            for code in sorted(values):
                raw = values[code]
                active = [lk for lk in self.links if lk.source_code == code and lk.active(year)]
                if isinstance(raw, Missing):
                    missing_sources.append(code)
                    incomplete.update(lk.target_code for lk in active)
                    continue
                v = _dec(raw)
                total_in += v
                if not active:
                    unmatched.append(code)
                    residual[code] = v
                    continue
                mapped = Decimal(0)
                for lk in active:
                    share = v * _dec(lk.weight)
                    targets[lk.target_code] = targets.get(lk.target_code, Decimal(0)) + share
                    mapped += share
                rest = v - mapped
                if rest != 0:
                    residual[code] = rest
            total_mapped = sum(targets.values(), Decimal(0))
            total_residual = sum(residual.values(), Decimal(0))
            if total_mapped + total_residual != total_in:  # pragma: no cover - exact arithmetic invariant
                raise ValidationProblem("crosswalk conservation violated")
        return CrosswalkResult(
            crosswalk_key=self.crosswalk_key,
            crosswalk_version=self.version,
            source_classification=self.source_classification,
            target_classification=self.target_classification,
            year=year,
            unit_ref=unit_ref,
            targets={k: decimal_str(v) for k, v in sorted(targets.items())},
            residual_by_source={k: decimal_str(v) for k, v in sorted(residual.items())},
            unmatched_source_codes=tuple(unmatched),
            missing_source_codes=tuple(missing_sources),
            incomplete_target_codes=tuple(sorted(incomplete)),
            total_input=decimal_str(total_in),
            total_mapped=decimal_str(total_mapped),
            total_residual=decimal_str(total_residual),
        )


class CrosswalkResult(_Frozen):
    """Crosswalk output. The residual is explicit and is never folded into targets."""

    crosswalk_key: str
    crosswalk_version: str
    source_classification: str
    target_classification: str
    year: int
    unit_ref: str
    targets: dict[str, str]
    residual_by_source: dict[str, str]
    unmatched_source_codes: tuple[str, ...]
    missing_source_codes: tuple[str, ...]
    incomplete_target_codes: tuple[str, ...]
    total_input: str
    total_mapped: str
    total_residual: str

    @property
    def complete(self) -> bool:
        """True only if nothing was unmapped and no input was missing."""
        return not self.residual_by_source and not self.missing_source_codes


WeightedCrosswalk.model_rebuild()
