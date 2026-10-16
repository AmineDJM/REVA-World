"""Event view (EVENTS) over a synthetic graph: classification (rank-aware P31 + P279 chains), interval with
precision, places, participants with roles (incl. inverse P1344/P607), structure in both directions, attributed
causal statements, listing filters and pagination, and the /v1/events routes.

Synthetic entities (``evsyn:`` ids, source ``eventstest``) use real Wikidata class QIDs (Q198 war, Q8465 civil
war, Q178561 battle, Q131569 treaty, ...); nothing here is a real-world fact.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from reva.events import view as ev
from reva.events.taxonomy import RecordedClasses
from reva.kg.model import KgEntity, KgProperty, KgStatement, KgTime, load_batch, register_properties
from reva.settings import Settings

pytestmark = pytest.mark.db
SRC = "eventstest"
EMPTY = RecordedClasses({}, {})

PROPS = [
    ("P31", "item", "instance_of"),
    ("P279", "item", "subclass_of"),
    ("P580", "time", "start_time"),
    ("P582", "time", "end_time"),
    ("P585", "time", "point_in_time"),
    ("P571", "time", "inception"),
    ("P17", "item", "country"),
    ("P276", "item", "location"),
    ("P710", "item", "participant"),
    ("P1344", "item", "participant_in"),
    ("P607", "item", "conflict"),
    ("P361", "item", "part_of"),
    ("P527", "item", "has_part"),
    ("P155", "item", "follows"),
    ("P156", "item", "followed_by"),
    ("P828", "item", "has_cause"),
    ("P1542", "item", "has_effect"),
    ("P1891", "item", "signatory"),
]


def t(iso: str, prec: int) -> dict[str, Any]:
    return {"type": "time", "time": iso, "precision": prec, "calendar": "gregorian"}


@pytest.fixture(scope="module")
def graph(db) -> Any:  # type: ignore[no-untyped-def]
    register_properties(db.worker, [KgProperty(p, k, n, n.replace("_", " ")) for p, k, n in PROPS])
    raw = db.store.put_bytes(b"events view synthetic graph v1", "raw", "application/json").sha256
    names = {
        "war1": ("Synthetic colonial war", "Guerre coloniale synthétique"),
        "op1": ("Synthetic operation", None),
        "battle1": ("Synthetic battle", None),
        "undated1": ("Undated war", None),
        "bce1": ("Ancient treaty", None),
        "person1": ("Not an event", None),
        "cls_colonial": ("synthetic war class", None),
        "fln": ("Synthetic front", None),
        "place1": ("Synthetic place", None),
        "big": ("Synthetic big war", None),
        "peace1": ("Synthetic peace", None),
        "cause1": ("Synthetic cause", None),
        "eff1": ("Synthetic effect", None),
        "role1": ("guerrilla", None),
    }
    ents = [
        KgEntity(f"evsyn:{k}", "test_event", {"en": en, **({"fr": fr} if fr else {})}) for k, (en, fr) in names.items()
    ]
    ents += [
        KgEntity(q, "test_place", {"en": lab})
        for q, lab in (("Q262", "Algeria"), ("Q142", "France"), ("Q30", "United States"))
    ]
    S = KgStatement
    sts = [
        S("evsyn:cls_colonial", "P279", "Q198", statement_id="c1"),
        S("evsyn:war1", "P31", "evsyn:cls_colonial", statement_id="w31a", rank="preferred"),
        S("evsyn:war1", "P31", "Q1190554", statement_id="w31b"),
        S("evsyn:war1", "P580", value=t("1954-11-01", 11), statement_id="w580"),
        S("evsyn:war1", "P582", value=t("1962-03-19", 11), statement_id="w582"),
        S("evsyn:war1", "P585", value=t("1957", 9), statement_id="w585"),
        S("evsyn:war1", "P17", "Q262", statement_id="w17"),
        S("evsyn:war1", "P276", "evsyn:place1", statement_id="w276"),
        S("evsyn:war1", "P710", "Q142", statement_id="w710", qualifiers={"P3831": ["evsyn:role1"]}),
        S("evsyn:fln", "P607", "evsyn:war1", statement_id="f607"),
        S("Q30", "P1344", "evsyn:war1", statement_id="us1344", start=KgTime("1956", 9)),
        S("evsyn:war1", "P361", "evsyn:big", statement_id="w361"),
        S("evsyn:battle1", "P361", "evsyn:war1", statement_id="b361"),
        S("evsyn:war1", "P156", "evsyn:peace1", statement_id="w156"),
        S("evsyn:war1", "P828", "evsyn:cause1", statement_id="w828"),
        S("evsyn:eff1", "P828", "evsyn:war1", statement_id="e828"),
        S("evsyn:op1", "P31", "Q645883", statement_id="o31a"),
        S("evsyn:op1", "P31", "Q8465", statement_id="o31b", rank="preferred"),
        S("evsyn:op1", "P580", value=t("1990", 9), statement_id="o580"),
        S("evsyn:op1", "P582", value=t("1980", 9), statement_id="o582"),
        S("evsyn:battle1", "P31", "Q178561", statement_id="b31"),
        S("evsyn:battle1", "P585", value=t("1956-05-18", 11), statement_id="b585"),
        S("evsyn:battle1", "P17", "Q262", statement_id="b17"),
        S("evsyn:undated1", "P31", "Q198", statement_id="u31"),
        S("evsyn:bce1", "P31", "Q131569", statement_id="t31"),
        S("evsyn:bce1", "P585", value=t("-0043-03-15", 11), statement_id="t585"),
        S("evsyn:bce1", "P1891", "Q142", statement_id="t1891", point_in_time=KgTime("-0043-03-15", 11)),
        S("evsyn:person1", "P31", "Q5", statement_id="p31"),
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
    return db


def test_classification_is_rank_aware_and_cites_its_chain(graph) -> None:  # type: ignore[no-untyped-def]
    with graph.api.connect() as c:
        war = ev.classify(c, "evsyn:war1", recorded=EMPTY)
        op = ev.classify(c, "evsyn:op1", recorded=EMPTY)
        person = ev.classify(c, "evsyn:person1", recorded=EMPTY)
    assert war["is_event"] and [x["id"] for x in war["primary"]] == ["war"]
    m = next(x for x in war["matches"] if x["type"] == "war")
    hop = m["chain"][0]
    assert m["rank"] == "preferred" and hop["via"] == "kg"
    assert (hop["class"], hop["subclass_of"]) == ("evsyn:cls_colonial", "Q198")
    assert m["statement"]["statement_key"] == f"{SRC}:w31a"
    # preferred P31 (civil war) decides the primary type; the normal-rank military operation stays listed
    assert [x["id"] for x in op["primary"]] == ["civil_war"]
    assert [x["id"] for x in op["secondary"]] == ["military_operation"]
    assert not person["is_event"] and person["p31_unmatched"] == ["Q5"]


def test_recorded_p279_is_a_fallback_only(graph) -> None:  # type: ignore[no-untyped-def]
    rec = RecordedClasses(
        {"evsyn:cls_colonial": (("Q178561", "normal", "x"),), "Q5": (("Q198", "normal", "y"),)},
        {"Q5": {"lastrevid": 1, "raw_sha256": "0" * 64}},
    )
    with graph.api.connect() as c:
        war = ev.classify(c, "evsyn:war1", recorded=rec)  # KG has P279 for cls_colonial: recorded ignored
        person = ev.classify(c, "evsyn:person1", recorded=rec)  # no KG P279 for Q5: recorded chain used
    assert [x["id"] for x in war["primary"]] == ["war"]
    assert [x["id"] for x in person["primary"]] == ["war"]
    assert person["matches"][0]["chain"][0]["via"] == "recorded"


def test_event_card(graph) -> None:  # type: ignore[no-untyped-def]
    with graph.api.connect() as c:
        card = ev.event_card(c, "evsyn:war1", recorded=EMPTY)
    assert card is not None and card["label"] == "Synthetic colonial war"
    iv = card["interval"]
    assert iv["start"]["time"] == "1954-11-01" and iv["start"]["precision"] == 11
    assert iv["earliest"] == "1954-11-01" and iv["latest"] == "1962-03-19" and iv["consistency"] == "consistent"
    assert iv["point_in_time"]["lo"] == "1957-01-01" and iv["point_in_time"]["hi"] == "1957-12-31"
    assert {(p["relation"], p["place"]["id"]) for p in card["places"]} == {
        ("country", "Q262"),
        ("location", "evsyn:place1"),
    }
    parts = {(p["participant"]["id"], p["role"]["id"], p["direction"]) for p in card["participants"]}
    assert parts == {("Q142", "participant", "out"), ("evsyn:fln", "belligerent", "in"), ("Q30", "participant", "in")}
    fr = next(p for p in card["participants"] if p["participant"]["id"] == "Q142")
    assert fr["role_qualifiers"][0]["label"] == "guerrilla"
    us = next(p for p in card["participants"] if p["participant"]["id"] == "Q30")
    assert us["valid_from"] == "1956-01-01"
    rel = {(s["relation"], s["event"]["id"], s["direction"]) for s in card["structure"]}
    assert rel == {
        ("part_of", "evsyn:big", "out"),
        ("has_part", "evsyn:battle1", "in"),
        ("followed_by", "evsyn:peace1", "out"),
    }
    causal = {(x["direction"], x["other"]["id"]) for x in card["causal_statements"]}
    assert causal == {("out", "evsyn:cause1"), ("in", "evsyn:eff1")}
    assert all("not established by REVA" in x["wording"] and x["asserted_by"] == SRC for x in card["causal_statements"])
    assert all(p["provenance"]["raw_sha256"] for p in card["places"])
    assert "not causation" in card["notes"][0]


def test_interval_inconsistency_and_bce(graph) -> None:  # type: ignore[no-untyped-def]
    with graph.api.connect() as c:
        op = ev.event_card(c, "evsyn:op1", recorded=EMPTY)
        bce = ev.event_card(c, "evsyn:bce1", recorded=EMPTY)
        und = ev.event_card(c, "evsyn:undated1", recorded=EMPTY)
        assert ev.event_card(c, "evsyn:person1", recorded=EMPTY) is None
    assert op["interval"]["consistency"] == "inconsistent"  # end 1980 before start 1990: shown, not fixed
    assert bce["interval"]["earliest"] == "-0043-03-15" and bce["types"]["primary"][0]["id"] == "treaty"
    sig = bce["participants"][0]
    assert sig["role"]["id"] == "signatory" and sig["statements"][0]["point_in_time"]["time"] == "-0043-03-15"
    assert und["interval"]["basis"] == "undated" and und["interval"]["consistency"] == "undetermined"


def test_listing_filters_and_pagination(graph) -> None:  # type: ignore[no-untyped-def]
    with graph.api.connect() as c:
        # (other test modules may share the session database: always filter on a window or a country)
        wars = ev.list_events(
            c, types=["war"], start="1950", end="1999", recorded=EMPTY, origin="test_fixture", limit=200
        )
        ids = [i["id"] for i in wars["items"]]
        assert "evsyn:war1" in ids and "evsyn:op1" in ids and "evsyn:battle1" not in ids
        assert "evsyn:undated1" not in ids
        conf = ev.list_events(
            c, types=["armed_conflict"], start="1956", end="1956", recorded=EMPTY, origin="test_fixture"
        )
        assert {i["id"] for i in conf["items"]} >= {"evsyn:war1", "evsyn:battle1"}
        assert "evsyn:undated1" not in {i["id"] for i in conf["items"]}
        dz = ev.list_events(c, types=["armed_conflict"], country="Q262", recorded=EMPTY, origin="test_fixture")
        assert {i["id"] for i in dz["items"] if i["id"].startswith("evsyn:")} == {"evsyn:war1", "evsyn:battle1"}
        fr = ev.list_events(c, types=["war"], country="Q142", recorded=EMPTY, origin="test_fixture")
        assert [i["id"] for i in fr["items"] if i["id"].startswith("evsyn:")] == []
        fr = ev.list_events(
            c, types=["war"], country="Q142", involving=True, recorded=EMPTY, origin="test_fixture", limit=200
        )
        assert [i["id"] for i in fr["items"] if i["id"].startswith("evsyn:")] == ["evsyn:war1"]
        seen: list[str] = []
        cur = None
        for _ in range(200):
            page = ev.list_events(
                c,
                types=["armed_conflict"],
                limit=2,
                cursor=cur,
                include_undated=True,
                start="1955",
                end="1957",
                recorded=EMPTY,
                origin="test_fixture",
            )
            seen += [i["id"] for i in page["items"]]
            cur = page["next"]
            if cur is None:
                break
        assert len(seen) == len(set(seen)) and {"evsyn:war1", "evsyn:battle1", "evsyn:undated1"} <= set(seen)
        bce = ev.list_events(c, types=["treaty"], start="-0050", end="-0040", recorded=EMPTY, origin="test_fixture")
        assert [i["id"] for i in bce["items"]] == ["evsyn:bce1"] and bce["items"][0]["earliest"] == "-0043-03-15"


# ---------------------------------------------------------------------------------------------- API
@pytest.fixture(scope="module")
def api(graph) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    from reva.api.app import create_app
    from reva.api.auth import new_api_token

    tok, sha = new_api_token()
    with graph.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'events-read')"),
            {"h": sha, "s": ["read"]},
        )
    settings = Settings.model_validate(
        {
            **graph.settings.model_dump(),
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


def test_api_event_card(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/events/evsyn:war1")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["data"]["types"]["primary"][0]["id"] == "war" and b["origin"] == "test_fixture"
    assert {w["code"] for w in b["warnings"]} >= {"context_not_causal", "fixture_data"}
    assert get(api, "/v1/events/evsyn:person1").status_code == 404
    assert get(api, "/v1/events/evsyn:nope").status_code == 404
    assert get(api, "/v1/events/FRA").status_code == 422


def test_api_listing_and_types(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/events/search", type="armed_conflict", country="DZA", origin="test_fixture")
    assert r.status_code == 200, r.text
    assert {i["id"] for i in r.json()["data"]["items"]} >= {"evsyn:war1", "evsyn:battle1"}
    r = get(api, "/v1/events/search", type="armed_conflict", country="DZA", limit=1, origin="test_fixture")
    assert r.json()["next_cursor"]
    r2 = get(
        api,
        "/v1/events/search",
        type="armed_conflict",
        country="DZA",
        limit=1,
        origin="test_fixture",
        cursor=r.json()["next_cursor"],
    )
    assert r2.status_code == 200 and r2.json()["data"]["items"][0]["id"] != r.json()["data"]["items"][0]["id"]
    assert get(api, "/v1/events/search", type="no_such_type").status_code == 422
    assert get(api, "/v1/events/search", **{"from": "1970", "to": "1960"}).status_code == 422
    assert get(api, "/v1/events/search", cursor="!!").status_code == 400
    r = get(api, "/v1/events/types")
    assert r.status_code == 200 and any(
        x["id"] == "war" and x["parent"] == "armed_conflict" for x in r.json()["data"]["types"]
    )
    client, _ = api
    assert client.get("/v1/events/types").status_code == 401


def test_marker_kinds_and_coded_exclusions(graph) -> None:  # type: ignore[no-untyped-def]
    """Records typed by a dataset's own event-type property (CAUSDATA-style): a coded outcome is not an event."""
    db = graph
    register_properties(db.worker, [KgProperty("causdata:event_type", "string", "causdata_event_type", "event type")])
    raw = db.store.put_bytes(b"events marker kinds synthetic v1", "raw", "application/json").sha256
    sv = {"type": "string"}
    sts = [
        KgStatement(
            "evsyn:icb1", "causdata:event_type", value={**sv, "value": "international_crisis"}, statement_id="k1"
        ),
        KgStatement("evsyn:icb1", "P580", value=t("1962-10-16", 11), statement_id="k2"),
        KgStatement(
            "evsyn:icb1:outcome", "causdata:event_type", value={**sv, "value": "crisis_outcome"}, statement_id="k3"
        ),
        KgStatement("evsyn:icb1:outcome", "P585", value=t("1962-11-20", 11), statement_id="k4"),
    ]
    rep = load_batch(
        db.worker,
        db.store,
        statements=sts,
        source_key="eventstest.causdata",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected
    with db.api.connect() as c:
        crisis = ev.classify(c, "evsyn:icb1", recorded=EMPTY)
        outcome = ev.classify(c, "evsyn:icb1:outcome", recorded=EMPTY)
        listed = ev.list_events(
            c, types=["crisis"], start="1962", end="1962", recorded=EMPTY, origin="test_fixture", limit=200
        )
    assert [x["id"] for x in crisis["primary"]] == ["international_crisis"] and crisis["basis"] == "dataset_kind"
    assert not outcome["is_event"]
    ids = {i["id"] for i in listed["items"]}
    assert "evsyn:icb1" in ids and "evsyn:icb1:outcome" not in ids
