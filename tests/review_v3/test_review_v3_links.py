"""Links path engine (reva.links.paths): defects found by the V3 review (docs/dev/review_v3.md, LINKS-*)."""

from __future__ import annotations

from reva.links.model import Stmt
from reva.links.paths import PathQuery, build_graph, find_paths

S, T, M = "Q900101", "Q900102", "Q900103"


def test_truncated_neighbourhood_is_reported_by_the_path_result() -> None:
    g2 = build_graph([Stmt("syn:4", S, "P37", M), Stmt("syn:5", T, "P37", "Q900104")])
    g2.truncated_nodes = {S}
    res2 = find_paths(g2, PathQuery(S, T))
    assert res2.reason == "no_path_within_max_hops"
    assert res2.truncated, "a 'no path' answer over a truncated neighbourhood must say the search was truncated"
