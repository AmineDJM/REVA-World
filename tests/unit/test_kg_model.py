"""reva.kg.model: time semantics, statement identity and per-statement validation (no database)."""

from __future__ import annotations

import pytest

from reva.kg.model import (
    KgEntity,
    KgStatement,
    KgTime,
    _normalise_entity,
    _normalise_statement,
    _Reject,
    parse_time,
    statement_key,
    wikibase_json_time,
)
from reva.kg.query import window

RAW = "a" * 64
PROPS = {"P37": "item", "P571": "time", "P1082": "quantity"}


def norm(st: KgStatement):  # type: ignore[no-untyped-def]
    return _normalise_statement(0, st, "wikidata", {RAW}, PROPS)


def reason(st: KgStatement) -> str:
    with pytest.raises(_Reject) as e:
        norm(st)
    return e.value.reason


@pytest.mark.parametrize(
    ("iso", "precision", "calendar", "expected"),
    [
        ("+1822-09-07T00:00:00Z", 11, "gregorian", ("1822-09-07", "1822-09-07")),
        ("+1975-00-00T00:00:00Z", 9, "gregorian", ("1975-01-01", "1975-12-31")),
        ("1975", 9, "gregorian", ("1975-01-01", "1975-12-31")),
        ("+1960-02-00T00:00:00Z", 10, "gregorian", ("1960-02-01", "1960-02-29")),
        ("1900-02", 10, "gregorian", ("1900-02-01", "1900-02-28")),  # 1900 is not a Gregorian leap year
        ("+1990-00-00T00:00:00Z", 8, "gregorian", ("1990-01-01", "1999-12-31")),
        # astronomical numbering: year 0 is 1 BCE, -43 is 44 BCE
        ("-0043-03-15T00:00:00Z", 11, "gregorian", ("0044-03-15 BC", "0044-03-15 BC")),
        ("+0000-00-00T00:00:00Z", 9, "gregorian", ("0001-01-01 BC", "0001-12-31 BC")),
        # century precision is widened by one century on both sides (conventions differ)
        ("+1901-00-00T00:00:00Z", 7, "gregorian", ("1801-01-01", "2001-12-31")),
        # Julian 4 October 1582 is Gregorian 14 October 1582
        ("+1582-10-04T00:00:00Z", 11, "julian", ("1582-10-14", "1582-10-14")),
        # Julian 25 December 1066 -> Gregorian 31 December 1066 (6-day gap in the 11th century)
        ("+1066-12-25T00:00:00Z", 11, "julian", ("1066-12-31", "1066-12-31")),
        # Julian year 1066 as a whole -> Gregorian 1066-01-07 .. 1067-01-06
        ("+1066-00-00T00:00:00Z", 9, "julian", ("1066-01-07", "1067-01-06")),
        # geological time: outside PostgreSQL's date range -> undated for filters (raw string kept)
        ("-13800000000-00-00T00:00:00Z", 0, "gregorian", (None, None)),
    ],
)
def test_parse_time_bounds(iso: str, precision: int, calendar: str, expected: tuple) -> None:
    assert parse_time(KgTime(iso, precision, calendar)) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("iso", "precision"),
    [("+1822-02-30T00:00:00Z", 11), ("+1822-00-00T00:00:00Z", 11), ("+1822-13-01", 10), ("next tuesday", 9),
     ("1822", 15), ("1822", -1)],
)
def test_parse_time_rejects_invalid(iso: str, precision: int) -> None:
    with pytest.raises(_Reject):
        parse_time(KgTime(iso, precision))


def test_wikibase_json_time_converts_historical_bce_years() -> None:
    t = wikibase_json_time(
        {"time": "-0044-03-15T00:00:00Z", "precision": 11, "calendarmodel": "http://www.wikidata.org/entity/Q1985786"}
    )
    assert t.iso.startswith("-0043-03-15") and t.calendar == "julian"
    t2 = wikibase_json_time(
        {"time": "+1492-10-12T00:00:00Z", "precision": 11, "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}
    )
    assert t2 == KgTime("+1492-10-12T00:00:00Z", 11, "gregorian")


def test_statement_key_uses_source_statement_id_else_content() -> None:
    a = KgStatement("Q155", "P37", "Q5146", statement_id="Q155$abc")
    assert statement_key(a, "wikidata") == "wikidata:Q155$abc"
    b = KgStatement("Q155", "P37", "Q5146", references=[{"P248": "Q1"}])
    c = KgStatement("Q155", "P37", "Q5146", rank="preferred")
    # rank and references are not identity: a change in them is a new revision of the same statement
    assert statement_key(b, "un") == statement_key(c, "un")
    assert statement_key(b, "un").startswith("un:h:")
    assert statement_key(KgStatement("Q155", "P37", "Q397"), "un") != statement_key(b, "un")
    # the source is part of the identity: two sources' identical claims stay distinct statements
    assert statement_key(b, "un") != statement_key(b, "ucdp")


def test_validity_derivation_from_start_end_and_point_in_time() -> None:
    r = norm(KgStatement("Q155", "P37", "Q5146", start=KgTime("1822", 9), end=KgTime("+1889-11-15T00:00:00Z", 11)))
    assert (r.lo, r.hi) == ("1822-01-01", "1889-11-15")
    r = norm(KgStatement("Q155", "P37", "Q5146", start=("1822", 9)))  # tuple accepted; open end
    assert (r.lo, r.hi) == ("1822-01-01", None)
    r = norm(KgStatement("Q155", "P37", "Q5146", point_in_time={"time": "1950-06", "precision": 10}))
    assert (r.lo, r.hi) == ("1950-06-01", "1950-06-30")
    r = norm(KgStatement("Q155", "P571", value={"type": "time", "time": "+1822-09-07T00:00:00Z", "precision": 11}))
    assert (r.lo, r.hi, r.vlo, r.vhi) == (None, None, "1822-09-07", "1822-09-07")


@pytest.mark.parametrize(
    ("st", "why"),
    [
        (KgStatement("Q155", "P37", "Q5146", rank="deprecated"), "deprecated_rank"),
        (KgStatement("Q155", "P37", "Q5146", rank="best"), "invalid_rank"),
        (KgStatement("Q155", "P37"), "object_xor_value"),
        (KgStatement("Q155", "P37", "Q5146", value={"type": "string", "value": "x"}), "object_xor_value"),
        (KgStatement("Q155", "P999", "Q5146"), "unknown_property"),
        (KgStatement("Q155", "P571", "Q5146"), "property_kind_mismatch"),
        (KgStatement("Q155", "P37", value={"type": "string", "value": "pt"}), "property_kind_mismatch"),
        (KgStatement("Q155", "P1082", value={"type": "quantity", "amount": 1.5}), "quantity_not_decimal_string"),
        (KgStatement("FRA", "P37", "Q5146"), "invalid_subject_qid"),
        (KgStatement("Q155", "P37", "fr"), "invalid_object_qid"),
        (KgStatement("Q155", "P37", "Q5146", start=KgTime("1900", 9), end=KgTime("1800", 9)), "end_before_start"),
        (KgStatement("Q155", "P37", "Q5146", raw_sha256="b" * 64), "raw_sha256_not_in_batch"),
        (KgStatement("Q155", "P37", "Q5146", source_key="un"), "source_key_mismatch"),
        (KgStatement("Q155", "P37", "Q5146", qualifiers={"x": "y" * 70_000}), "too_large"),
    ],
)
def test_statement_rejections(st: KgStatement, why: str) -> None:
    assert reason(st) == why


def test_somevalue_allowed_for_item_property_and_single_raw_default() -> None:
    r = norm(KgStatement("Q155", "P37", value={"type": "somevalue"}))
    assert r.raw == RAW  # the batch's only raw artifact


def test_entity_normalisation_skips_bad_labels_visibly() -> None:
    row, skipped = _normalise_entity(
        0,
        KgEntity("Q155", "country", {"en": "Brazil", "fr": "Brésil", "not a lang": "x", "de": "y" * 600},
                 aliases={"en": ["Brasil", "Brazil", ""]}),
    )
    langs = {(lang, alias) for lang, alias, _ in row.labels}
    assert langs == {("en", False), ("fr", False), ("en", True)}
    assert skipped == 3  # invalid language, over-long label, empty alias (never silently dropped)
    with pytest.raises(_Reject):
        _normalise_entity(0, KgEntity("France", "country", {"en": "France"}))


def test_window_bounds() -> None:
    w = window("1950", "1965-06")
    assert w is not None and (w.lo, w.hi) == ("1950-01-01", "1965-06-30")
    assert window(None, None) is None
    from reva.kernel.errors import ValidationProblem

    with pytest.raises(ValidationProblem):
        window("1970", "1960")
    with pytest.raises(ValidationProblem):
        window("1960-13", None)
