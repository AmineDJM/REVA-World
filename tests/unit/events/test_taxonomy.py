"""EVENTS: taxonomy file, recorded Wikidata verification, and the pure parts of the event view (offline)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.events import view as ev
from reva.events.taxonomy import (
    TAXONOMY_PATH,
    VERIFICATION_PATH,
    RecordedClasses,
    TaxonomyError,
    _parse,
    all_type_ids,
    check_taxonomy,
    load_taxonomy,
    load_verification,
    recorded_classes,
    taxonomy_summary,
)
from reva.kg.model import CAUSAL_PIDS

ROOT = Path(__file__).resolve().parents[3]


def test_taxonomy_is_valid_and_every_wikidata_id_is_verified() -> None:
    tax = load_taxonomy()
    ver = load_verification()
    assert ver, "registry/events/_recorded/wikidata_verification.json missing (python -m reva.events.verify)"
    assert check_taxonomy(tax, ver) == []
    assert tax.types["event"].parent is None and tax.depth("civil_war") == 3
    for t in tax.types.values():
        assert t.label_fr and t.label_en and t.roles, t.id


def test_verified_classes_have_labels_revision_and_raw_sha() -> None:
    tax = load_taxonomy()
    ents = load_verification()["entities"]
    for q in tax.class_index:
        rec = ents[q]
        assert rec["labels"].get("en") and rec["lastrevid"] and re.fullmatch(r"[0-9a-f]{64}", rec["raw_sha256"]), q


def test_verified_class_labels_match_their_type() -> None:
    """A guard against a mistyped QID: the recorded English label of each class shares a word with its type."""
    tax = load_taxonomy()
    ents = load_verification()["entities"]
    exceptions = {"Q1190554"}  # 'occurrence' is the root class of events
    for q, types in tax.class_index.items():
        if q in exceptions:
            continue
        label = ents[q]["labels"]["en"].lower()
        words = {w for t in types for w in re.split(r"[ _'()]+", tax.types[t].label_en.lower()) if len(w) > 3}
        assert any(w[:5] in label for w in words), (q, label, types)


def test_properties_are_verified_and_typed() -> None:
    tax = load_taxonomy()
    ents = load_verification()["entities"]
    for p in tax.properties:
        if p.pid.startswith("P"):
            assert ents[p.pid]["datatype"] in ("wikibase-item", "time", "external-id"), p.pid
    for pid in CAUSAL_PIDS:
        assert ents[pid]["datatype"] == "wikibase-item"


def test_check_taxonomy_refuses_bad_definitions() -> None:
    doc = yaml.safe_load(TAXONOMY_PATH.read_text())
    doc["types"].append({"id": "dup", "label_en": "dup", "label_fr": "dup", "parent": "event",
                         "wikidata_classes": ["Q198"], "roles": ["nobody"]})
    doc["types"].append({"id": "orphan", "label_en": "o", "label_fr": "", "parent": "nope",
                         "wikidata_classes": [], "roles": []})
    errs = check_taxonomy(_parse(doc, "x"))
    text = " | ".join(errs)
    assert "Q198 mapped to both" in text and "unknown role" in text and "unknown parent" in text
    assert "label_fr required" in text
    tax = _parse(doc, "x")
    assert check_taxonomy(tax, {"entities": {}})  # nothing verified -> refused
    with pytest.raises(TaxonomyError):
        all_type_ids(load_taxonomy(), ["no_such_type"])


def test_type_tree_helpers() -> None:
    tax = load_taxonomy()
    assert tax.most_specific(["event", "armed_conflict", "war", "civil_war", "treaty"]) == ["civil_war", "treaty"]
    assert {"war", "civil_war", "battle", "siege"} <= tax.descendants("armed_conflict")
    assert "event" not in all_type_ids(tax, ["war"]) and "civil_war" in all_type_ids(tax, ["war"])
    roles = tax.role_for("participant", "P1344", "in", "cow.data")
    assert roles is not None and roles.role == "belligerent"
    generic = tax.role_for("participant", "P1344", "in", "wikidata.wbapi")
    assert generic is not None and generic.role == "participant"
    summary = taxonomy_summary(tax)
    assert {x["id"] for x in summary} == set(tax.types)
    assert next(x for x in summary if x["id"] == "armed_conflict")["dataset_kinds"] == ["ucdp:conflict:"]


def test_recorded_classes_index() -> None:
    rec = recorded_classes()
    assert rec.parents, "no recorded P279 claims"
    # every recorded parent edge is non-deprecated and names its statement
    for c, ps in rec.parents.items():
        for parent, rank, sid in ps:
            assert rank in ("preferred", "normal") and parent.startswith("Q") and sid.startswith(c), (c, parent)


def test_recorded_verification_is_compact_and_has_no_personal_data() -> None:
    raw = VERIFICATION_PATH.read_text()
    assert len(raw) < 400_000
    doc = json.loads(raw)
    assert doc["schema"] == "reva-events-wikidata-verification/1"
    assert "@" not in raw  # no e-mail address or other contact data


# ------------------------------------------------------------------------------------------- pure view parts
def _row(pid: str, time: str, prec: int, lo: str, hi: str, rank: str = "normal", key: str = "k") -> dict[str, Any]:
    return {"property_pid": pid, "value": {"type": "time", "time": time, "precision": prec}, "value_time_lo": lo,
            "value_time_hi": hi, "statement_key": key, "source_key": "wikidata.wbapi", "source_revision": "1",
            "raw_sha256": "0" * 64, "rank": rank, "origin": "live", "qualifiers": {}}


def test_interval_prefers_rank_then_extremes_and_keeps_alternatives() -> None:
    tax = load_taxonomy()
    rows = [
        _row("P580", "1956-10-29", 11, "1956-10-29", "1956-10-29", key="a"),
        _row("P580", "1956", 9, "1956-01-01", "1956-12-31", rank="preferred", key="b"),
        _row("P582", "1956-11-07", 11, "1956-11-07", "1956-11-07", key="c"),
        _row("P585", "1957-03", 10, "1957-03-01", "1957-03-31", key="d"),
    ]
    iv = ev._interval(tax, rows, [])
    assert iv["start"]["provenance"]["statement_key"] == "b" and iv["earliest"] == "1956-01-01"
    assert iv["latest"] == "1956-11-07" and iv["consistency"] == "consistent"
    assert {a["provenance"]["statement_key"] for a in iv["alternatives"]} == {"a"}
    assert any("outside" in n for n in iv["notes"])


def test_interval_inconsistent_undated_and_spells() -> None:
    tax = load_taxonomy()
    bad = ev._interval(tax, [_row("P580", "1990", 9, "1990-01-01", "1990-12-31"),
                             _row("P582", "1980", 9, "1980-01-01", "1980-12-31")], [])
    assert bad["consistency"] == "inconsistent"
    assert ev._interval(tax, [], [])["basis"] == "undated"
    sp = ev._interval(tax, [], [{"valid_lo": "1823-04-07", "valid_hi": "1823-11-13"},
                                {"valid_lo": "1823-05-01", "valid_hi": None}])
    assert sp["basis"] == "derived_from_participation_spells" and sp["earliest"] == "1823-04-07"
    bce = ev._interval(tax, [_row("P585", "-0043-03-15", 11, "0044-03-15 BC", "0044-03-15 BC")], [])
    assert bce["earliest"] == "-0043-03-15" and bce["latest"] == "-0043-03-15"


def test_chains_are_shortest_and_cite_hops() -> None:
    g = ev.ClassGraph({"A": [{"parent": "B", "via": "kg", "statement_key": "s1"},
                             {"parent": "Q198", "via": "recorded", "statement_id": "s2"}],
                       "B": [{"parent": "Q198", "via": "kg", "statement_key": "s3"}]}, False)
    out = ev._chains(g, "A", {"Q198": ("war",)})
    assert [h["subclass_of"] for h in out["Q198"]] == ["Q198"] and out["Q198"][0]["via"] == "recorded"
    assert ev._chains(g, "Q198", {"Q198": ("war",)}) == {"Q198": []}


def test_recorded_classes_from_document() -> None:
    doc = {"entities": {"Q1": {"labels": {"en": "x"}, "P279": [{"o": "Q2", "rank": "normal", "statement": "Q1$a"},
                                                               {"o": "Q3", "rank": "deprecated", "statement": "Q1$b"}],
                               "lastrevid": 5, "raw_sha256": "f" * 64}}}
    rec = recorded_classes(doc)
    assert rec.parents == {"Q1": (("Q2", "normal", "Q1$a"),)} and rec.children() == {"Q2": ["Q1"]}
    assert isinstance(rec, RecordedClasses)


def test_check_id() -> None:
    assert ev._check_id("Q49101") == "Q49101" and ev._check_id("ucdp:conflict:246")
    with pytest.raises(Exception):
        ev._check_id("FRA")
