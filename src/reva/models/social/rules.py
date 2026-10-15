"""Retirement-age rule tables (``registry/rules/retirement_age/<ISO3>.yaml``).

A rule table is legal information transcribed by an agent from archived source
text; every row cites a source file in ``registry/rules/sources`` and quotes the
verbatim locator, which :func:`load_rule_table` checks against the archived text
(whitespace-normalised). Tables carry ``review_status: maintainer_review_pending``
until a maintainer reviews them; nothing here records a human review.

Rows give an age (years + months) by **birth cohort** (inclusive birth-date
bounds) or by **claim year** (calendar year in which the age is reached, e.g.
the Dutch AOW age). Rows are grouped into named *paths* (legal states such as
"law in force" or an explicitly labelled counterfactual); within one path, age
kind and track, rows must be disjoint. Coverage gaps are allowed and are
reported (a model refuses to compute a cell outside coverage, never guesses).

Eligibility arithmetic (:func:`ineligible_share`): for an age group observed
in calendar year ``t`` the share of person-time below the eligibility age is
computed exactly on a 12 x 12 grid: observation month (uniform over the year)
x month of exact age inside each completed year of age (uniform), i.e. births
are assumed uniform within each month-cohort and the population uniform across
months of age inside a completed year. A person is eligible from the month the
eligibility age is reached. The result is an exact ``Fraction``.
"""

from __future__ import annotations

import calendar
import html
import itertools
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import logical_hash

RULES_ROOT = Path(__file__).resolve().parents[4] / "registry" / "rules"
RULE_DIR = RULES_ROOT / "retirement_age"
SOURCE_DIR = RULES_ROOT / "sources"
SCHEMA_ID = "reva-retirement-age-rules/1"
AGE_KINDS = ("minimum_pension_age", "full_rate_age", "normal_pension_age", "early_pension_age_long_insured",
             "state_pension_age")
DERIVATIONS = ("verbatim", "counterfactual_extension", "secondary_table")
SOURCE_KINDS = ("official_consolidated_text", "official_information_page", "secondary_consolidated_mirror",
                "secondary_statutory_fund_table")
PATH_STATUSES = ("in_force", "enacted_superseded", "enacted_as_amended", "counterfactual")
REVIEW_STATUS = "maintainer_review_pending"
TRANSCRIPTION_NOTE = "transcribed from law text by an agent; maintainer review pending"


class RuleCoverageGap(CapabilityGap):
    """A cohort/year needed by a computation is outside the rule table's coverage."""

    code = "retirement_rule_coverage_gap"


# --------------------------------------------------------------------------- source text
def normalise_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ").replace("|", " ")).strip()


def html_text(raw: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


@lru_cache(maxsize=64)
def source_text(file_name: str) -> str:
    """Whitespace-normalised text of an archived source file (HTML, text; PDF via its committed
    ``.pdftotext.txt`` extraction)."""
    p = SOURCE_DIR / file_name
    if p.suffix == ".pdf":
        p = p.with_suffix(".pdftotext.txt")
    data = p.read_bytes()
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        m = re.search(rb"charset=[\"']?([A-Za-z0-9_\-]+)", data[:4096])
        if m is None:
            raise ValidationProblem(f"archived source {file_name} is not UTF-8 and declares no charset") from None
        raw = data.decode(m.group(1).decode("ascii"))
    if p.suffix in (".html", ".htm"):
        raw = html_text(raw)
    return normalise_text(raw)


# --------------------------------------------------------------------------- records
@dataclass(frozen=True)
class RuleRow:
    row_id: str
    paths: tuple[str, ...]
    age_kind: str
    basis: str  # birth_cohort | claim_year
    age_months: int
    born_from: date | None = None
    born_to: date | None = None
    year_from: int | None = None
    year_to: int | None = None
    track: str = "general"
    condition: str | None = None
    citation: Mapping[str, Any] = field(default_factory=dict)
    derivation: str = "verbatim"
    note: str | None = None

    def covers_birth(self, y: int, m: int) -> bool:
        first, last = date(y, m, 1), _month_end(y, m)
        lo_ok = self.born_from is None or self.born_from <= first
        hi_ok = self.born_to is None or last <= self.born_to
        if lo_ok and hi_ok:
            return True
        overlap = (self.born_from is None or self.born_from <= last) and (self.born_to is None or first <= self.born_to)
        if overlap:
            raise ValidationProblem(f"rule row {self.row_id} splits birth month {y}-{m:02d}; month-level cohorts only")
        return False

    def covers_year(self, y: int) -> bool:
        return (self.year_from is None or self.year_from <= y) and (self.year_to is None or y <= self.year_to)

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id, "paths": list(self.paths), "age_kind": self.age_kind, "basis": self.basis,
            "age": {"years": self.age_months // 12, "months": self.age_months % 12},
            "born_from": self.born_from.isoformat() if self.born_from else None,
            "born_to": self.born_to.isoformat() if self.born_to else None,
            "year_from": self.year_from, "year_to": self.year_to, "track": self.track, "condition": self.condition,
            "citation": dict(self.citation), "derivation": self.derivation, "note": self.note,
        }


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


@dataclass(frozen=True)
class RulePath:
    path_id: str
    label: str
    status: str
    description: str


@dataclass(frozen=True)
class RuleTable:
    country: str
    version: str
    review_status: str
    transcription: str
    model_lever: Mapping[str, Any]
    sources: Mapping[str, Mapping[str, Any]]
    paths: Mapping[str, RulePath]
    rows: tuple[RuleRow, ...]
    raw: Mapping[str, Any]

    def digest(self) -> str:
        return logical_hash(json.loads(json.dumps(self.raw, default=str)))

    def rows_for(self, path: str, age_kind: str, track: str = "general") -> tuple[RuleRow, ...]:
        if path not in self.paths:
            raise ValidationProblem(f"{self.country}: unknown rule path {path!r}", known=sorted(self.paths))
        return tuple(r for r in self.rows if path in r.paths and r.age_kind == age_kind and r.track == track)

    def age_months_at(self, path: str, age_kind: str, birth_y: int, birth_m: int, claim_year: int,
                      track: str = "general") -> int:
        """Eligibility age in months for a birth month and claim year; RuleCoverageGap outside coverage."""
        hits = []
        for r in self.rows_for(path, age_kind, track):
            if r.basis == "birth_cohort" and r.covers_birth(birth_y, birth_m):
                hits.append(r)
            elif r.basis == "claim_year" and r.covers_year(claim_year):
                hits.append(r)
        if len(hits) > 1:
            raise ValidationProblem(f"{self.country}/{path}: rows {[h.row_id for h in hits]} overlap")
        if not hits:
            raise RuleCoverageGap(
                f"{self.country} rule path {path!r} ({age_kind}, track {track}) has no sourced row for birth "
                f"{birth_y}-{birth_m:02d} / claim year {claim_year}",
                country=self.country, path=path, age_kind=age_kind, birth=f"{birth_y}-{birth_m:02d}",
                claim_year=claim_year)
        return hits[0].age_months

    def as_dict(self) -> dict[str, Any]:
        return {"country": self.country, "version": self.version, "review_status": self.review_status,
                "transcription": self.transcription, "digest": self.digest(),
                "model_lever": dict(self.model_lever),
                "paths": {k: vars(v) for k, v in self.paths.items()},
                "rows": [r.as_dict() for r in self.rows], "sources": {k: dict(v) for k, v in self.sources.items()}}


# --------------------------------------------------------------------------- loading
def _age_months(d: Mapping[str, Any], where: str) -> int:
    y, m = d.get("years"), d.get("months", 0)
    if not (isinstance(y, int) and isinstance(m, int) and 40 <= y <= 80 and 0 <= m <= 11):
        raise ValidationProblem(f"{where}: age must be {{years: 40..80, months: 0..11}}")
    return 12 * y + m


def _date(v: Any, where: str) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise ValidationProblem(f"{where}: invalid date {v!r}") from None


def parse_rule_table(doc: Mapping[str, Any], *, check_locators: bool = True) -> RuleTable:
    if doc.get("schema") != SCHEMA_ID:
        raise ValidationProblem(f"rule table schema must be {SCHEMA_ID}")
    country = str(doc.get("country", ""))
    if not re.fullmatch(r"[A-Z]{3}", country):
        raise ValidationProblem("country must be an ISO3 code")
    if doc.get("review_status") != REVIEW_STATUS:
        raise ValidationProblem(f"{country}: review_status must be {REVIEW_STATUS!r} (no agent records a review)")
    if doc.get("transcription") != TRANSCRIPTION_NOTE:
        raise ValidationProblem(f"{country}: transcription note must read {TRANSCRIPTION_NOTE!r}")
    sources: dict[str, Mapping[str, Any]] = {}
    for s in doc.get("sources") or []:
        sid = s.get("source_id")
        for k in ("source_id", "title", "url", "kind", "retrieved_at", "archived_file"):
            if not s.get(k):
                raise ValidationProblem(f"{country}: source {sid} lacks {k}")
        if s["kind"] not in SOURCE_KINDS:
            raise ValidationProblem(f"{country}: source {sid} kind {s['kind']!r} not in {SOURCE_KINDS}")
        if not (SOURCE_DIR / s["archived_file"]).exists():
            raise ValidationProblem(f"{country}: archived file {s['archived_file']} missing")
        sources[sid] = s
    paths: dict[str, RulePath] = {}
    for p in doc.get("paths") or []:
        if p.get("status") not in PATH_STATUSES:
            raise ValidationProblem(f"{country}: path {p.get('path_id')} has invalid status")
        paths[p["path_id"]] = RulePath(p["path_id"], p["label"], p["status"], p.get("description", ""))
    lever = dict(doc.get("model_lever") or {})
    if lever:
        if lever.get("age_kind") not in AGE_KINDS or not lever.get("rationale"):
            raise ValidationProblem(f"{country}: model_lever needs age_kind and rationale")
    rows: list[RuleRow] = []
    seen: set[str] = set()
    for r in doc.get("rows") or []:
        rid = str(r.get("row_id", ""))
        if not rid or rid in seen:
            raise ValidationProblem(f"{country}: missing or duplicate row_id {rid!r}")
        seen.add(rid)
        cit = r.get("citation") or {}
        if not (cit.get("source_id") in sources and cit.get("legal_reference") and cit.get("locator")):
            raise ValidationProblem(f"{country}: row {rid} must cite a declared source with legal_reference and "
                                    "a verbatim locator")
        if check_locators:
            src = source_text(sources[cit["source_id"]]["archived_file"])
            if normalise_text(str(cit["locator"])) not in src:
                raise ValidationProblem(f"{country}: row {rid} locator not found verbatim in "
                                        f"{sources[cit['source_id']]['archived_file']}",
                                        locator=cit["locator"])
        derivation = r.get("derivation", "verbatim")
        if derivation not in DERIVATIONS:
            raise ValidationProblem(f"{country}: row {rid} derivation {derivation!r} invalid")
        if derivation != "verbatim" and not r.get("note"):
            raise ValidationProblem(f"{country}: row {rid} ({derivation}) must explain itself in note")
        rpaths = tuple(r.get("paths") or ())
        if not rpaths or any(p not in paths for p in rpaths):
            raise ValidationProblem(f"{country}: row {rid} names unknown paths {rpaths}")
        if r.get("age_kind") not in AGE_KINDS:
            raise ValidationProblem(f"{country}: row {rid} age_kind invalid")
        basis = r.get("basis")
        if basis == "birth_cohort":
            row = RuleRow(rid, rpaths, r["age_kind"], basis, _age_months(r.get("age") or {}, rid),
                          born_from=_date(r.get("born_from"), rid), born_to=_date(r.get("born_to"), rid),
                          track=r.get("track", "general"), condition=r.get("condition"), citation=dict(cit),
                          derivation=derivation, note=r.get("note"))
            if row.born_from and row.born_to and row.born_to < row.born_from:
                raise ValidationProblem(f"{country}: row {rid} born_to < born_from")
        elif basis == "claim_year":
            row = RuleRow(rid, rpaths, r["age_kind"], basis, _age_months(r.get("age") or {}, rid),
                          year_from=r.get("year_from"), year_to=r.get("year_to"),
                          track=r.get("track", "general"), condition=r.get("condition"), citation=dict(cit),
                          derivation=derivation, note=r.get("note"))
            if row.year_from is not None and row.year_to is not None and row.year_to < row.year_from:
                raise ValidationProblem(f"{country}: row {rid} year_to < year_from")
        else:
            raise ValidationProblem(f"{country}: row {rid} basis must be birth_cohort or claim_year")
        rows.append(row)
    table = RuleTable(country, str(doc.get("version", "")), doc["review_status"], doc["transcription"], lever,
                      sources, paths, tuple(rows), dict(doc))
    _check_disjoint(table)
    return table


def _intervals(r: RuleRow) -> tuple[float, float]:
    if r.basis == "birth_cohort":
        lo = r.born_from.toordinal() if r.born_from else float("-inf")
        hi = r.born_to.toordinal() if r.born_to else float("inf")
    else:
        lo = r.year_from if r.year_from is not None else float("-inf")
        hi = r.year_to if r.year_to is not None else float("inf")
    return lo, hi


def _check_disjoint(t: RuleTable) -> None:
    groups: dict[tuple[str, str, str], list[RuleRow]] = {}
    for r in t.rows:
        for p in r.paths:
            groups.setdefault((p, r.age_kind, r.track), []).append(r)
    for key, rows in groups.items():
        if len({r.basis for r in rows}) > 1:
            raise ValidationProblem(f"{t.country}: path/age kind {key} mixes birth-cohort and claim-year rows")
        ivs = sorted((_intervals(r), r.row_id) for r in rows)
        for (a, ida), (b, idb) in itertools.pairwise(ivs):
            if b[0] <= a[1]:
                raise ValidationProblem(f"{t.country}: rows {ida} and {idb} overlap in {key}")


def available_countries() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in RULE_DIR.glob("*.yaml") if re.fullmatch(r"[A-Z]{3}", p.stem)))


@lru_cache(maxsize=32)
def load_rule_table(iso3: str) -> RuleTable:
    p = RULE_DIR / f"{iso3}.yaml"
    if not p.exists():
        raise CapabilityGap(f"no sourced retirement-age rule table for {iso3}", capability="social.retirement_age",
                            country=iso3, reason=gap_reason(iso3))
    with open(p, encoding="utf-8") as f:
        return parse_rule_table(yaml.safe_load(f))


@lru_cache(maxsize=1)
def _gaps() -> Mapping[str, Any]:
    p = RULE_DIR / "capability_gaps.yaml"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {g["country"]: g for g in (yaml.safe_load(f) or {}).get("gaps", [])}


def gap_reason(iso3: str) -> str:
    g = _gaps().get(iso3)
    return str(g["reason"]) if g else "no rule table has been transcribed from an archived legal source"


# --------------------------------------------------------------------------- eligibility paths
@dataclass(frozen=True)
class TablePath:
    """Eligibility ages of one named path of a rule table (one age kind and track)."""

    table: RuleTable
    path: str
    age_kind: str
    track: str = "general"

    def __post_init__(self) -> None:
        if not self.table.rows_for(self.path, self.age_kind, self.track):
            raise ValidationProblem(f"{self.table.country}: path {self.path!r} has no {self.age_kind} rows for "
                                    f"track {self.track!r}")

    def age_months(self, birth_y: int, birth_m: int, claim_year: int) -> int:
        return self.table.age_months_at(self.path, self.age_kind, birth_y, birth_m, claim_year, self.track)

    def describe(self) -> dict[str, Any]:
        p = self.table.paths[self.path]
        return {"kind": "rule_table_path", "country": self.table.country, "path": self.path, "label": p.label,
                "status": p.status, "age_kind": self.age_kind, "track": self.track,
                "rule_table_version": self.table.version, "rule_table_digest": self.table.digest(),
                "review_status": self.table.review_status}


@dataclass(frozen=True)
class ShiftedPath:
    """A user-specified change: ``base`` plus ``min(shift_months, step * (c - first + 1))`` months for birth
    years ``c >= first_birth_year`` (``step = 0``: the full shift from the first cohort). A scenario
    assumption, not law."""

    base: TablePath
    shift_months: int
    first_birth_year: int
    phase_in_months_per_cohort: int = 0

    def __post_init__(self) -> None:
        if not -60 <= self.shift_months <= 60 or self.shift_months == 0:
            raise ValidationProblem("shift_months must be a non-zero integer in [-60, 60]")
        if not 0 <= self.phase_in_months_per_cohort <= 24:
            raise ValidationProblem("phase_in_months_per_cohort must be in [0, 24]")
        if not 1900 <= self.first_birth_year <= 2100:
            raise ValidationProblem("first_birth_year out of range")

    def extra_months(self, birth_y: int) -> int:
        if birth_y < self.first_birth_year:
            return 0
        if self.phase_in_months_per_cohort == 0:
            return self.shift_months
        step = self.phase_in_months_per_cohort * (birth_y - self.first_birth_year + 1)
        return max(self.shift_months, -step) if self.shift_months < 0 else min(self.shift_months, step)

    def age_months(self, birth_y: int, birth_m: int, claim_year: int) -> int:
        return self.base.age_months(birth_y, birth_m, claim_year) + self.extra_months(birth_y)

    def describe(self) -> dict[str, Any]:
        return {"kind": "shift_of_rule_table_path", "base": self.base.describe(), "shift_months": self.shift_months,
                "first_birth_year": self.first_birth_year,
                "phase_in_months_per_cohort": self.phase_in_months_per_cohort,
                "status": "scenario_assumption (not law)"}


AgePath = TablePath | ShiftedPath


# --------------------------------------------------------------------------- eligibility arithmetic
def ineligible_share_single_age(path: AgePath, year: int, age: int) -> Fraction:
    """Exact share of person-time at completed age ``age`` in calendar ``year`` below the eligibility age."""
    below = 0
    for m in range(12):  # observation month in year
        for k in range(12):  # month of exact age inside the completed year
            b = 12 * (year - age) + m - k  # birth month index (months since year 0)
            by, bm = divmod(b, 12)
            if 12 * age + k < path.age_months(by, bm + 1, year):
                below += 1
    return Fraction(below, 144)


def ineligible_share(path: AgePath, year: int, ages: Iterable[int],
                     weights: Mapping[int, Fraction] | None = None) -> Fraction:
    """Share for an age group (``weights`` by single age; uniform when ``None``)."""
    ages = tuple(ages)
    w = weights or {a: Fraction(1) for a in ages}
    total = sum((w[a] for a in ages), Fraction(0))
    if total <= 0:
        raise ValidationProblem("age-group weights must be positive")
    return sum((w[a] * ineligible_share_single_age(path, year, a) for a in ages), Fraction(0)) / total


def coverage_problems(path: AgePath, years: Sequence[int], ages: Sequence[int]) -> list[dict[str, Any]]:
    """Typed list of (year, age) cells the path cannot answer (empty when fully covered)."""
    out: list[dict[str, Any]] = []
    for y in years:
        for a in ages:
            try:
                ineligible_share_single_age(path, y, a)
            except RuleCoverageGap as e:
                out.append({"code": e.code, "year": y, "age": a, **{k: v for k, v in e.details.items()
                                                                     if k in ("country", "path", "birth",
                                                                              "claim_year")}})
    return out
