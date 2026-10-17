"""V6CULTURE atlas sources (offline): MusicBrainz statistics pages, Open Library subjects/authors, registry files."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_musicbrainz as mb
from reva.adapters.connectors import atlas_openlibrary as ol
from reva.atlas.tables import country_iso3

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "source"
SHA = "a" * 64


def _fixture(name: str, url_end: str) -> tuple[bytes, dict[str, Any]]:
    d = FIX / name
    for e in json.loads((d / "manifest.json").read_text()):
        if e["url"].endswith(url_end):
            return (d / e["file"]).read_bytes(), e
    raise AssertionError(url_end)


# ----------------------------------------------------------------------------- MusicBrainz
@pytest.fixture(scope="module")
def mb_countries() -> tuple[list[mb.StatRow], date]:
    body, e = _fixture("atlas.musicbrainz_stats", "/statistics/countries")
    assert "EXCERPT" in e["note"] and e["excerpt_of_sha256"]
    return mb.parse_countries(body)


def test_mb_countries_parse(mb_countries) -> None:
    rows, updated = mb_countries
    assert updated == date(2026, 10, 17)
    by = {r.code: r for r in rows}
    assert {"null", "US", "BR", "SU", "XW", "KI"} <= set(by)
    assert by["null"].label == "Unknown country" and by["null"].counts["Artists"] == 1_518_606
    assert by["BR"].counts == {"Artists": 22156, "Releases": 86057, "Labels": 1781, "Events": 591, "Places": 521}
    # the page's Total column is not stored but must equal the sum of the five columns (layout check)
    assert mb.validate_countries(rows, full=False) == []
    assert mb.validate_countries(rows, full=True)  # an excerpt is too short for a live run


def test_mb_country_rows_flags_and_geos(mb_countries) -> None:
    rows, updated = mb_countries
    out = mb.country_rows(rows, updated, raw_sha256=SHA, retrieved_at=datetime(2026, 10, 17, tzinfo=UTC))
    assert {r["indicator"] for r in out} == {"mb_artists", "mb_release_events", "mb_labels", "mb_events", "mb_places"}
    unknown = [r for r in out if r["provider_geo"] == "mb:null"]
    assert len(unknown) == 5 and all(json.loads(r["flags"])["unknown_country"] for r in unknown)
    rel = [json.loads(r["flags"]) for r in out if r["indicator"] == "mb_release_events"]
    assert all(f["release_events_not_origin"] for f in rel)
    su = next(r for r in out if r["provider_geo"] == "SU" and r["indicator"] == "mb_artists")
    assert json.loads(su["flags"])["not_a_current_country"] == "Soviet Union"
    # historical / pseudo codes never map to a successor
    for code in ("SU", "YU", "CS", "XC", "XG", "XE", "XW", "XK", "AN"):
        assert country_iso3(code, "iso2") is None, code
    assert all(r["period_start"] == date(2026, 10, 17) and r["period_end"] == date(2026, 10, 18) for r in out)
    assert all(r["value"].isdigit() for r in out)


def test_mb_languages_and_scripts() -> None:
    body, _ = _fixture("atlas.musicbrainz_stats", "/statistics/languages-scripts")
    langs, scripts, updated = mb.parse_languages(body)
    by = {r.code: r for r in langs}
    assert by["eng"].counts == {"Releases": 3_701_389, "Works": 693_947}
    assert by["fan"].counts["Releases"] == 0 and by["fan"].counts["Works"] == 1  # a provider zero is kept as 0
    assert {"Latn", "Jpan", "Cyrl", "null"} <= {r.code for r in scripts}
    out = mb.language_rows(langs, scripts, updated, raw_sha256=SHA, retrieved_at=datetime(2026, 10, 17, tzinfo=UTC))
    assert {r["provider_geo"] for r in out} == {"world"}
    assert "mb_releases_by_language|language=eng" in {r["indicator"] for r in out}
    assert "mb_releases_by_script|script=Latn" in {r["indicator"] for r in out}


def test_mb_layout_drift_is_refused() -> None:
    body, _ = _fixture("atlas.musicbrainz_stats", "/statistics/countries")
    with pytest.raises(mb.MusicBrainzStatsError):
        mb.parse_countries(body.replace(b"Last updated:", b"Updated"))
    with pytest.raises(mb.MusicBrainzStatsError):
        mb.parse_countries(body.replace(b"<th>Artists", b"<th>Artistes"))
    with pytest.raises(mb.MusicBrainzStatsError):
        mb.parse_countries(body.replace(b"<bdi>22,156</bdi>", b"<bdi>about 22k</bdi>"))


def test_mb_missing_count_is_not_zero() -> None:
    cell = mb.Cell(text="")
    assert mb._count(cell, "x") is None


# ----------------------------------------------------------------------------- Open Library
@pytest.fixture(scope="module")
def ol_parsed() -> tuple[list[str], list[ol.Subject]]:
    page, _ = _fixture("atlas.openlibrary_subjects", "openlibrary.org/subjects")
    keys = ol.parse_browse_page(page)
    subs = []
    for k in ol.FIXTURE_SUBJECTS:
        body, _ = _fixture("atlas.openlibrary_subjects", ol.subject_url(k).split("openlibrary.org", 1)[1])
        subs.append(ol.parse_subject(body, k))
    return keys, subs


def test_ol_browse_list_is_the_providers(ol_parsed) -> None:
    keys, _ = ol_parsed
    assert len(keys) == 70 and keys[0] == "architecture" and "place:brazil" in keys and "poetry" in keys
    assert set(ol.FIXTURE_SUBJECTS) <= set(keys)


def test_ol_subject_parse(ol_parsed) -> None:
    _, subs = ol_parsed
    poetry = subs[0]
    assert poetry.key == "poetry" and poetry.work_count > 100_000 and poetry.ebook_count
    assert len(poetry.languages) == ol.LANGUAGE_FACET_LIMIT and poetry.languages[0][0] == "eng"
    assert all(1000 < y <= 2027 for y, _ in poetry.publishing_history)
    assert poetry.authors[0][0] == "OL28294A"  # Virgil, most works in the subject
    brazil = subs[2]
    assert brazil.subject_type == "place" and brazil.key == "place:brazil"


def test_ol_rows(ol_parsed) -> None:
    _, subs = ol_parsed
    at = datetime(2026, 10, 17, 16, 33, tzinfo=UTC)
    rows = ol.atlas_rows([(s, SHA, at) for s in subs])
    assert {r["provider_geo"] for r in rows} == {"world"}
    works = next(r for r in rows if r["indicator"] == "ol_works|subject=poetry")
    assert works["value"] == str(subs[0].work_count) and works["period_start"] == date(2026, 10, 17)
    lang = [
        json.loads(r["flags"])
        for r in rows
        if r["indicator"].startswith("ol_works_with_edition_language|subject=poetry")
    ]
    assert lang and all(f["facet_top_25_only"] for f in lang)
    years = [r for r in rows if r["indicator"] == "ol_works_with_edition_in_year|subject=poetry"]
    assert len(years) == len(subs[0].publishing_history)
    assert all(json.loads(r["flags"])["counted_per_edition_year"] for r in years)
    assert all(json.loads(r["flags"])["licence_review"] == "required" for r in rows)


def test_ol_authors_and_kg(ol_parsed) -> None:
    _, subs = ol_parsed
    olids = ol.top_authors(subs, ol.FIXTURE_AUTHORS_PER_SUBJECT)
    assert olids[0] == "OL28294A" and len(olids) == len(set(olids)) <= 6
    authors = {}
    for o in olids:
        body, _ = _fixture("atlas.openlibrary_subjects", f"/authors/{o}.json")
        authors[o] = (ol.parse_author(body, o), SHA)
    virgil = authors["OL28294A"][0]
    assert virgil.wikidata == "Q1398" and virgil.viaf == "8194433" and virgil.birth_date == "15 October 70 BCE"
    at = datetime(2026, 10, 17, tzinfo=UTC)
    ents, sts, stats = ol.kg_records([(s, SHA, at) for s in subs], authors, ol.FIXTURE_AUTHORS_PER_SUBJECT)
    assert stats["subjects"] == 3 and stats["authors"] == len(olids)
    keys = {e.qid for e in ents}
    assert "openlibrary:subject.place:brazil" in keys and "openlibrary:OL28294A" in keys
    links = [s for s in sts if s.property_pid == "openlibrary:has_works_in_subject"]
    assert all(s.object_qid in keys and s.subject_qid in keys for s in links)
    wd = {s.subject_qid: s.value["value"] for s in sts if s.property_pid == "openlibrary:wikidata_id"}
    assert wd["openlibrary:OL28294A"] == "Q1398"  # identifier as published, never a name match
    from reva.kg.model import PID_RE, QID_RE

    assert all(QID_RE.match(e.qid) for e in ents) and all(PID_RE.match(p) for p in ol.PROPERTIES)


def test_ol_redirect_author_is_not_an_entity() -> None:
    body = json.dumps({"key": "/authors/OL1A", "type": {"key": "/type/redirect"}, "location": "/authors/OL2A"}).encode()
    a = ol.parse_author(body, "OL1A")
    assert a.redirect_to == "OL2A"
    ents, sts, stats = ol.kg_records([], {"OL1A": (a, SHA)}, 5)
    assert ents == [] and sts == [] and stats["author_redirects"] == 1


def test_ol_wrong_subject_answer_is_refused(ol_parsed) -> None:
    body, _ = _fixture("atlas.openlibrary_subjects", ol.subject_url("film").split("openlibrary.org", 1)[1])
    with pytest.raises(ol.OpenLibraryError):
        ol.parse_subject(body, "poetry")


# ----------------------------------------------------------------------------- registry
def test_sources_registered_and_specs_valid() -> None:
    keys = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    for mod in (mb, ol):
        spec = mod.atlas_source()
        assert spec.source_key in keys and spec.min_interval_seconds >= 1.0
        assert spec.allowed_hosts == (mod.HOST,)


def test_relation_families_dropin() -> None:
    from reva.kg import families as F

    reg = F.load_registry()
    assert reg.mappings["openlibrary:has_works_in_subject"].family == "context"
    for pid in ("P50", "P57", "P136", "P272", "P840"):
        assert pid in reg.mappings, pid


def test_wikidata_specs_valid_and_pending_properties_requested() -> None:
    """culture/v6 specs: schema- and harvester-valid; properties not yet allow-listed are all in v6culture.yaml."""
    jsonschema = pytest.importorskip("jsonschema")
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg import harvest as hv

    validator = jsonschema.Draft202012Validator(json.loads((REPO / "registry/kg/queries/_schema.json").read_text()))
    requested = {
        r["property"] for r in yaml.safe_load((REPO / "registry/kg/property_requests/v6culture.yaml").read_text())
    }
    allow = load_allowlist()
    specs = hv.discover_specs([REPO / "registry/kg/queries/culture/v6"])
    known = [s.id for s in hv.discover_specs()]
    assert len(specs) == 5 and len(known) == len(set(known))
    for s in specs:
        assert s.data["owner"] == "V6CULTURE" and s.id.startswith("culture.v6_")
        assert not list(validator.iter_errors(s.data)), s.id
        assert hv.validate_spec(s, allow, known_ids=known) == [], s.id
        pending = (
            re.findall(r"P[0-9]+", s.path.read_text().split("# Pending allow-listing", 1)[-1])
            if "# Pending" in s.path.read_text()
            else []
        )
        assert set(pending) <= requested, (s.id, pending)
        assert "LIMIT" not in s.data["sparql"].upper() and "wikibase:label" not in s.data["sparql"]
    # bounded by construction: seeds + open-subject statement specs stay well under 100k entities
    assert sum(s.data["max_rows"] for s in specs) <= 100_000
