"""Derived PROV-O / OWL-Time / GeoSPARQL exports."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from reva.kernel.errors import IncompatibleSemantics, ValidationProblem
from reva.kernel.time import Calendar, Precision, TimeExtent, UncertainBound
from reva.ontology.exports import (
    CRS84_IRI,
    JSONLD_CONTEXT,
    activity_iri,
    artifact_iri,
    geometry_node,
    prov_jsonld,
    time_extent_to_owl_time,
    wkt_literal,
)

RAW, NORM, BELIEF = "a" * 64, "b" * 64, "c" * 64
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _act(kind: str, origin: str) -> dict:
    return {"id": uuid4(), "kind": kind, "origin": origin, "code_digest": "git:abc", "environment_digest": "env:1",
            "parameters": {"connector": "worldbank"}, "actor": "worker:reva", "started_at": T0, "ended_at": T0}


def _lineage() -> tuple[list, list, list, list]:
    acq, norm = _act("acquisition", "source"), _act("normalise", "normalisation")
    arts = [{"sha256": s, "media_type": "application/json", "byte_size": 10, "privacy_class": "public_aggregate"}
            for s in (RAW, NORM, BELIEF)]
    usages = [{"activity_id": norm["id"], "artifact_sha256": RAW, "ordinal": 0, "selector": {"json_pointer": "/1"}}]
    gens = [{"activity_id": acq["id"], "artifact_sha256": RAW}, {"activity_id": norm["id"], "artifact_sha256": NORM}]
    return [acq, norm], arts, usages, gens


def test_prov_export_shape() -> None:
    acts, arts, usages, gens = _lineage()
    doc = prov_jsonld(acts, arts, usages, gens, focus_artifact=NORM)
    assert doc["@context"]["prov"] == "http://www.w3.org/ns/prov#"
    assert doc["@context"]["time"] == "http://www.w3.org/2006/time#"
    assert doc["reva:authoritative"] is False and "non-authoritative" in doc["rdfs:comment"]
    versions = {v["prefix"]: v["version_iri"] for v in doc["reva:vocabularies"]["@value"]}
    assert versions["prov"] == "http://www.w3.org/TR/2013/REC-prov-o-20130430/"
    nodes = {n["@id"]: n for n in doc["@graph"]}
    norm = nodes[activity_iri(acts[1]["id"])]
    assert norm["@type"] == "prov:Activity"
    assert norm["prov:used"] == [{"@id": artifact_iri(RAW)}]
    assert norm["prov:qualifiedUsage"][0]["reva:selector"]["@value"] == {"json_pointer": "/1"}
    assert norm["prov:startedAtTime"]["@type"] == "xsd:dateTime"
    assert "connector" not in json.dumps(norm)  # parameters exported only as a digest
    ent = nodes[artifact_iri(NORM)]
    assert ent["@type"] == "prov:Entity"
    assert ent["prov:wasGeneratedBy"] == [{"@id": activity_iri(acts[1]["id"])}]
    assert "prov:wasGeneratedBy" not in nodes[artifact_iri(BELIEF)]
    assert not any("prov:wasDerivedFrom" in n for n in doc["@graph"])
    assert any(n["@type"] == ["prov:Agent", "prov:SoftwareAgent"] for n in doc["@graph"])
    json.dumps(doc, allow_nan=False)
    assert prov_jsonld(acts, arts, usages, gens, focus_artifact=NORM)["@id"] == doc["@id"]  # deterministic


def test_prov_export_rejects_dangling_refs_cycles_and_naive_times() -> None:
    acts, arts, usages, gens = _lineage()
    with pytest.raises(ValidationProblem, match="unknown"):
        prov_jsonld(acts, arts[:1], usages, gens)
    cyc_use = [*usages, {"activity_id": acts[0]["id"], "artifact_sha256": NORM, "ordinal": 0, "selector": {}}]
    with pytest.raises(ValidationProblem, match="cycle"):
        prov_jsonld(acts, arts, cyc_use, gens)
    naive = [{**acts[0], "started_at": datetime(2026, 1, 1)}, acts[1]]
    with pytest.raises(ValidationProblem, match="timezone"):
        prov_jsonld(naive, arts, usages, gens)


def test_owl_time_point_and_half_open_interval() -> None:
    jan1 = time_extent_to_owl_time(TimeExtent.on_date(date(2023, 1, 1), label="2023-01-01"))
    assert jan1["@type"] == "time:Instant"
    assert jan1["time:inXSDDate"] == {"@value": "2023-01-01", "@type": "xsd:date"}
    year = time_extent_to_owl_time(TimeExtent.calendar_year(2023), iri="urn:x")
    assert year["@id"] == "urn:x" and year["@type"] == "time:ProperInterval"
    assert year["time:hasBeginning"]["time:inXSDGYear"]["@value"] == "2023"
    assert year["time:hasEnd"]["time:inXSDGYear"]["@value"] == "2024"
    assert year["reva:endExclusive"] is True
    assert "@context" not in year and all(k.split(":")[0] in JSONLD_CONTEXT or k.startswith("@") for k in year)


def test_owl_time_uncertain_open_and_julian_extents() -> None:
    te = TimeExtent(kind="interval", precision=Precision.year, start=None,
                    start_uncertainty=UncertainBound(earliest=date(1500, 1, 1), latest=date(1510, 1, 1)))
    node = time_extent_to_owl_time(te)
    assert node["reva:openEnded"] is True
    assert node["time:hasBeginning"]["reva:earliestPossible"]["@value"] == "1500-01-01"
    assert "time:inXSDGYear" not in node["time:hasBeginning"]
    jul = time_extent_to_owl_time(TimeExtent(kind="instant", precision=Precision.day, calendar=Calendar.julian,
                                             start=date(1582, 10, 4)))
    desc = jul["time:inDateTime"]
    assert desc["@type"] == "time:GeneralDateTimeDescription"
    assert desc["time:day"]["@value"] == "---04" and "time:inXSDDate" not in jul


def test_wkt_literal_pins_crs_and_refuses_axis_swaps() -> None:
    lit = wkt_literal("POLYGON ((2 48, 3 48, 3 49, 2 48))")
    assert lit == {"@value": f"<{CRS84_IRI}> POLYGON ((2 48, 3 48, 3 49, 2 48))", "@type": "geo:wktLiteral"}
    with pytest.raises(IncompatibleSemantics, match="latitude/longitude"):
        wkt_literal("POINT (2 48)", crs="EPSG:4326")
    assert wkt_literal("POINT (48 2)", crs="EPSG:4326", axis_order="lat_lon")["@value"].startswith("<http")
    for bad in ("POINT (1 2", "CIRCLE (1 2)", "POINT (nan 1)", "POINT (1 x)"):
        with pytest.raises(ValidationProblem):
            wkt_literal(bad)
    node = geometry_node("urn:g", "POINT (1 2)", feature_iri="urn:f")
    assert node["geo:hasGeometry"]["reva:authoritative"] is False
