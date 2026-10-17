"""Conflict-risk question families (V6 PRED-CONFLICT): country-year state-based fatalities, onset, escalation.

Every family reads one atlas cell: the UCDP *Country-Year Dataset on Organized Violence within Country Borders*
column ``sb_total_deaths_best`` (atlas ``atlas.ucdp.orgvio_cy``, indicator verbatim), i.e. UCDP's best estimate of
battle-related deaths in state-based armed conflict located within the country's borders in the calendar year.
Binary families apply UCDP's own numeric cut-offs (25 and 1,000 battle-related deaths in one calendar year) to that
**country-year total** -- a REVA rule borrowing UCDP's thresholds, not UCDP's conflict-level coding (UCDP applies
them per conflict/dyad; a country can host several conflicts). The definitions below are quoted verbatim from the
UCDP definitions page (fetched 2026-10-17, sha256 of the HTML in ``DEFINITIONS_SOURCE``).

These are **risk estimates for a country and a calendar year**, never predictions of a specific attack, event,
actor or person; no question targets an individual.

Resolution: the first version of ``atlas.ucdp.orgvio_cy`` registered after the seal that publishes the
country-year (UCDP releases a new annual version around June; 26.1 covers 1989-2025). The registry's generic
resolver (``reva.evaluation.registry.service.resolve_due``) resolves these questions from the frozen family.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DATASET_KEY = "atlas.ucdp.orgvio_cy"
INDICATOR = "sb_total_deaths_best"
OWNER = "PRED-CONFLICT"
UNIT = "battle-related deaths (UCDP best estimate)"
DEFINITIONS_SOURCE = (
    "UCDP definitions page https://www.uu.se/en/department/peace-and-conflict-research/research/ucdp/ucdp-definitions "
    "(fetched 2026-10-17, HTML sha256 b938d7a6352b1229a3c45bc4ea0d0e7c1124d1fc77bc40d47de71c0587159fd0); atlas table "
    "atlas.ucdp.orgvio_cy (UCDP Country-Year Dataset on Organized Violence within Country Borders 26.1)"
)
Q_STATE_BASED = (
    "A state-based armed conflict is a contested incompatibility that concerns government and/or territory where the "
    "use of armed force between two parties, of which at least one is the government of a state, results in at "
    "least 25 battle-related deaths in one calendar year."
)
Q_BRD = (
    "Counted as battle-related deaths is the use of armed force between warring parties in a conflict dyad, be it "
    "state-based or non-state, resulting in deaths."
)
Q_BEST = "The best estimate consists of the aggregated most reliable number of deaths."
Q_WAR = "War: At least 1000 battle-related deaths in one calendar year."
RISK_NOTE = (
    "Country-year risk estimate from aggregate statistics; not a prediction of any specific attack, event, actor or "
    "person."
)
PUBLICATION_LAG_DAYS = 180  # UCDP annual release around June of the following year
VOID_AFTER_DAYS = 640  # about 21 months after the year ends (one missed annual release tolerated)

Kind = Literal["quantity", "binary"]


@dataclass(frozen=True)
class ConflictFamily:
    key: str
    kind: Kind
    threshold: int | None  # binary: event = deaths strictly above threshold (integers: > 24 <=> >= 25)
    condition: tuple[int, int] | None  # asked only when the last observed value lies in [lo, hi]
    label_fr: str
    label_en: str
    rule_en: str

    def event(self, deaths: float) -> int:
        assert self.threshold is not None
        return 1 if deaths > self.threshold else 0

    def applies(self, last: float) -> bool:
        return self.condition is None or self.condition[0] <= last <= self.condition[1]

    def definition(self) -> str:
        quotes = f'UCDP: "{Q_STATE_BASED}" "{Q_BRD}" "{Q_BEST}"'
        if self.threshold == 999:
            quotes += f' "{Q_WAR}"'
        return f"{self.rule_en} {quotes} {RISK_NOTE}"


FAMILIES: tuple[ConflictFamily, ...] = (
    ConflictFamily(
        "ucdp.sb_deaths_cy",
        "quantity",
        None,
        None,
        "Morts au combat, conflits armés étatiques (total annuel du pays, UCDP)",
        "State-based battle-related deaths (country-year total, UCDP)",
        "Value: UCDP best estimate of battle-related deaths in state-based armed conflict located within the "
        "country's borders in the calendar year (orgvio_cy column sb_total_deaths_best).",
    ),
    ConflictFamily(
        "ucdp.sb_conflict25_cy",
        "binary",
        24,
        None,
        "Au moins 25 morts au combat (conflits étatiques) dans l'année",
        "At least 25 state-based battle-related deaths in the year",
        "Event: the country-year total sb_total_deaths_best is at least 25 (REVA rule applying UCDP's 25-deaths "
        "cut-off to the country total).",
    ),
    ConflictFamily(
        "ucdp.sb_onset25_cy",
        "binary",
        24,
        (0, 24),
        "Déclenchement : au moins 25 morts au combat après une année sous ce seuil",
        "Onset: at least 25 state-based battle-related deaths after a year below that level",
        "Event: the country-year total is at least 25; asked only for countries whose last published total was "
        "below 25 (REVA onset rule).",
    ),
    ConflictFamily(
        "ucdp.sb_war1000_cy",
        "binary",
        999,
        None,
        "Au moins 1 000 morts au combat (conflits étatiques) dans l'année",
        "At least 1,000 state-based battle-related deaths in the year",
        "Event: the country-year total sb_total_deaths_best is at least 1000 (REVA rule applying UCDP's war "
        "cut-off to the country total).",
    ),
    ConflictFamily(
        "ucdp.sb_escalation1000_cy",
        "binary",
        999,
        (25, 999),
        "Escalade : au moins 1 000 morts au combat après une année entre 25 et 999",
        "Escalation: at least 1,000 state-based battle-related deaths after a year with 25-999",
        "Event: the country-year total is at least 1000; asked only for countries whose last published total was "
        "between 25 and 999 (REVA escalation rule).",
    ),
)
BY_KEY = {f.key: f for f in FAMILIES}


def family_doc(fam: ConflictFamily, countries: list[str]) -> dict[str, Any]:
    """The frozen family document stored in a batch manifest; it parses with the registry's family validator
    (``reva.evaluation.registry.specs._family``), which the generic resolver uses."""
    q: dict[str, Any] = {"type": fam.kind, "horizons": [0, 1]}
    doc: dict[str, Any] = {
        "key": fam.key,
        "concept_id": None,
        "labels": {"fr": fam.label_fr, "en": fam.label_en},
        "definition": fam.definition(),
        "definition_source": DEFINITIONS_SOURCE,
        "dataset_key": DATASET_KEY,
        "indicator": INDICATOR,
        "frequency": "A",
        "value_rule": "cell",
        "unit": UNIT,
        "countries": sorted(countries),
        "publication_lag_days": PUBLICATION_LAG_DAYS,
        "void_after_days": VOID_AFTER_DAYS,
        "backtest_lag_periods": 1,
        "resolution_note": "First atlas.ucdp.orgvio_cy version registered after the seal that publishes the "
        "country-year (UCDP annual release, around June of the following year). " + RISK_NOTE,
        "owner_agent": OWNER,
        "conflict": {
            "condition_last_between": list(fam.condition) if fam.condition else None,
            "event_rule": None if fam.threshold is None else f"deaths > {fam.threshold}",
        },
    }
    if fam.kind == "binary":
        q["threshold_rule"] = "above_fixed"
        doc["threshold"] = str(fam.threshold)
    doc["questions"] = [q]
    return doc


def question_texts(fam: ConflictFamily, geo_fr: str, geo_en: str, iso3: str, year: str, last: dict[str, str]) -> tuple[str, str]:
    lp, lv = last["period"], last["value"]
    if fam.kind == "quantity":
        return (
            f"Estimation de risque : combien de morts au combat dans des conflits armés étatiques (UCDP, estimation "
            f"« best », total annuel) seront recensés pour {geo_fr} ({iso3}) en {year} ? (dernier total publié : "
            f"{lv} en {lp})",
            f"Risk estimate: how many battle-related deaths in state-based armed conflict (UCDP best estimate, "
            f"country-year total) will be recorded for {geo_en} ({iso3}) in {year}? (latest published total: {lv} "
            f"in {lp})",
        )
    n = "25" if fam.threshold == 24 else "1 000"
    n_en = "25" if fam.threshold == 24 else "1,000"
    cond_fr = cond_en = ""
    if fam.condition:
        cond_fr = f" (question posée car le total publié pour {lp} était de {lv})"
        cond_en = f" (asked because the published total for {lp} was {lv})"
    return (
        f"Estimation de risque : le total UCDP {year} des morts au combat dans des conflits armés étatiques "
        f"localisés dans {geo_fr} ({iso3}) atteindra-t-il au moins {n} ?{cond_fr}",
        f"Risk estimate: will UCDP's {year} total of battle-related deaths in state-based armed conflict located in "
        f"{geo_en} ({iso3}) reach at least {n_en}?{cond_en}",
    )


__all__ = [
    "BY_KEY",
    "DATASET_KEY",
    "FAMILIES",
    "INDICATOR",
    "RISK_NOTE",
    "ConflictFamily",
    "family_doc",
    "question_texts",
]
