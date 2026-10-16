"""Relation vocabulary, direction normalisation and the temporal check (precision bounds, BCE, Julian)."""

from __future__ import annotations

import pytest

from reva.causal import temporal as tm
from reva.causal.vocab import CAUSAL_PIDS, RELATIONS, normalise_wikidata, vocabulary
from reva.kg.model import CAUSAL_PIDS as KG_CAUSAL_PIDS


# ---------------------------------------------------------------------------------------------- vocabulary
def test_vocabulary_is_closed_and_complete() -> None:
    v = vocabulary()
    assert set(v.relations) == set(RELATIONS)
    assert set(v.wikidata) == set(CAUSAL_PIDS) == set(KG_CAUSAL_PIDS)
    for r in v.relations.values():
        assert r.label_en and r.label_fr and r.inverse_label_en and r.inverse_label_fr
    assert v.relation("prevented_or_delayed").family == "prevention"
    assert v.family("has_cause", "inhibits") == "prevention" and v.family("has_cause", "promotes") == "cause"
    assert "P1344" not in v.wikidata and "P710" not in v.wikidata  # participation is context only
    assert v.context["P1344"]["en"] == "participant in"


@pytest.mark.parametrize(
    ("pid", "relation", "cause", "effect", "asserted_on"),
    [
        ("P828", "has_cause", "O", "S", "effect"),  # S has cause O
        ("P1542", "has_effect", "S", "O", "cause"),  # S has effect O
        ("P1478", "immediate_cause", "O", "S", "effect"),  # S has immediate cause O
        ("P1536", "immediate_cause", "S", "O", "cause"),  # S immediate cause of O
        ("P1479", "contributing_factor", "O", "S", "effect"),  # S has contributing factor O
        ("P1537", "contributing_factor", "S", "O", "cause"),  # S contributing factor of O
    ],
)
def test_direction_normalisation(pid: str, relation: str, cause: str, effect: str, asserted_on: str) -> None:
    n = normalise_wikidata("S", pid, "O")
    assert (n.relation, n.cause_id, n.effect_id, n.asserted_on) == (relation, cause, effect, asserted_on)


def test_has_effect_and_has_cause_are_the_same_claim_direction() -> None:
    a = normalise_wikidata("Q201684", "P1542", "Q8698")  # crash has effect Great Depression
    b = normalise_wikidata("Q8698", "P828", "Q201684")  # Great Depression has cause crash
    assert (a.cause_id, a.effect_id) == (b.cause_id, b.effect_id) == ("Q201684", "Q8698")
    with pytest.raises(ValueError):
        normalise_wikidata("Q1", "P710", "Q2")


# ---------------------------------------------------------------------------------------------- temporal
def w(iso: str, prec: int, cal: str = "gregorian") -> tm.Window:
    x = tm.window_of_time(iso, prec, "P580", cal)
    assert x is not None
    return x


def test_precision_windows() -> None:
    assert (w("+1929-10-24", 11).lo, w("+1929-10-24", 11).hi) == ((1929, 10, 24), (1929, 10, 24))
    assert (w("+1918-01-00", 10).lo, w("+1918-01-00", 10).hi) == ((1918, 1, 1), (1918, 1, 31))
    assert (w("+1929", 9).lo, w("+1929", 9).hi) == ((1929, 1, 1), (1929, 12, 31))
    assert (w("+1990", 8).lo, w("+1990", 8).hi) == ((1990, 1, 1), (1999, 12, 31))
    # century precision is widened one unit on each side (sources disagree on which year names a century)
    assert (w("+1900", 7).lo, w("+1900", 7).hi) == ((1800, 1, 1), (2000, 12, 31))
    assert tm.window_of_time("-13798000000", 3) is None  # geological time: outside the date range


def test_check_day_month_year_precision() -> None:
    crash, gd = w("+1929-10-24", 11), w("+1929-10-29", 11)
    assert tm.check(crash, gd) == "consistent"
    assert tm.check(gd, crash) == "inconsistent"
    # the same year at year precision overlaps a day in it: order undecidable, never "inconsistent"
    assert tm.check(w("+1929", 9), crash) == "overlapping"
    assert tm.check(crash, w("+1929", 9)) == "overlapping"
    assert tm.check(w("+1918-01-00", 10), w("+1918-01-15", 11)) == "overlapping"
    assert tm.check(crash, crash) == "overlapping"  # same day: not "before"
    assert tm.check(None, crash) == "undetermined" and tm.check(crash, None) == "undetermined"


def test_check_bce_astronomical_years() -> None:
    caesar = w("-0043-03-15", 11)  # 15 March 44 BCE (astronomical -43)
    actium = w("-0030-09-02", 11)  # 2 September 31 BCE
    year0 = w("+0000", 9)  # 1 BCE
    ad1 = w("+0001", 9)
    assert caesar.as_dict()["from"] == "-0043-03-15"
    assert tm.check(caesar, actium) == "consistent" and tm.check(actium, caesar) == "inconsistent"
    assert tm.check(year0, ad1) == "consistent" and tm.check(ad1, year0) == "inconsistent"
    # BCE century precision, widened one unit each side: [-500, -300] overlaps -450 but starts after -508
    assert tm.check(w("-0400", 7), w("-0450", 9)) == "overlapping"
    assert tm.check(w("-0400", 7), w("-0508", 9)) == "inconsistent"


def test_julian_dates_compare_in_proleptic_gregorian() -> None:
    ru = w("+1721-10-22", 11, "julian")  # Russian Empire proclaimed (Julian calendar)
    assert ru.as_dict()["from"] == "1721-11-02"
    assert tm.check(w("+1721-10-30", 11), ru) == "consistent"  # 30 Oct Gregorian is before 2 Nov Gregorian


def _dv(pid: str, lo: str, hi: str, rank: str = "normal", prec: int = 11) -> tm.DateValue:
    return tm.DateValue(pid, lo, hi, prec, f"+{lo}", rank)


def test_event_dates_priority_and_join() -> None:
    d = tm.event_dates(
        [
            _dv("P571", "1700-01-01", "1700-12-31", prec=9),
            _dv("P585", "1929-10-29", "1929-10-29"),
            _dv("P580", "1929-10-24", "1929-10-24"),
            _dv("P582", "1929-10-29", "1929-10-29"),
        ]
    )
    assert d.start is not None and d.start.basis == "P580" and d.start.lo == (1929, 10, 24)
    assert d.end is not None and d.end.basis == "P582"
    # several values: preferred rank wins; otherwise the covering window (REVA does not pick one)
    two = tm.event_dates([_dv("P580", "1914-07-28", "1914-07-28"), _dv("P580", "1914-08-01", "1914-08-01")])
    assert (two.start.lo, two.start.hi, two.start.precision) == ((1914, 7, 28), (1914, 8, 1), 11)  # type: ignore[union-attr]
    pref = tm.event_dates(
        [_dv("P580", "1914-07-28", "1914-07-28", "preferred"), _dv("P580", "1914-08-01", "1914-08-01")]
    )
    assert pref.start.hi == (1914, 7, 28)  # type: ignore[union-attr]
    # BCE literals from SQL (to_char ... 'BC')
    bce = tm.dates_from_rows(
        [{"subject_qid": "Q1", "property_pid": "P585", "lo": "0044-03-15 BC", "hi": "0044-03-15 BC", "precision": 11}]
    )
    assert bce["Q1"].start.lo == (-43, 3, 15)  # type: ignore[union-attr]
    assert tm.event_dates([]).start is None


def test_temporal_detail_is_explained() -> None:
    res, detail = tm.temporal_detail(
        tm.EventDates(w("+1914-07-28", 11), None), tm.EventDates(w("+1918-01-00", 10), None)
    )
    assert res == "consistent" and detail["cause_start"]["from"] == "1914-07-28"
    assert detail["effect_start"]["to"] == "1918-01-31" and detail["effect_start"]["precision"] == 10
