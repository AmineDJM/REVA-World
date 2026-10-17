"""GET /v1/kg/families, /v1/kg/entities/{id}/families, /v1/kg/entities/{id}/places, /v1/kg/families/path (V4CORE).

Synthetic graph (``syn:`` ids, sources ``kgfamtest*``); nothing here is a real-world fact. Shapes follow the V4
use case "petroleum made of alkanes, alkanes part of hydrocarbons; a firm produces crude at a field located in
an ADM1 unit of a country".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from reva.kg.families import ATLAS_MEASURED_BY
from reva.kg.model import KgEntity, KgProperty, KgStatement, KgTime, load_batch, register_properties
from reva.kg.spacetime import PLACE_LOCATED_IN
from reva.settings import Settings

pytestmark = pytest.mark.db
SRC = "kgfamtest"


@pytest.fixture(scope="module")
def api(db) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    from reva.api.app import create_app
    from reva.api.auth import new_api_token

    register_properties(
        db.worker,
        [
            KgProperty("P186", "item", "made_from_material", "made from material", "matériau"),
            KgProperty("P361", "item", "part_of", "part of", "partie de", "has_part"),
            KgProperty("P1056", "item", "product_or_material_produced", "product or material produced", "produit"),
            KgProperty("P828", "item", "has_cause", "has cause", "a pour cause", "cause_of", "cause of"),
            KgProperty("P625", "globe_coordinate", "coordinate_location", "coordinate location", "coordonnées"),
            KgProperty("syn:unrelated", "item", "unrelated", "unrelated", "sans rapport"),
            PLACE_LOCATED_IN,
            ATLAS_MEASURED_BY,
            # V4ECON's region -> atlas table predicate (registry/kg/relations_v4.d/v4econ.yaml: measured_by)
            KgProperty("econ:measured_by", "item", "econ_measured_by", "measured by (atlas table)", "mesuré par"),
        ],
    )
    raw = db.store.put_bytes(b"kg families test synthetic graph v1", "raw", "application/json").sha256
    names = ["petroleum", "alkane", "hydrocarbon", "firm", "field", "adm1", "country", "war", "other"]
    ents = [KgEntity(f"syn:fam_{n}", "test_thing", {"en": n.title(), "fr": f"{n} (fr)"}) for n in names]
    ents.append(KgEntity("atlas:kgfamtest.reg_pop", "data_series", {"en": "Regional population (test table)"}))
    share = {"P1107": [{"amount": "+0.25", "unit": "1"}]}
    sts = [
        KgStatement("syn:fam_petroleum", "P186", "syn:fam_alkane", statement_id="pa", qualifiers=share),
        KgStatement("syn:fam_alkane", "P361", "syn:fam_hydrocarbon", statement_id="ah"),
        KgStatement("syn:fam_firm", "P1056", "syn:fam_petroleum", statement_id="fp", start=KgTime("1950", 9)),
        KgStatement("syn:fam_field", "place:located_in", "syn:fam_adm1", statement_id="fa"),
        KgStatement("syn:fam_adm1", "place:located_in", "syn:fam_country", statement_id="ac"),
        KgStatement("syn:fam_firm", "place:located_in", "syn:fam_field", statement_id="ff"),
        KgStatement("syn:fam_war", "P828", "syn:fam_petroleum", statement_id="wp"),
        KgStatement("syn:fam_petroleum", "syn:unrelated", "syn:fam_other", statement_id="po"),
        KgStatement(
            "syn:fam_petroleum",
            "atlas:measured_by",
            "atlas:energy.oil_production",
            statement_id="pm",
            qualifiers={"concept": "energy.oil_production", "iso3": "sau"},
        ),
        KgStatement(
            "syn:fam_adm1",
            "econ:measured_by",
            "atlas:kgfamtest.reg_pop",
            statement_id="adm1m",
            qualifiers={"table": "kgfamtest.reg_pop", "provider_geo": "XX01"},
        ),
        KgStatement(
            "syn:fam_adm1",
            "P625",
            value={"type": "globe_coordinate", "latitude": 25.0, "longitude": 49.5},
            statement_id="adm1coord",
        ),
    ]
    rep = load_batch(
        db.worker,
        db.store,
        entities=ents,
        statements=sts,
        source_key=SRC,
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected
    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'kgfam-read')"),
            {"h": sha, "s": ["read"]},
        )
    settings = Settings.model_validate(
        {
            **db.settings.model_dump(),
            "reva_session_secret": SecretStr("s" * 48),
            "reva_allowed_hosts": "testserver,localhost",
            "reva_cors_origins": "",
        }
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            yield client, tok
    finally:
        app.state.engine.dispose()


def get(api: tuple[TestClient, str], path: str, **params: Any) -> Any:
    client, tok = api
    return client.get(path, params=params, headers={"Authorization": f"Bearer {tok}"})


def test_registry(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/families")
    assert r.status_code == 200, r.text
    b = r.json()
    ids = {f["id"] for f in b["data"]["families"]}
    assert {"made_of", "located_in", "associated_with", "measured_by"} <= ids
    assert all(f["causal"] is False for f in b["data"]["families"])
    assert b["warnings"][0]["code"] == "families_not_causal"
    assert any(m["property"] == "P186" and m["family"] == "made_of" for m in b["data"]["mappings"])


def test_entity_families(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/entities/syn:fam_petroleum/families")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    fams = {g["family"]: g for g in d["families"]}
    made = fams["made_of"]["neighbours"][0]
    assert made["neighbour"]["qid"] == "syn:fam_alkane" and made["neighbour"]["entity_loaded"] is True
    # AUDIT3 L7: an edge to an id no source loaded is kept and marked, with a note
    mb = fams["measured_by"]["neighbours"][0]["neighbour"]
    assert mb["qid"] == "atlas:energy.oil_production" and mb["entity_loaded"] is False
    assert any("entity_loaded = false" in n for n in d["notes"])
    assert made["share"] == {
        "value": "0.25",
        "fraction": "0.25",
        "unit": "1",
        "scale": "fraction",
        "qualifier": "P1107",
    }
    assert made["statement"]["provenance"]["source_key"] == SRC and made["rights"] is not None
    # read from the product: the firm's "product or material produced" becomes produced_by
    assert fams["produced_by"]["neighbours"][0]["neighbour"]["qid"] == "syn:fam_firm"
    assert fams["measured_by"]["neighbours"][0]["atlas"] == {
        "kind": "concept",
        "concept": "energy.oil_production",
        "iso3": "SAU",
        "table": None,
        "source": None,
        "dataset": None,
        "provider_geo": None,
        "indicators": [],
        "indicator_contains": None,
    }
    # the causal statement and the unmapped property are not in any family, but are counted
    unm = {(u["pid"], u["direction"]): u for u in d["unmapped"]}
    assert unm[("P828", "in")]["causal_assertion"] is True
    assert ("syn:unrelated", "out") in unm
    assert all(g["causal"] is False for g in d["families"])
    # filter
    r = get(api, "/v1/kg/entities/syn:fam_petroleum/families", family="made_of")
    assert [g["family"] for g in r.json()["data"]["families"]] == ["made_of"]
    # inverse reading of a non-paired family
    d2 = get(api, "/v1/kg/entities/syn:fam_alkane/families").json()["data"]
    fam2 = {g["family"]: g for g in d2["families"]}
    assert fam2["made_of"]["neighbours"][0]["reading"]["inverse"] is True
    assert fam2["part_of"]["neighbours"][0]["neighbour"]["qid"] == "syn:fam_hydrocarbon"


def test_entity_families_problems(api) -> None:  # type: ignore[no-untyped-def]
    assert get(api, "/v1/kg/entities/syn:nope/families").status_code == 404
    r = get(api, "/v1/kg/entities/syn:fam_petroleum/families", family="causes")
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


def test_places(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/entities/syn:fam_firm/places")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert [c["entity"]["qid"] for c in d["chain"]] == [
        "syn:fam_firm",
        "syn:fam_field",
        "syn:fam_adm1",
        "syn:fam_country",
    ]
    assert d["chain"][2]["coordinates"]["latitude"] == 25.0
    assert all(c["basis"] in ("start", "statement") for c in d["chain"])


def test_family_path(api) -> None:  # type: ignore[no-untyped-def]
    r = get(
        api,
        "/v1/kg/families/path",
        **{"from": "syn:fam_firm", "to": "syn:fam_hydrocarbon", "family": ["produces", "made_of", "part_of"]},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["warnings"][0]["code"] == "path_not_causal"
    p = b["data"]["paths"][0]
    assert [h["family"]["id"] for h in p["hops"]] == ["produces", "made_of", "part_of"]
    # the reverse reading is not allowed: hydrocarbon -> alkane is "has_part", not "part_of"
    r = get(
        api,
        "/v1/kg/families/path",
        **{"from": "syn:fam_hydrocarbon", "to": "syn:fam_firm", "family": ["produces", "made_of", "part_of"]},
    )
    assert r.status_code == 200 and r.json()["data"]["found"] is False
    # a causal statement is never a family hop
    r = get(api, "/v1/kg/families/path", **{"from": "syn:fam_war", "to": "syn:fam_alkane", "family": ["made_of"]})
    assert r.json()["data"]["found"] is False


def test_plain_path_hops_carry_family(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/path", **{"from": "syn:fam_petroleum", "to": "syn:fam_hydrocarbon"})
    assert r.status_code == 200, r.text
    hops = r.json()["data"]["paths"][0]["hops"]
    assert [h["family"]["id"] for h in hops] == ["made_of", "part_of"]


def test_measured_by_table_entity_bridge(api) -> None:  # type: ignore[no-untyped-def]
    """V6 INTEG: an atlas table entity (V4ECON convention) resolves to the table + provider geography."""
    r = get(api, "/v1/kg/entities/syn:fam_adm1/families", family="measured_by")
    assert r.status_code == 200, r.text
    (g,) = r.json()["data"]["families"]
    hint = g["neighbours"][0]["atlas"]
    assert hint["kind"] == "table" and hint["concept"] is None
    assert (hint["source"], hint["dataset"], hint["provider_geo"]) == ("kgfamtest", "reg_pop", "XX01")
