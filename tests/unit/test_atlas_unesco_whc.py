"""UNESCO World Heritage List connector (agent KGCULT): offline, on the captured live fixture."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import pytest

from reva.adapters.connectors import atlas_unesco_whc as whc
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.ids import sha256_bytes

RETRIEVED = datetime(2026, 10, 15, 22, 8, tzinfo=UTC)


@pytest.fixture(scope="module")
def raws() -> dict[str, bytes]:
    http = FixtureHttp(whc.CONNECTOR_NAME)

    async def go() -> dict[str, bytes]:
        return {it.key: (await whc.fetch(it, http)).body for it in whc.fetch_items()}

    return asyncio.run(go())


@pytest.fixture(scope="module")
def parsed(raws):
    meta = whc.parse_metadata(raws["whc001.metadata"])
    props, info = whc.parse_export(raws["whc001.export"])
    return meta, props, info


def test_fixture_is_the_whole_list_with_declared_licence(parsed):
    meta, props, info = parsed
    assert meta["licence"] == "CC BY-SA 4.0"
    assert meta["publisher"] == "UNESCO"
    assert info["record_count"] == len(props) == int(meta["records_count"])
    assert whc.validate_properties(props, meta) == []
    assert {p.category for p in props} == {"Cultural", "Natural", "Mixed"}


def test_brazil_properties(parsed):
    _, props, _ = parsed
    brazil = [p for p in props if "BR" in p.iso2]
    assert len(brazil) == 26  # List as published 2026-10-15 (whc001 revision 2026-10-15T02:07:43Z)
    names = {p.name_en for p in brazil}
    assert "Historic Centre of Salvador de Bahia" in names
    assert "Brasilia" in names
    by_id = {p.id_no: p for p in brazil}
    brasilia = by_id["445"]
    assert brasilia.inscribed_year == 1987 and brasilia.criteria == ("i", "iv") and brasilia.category == "Cultural"
    # Iguaçu National Park: natural, criteria (vii)(x)
    iguacu = next(p for p in brazil if p.name_en.startswith("Iguaçu"))
    assert iguacu.category == "Natural" and iguacu.criteria == ("vii", "x")
    # Missions of the Guaranis: transboundary with Argentina
    missions = next(p for p in brazil if "Guaran" in p.name_en)
    assert missions.transboundary and set(missions.iso2) == {"AR", "BR"}


def test_criteria_parser_is_strict():
    assert whc.parse_criteria("(ii)(iv)(vi)") == ("ii", "iv", "vi")
    assert whc.parse_criteria(None) == ()
    with pytest.raises(whc.WhcParseError):
        whc.parse_criteria("(ii)(xi)")
    with pytest.raises(whc.WhcParseError):
        whc.parse_criteria("ii, iv")


def test_unmapped_property_is_listed_not_assigned(parsed):
    _, props, _ = parsed
    un = whc.unmapped(props)
    assert [u["id_no"] for u in un] == ["148"]  # Old City of Jerusalem: no State Party code in the List
    assert un[0]["reason"] == "no_iso_code"


def test_atlas_frame_validates_and_counts(parsed, raws):
    meta, props, _ = parsed
    sha = sha256_bytes(raws["whc001.export"])
    rows = whc.atlas_rows(props, raw_sha256=sha, source_revision=meta["modified"], retrieved_at=RETRIEVED)
    frame, unmapped = validate_atlas_frame(
        whc.atlas_frame(rows),
        source=whc.ATLAS_SOURCE,
        dataset=whc.ATLAS_DATASET,
        raw_sha256s=[sha],
        geo_scheme="iso2",
    )
    assert [u["provider_geo"] for u in unmapped] == ["states_names:Jerusalem (Site proposed by Jordan)"]
    bra = {(r["indicator"], r["period_start"]): r for r in frame.filter(frame["iso3"] == "BRA").to_dicts()}
    snap = date(2026, 10, 15)
    assert bra[("whc_properties_total", snap)]["value"] == "26"
    assert bra[("whc_properties_cultural", snap)]["value"] == "16"
    assert bra[("whc_properties_natural", snap)]["value"] == "9"
    assert bra[("whc_properties_mixed", snap)]["value"] == "1"
    assert bra[("whc_properties_total", snap)]["period_end"] == date(2026, 10, 16)
    # Brasilia's inscription year
    y1987 = bra[("whc_properties_inscribed_in_year", date(1987, 1, 1))]
    assert "445" in json.loads(y1987["selector"])["id_no"]
    flags = json.loads(bra[("whc_properties_total", snap)]["flags"])
    assert flags["aggregation"] == "count" and flags["excludes_removed_properties"] is True
    assert flags["licence"] == "CC BY-SA 4.0"
    # per-year counts of a country sum to its snapshot total (same set of listed properties)
    years = [r for (ind, _), r in bra.items() if ind == "whc_properties_inscribed_in_year"]
    assert sum(int(r["value"]) for r in years) == 26
    # a transboundary property counts once for each State Party
    total = frame.filter(frame["indicator"] == "whc_properties_total")
    assert sum(int(v) for v in total["value"]) > len(props)


def test_schema_drift_is_refused():
    good = {
        "id_no": "1",
        "name_en": "x",
        "date_inscribed": "1978",
        "category": "Cultural",
        "iso_codes": "BR",
        "danger": "False",
        "transboundary": "False",
        "criteria_txt": "(i)",
    }
    whc.parse_export(json.dumps([good]).encode())
    for bad in (
        {**good, "category": "Intangible"},
        {**good, "date_inscribed": "c. 1978"},
        {**good, "iso_codes": "BRA"},
        {**good, "danger": "maybe"},
        {k: v for k, v in good.items() if k != "iso_codes"},
    ):
        with pytest.raises(whc.WhcParseError):
            whc.parse_export(json.dumps([bad]).encode())
    with pytest.raises(whc.WhcParseError):
        whc.parse_export(json.dumps([good, good]).encode())  # duplicate id_no


def test_truncated_export_is_blocked(parsed):
    meta, props, _ = parsed
    assert whc.validate_properties(props[:500], meta)


def test_licence_change_blocks(raws):
    doc = json.loads(raws["whc001.metadata"])
    doc["metas"]["default"]["license"] = "All rights reserved"
    with pytest.raises(whc.SourceResponseError):
        whc.parse_metadata(json.dumps(doc).encode())


def test_missing_criteria_are_flagged_not_inferred(parsed):
    _, props, _ = parsed
    valongo = next(p for p in props if p.id_no == "1548")  # Valongo Wharf: no criteria in the export
    assert valongo.criteria == () and valongo.raw_flags.get("criteria_not_reported") is True
    assert sum(1 for p in props if p.raw_flags.get("criteria_not_reported")) == 17
