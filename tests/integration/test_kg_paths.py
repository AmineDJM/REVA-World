"""Path engine, neighbours, timeline and search (``reva.kg.query``) on a small synthetic graph.

The graph is synthetic (``pth:`` ids, source ``kgpathtest``); nothing here is a real-world fact::

    a --linked--> b --linked[1500..1600]--> c --linked--> d          (3 hops, middle hop dated)
    b --member[1900..1950]--> e --member[1900..1950]--> c            (detour dated in the 20th century)
    a --member--> hub <--member-- d,  x01..x30 --member--> hub      (hub: 32 entity-valued statements)
    a --P31--> cls <--P31-- d                                         (class membership, excluded by default)
    war --P828 has cause--> spark                                     (a source-asserted causal statement)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from reva.kg import query as kq
from reva.kg.model import KgEntity, KgProperty, KgStatement, KgTime, load_batch, register_properties

pytestmark = pytest.mark.db
SRC = "kgpathtest"


def L(qid: str) -> str:  # noqa: N802
    return qid.split(":")[1].upper()


@pytest.fixture(scope="module")
def g(db):  # type: ignore[no-untyped-def]
    register_properties(
        db.worker,
        [
            KgProperty("syn:linked", "item", "linked_to", "linked to", "lié à", "linked_from", "linked from", "lié depuis"),
            KgProperty("syn:member", "item", "member_of", "member of", "membre de", "has_member", "has member", "a pour membre"),
            KgProperty("syn:founded", "time", "founded", "founded", "fondé"),
            KgProperty("P31", "item", "instance_of", "instance of", "nature de l'élément"),
            KgProperty("P828", "item", "has_cause", "has cause", "a pour cause", "cause_of", "cause of"),
        ],
    )
    raw = db.store.put_bytes(b"kg path test synthetic graph v1", "raw", "application/json").sha256
    names = ["a", "b", "c", "d", "e", "hub", "cls", "war", "spark"] + [f"x{i:02d}" for i in range(1, 31)]
    ents = [KgEntity(f"pth:{n}", "test_thing", {"en": f"Node {n.upper()}", "fr": f"Nœud {n.upper()}"}, sitelinks=len(n))
            for n in names]
    y = lambda v: KgTime(v, 9)  # noqa: E731
    sts = [
        KgStatement("pth:a", "syn:linked", "pth:b", statement_id="ab"),
        KgStatement("pth:b", "syn:linked", "pth:c", statement_id="bc", start=y("1500"), end=y("1600")),
        KgStatement("pth:c", "syn:linked", "pth:d", statement_id="cd"),
        KgStatement("pth:b", "syn:member", "pth:e", statement_id="be", start=y("1900"), end=y("1950")),
        KgStatement("pth:e", "syn:member", "pth:c", statement_id="ec", start=y("1900"), end=y("1950")),
        KgStatement("pth:a", "syn:member", "pth:hub", statement_id="ah"),
        KgStatement("pth:d", "syn:member", "pth:hub", statement_id="dh"),
        KgStatement("pth:a", "P31", "pth:cls", statement_id="acls"),
        KgStatement("pth:d", "P31", "pth:cls", statement_id="dcls"),
        KgStatement("pth:war", "P828", "pth:spark", statement_id="cause", references=[{"P248": "pth:book"}]),
        KgStatement("pth:a", "syn:founded", value={"type": "time", "time": "+1492-10-12T00:00:00Z", "precision": 11},
                    statement_id="founded"),
    ] + [KgStatement(f"pth:x{i:02d}", "syn:member", "pth:hub", statement_id=f"xh{i}") for i in range(1, 31)]
    rep = load_batch(db.worker, db.store, entities=ents, statements=sts, source_key=SRC, raw_sha256s=[raw],
                     retrieved_at=datetime.now(UTC), origin="test_fixture")
    assert rep.rejected_total == 0, rep.rejected
    with db.api.connect() as conn:
        yield conn


def hop_ids(p: dict) -> list[tuple[str, str]]:
    return [(h["from"]["qid"], h["to"]["qid"]) for h in p["hops"]]


def test_shortest_path_skips_hubs_and_class_membership(g) -> None:  # type: ignore[no-untyped-def]
    r = kq.paths(g, "pth:a", "pth:d", hub_limit=10)
    assert r["found"]
    assert [hop_ids(p) for p in r["paths"]] == [[("pth:a", "pth:b"), ("pth:b", "pth:c"), ("pth:c", "pth:d")]]
    assert {h["qid"] for h in r["search"]["hubs_skipped"]} == {"pth:hub"}
    assert r["search"]["hubs_skipped"][0]["degree_at_least"] > 10
    assert r["search"]["excluded_properties"] == ["P31", "P279"]
    hop = r["paths"][0]["hops"][1]
    assert hop["provenance"]["statement_key"] == f"{SRC}:bc" and hop["provenance"]["raw_sha256"]
    assert hop["start"] == {"time": "1500", "precision": 9} and hop["valid_from"] == "1500-01-01"
    assert hop["wording"] == "Node B is connected via 'linked to' to Node C"
    assert "cause" not in r["paths"][0]["wording"].replace("not a cause", "")


def test_allowed_hub_and_inverse_hop_labels(g) -> None:  # type: ignore[no-untyped-def]
    r = kq.paths(g, "pth:a", "pth:d", hub_limit=10, allow_hubs=["pth:hub"])
    (p,) = r["paths"]
    assert hop_ids(p) == [("pth:a", "pth:hub"), ("pth:hub", "pth:d")]
    assert [h["direction"] for h in p["hops"]] == ["forward", "inverse"]
    assert p["hops"][1]["relation_label_en"] == "has member" and p["hops"][1]["relation_label_fr"] == "a pour membre"
    # with the default hub limit the hub is an ordinary node
    assert kq.paths(g, "pth:a", "pth:d")["paths"][0]["length"] == 2


def test_hub_endpoint_is_always_traversable(g) -> None:  # type: ignore[no-untyped-def]
    r = kq.paths(g, "pth:hub", "pth:x07", hub_limit=10)
    assert [hop_ids(p) for p in r["paths"]] == [[("pth:hub", "pth:x07")]]


def test_allowed_properties_restrict_hops(g) -> None:  # type: ignore[no-untyped-def]
    r = kq.paths(g, "pth:a", "pth:d", allowed_properties=["P31"])
    assert [hop_ids(p) for p in r["paths"]] == [[("pth:a", "pth:cls"), ("pth:cls", "pth:d")]]
    assert r["search"]["excluded_properties"] == ["P279"]
    r = kq.paths(g, "pth:a", "pth:d", allowed_properties=["syn:member"], hub_limit=10)
    assert not r["found"] and "hub" in r["not_found_reason"]


def test_time_windows(g) -> None:  # type: ignore[no-untyped-def]
    # 1550: the dated middle hop is valid; the undated hops are allowed and flagged
    r = kq.paths(g, "pth:a", "pth:d", hub_limit=10, time_window=kq.window("1550", "1560"))
    (p,) = r["paths"]
    assert hop_ids(p)[1] == ("pth:b", "pth:c")
    assert [h["time_status"] for h in p["hops"]] == ["undated", "within_window", "undated"]
    # 1920: only the 20th-century detour is valid
    r = kq.paths(g, "pth:a", "pth:d", hub_limit=10, time_window=kq.window("1920", "1925"))
    assert [hop_ids(p) for p in r["paths"]] == [
        [("pth:a", "pth:b"), ("pth:b", "pth:e"), ("pth:e", "pth:c"), ("pth:c", "pth:d")]
    ]
    # excluding undated statements leaves no connection
    r = kq.paths(g, "pth:a", "pth:d", hub_limit=10, time_window=kq.window("1920", "1925", include_undated=False))
    assert not r["found"] and r["paths"] == []
    # 1700: nothing dated is valid and there is no undated route without the hub
    assert not kq.paths(g, "pth:a", "pth:d", hub_limit=10, time_window=kq.window("1700", "1700"))["found"]


def test_max_hops_bound_and_truncation_are_reported(g) -> None:  # type: ignore[no-untyped-def]
    r = kq.paths(g, "pth:a", "pth:d", hub_limit=10, max_hops=2)
    assert not r["found"] and "within 2 hops" in r["not_found_reason"]
    r = kq.paths(g, "pth:hub", "pth:a", max_edges_per_level=5, hub_limit=10)
    assert r["search"]["truncated"] and r["search"]["truncation_reasons"]
    from reva.kernel.errors import ValidationProblem

    for bad in ({"max_hops": 6}, {"max_hops": 0}, {"hub_limit": 0}):
        with pytest.raises(ValidationProblem):
            kq.paths(g, "pth:a", "pth:d", **bad)
    with pytest.raises(ValidationProblem):
        kq.paths(g, "pth:a", "FRA")


def test_causal_wording_only_for_source_asserted_cause(g) -> None:  # type: ignore[no-untyped-def]
    r = kq.paths(g, "pth:war", "pth:spark")
    (hop,) = r["paths"][0]["hops"]
    assert hop["causal_assertion"] == {"asserted_by": SRC, "statement_key": f"{SRC}:cause"}
    assert f"asserted by {SRC}" in hop["wording"] and "not established by REVA" in hop["wording"]
    assert hop["provenance"]["references"] == [{"P248": "pth:book"}]
    other = kq.paths(g, "pth:a", "pth:b")["paths"][0]["hops"][0]
    assert other["causal_assertion"] is None and "connected via" in other["wording"]
    assert "cause" not in other["wording"]


def test_neighbours_direction_kind_filter_and_pagination(g) -> None:  # type: ignore[no-untyped-def]
    out = kq.neighbours(g, "pth:hub", direction="in", limit=10)
    assert len(out["edges"]) == 10 and out["next_after_id"] is not None
    rest = kq.neighbours(g, "pth:hub", direction="in", limit=100, after_id=out["next_after_id"])
    assert len(out["edges"]) + len(rest["edges"]) == 32 and rest["next_after_id"] is None
    assert {e["direction"] for e in out["edges"]} == {"in"}
    a = kq.neighbours(g, "pth:a", direction="out")
    assert {e["object"]["qid"] for e in a["edges"]} == {"pth:b", "pth:hub", "pth:cls"}  # literals excluded
    a = kq.neighbours(g, "pth:a", direction="out", include_literals=True)
    assert any(e["value"] and e["value"]["type"] == "time" for e in a["edges"])
    b = kq.neighbours(g, "pth:b", properties=["syn:member"], time_window=kq.window("1910", "1911"))
    assert {e["object"]["qid"] for e in b["edges"]} == {"pth:e"}
    assert all(e["provenance"]["verification"] == "source_reported_not_verified" for e in b["edges"])


def test_timeline_is_sorted_and_includes_time_values(g) -> None:  # type: ignore[no-untyped-def]
    tl = kq.timeline(g, "pth:b")
    assert [(e["sort_date"], e["provenance"]["statement_key"]) for e in tl["events"]] == [
        ("1500-01-01", f"{SRC}:bc"), ("1900-01-01", f"{SRC}:be")
    ]
    tl = kq.timeline(g, "pth:a")
    assert [e["dated_by"] for e in tl["events"]] == ["time_value"] and tl["events"][0]["sort_date"] == "1492-10-12"
    assert kq.timeline(g, "pth:b", time_window=kq.window("1800", None))["events"][0]["sort_date"] == "1900-01-01"


def test_search_ranks_exact_then_prefix_and_reads_both_languages(g) -> None:  # type: ignore[no-untyped-def]
    r = kq.search(g, "node b")
    assert r["results"][0]["qid"] == "pth:b" and r["results"][0]["match"] == "exact"
    r = kq.search(g, "nœud x0", langs=["fr"], limit=50)
    assert {h["qid"] for h in r["results"]} == {f"pth:x0{i}" for i in range(1, 10)}
    assert all(h["match"] == "prefix" for h in r["results"])
    r = kq.search(g, "ode h")  # substring
    assert [h["qid"] for h in r["results"]] == ["pth:hub"] and r["results"][0]["match"] == "substring"
    assert kq.search(g, "100%_x")["results"] == []  # LIKE wildcards are escaped


def test_entity_snapshot(g) -> None:  # type: ignore[no-untyped-def]
    e = kq.entity(g, "pth:hub")
    assert e["labels"] == {"en": "Node HUB", "fr": "Nœud HUB"} and e["statements_in"] == 32
    assert e["provenance"]["source_key"] == SRC and len(e["provenance"]["raw_sha256s"]) == 1
    assert kq.entity(g, "pth:nothing") is None
