"""Decision schema, instruments and the reference-decision library (V5, agent PRED-ENGINE).

A *decision* names who decides (actor), with which instrument, on what (target), how much (magnitude), from
when (start quarter) and for how long (duration in quarters, ``None`` = held over the whole horizon). The
instrument registry (``registry/predict/instruments.yaml``) turns it into *direct changes*: changes of one
variable at one geography, in the unit effect estimates state their cause in. That translation is a stated
scenario assumption shown on every result; it never adds a number the decision does not state (unit factors
are exact definitions: 1 mb/d = 1000 kb/d, 1 bp = 0.01 pp, 1 year = 12 months).

Pure module: standard library, pydantic and PyYAML only (no database).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reva.kernel.errors import CapabilityGap, ValidationProblem

REGISTRY_DIR = Path(__file__).resolve().parents[3] / "registry" / "predict"
QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")
ISO3_RE = r"^[A-Z]{3}$"
GEO_RE = r"^([A-Z]{3})$"
ENTITY_ID_RE = r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$"
MAX_HORIZON_QUARTERS = 12
MAX_COUNTRIES = 12
MAX_ANCHORS = 10
VariableKind = Literal["atlas_concept", "variable", "event_type", "event"]
Timing = Literal["step", "occurrence"]


# ---------------------------------------------------------------------------------------------- periods
def parse_quarter(q: str) -> tuple[int, int]:
    m = QUARTER_RE.match(q)
    if not m:
        raise ValidationProblem(f"start must be a quarter like 2027Q1, got {q!r}")
    return int(m.group(1)), int(m.group(2))


def quarter_label(start: str, offset: int) -> str:
    y, q = parse_quarter(start)
    idx = y * 4 + (q - 1) + offset
    return f"{idx // 4}Q{idx % 4 + 1}"


def quarter_labels(start: str, n: int) -> list[str]:
    return [quarter_label(start, i) for i in range(n)]


def _dec(v: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError) as e:
        raise ValidationProblem(f"{what} must be a decimal number, got {v!r}") from e
    if not d.is_finite():
        raise ValidationProblem(f"{what} must be finite")
    return d


# ---------------------------------------------------------------------------------------------- schema
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Party(_Model):
    label_fr: str = Field(min_length=1, max_length=200)
    label_en: str | None = Field(None, max_length=200)
    kg_id: str | None = Field(None, pattern=ENTITY_ID_RE, description="KG entity id (QID or provider key)")
    geo: str | None = Field(None, pattern=r"^([A-Z]{3})$", description="ISO3 or WLD")


class Magnitude(_Model):
    value: str = Field(description="Decimal string as decided, e.g. '-2' (mb/d) or '50' (bp)", max_length=40)
    unit: str = Field(min_length=1, max_length=60)

    @field_validator("value")
    @classmethod
    def _decimal(cls, v: str) -> str:
        _dec(v, "magnitude.value")
        return v


class Decision(_Model):
    """A decision to propagate (who, what, when, how much)."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$")
    label_fr: str = Field(min_length=1, max_length=300)
    label_en: str | None = Field(None, max_length=300)
    actor: Party
    instrument: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    target: Party
    magnitude: Magnitude
    start: str = Field(pattern=r"^\d{4}Q[1-4]$", description="First quarter the decision applies, e.g. 2027Q1")
    duration_quarters: int | None = Field(None, ge=1, le=40, description="None = held over the whole horizon")
    horizon_quarters: int = Field(8, ge=1, le=MAX_HORIZON_QUARTERS)
    countries: list[str] = Field(default_factory=list, max_length=MAX_COUNTRIES)
    countries_note_fr: str | None = Field(None, max_length=300)
    anchors: list[str] = Field(default_factory=list, max_length=MAX_ANCHORS)
    anchor_labels: dict[str, str] = Field(default_factory=dict)
    plugin_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("countries")
    @classmethod
    def _iso3(cls, v: list[str]) -> list[str]:
        for c in v:
            if not re.match(ISO3_RE, c):
                raise ValueError(f"country {c!r} is not an ISO 3166-1 alpha-3 code")
        if len(set(v)) != len(v):
            raise ValueError("countries must be distinct")
        return v

    @field_validator("anchors")
    @classmethod
    def _anchors(cls, v: list[str]) -> list[str]:
        for a in v:
            if not re.match(ENTITY_ID_RE, a):
                raise ValueError(f"anchor {a!r} is not a KG entity id")
        return v

    @model_validator(mode="after")
    def _start(self) -> Decision:
        parse_quarter(self.start)
        return self

    @property
    def start_year(self) -> int:
        return parse_quarter(self.start)[0]


# ---------------------------------------------------------------------------------------------- instruments
@dataclass(frozen=True)
class VarRef:
    kind: str
    id: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    def as_json(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True)
class DirectChange:
    """One variable change at one geography caused by the decision (a stated scenario assumption)."""

    variable: VarRef
    geo: str
    transform: str
    unit: str
    change: Decimal
    timing: str  # step | occurrence
    start_offset: int
    duration: int | None
    basis_fr: str
    basis_en: str
    plugins: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "variable": self.variable.as_json(),
            "geo": self.geo,
            "transform": self.transform,
            "unit": self.unit,
            "change": format(self.change.normalize(), "f"),
            "timing": self.timing,
            "duration_quarters": self.duration,
            "basis_fr": self.basis_fr,
            "basis_en": self.basis_en,
            "kind": "scenario_assumption",
        }


@dataclass(frozen=True)
class Instrument:
    id: str
    label_fr: str
    label_en: str
    magnitude_units: Mapping[str, Decimal]
    templates: tuple[Mapping[str, Any], ...]
    plugins: tuple[str, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label_fr": self.label_fr,
            "label_en": self.label_en,
            "magnitude_units": sorted(self.magnitude_units),
            "direct_changes": [
                {
                    "variable": dict(t["variable"]),
                    "geo": t["geo"],
                    "transform": t["transform"],
                    "unit": t["unit"],
                    "timing": t.get("timing", "step"),
                    "basis_fr": t["basis_fr"].strip(),
                }
                for t in self.templates
            ],
            "plugins": list(self.plugins),
        }


def _load_yaml(name: str) -> Any:
    with open(REGISTRY_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def instruments() -> dict[str, Instrument]:
    doc = _load_yaml("instruments.yaml")
    out: dict[str, Instrument] = {}
    for i in doc["instruments"]:
        out[i["id"]] = Instrument(
            id=i["id"],
            label_fr=i["label_fr"],
            label_en=i["label_en"],
            magnitude_units={str(k): _dec(v, f"{i['id']} factor") for k, v in i["magnitude_units"].items()},
            templates=tuple(i["direct_changes"]),
            plugins=tuple(i.get("plugins", ())),
        )
    return out


@lru_cache(maxsize=1)
def reference_decisions() -> dict[str, Decision]:
    doc = _load_yaml("reference_decisions.yaml")
    return {d["id"]: Decision.model_validate(d) for d in doc["decisions"]}


def _geo(spec: str, d: Decision) -> list[str]:
    if spec == "WLD":
        return ["WLD"]
    if spec == "ACTOR":
        if not d.actor.geo:
            raise ValidationProblem(f"instrument {d.instrument} needs the actor's geography (actor.geo)")
        return [d.actor.geo]
    if spec == "TARGET":
        if not d.target.geo:
            raise ValidationProblem(f"instrument {d.instrument} needs the target's geography (target.geo)")
        return [d.target.geo]
    if spec == "EACH":
        if not d.countries:
            raise ValidationProblem(f"instrument {d.instrument} needs countries of interest")
        return list(d.countries)
    if re.match(GEO_RE, spec):
        return [spec]
    raise ValidationProblem(f"unknown geography {spec!r} in the instrument registry")


def direct_changes(d: Decision) -> list[DirectChange]:
    """The decision's direct changes (scenario assumptions), with exact unit factors only."""
    ins = instruments().get(d.instrument)
    if ins is None:
        raise CapabilityGap(
            f"instrument {d.instrument!r} is not in registry/predict/instruments.yaml",
            capability="predict.instrument",
            available=sorted(instruments()),
        )
    factor = ins.magnitude_units.get(d.magnitude.unit)
    if factor is None:
        raise ValidationProblem(
            f"instrument {d.instrument} takes its magnitude in {sorted(ins.magnitude_units)}, not {d.magnitude.unit!r}"
        )
    value = _dec(d.magnitude.value, "magnitude.value") * factor
    out: list[DirectChange] = []
    for t in ins.templates:
        var = VarRef(str(t["variable"]["kind"]), str(t["variable"]["id"]))
        timing = str(t.get("timing", "step"))
        change = value * int(t.get("sign", 1))
        if timing == "occurrence":
            # an occurrence is binary: the magnitude states the decision's coverage, not a number to scale by
            if change != 1:
                raise CapabilityGap(
                    f"instrument {d.instrument} propagates only a complete measure (magnitude 1 = the whole flow); "
                    "partial bans have no attributed estimate to scale with",
                    capability="predict.partial_occurrence",
                )
        for g in _geo(str(t["geo"]), d):
            out.append(
                DirectChange(
                    variable=var,
                    geo=g,
                    transform=str(t["transform"]),
                    unit=str(t["unit"]),
                    change=change,
                    timing=timing,
                    start_offset=0,
                    duration=d.duration_quarters,
                    basis_fr=" ".join(str(t["basis_fr"]).split()),
                    basis_en=" ".join(str(t["basis_en"]).split()),
                    plugins=ins.plugins,
                )
            )
    return out
