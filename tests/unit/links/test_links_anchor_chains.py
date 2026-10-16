"""KGHIST's 20 anchor chains (registry/kg/anchor_chains.yaml) evaluated by the LINKS path engine over the recorded
Wikidata claims (registry/kg/queries/history/_recorded/snapshot_claims_*.json): per-query property allow/deny
lists, ordered hop-type steps, via_any / via_all waypoints, non-transit entity kinds (objects of P37/P2936/P30/P2348
may end a path but are never passed through), k >= 10 candidates, max_hops up to 8. 18 chains expect a sourced
path, 2 must answer "no sourced path" (never a bridged hop)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.links.model import PartialDate, Stmt
from reva.links.paths import PathGraph, PathQuery, build_graph, find_paths

ROOT = Path(__file__).resolve().parents[3]
CHAINS = ROOT / "registry" / "kg" / "anchor_chains.yaml"
RECORDED = ROOT / "registry" / "kg" / "queries" / "history" / "_recorded"


@cache
def _doc() -> dict[str, Any]:
    return yaml.safe_load(CHAINS.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _date(v: Any) -> PartialDate | None:
    if not v:
        return None
    try:
        return PartialDate.parse(v[0] if isinstance(v, list) else v, 9)
    except ValueError:
        return None


@cache
def _graph() -> PathGraph:
    sts = []
    for p in sorted(RECORDED.glob("snapshot_claims_*.json")):
        ents = json.loads(p.read_text(encoding="utf-8"))["entities"]
        for q, e in ents.items():
            for t in e["claims"]:
                o = t["o"]
                if not (isinstance(o, str) and o.startswith("Q") and o[1:].isdigit()) or o == q:
                    continue
                quals = t.get("q") or {}
                sts.append(
                    Stmt(
                        t["statement"],
                        q,
                        t["p"],
                        o,
                        start=_date(quals.get("P580")),
                        end=_date(quals.get("P582")),
                        point=_date(quals.get("P585")),
                        rank=t.get("rank", "normal"),
                        source_key="wikidata",
                        source_revision=str(e.get("lastrevid")),
                        origin="test_fixture",
                    )
                )
    return build_graph(sts)


def query_for(chain: dict[str, Any]) -> PathQuery:
    doc = _doc()
    classes = {k: set(v["properties"]) for k, v in doc["hop_classes"].items()}
    allowed = set().union(*classes.values()) - set(doc["excluded_properties"])
    for x in chain.get("exclude", []):
        allowed -= classes[x]
    if chain.get("allowed_classes"):
        allowed &= set().union(*(classes[x] for x in chain["allowed_classes"]))
    qid = lambda k: str(doc["entities"][k]["qid"])  # noqa: E731
    return PathQuery(
        qid(chain["from"]),
        qid(chain["to"]),
        k=10,
        max_hops=int(chain["max_hops"]),
        kinds=frozenset(allowed),
        steps=tuple(frozenset(set().union(*(classes[c] for c in grp)) & allowed) for grp in chain["steps"]),
        via_any=frozenset(qid(k) for k in chain.get("via_any", [])),
        via_all=frozenset(qid(k) for k in chain.get("via_all", [])),
        non_transit_props=frozenset(doc["non_transit_object_properties"]),
    )


@pytest.mark.parametrize("chain_id", [c["id"] for c in yaml.safe_load(CHAINS.read_text())["chains"]])
def test_anchor_chain(chain_id: str) -> None:
    chain = next(c for c in _doc()["chains"] if c["id"] == chain_id)
    q = query_for(chain)
    res = find_paths(_graph(), q)
    if chain["expectation"] == "path_expected":
        assert res.found, (chain_id, res.reason, res.truncation_reasons)
        nt = _graph().non_transit(q.non_transit_props)
        for p in res.paths:
            assert len(p.hops) <= q.max_hops
            assert not (set(p.nodes[1:-1]) & nt), (chain_id, p.nodes)  # languages/continents/eras never transited
            assert all(h.edge.evidence and h.edge.evidence[0].statement_id for h in p.hops)
            assert all(h.edge.kind_key in {f"statement:{x}" for x in q.kinds or ()} for h in p.hops)
            assert all("parce que" not in t and "because" not in t for h in p.hops for t in h.text.values())
    else:
        assert not res.found, (chain_id, [p.nodes for p in res.paths])
        assert res.reason == "no_path_satisfying_constraints" or res.reason == "no_path_within_max_hops"


def test_twenty_chains_eighteen_expected() -> None:
    exp = [c["expectation"] for c in _doc()["chains"]]
    assert len(exp) == 20 and exp.count("path_expected") == 18


def test_without_non_transit_columbus_portuguese_is_a_trivial_language_hop() -> None:
    chain = next(c for c in _doc()["chains"] if c["id"] == "columbus_portuguese_brazil")
    q = query_for(chain)
    plain = PathQuery(q.source, q.target, k=1, max_hops=3)
    res = find_paths(_graph(), plain)
    constrained = find_paths(_graph(), q)
    assert constrained.found and len(constrained.paths[0].hops) > (len(res.paths[0].hops) if res.found else 0)
    assert "Q180897" in constrained.paths[0].nodes  # via the Treaty of Tordesillas
