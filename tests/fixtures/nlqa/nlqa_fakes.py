"""In-memory fakes of the NLQA ports + a small TEST FIXTURE world (origin ``test_fixture``).

The statements and cells below are shaped like real KG rows / atlas cells so the executor and
composer can be tested offline. They are fixtures, not data: statement ids end in ``FIXTURE``,
raw sha256s are fixed patterns, and invented items use QIDs from ``Q9000000x``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from reva.nlqa.catalog import build_catalog, fold
from reva.nlqa.ports import (
    AtlasCell,
    AtlasResult,
    Backends,
    CapabilityUnavailable,
    Concept,
    CountryLink,
    Entity,
    EntityMatch,
    KgStatement,
    TimeValue,
    is_projection,
)

HERE = Path(__file__).resolve().parent
CATALOG_DIR = HERE / "catalog"
RECORDED_DIR = HERE / "recorded"
RAW_KG = "a" * 64
RAW_WDI = "b" * 64
RAW_WPP = "c" * 64
RAW_ILO = "d" * 64


def catalog():
    return build_catalog(catalog_dir=CATALOG_DIR)


E = {
    "Q7322": Entity(
        "Q7322",
        {"en": "Christopher Columbus", "fr": "Christophe Colomb"},
        {"en": "Italian explorer", "fr": "navigateur"},
        "person",
        200,
    ),
    "Q155": Entity("Q155", {"en": "Brazil", "fr": "Brésil"}, {}, "country", 300, iso3="BRA"),
    "Q45": Entity("Q45", {"en": "Portugal", "fr": "Portugal"}, {}, "country", 300, iso3="PRT"),
    "Q29": Entity("Q29", {"en": "Spain", "fr": "Espagne"}, {}, "country", 300, iso3="ESP"),
    "Q30": Entity("Q30", {"en": "United States", "fr": "États-Unis"}, {}, "country", 300, iso3="USA"),
    "Q1041": Entity("Q1041", {"en": "Senegal", "fr": "Sénégal"}, {}, "country", 250, iso3="SEN"),
    "Q5146": Entity("Q5146", {"en": "Portuguese", "fr": "portugais"}, {}, "language", 250),
    "Q1065": Entity("Q1065", {"en": "United Nations", "fr": "Organisation des Nations unies"}, {}, "organisation", 250),
    "Q7184": Entity("Q7184", {"en": "NATO", "fr": "OTAN"}, {}, "organisation", 200),
    "Q9916": Entity("Q9916", {"en": "Dwight D. Eisenhower", "fr": "Dwight D. Eisenhower"}, {}, "person", 150),
    "Q9696": Entity("Q9696", {"en": "John F. Kennedy", "fr": "John F. Kennedy"}, {}, "person", 150),
    "Q90000001": Entity("Q90000001", {"en": "Treaty of Tordesillas", "fr": "traité de Tordesillas"}, {}, "treaty", 60),
    "Q90000003": Entity("Q90000003", {"en": "Colonial Brazil", "fr": "Brésil colonial"}, {}, "colony", 40),
    "Q90000004": Entity(
        "Q90000004",
        {"en": "first voyage of Christopher Columbus", "fr": "premier voyage de Christophe Colomb"},
        {},
        "voyage",
        30,
    ),
    "Q90000002": Entity("Q90000002", {"en": "decolonization", "fr": "décolonisation"}, {}, "event", 80),
    "Q90000009": Entity("Q90000009", {"en": "Colombo", "fr": "Colombo"}, {}, "place", 90),
}
ALIASES = {
    "Q7322": ["Colomb", "Columbus"],
    "Q30": ["USA", "Etats-Unis", "United States of America"],
    "Q5146": ["langue portugaise", "Portuguese language"],
    "Q155": ["Brésiliens"],
}
PROPS = {
    "P37": {"en": "official language", "fr": "langue officielle"},
    "P793": {"en": "significant event", "fr": "événement clé"},
    "P828": {"en": "has cause", "fr": "a pour cause"},
    "P1891": {"en": "signatory", "fr": "signataire"},
    "P1365": {"en": "replaces", "fr": "remplace"},
    "P17": {"en": "country", "fr": "pays"},
    "P463": {"en": "member of", "fr": "membre de"},
    "P35": {"en": "head of state", "fr": "chef d'État"},
    "P571": {"en": "inception", "fr": "date de fondation ou de création"},
}


def st(
    n: int,
    s: str,
    p: str,
    o: str | None = None,
    *,
    lit: str | None = None,
    start: str | None = None,
    end: str | None = None,
    pit: str | None = None,
    prec: int = 9,
) -> KgStatement:
    return KgStatement(
        statement_id=f"{s}$FIXTURE-{n:04d}",
        subject_qid=s,
        property=p,
        subject_labels=dict(E[s].labels) if s in E else {},
        property_labels=PROPS.get(p, {}),
        object_qid=o,
        object_labels=dict(E[o].labels) if o and o in E else {},
        literal=lit,
        start=TimeValue(start, prec) if start else None,
        end=TimeValue(end, prec) if end else None,
        point_in_time=TimeValue(pit, prec) if pit else None,
        source_key="wikidata",
        source_revision=str(2_000_000_000 + n),
        retrieved_at="2026-10-15T00:00:00+00:00",
        raw_sha256=RAW_KG,
        origin="test_fixture",
    )


STATEMENTS = [
    st(1, "Q7322", "P793", "Q90000004", pit="1492", prec=9),
    st(2, "Q90000001", "P828", "Q90000004"),
    st(3, "Q90000001", "P1891", "Q29", pit="1494"),
    st(4, "Q90000001", "P1891", "Q45", pit="1494"),
    st(5, "Q45", "P37", "Q5146"),
    st(6, "Q155", "P37", "Q5146"),
    st(7, "Q155", "P1365", "Q90000003"),
    st(8, "Q90000003", "P17", "Q45"),
    st(9, "Q30", "P463", "Q1065", start="1945-10-24", prec=11),
    st(10, "Q30", "P463", "Q7184", start="1949-08-24", prec=11),
    st(11, "Q30", "P35", "Q9916", start="1953-01-20", end="1961-01-20", prec=11),
    st(12, "Q30", "P35", "Q9696", start="1961-01-20", end="1963-11-22", prec=11),
    st(13, "Q155", "P571", lit="1822-09-07", prec=11),
]


def _overlaps(s: KgStatement, lo: int | None, hi: int | None) -> bool:
    years = [t.year for t in (s.start, s.end, s.point_in_time) if t is not None]
    if not years:
        return False
    a = s.start.year if s.start else (s.point_in_time.year if s.point_in_time else None)
    b = s.end.year if s.end else (s.point_in_time.year if s.point_in_time else None)
    a = a if a is not None else -(10**6)
    b = b if b is not None else 10**6
    return (lo is None or b >= lo) and (hi is None or a <= hi)


class FakeKg:
    name = "fake-kg (test fixture)"

    def __init__(self, statements: Sequence[KgStatement] = STATEMENTS) -> None:
        self.statements = list(statements)
        self.calls: list[tuple[str, str]] = []

    def search_entities(self, text: str, *, kind: str | None, lang: str, limit: int) -> list[EntityMatch]:
        self.calls.append(("search", text))
        t = fold(text)
        out = []
        for qid, e in E.items():
            if any(fold(v) == t for v in e.labels.values()):
                out.append(EntityMatch(e, "label_exact", text))
            elif any(fold(a) == t for a in ALIASES.get(qid, [])):
                out.append(EntityMatch(e, "alias_exact", text))
            elif any(fold(v).startswith(t) for v in e.labels.values()) and len(t) >= 4:
                out.append(EntityMatch(e, "label_prefix", text))
        return out[:limit]

    def entity(self, qid: str) -> Entity | None:
        return E.get(qid)

    def neighbours(self, qid, *, properties, as_of, limit):
        self.calls.append(("neighbours", qid))
        y = int(as_of[:4]) if as_of else None
        out = [
            s
            for s in self.statements
            if qid in (s.subject_qid, s.object_qid)
            and (not properties or s.property in properties)
            and (y is None or not (s.start or s.end or s.point_in_time) or _overlaps(s, y, y))
        ]
        return out[:limit]

    def timeline(self, qid, *, from_year, to_year, limit):
        self.calls.append(("timeline", qid))
        return [
            s for s in self.statements if qid in (s.subject_qid, s.object_qid) and _overlaps(s, from_year, to_year)
        ][:limit]

    def labels_for_iso3(self, iso3s):
        return {e.iso3: dict(e.labels) for e in E.values() if e.iso3 in iso3s}


def cell(
    src: str,
    ds: str,
    ind: str,
    iso3: str,
    year: int,
    value: str | None,
    unit: str,
    raw: str,
    concept: str,
    flags: dict | None = None,
) -> AtlasCell:
    return AtlasCell(
        source=src,
        dataset=ds,
        indicator=ind,
        iso3=iso3,
        period_start=f"{year}-01-01",
        period_end=f"{year + 1}-01-01",
        value=value,
        unit=unit,
        concept_id=concept,
        flags=flags or {},
        raw_sha256=raw,
        selector=json.dumps({"file": f"{ds}.csv", "row": iso3, "column": str(year)}),
    )


DENS = ("population.density", "wb", "wdi", "EN.POP.DNST", "people per sq. km of land area", RAW_WDI)
CELLS = [
    *(
        cell("wb", "wdi", "EN.POP.DNST", iso, 2022, v, DENS[4], RAW_WDI, DENS[0])
        for iso, v in [
            ("MCO", "26337.4615384615"),
            ("SGP", "7595.2"),
            ("BHR", "1924.8"),
            ("MLT", "1666.1"),
            ("BGD", "1329.4"),
            ("BRA", "25.6"),
            ("FRA", "123.4"),
            ("SEN", "88.1"),
            ("USA", "36.6"),
        ]
    ),
    *(
        cell("wb", "wdi", "EN.POP.DNST", iso, 2021, v, DENS[4], RAW_WDI, DENS[0])
        for iso, v in [
            ("MCO", "26000.1"),
            ("SGP", "7600.3"),
            ("BRA", "25.4"),
            ("FRA", "123.1"),
            ("SEN", "86.0"),
            ("USA", "36.5"),
            ("BHR", "1900.0"),
            ("MLT", "1650.0"),
            ("BGD", "1310.0"),
        ]
    ),
    *(cell("wb", "wdi", "EN.POP.DNST", iso, 2023, v, DENS[4], RAW_WDI, DENS[0]) for iso, v in [("SGP", "7650.0")]),
    *(
        cell("un", "wpp", "PopDensity", iso, 2022, v, "persons per square km", RAW_WPP, DENS[0])
        for iso, v in [("MCO", "24620.5"), ("MAC", "21000.0"), ("SGP", "8000.0")]
    ),
    cell(
        "ilo",
        "ilostat_mw",
        "EAR_INEE_NOC_NB|cur=LCU",
        "SEN",
        2019,
        "58900",
        "XOF per month",
        RAW_ILO,
        "labour.minimum_wage_monthly",
        {"note": "national legislation"},
    ),
    cell(
        "ilo",
        "ilostat_mw",
        "EAR_INEE_NOC_NB|cur=LCU",
        "SEN",
        2015,
        "36243",
        "XOF per month",
        RAW_ILO,
        "labour.minimum_wage_monthly",
    ),
    cell(
        "ilo",
        "ilostat_mw",
        "EAR_INEE_NOC_NB|cur=LCU",
        "FRA",
        2022,
        "1678.95",
        "EUR per month",
        RAW_ILO,
        "labour.minimum_wage_monthly",
    ),
]


class FakeAtlas:
    name = "fake-atlas (test fixture)"

    def __init__(self, cells: Sequence[AtlasCell] = CELLS) -> None:
        self.cells = list(cells)
        self.order = {c.id: c.source_order for c in catalog().concepts.values()}

    def concepts(self) -> list[Concept]:
        return list(catalog().concepts.values())

    def _of(self, concept_id):
        return [c for c in self.cells if c.concept_id == concept_id]

    def values(self, concept_id, iso3s, year):
        cells = [c for c in self._of(concept_id) if iso3s is None or c.iso3 in iso3s]
        if year is not None:
            cells = [c for c in cells if c.year == year]
        elif iso3s is not None:
            latest = {}
            for c in cells:
                k = (c.source, c.dataset, c.indicator, c.iso3)
                # the latest actual cell; a projection only when the series has nothing else (as the adapter)
                if k not in latest or (not is_projection(c.flags), c.year) > (
                    not is_projection(latest[k].flags),
                    latest[k].year,
                ):
                    latest[k] = c
            cells = list(latest.values())
        return AtlasResult(tuple(cells), tuple(self.order.get(concept_id, ())))

    def series(self, concept_id, iso3, from_year, to_year):
        cells = [
            c
            for c in self._of(concept_id)
            if c.iso3 == iso3 and (from_year is None or c.year >= from_year) and (to_year is None or c.year <= to_year)
        ]
        return AtlasResult(tuple(cells), tuple(self.order.get(concept_id, ())))


class FakeLinks:
    name = "fake-links (test fixture)"

    def links(self, iso3, kinds, limit):
        if iso3 != "BRA":
            return []
        out = [
            CountryLink(
                "shared_language",
                "BRA",
                "PRT",
                statements=(STATEMENTS[5], STATEMENTS[4]),
                derived_by="links.shared_official_language",
                source="wikidata",
            )
        ]
        return [x for x in out if not kinds or x.kind in kinds][:limit]


class BrokenAtlas(FakeAtlas):
    def values(self, *a, **k):
        raise CapabilityUnavailable("atlas down", backend="atlas")


def backends(**over) -> Backends:
    b = Backends(kg=FakeKg(), paths=None, atlas=FakeAtlas(), links=FakeLinks())
    for k, v in over.items():
        setattr(b, k, v)
    return b
