"""Space-time helpers (reva.kg.spacetime): periods with precision, place ids and the evidenced place chain."""

from __future__ import annotations

from datetime import date

import pytest

from reva.kg import spacetime as st
from reva.kg.families import load_registry


@pytest.mark.parametrize(
    ("text", "prec", "lo", "hi"),
    [
        ("2024-03-15", "day", "2024-03-15", "2024-03-15"),
        ("2024-02", "month", "2024-02-01", "2024-02-29"),
        ("2023-02", "month", "2023-02-01", "2023-02-28"),
        ("2024-Q1", "quarter", "2024-01-01", "2024-03-31"),
        ("2024Q4", "quarter", "2024-10-01", "2024-12-31"),
        ("2024", "year", "2024-01-01", "2024-12-31"),
        ("1990s", "decade", "1990-01-01", "1999-12-31"),
        ("19C", "century", "1801-01-01", "1900-12-31"),
        ("-0043", "year", "-0043-01-01", "-0043-12-31"),
        ("0000-02", "month", "0000-02-01", "0000-02-29"),  # 1 BCE is a leap year (astronomical 0)
    ],
)
def test_parse_and_bounds(text: str, prec: str, lo: str, hi: str) -> None:
    p = st.parse_period(text)
    assert p.precision == prec
    assert p.bounds() == (lo, hi)
    assert st.parse_period(p.text()) == p


@pytest.mark.parametrize("bad", ["2024-13", "2023-02-29", "2024-Q5", "1995s", "abc", "", "2024-1"])
def test_parse_rejects(bad: str) -> None:
    with pytest.raises((st.PeriodError, ValueError)):
        st.parse_period(bad)


def test_precision_is_checked() -> None:
    with pytest.raises(st.PeriodError):
        st.parse_period("2024", "month")
    assert st.parse_period("2024-03", "month").month == 3


def test_atlas_bounds_half_open() -> None:
    assert st.parse_period("2024-Q2").atlas_bounds() == (date(2024, 4, 1), date(2024, 7, 1))
    assert st.parse_period("2020").atlas_bounds() == (date(2020, 1, 1), date(2021, 1, 1))
    assert st.parse_period("2024-12-31").atlas_bounds() == (date(2024, 12, 31), date(2025, 1, 1))
    with pytest.raises(st.PeriodError):
        st.parse_period("-0043").atlas_bounds()


def test_kg_times() -> None:
    a, b = st.parse_period("2024-Q3").kg_times()
    assert (a.iso, a.precision, b and b.iso, b and b.precision) == ("2024-07-01", 10, "2024-09-01", 10)
    pt, end = st.parse_period("1975").kg_times()
    assert (pt.iso, pt.precision, end) == ("1975-01-01", 9, None)
    c, _ = st.parse_period("19C").kg_times()
    assert (c.iso, c.precision) == ("1900-01-01", 7)
    assert st.period_from_kg_time("+1975-01-01", 9) == st.parse_period("1975")
    assert st.period_from_kg_time("1900-01-01", 7) == st.parse_period("19C")
    assert st.period_from_kg_time("1995-01-01", 8) == st.parse_period("1990s")
    assert st.period_from_kg_time("1995-01-01", 6) is None  # millennium: not modelled


def test_overlaps_contains_iter() -> None:
    y = st.parse_period("2024")
    q = st.parse_period("2024-Q2")
    assert y.contains(q) and q.overlaps(y) and not q.contains(y)
    assert not st.parse_period("2023").overlaps(q)
    qs = list(st.iter_periods(st.parse_period("2023-Q3"), st.parse_period("2024-Q2")))
    assert [p.text() for p in qs] == ["2023-Q3", "2023-Q4", "2024-Q1", "2024-Q2"]
    days = list(st.iter_periods(st.parse_period("2024-02-28"), st.parse_period("2024-03-01")))
    assert [p.text() for p in days] == ["2024-02-28", "2024-02-29", "2024-03-01"]
    ms = list(st.iter_periods(st.parse_period("2023-11"), st.parse_period("2024-02")))
    assert len(ms) == 4
    with pytest.raises(st.PeriodError):
        list(st.iter_periods(st.parse_period("2023"), st.parse_period("2024-Q1")))


def test_label_fr() -> None:
    assert st.parse_period("2024-Q1").label_fr() == "1er trimestre 2024"
    assert st.parse_period("2024-03-15").label_fr() == "15 mars 2024"
    assert st.parse_period("-0043").label_fr() == "44 av. J.-C."
    assert st.parse_period("19C").label_fr() == "XIXe siècle"
    assert st.parse_period("1990s").label_fr() == "années 1990"


def test_parse_place() -> None:
    assert st.parse_place("FRA") == st.PlaceId("iso3", "FRA", 0, country_iso3="FRA")
    p = st.parse_place("iso3166_2:SA-04")
    assert (p.scheme, p.level, p.country_iso2, p.kg_id) == ("iso3166_2", 1, "SA", "iso3166_2:SA-04")
    assert st.parse_place("FR-75", level=2).level == 2
    g = st.parse_place("geoboundaries:FRA-ADM2-12345B1")
    assert (g.level, g.country_iso3) == (2, "FRA")
    with pytest.raises(ValueError):
        st.parse_place("FRA-ADM2-1", level=1)
    with pytest.raises(ValueError):
        st.parse_place("France")


def test_located_in_statement_and_family() -> None:
    s = st.located_in_statement(
        st.parse_place("FRA-ADM2-1B1"), "geoboundaries:FRA-ADM1-9B1", source_key="geoboundaries.api",
        valid=st.parse_period("2024"),
    )  # fmt: skip
    assert s.property_pid == "place:located_in" and s.point_in_time is not None and s.start is None
    reg = load_registry()
    ef = reg.edge_family(st.PLACE_LOCATED_IN.pid, "out")
    assert ef is not None and ef.family == "located_in"


def test_place_chain() -> None:
    edges = [
        {"subject_qid": "geoboundaries:FRA-ADM2-1B1", "property_pid": "place:located_in",
         "object_qid": "geoboundaries:FRA-ADM1-9B1", "statement_key": "gb:1"},
        {"subject_qid": "geoboundaries:FRA-ADM1-9B1", "property_pid": "place:located_in",
         "object_qid": "iso3:FRA", "statement_key": "gb:2"},
        {"subject_qid": "geoboundaries:FRA-ADM1-9B1", "property_pid": "P17", "object_qid": "Q142",
         "statement_key": "wd:3"},
        {"subject_qid": "iso3:FRA", "property_pid": "place:located_in", "object_qid": "geoboundaries:FRA-ADM2-1B1",
         "statement_key": "cycle"},
        {"subject_qid": "Q90", "property_pid": "P31", "object_qid": "Q515", "statement_key": "wd:not_place"},
    ]  # fmt: skip
    ch = st.place_chain("geoboundaries:FRA-ADM2-1B1", edges)
    assert [s.qid for s in ch] == ["geoboundaries:FRA-ADM2-1B1", "geoboundaries:FRA-ADM1-9B1", "iso3:FRA"]
    assert [s.basis for s in ch] == ["start", "statement", "statement"]
    assert st.chain_levels(ch) == {
        "adm2": "geoboundaries:FRA-ADM2-1B1", "adm1": "geoboundaries:FRA-ADM1-9B1", "country": "iso3:FRA",
    }  # fmt: skip
    # ISO 3166-2 without a stated parent: country from the code prefix, labelled as such
    ch2 = st.place_chain("iso3166_2:SA-04", [], country_by_iso2={"SA": "Q851"})
    assert [(s.qid, s.basis) for s in ch2] == [("iso3166_2:SA-04", "start"), ("Q851", "iso3166_2_code_prefix")]
    assert st.place_chain("Q90", edges) == [st.PlaceStep("Q90", None, None, None, "start")]
