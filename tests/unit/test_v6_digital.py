"""V6DIGITAL parsers on the captured fixtures (Access Now STOP, CFR Cyber Operations Tracker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from reva.adapters.connectors import atlas_cfr_cyber as cfr
from reva.adapters.connectors import atlas_keepiton as kio
from reva.adapters.connectors import digital_common as dc
from reva.atlas.snapshot import normalise_rights
from reva.atlas.tables import country_iso3

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"
KIO = FIX / kio.CONNECTOR_NAME
CFR = FIX / cfr.CONNECTOR_NAME
QIDS = {"IND": "Q668", "IRN": "Q794", "MMR": "Q836", "RUS": "Q159", "USA": "Q30"}


def test_split_countries_keeps_known_names_with_separators() -> None:
    known = {"Palestine, State of", "Israel", "United States", "Tanzania, United Republic of", "Kenya"}
    assert dc.split_countries("Israel, United States", known) == ["Israel", "United States"]
    assert dc.split_countries("Palestine, State of", known) == ["Palestine, State of"]
    assert dc.split_countries("Kenya; Tanzania, United Republic of", known, seps=(";",)) == [
        "Kenya",
        "Tanzania, United Republic of",
    ]
    assert dc.split_countries("Atlantis, Kenya", known) == ["Atlantis", "Kenya"]


def test_exact_name_schemes() -> None:
    assert country_iso3("Iran (Islamic Republic of)", kio.GEO_SCHEME) == "IRN"
    assert country_iso3("Turkey", kio.GEO_SCHEME) == "TUR"
    assert country_iso3("Somaliland", kio.GEO_SCHEME) is None
    assert country_iso3("Korea (Democratic People's Republic of)", cfr.GEO_SCHEME) == "PRK"
    assert country_iso3("Palestine, State of", cfr.GEO_SCHEME) == "PSE"


def test_stop_definitions_and_types() -> None:
    f = kio.page_facts((KIO / "methodology.html").read_bytes(), (KIO / "media_usage.html").read_bytes())
    assert f["columns"]["duration"].startswith("The number of days between start_date and end_date")
    assert f["columns"]["ordered_by"].startswith("Categorical data describing which entity ordered the shutdown")
    assert f["columns"]["geo_scope"] == ""  # not described on the page
    assert f["shutdown_definition"].startswith("An internet shutdown is an intentional disruption")
    assert "Creative Commons Attribution (CC BY) 4.0" in f["media_licence"]
    typed = kio.gviz_types((KIO / "gviz_header.js").read_bytes())
    assert tuple(typed) == kio.HEADER
    assert {c for c, t in typed.items() if t != "string"} == {
        "start_date",
        "end_date",
        "duration",
        "econ_impact",
        "count_year",
    }


def test_stop_instances_years_and_countries() -> None:
    insts, stats = kio.parse_sheet((KIO / "stop_combined.csv").read_bytes(), dc.scheme_names(kio.GEO_SCHEME))
    assert stats["rows"] == 24 and stats["instances"] == 15
    ongoing = next(i for i in insts if len(i.count_years) > 1)
    assert ongoing.count_years == [str(y) for y in range(2016, 2026)]  # one instance, counted every year
    assert any(i.key.endswith("-2") for i in insts)  # identical rows in one count year stay two instances
    five = next(i for i in insts if len(i.countries) == 5)
    assert "Tanzania, United Republic of" in five.countries
    defs = {c: "" for c in kio.HEADER}
    frames = kio.atlas_frames(insts, "a" * 64, {}, defs)
    cy = frames["shutdowns_cy"]
    tot = cy.filter(cy["indicator"] == "SHUTDOWN_INSTANCES")
    assert int(tot["value"].cast(float).sum()) == 24 + 4 + 1 + 1  # multi-country rows count for each country
    assert set(tot.filter(tot["provider_geo"] == "Somaliland")["iso3"].to_list()) == {None}
    _ents, sts, ks = kio.kg_records(insts, "a" * 64, QIDS, defs, {"end_date": "date"})
    assert ks["entities"] == 15 + len(kio.SERVICES)
    p17 = {s.object_qid for s in sts if s.property == "P17"}
    assert "Q794" in p17 and "iso3:TUR" in p17
    assert not any(s.property == "P17" and "Somaliland" in str(s.qualifiers) for s in sts)


def test_stop_header_drift_refused() -> None:
    body = (KIO / "stop_combined.csv").read_bytes().replace(b'"duration"', b'"duration_days"', 1)
    with pytest.raises(kio.StopError):
        kio.parse_sheet(body, set())


def test_cfr_attribution_and_counts() -> None:
    f = cfr.page_facts((CFR / "tracker.html").read_bytes(), (CFR / "terms.html").read_bytes())
    assert f["attribution"].startswith(cfr.ATTRIBUTION_QUOTE)
    assert f["glossary"]["Espionage"].startswith("Espionage The act of obtaining confidential information")
    assert "public or commercial purpose" in f["terms"]
    ops, st = cfr.parse_csv((CFR / "incidents.csv").read_bytes(), dc.scheme_names(cfr.GEO_SCHEME))
    assert st["incidents"] == 40 and st["undated"] == 3 and st["multi_sponsor"] == 12
    assert any(o.sponsors == ["Palestine, State of"] for o in ops)
    frame = cfr.atlas_frame(ops, "b" * 64, f["glossary"], f["attribution"])
    assert frame["iso3"].null_count() == 0
    assert all("suspected state sponsor as attributed by CFR" in x for x in frame["flags"])
    ents, sts, _ks = cfr.kg_records(ops, "b" * 64, QIDS, f)
    sp = [s for s in sts if s.property == "cfr_cyber:suspected_state_sponsor"]
    assert sp and all(s.qualifiers["claim"] == "suspected state sponsor as attributed by CFR" for s in sp)
    assert {"Q159", "iso3:PSE"} <= {s.object_qid for s in sp}
    assert len({e.qid for e in ents}) == 40


def test_rights_are_restricted() -> None:
    a = normalise_rights(kio.rights({}), basis="test")
    b = normalise_rights(cfr.rights({}), basis="test")
    assert a and a["restricted"] and a["open_licence"] is False
    assert b and b["restricted"] and b["non_commercial"]
