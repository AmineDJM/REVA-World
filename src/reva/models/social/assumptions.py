"""Employment-response assumption grid from the literature registry (agent LIT).

Source: ``registry/assumptions/retirement_employment_response.yaml`` (curated by
agent LIT, ``maintainer_review_pending``). These numbers are **stated scenario
assumptions with citations**, never reviewed evidence claims.

Only entries whose ``verification_status`` is ``verified_from_text`` and whose
values used here are themselves ``verified_from_text`` enter the grid. From each
entry the module takes, per recorded sex where the entry is sex-specific:

* ``s`` - the derived *employment share of the pension-receipt (or retirement)
  drop*: of the persons kept out of pension receipt by a higher eligibility age,
  the share employed instead (LIT ``derived`` items named
  ``employment_share_of_*``; the main estimator, not a robustness variant);
* ``rho`` - the size of that drop (the derived item's denominator value, as a
  proportion): the share of an affected cohort-age cell that would otherwise
  receive a pension.

Only ``s`` enters the model. The pension-receipt drop of a country is taken
from that country's own data (base-year LFS inactivity, see the model), never
from a foreign study; the study's ``rho`` is kept for documentation. Estimates
are also tagged with the reform they were estimated on, so a back-test can
exclude every estimate whose estimation sample overlaps the reform it tests. Entries without both numbers (e.g. effects on exit ages in years)
are listed as ``excluded`` with the reason. A women-only estimate applied to men
(or a single pooled estimate applied to both sexes) is flagged
``sex_transfer``. A reference point with no employment response (``s = 0``) is
added, labelled as a reference, not literature.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash

ASSUMPTIONS_FILE = Path(__file__).resolve().parents[4] / "registry" / "assumptions" / \
    "retirement_employment_response.yaml"
VERIFIED = "verified_from_text"
NO_RESPONSE_ID = "reference_no_employment_response"
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
SEXES = ("F", "M")


@dataclass(frozen=True)
class GridPoint:
    point_id: str
    label: str
    share_employed: Mapping[str, Decimal]  # s by sex
    pension_drop: Mapping[str, Decimal]  # rho by sex (proportion)
    country: str | None
    doi: str | None
    citation: str
    kind: str  # "literature" | "reference"
    evidence: tuple[str, ...] = ()
    sex_transfer: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def study_employment_effect(self, sex: str) -> Decimal:
        """The study's own employment effect for a fully affected cell (s * rho of the study; documentation)."""
        return self.share_employed[sex] * self.pension_drop[sex]

    def overlaps(self, country: str, first_year: int, last_year: int) -> bool:
        """True when the estimate may come from the same country's reforms in or before the window
        (conservative: any own-country estimate whose period text mentions a year <= last_year)."""
        if self.country != country:
            return False
        years = [int(y) for y in _YEAR_RE.findall(" ".join(str(v) for v in self.provenance.values()))]
        return not years or min(years) <= last_year

    def as_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id, "label": self.label, "kind": self.kind, "country": self.country,
            "doi": self.doi, "citation": self.citation,
            "share_employed": {k: str(v) for k, v in self.share_employed.items()},
            "pension_drop": {k: str(v) for k, v in self.pension_drop.items()},
            "study_employment_effect_full_cell": {k: str(self.study_employment_effect(k)) for k in SEXES},
            "pension_drop_used_by_model": "own-country data (not this study's value)",
            "evidence": list(self.evidence), "sex_transfer": self.sex_transfer,
            "provenance": dict(self.provenance),
            "status": "stated scenario assumption with citation (not a reviewed evidence claim)",
        }


@dataclass(frozen=True)
class AssumptionGrid:
    points: tuple[GridPoint, ...]
    excluded: tuple[Mapping[str, Any], ...]
    source_digest: str
    curation: Mapping[str, Any]

    def point(self, point_id: str) -> GridPoint:
        for p in self.points:
            if p.point_id == point_id:
                return p
        raise ValidationProblem(f"unknown employment-response grid point {point_id!r}",
                                known=[p.point_id for p in self.points])

    def literature(self) -> tuple[GridPoint, ...]:
        return tuple(p for p in self.points if p.kind == "literature")

    def default_central(self, iso3: str) -> GridPoint | None:
        """The own-country literature point when exactly one exists (else None: the caller must choose)."""
        own = [p for p in self.literature() if p.country == iso3]
        return own[0] if len(own) == 1 else None

    def as_dict(self) -> dict[str, Any]:
        return {"source_file": str(ASSUMPTIONS_FILE.name), "source_digest": self.source_digest,
                "curation": dict(self.curation), "points": [p.as_dict() for p in self.points],
                "excluded": [dict(e) for e in self.excluded]}


def _proportion(v: Mapping[str, Any]) -> Decimal:
    val = Decimal(str(v["value"]))
    unit = v.get("unit")
    if unit == "percentage_points":
        return abs(val) / Decimal(100)
    if unit in ("proportion", "share"):
        return abs(val)
    raise ValidationProblem(f"value {v.get('name')!r} has unit {unit!r}, not a proportion or percentage points")


def _sex_of(name: str) -> str | None:
    if name.endswith("_women") or "_women_" in name:
        return "F"
    if name.endswith("_men") or "_men_" in name:
        return "M"
    return None


def build_grid(doc: Mapping[str, Any]) -> AssumptionGrid:
    points: list[GridPoint] = []
    excluded: list[dict[str, Any]] = []
    for e in doc.get("entries") or []:
        eid = str(e.get("id"))
        if e.get("verification_status") != VERIFIED:
            excluded.append({"id": eid, "reason": f"verification_status {e.get('verification_status')!r}"})
            continue
        values = {v["name"]: v for v in e.get("values") or []}
        derived = [d for d in e.get("derived") or [] if str(d.get("name", "")).startswith("employment_share_of_")
                   and "staggered" not in str(d.get("name"))]
        if not derived:
            excluded.append({"id": eid, "reason": "no derived employment share of the pension/retirement drop "
                             "(needs both an employment effect and a pension-receipt effect)"})
            continue
        s_by: dict[str, Decimal] = {}
        rho_by: dict[str, Decimal] = {}
        evidence: set[str] = set()
        bad = None
        for d in derived:
            num, den = values.get(d.get("numerator")), values.get(d.get("denominator"))
            if num is None or den is None:
                bad = f"derived {d.get('name')} refers to unknown values"
                break
            if num.get("verification") != VERIFIED or den.get("verification") != VERIFIED:
                bad = f"values of {d.get('name')} are not verified_from_text"
                break
            rho = _proportion(den)
            s = Decimal(str(d["value"]))
            if not (Decimal(0) <= s <= Decimal(1)) or not (Decimal(0) < rho <= Decimal(1)):
                bad = f"derived {d.get('name')} outside [0, 1]"
                break
            sex = _sex_of(str(d["name"]))
            for sx in ((sex,) if sex else SEXES):
                s_by[sx], rho_by[sx] = s, rho
            evidence.update({str(num.get("evidence")), str(den.get("evidence"))})
        if bad:
            excluded.append({"id": eid, "reason": bad})
            continue
        transfer = None
        if set(s_by) != set(SEXES):
            only = next(iter(s_by))
            transfer = f"estimate for sex {only} only; applied to both sexes"
            for sx in SEXES:
                s_by.setdefault(sx, s_by[only])
                rho_by.setdefault(sx, rho_by[only])
        elif len(derived) == 1:
            transfer = "single estimate (sexes pooled or one population) applied to both sexes"
        points.append(GridPoint(
            point_id=eid, label=str(e.get("citation", eid)).split(".")[0], share_employed=s_by, pension_drop=rho_by,
            country=e.get("country"), doi=e.get("doi"), citation=str(e.get("citation")), kind="literature",
            evidence=tuple(sorted(evidence)), sex_transfer=transfer,
            provenance={"policy_change": e.get("policy_change"), "population": e.get("population"),
                        "period": e.get("period"),
                        "design": e.get("design"), "text_sources": [t.get("id") for t in e.get("text_sources") or []],
                        "derived": [d.get("name") for d in derived]}))
    if not points:
        raise ValidationProblem("the literature registry contains no verified employment-response entry")
    return AssumptionGrid(tuple(points), tuple(excluded), logical_hash(doc), dict(doc.get("curation") or {}))


def no_response_point(rho_from: GridPoint) -> GridPoint:
    """Reference point: the eligibility change moves nobody into employment (``s = 0``); pension drop of
    ``rho_from`` so the recipients change stays comparable."""
    return GridPoint(
        point_id=NO_RESPONSE_ID, label="Reference: no employment response (not literature)",
        share_employed={s: Decimal(0) for s in SEXES}, pension_drop=dict(rho_from.pension_drop), country=None,
        doi=None, citation=f"reference point (pension drop taken from {rho_from.point_id})", kind="reference")


@lru_cache(maxsize=1)
def load_grid(path: str | None = None) -> AssumptionGrid:
    p = Path(path) if path else ASSUMPTIONS_FILE
    if not p.exists():
        raise ValidationProblem(f"literature assumption registry {p.name} is missing")
    with open(p, encoding="utf-8") as f:
        return build_grid(yaml.safe_load(f))


def select_points(grid: AssumptionGrid, ids: Sequence[str] | None) -> tuple[GridPoint, ...]:
    if ids is None:
        return grid.literature()
    return tuple(grid.point(i) for i in ids)
