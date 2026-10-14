"""World graph library: pure parts (no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from reva.kernel.errors import CapabilityGap, IncompatibleSemantics, ValidationProblem
from reva.world import graph as g
from reva.world.entities import check_limit, decode_cursor, encode_cursor
from reva.world.events import Occurrence, TimeBounds, sequence_relations, temporal_relation
from reva.world.queries import FlowItem, GraphContext, aggregate_flow_items

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _edge(*participants: tuple[str, UUID], layer: str = "trade.crude_petroleum") -> g.Hyperedge:
    parts = tuple(
        g.Participant(i, role, eid, f"E{str(eid)[:4]}", "entity.country", "undirected")
        for i, (role, eid) in enumerate(participants)
    )
    return g.Hyperedge(
        id=uuid4(),
        predicate="predicate.trade_flow",
        predicate_version="1.0.0",
        layer=layer,
        support={},
        provenance=g.EdgeProvenance("evidence", None, uuid4(), "test_fixture", "src", 1, None, uuid4()),
        component={},
        existence_uncertainty={"status": "unquantified"},
        flow_magnitude={"status": "not_recorded"},
        tie_strength=dict(g.NOT_ASSESSED_STRENGTH),
        scientific_confidence=dict(g.NOT_ASSESSED_CONFIDENCE),
        recorded_at=datetime.now(UTC),
        participants=parts,
    )


def _flow(
    value: str | None,
    *,
    breakdown: str = "bilateral",
    classification: str | None = "H6",
    valuation: str = "CIF",
    year: int = 2023,
    concept: str = "trade.hs2709.import_value.bilateral",
    subject: UUID | None = None,
    support: UUID | None = None,
    kind: str = "observation",
    flow: str = "M",
) -> FlowItem:
    v = (
        {"kind": "missing", "reason": "unavailable"}
        if value is None
        else {
            "kind": "decimal",
            "value": value,
            "unit_ref": "unit.usd",
            "basis": {"currency": "USD", "price_basis": "nominal", "scale_factor": "1", "valuation": valuation},
        }
    )
    return FlowItem(
        id=uuid4(),
        record_kind=kind,
        subject_id=subject or uuid4(),
        concept_id=concept,
        concept_version="1.0.0",  # type: ignore[arg-type]
        scale_type="flow",
        breakdown=breakdown,
        support_id=support or uuid4(),
        temporal_extent={"start": f"{year}-01-01", "end": f"{year + 1}-01-01", "label": str(year)},
        value=v,
        classification=classification,
        flow=flow,
        uncertainty_status="unknown",
    )


# ---------------------------------------------------------------------------
# flow aggregation
# ---------------------------------------------------------------------------


def test_compatible_bilateral_flows_sum_exactly() -> None:
    agg = aggregate_flow_items([_flow("1.10"), _flow("2.205"), _flow("3")])
    assert len(agg.groups) == 1
    grp = agg.groups[0]
    assert grp.total == {
        "kind": "decimal",
        "value": "6.305",
        "unit_ref": "unit.usd",
        "basis": {"currency": "USD", "price_basis": "nominal", "scale_factor": "1", "valuation": "CIF"},
    }
    assert grp.n_items == 3 and not grp.is_lower_bound
    assert agg.semantics["breakdown"] == "bilateral" and agg.semantics["classification"] == "H6"
    assert agg.uncertainty["status"] == "unquantified"
    assert agg.as_dict()["derived"] is True


def test_world_total_plus_bilateral_is_refused() -> None:
    total = _flow("10", breakdown="partner_world_total", concept="trade.hs2709.import_value.world_total")
    with pytest.raises(IncompatibleSemantics) as exc:
        aggregate_flow_items([total, _flow("3")])
    assert exc.value.details["reason"] == "mixed_breakdown"


@pytest.mark.parametrize(
    ("a", "b", "reason"),
    [
        ({"classification": "H5"}, {"classification": "H6"}, "mixed_classification"),
        ({"classification": None}, {"classification": "H6"}, "mixed_classification"),
        ({"valuation": "CIF"}, {"valuation": "FOB"}, "mixed_valuation"),
        ({"flow": "M"}, {"flow": "X"}, "mixed_flow_direction"),
        ({"kind": "observation"}, {"kind": "estimate"}, "mixed_record_kind"),
        ({"concept": "c.one"}, {"concept": "c.two"}, "mixed_concept"),
    ],
)
def test_incompatible_semantics_are_refused(a: dict, b: dict, reason: str) -> None:
    with pytest.raises(IncompatibleSemantics) as exc:
        aggregate_flow_items([_flow("1", **a), _flow("2", **b)])
    assert exc.value.details["reason"] == reason


def test_identical_classification_revision_is_allowed() -> None:
    agg = aggregate_flow_items([_flow("1", classification="H5"), _flow("2", classification="H5")])
    assert agg.groups[0].total["value"] == "3"


def test_duplicate_record_is_refused() -> None:
    subj, sup = uuid4(), uuid4()
    with pytest.raises(IncompatibleSemantics) as exc:
        aggregate_flow_items([_flow("1", subject=subj, support=sup), _flow("1.5", subject=subj, support=sup)])
    assert exc.value.details["reason"] == "duplicate_record"


def test_missing_is_never_zero() -> None:
    items = [_flow("5"), _flow(None)]
    agg = aggregate_flow_items(items)
    assert agg.groups[0].total["kind"] == "missing"
    assert agg.groups[0].n_missing == 1
    lower = aggregate_flow_items(items, skip_missing=True)
    assert lower.groups[0].total["value"] == "5" and lower.groups[0].is_lower_bound


def test_periods_are_grouped_not_summed() -> None:
    agg = aggregate_flow_items([_flow("1", year=2022), _flow("2", year=2023), _flow("3", year=2023)])
    assert [(grp.period_label, grp.total["value"]) for grp in agg.groups] == [("2022", "1"), ("2023", "5")]


def test_empty_aggregation_is_honest() -> None:
    agg = aggregate_flow_items([])
    assert agg.groups == () and agg.record_kind is None and agg.warnings


# ---------------------------------------------------------------------------
# projections and NetworkX caps
# ---------------------------------------------------------------------------


def test_incidence_preserves_roles_and_repeated_participants() -> None:
    a, b, p = uuid4(), uuid4(), uuid4()
    e = _edge(("role.reporter", a), ("role.partner", a), ("role.product", p))
    e2 = _edge(("role.reporter", b), ("role.partner", a), ("role.product", p))
    inc = g.incidence_matrix([e, e2])
    ia = inc.entity_ids.index(a)
    assert inc.matrix[ia, 0] == 2  # same entity in two roles of one hyperedge
    assert inc.roles[(ia, 0)] == ("role.reporter", "role.partner")
    assert not inc.derived_projection


def test_clique_projection_is_flagged_derived() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    proj = g.clique_projection([_edge(("r", a), ("r", b), ("r", c)), _edge(("r", a), ("r", b))])
    assert proj.derived_projection is True
    assert any("distort" in w for w in proj.warnings)
    ia, ib = proj.entity_ids.index(a), proj.entity_ids.index(b)
    assert proj.matrix[ia, ib] == 2 and proj.matrix[ia, ia] == 0
    d = proj.as_dict()
    assert d["derived_projection"] is True and len(d["pairs"]) == 3


def test_networkx_cap_raises_capability_gap_before_import() -> None:
    big = _edge(*[("role.partner", uuid4()) for _ in range(g.NX_MAX_VERTICES + 1)])
    with pytest.raises(CapabilityGap):
        g.to_networkx([big])
    # clique expansion of 400 participants = 79,800 pairs > 50,000 edge cap
    with pytest.raises(CapabilityGap) as exc:
        g.to_networkx([_edge(*[("r", uuid4()) for _ in range(400)])], mode="clique")
    assert exc.value.details["edges"] == 400 * 399 // 2
    with pytest.raises(CapabilityGap):
        g.check_networkx_caps(10, 51, max_edges=50)
    # callers cannot raise the cap above the documented maximum
    with pytest.raises(CapabilityGap):
        g.check_networkx_caps(g.NX_MAX_VERTICES + 1, 0, max_vertices=10**9)


def test_networkx_small_graphs() -> None:
    nx = pytest.importorskip("networkx")
    a, b, p = uuid4(), uuid4(), uuid4()
    edges = [_edge(("role.reporter", a), ("role.partner", b), ("role.product", p))]
    inc = g.to_networkx(edges)
    assert isinstance(inc, nx.Graph) and inc.graph["derived_projection"] is False
    assert inc.number_of_nodes() == 4 and inc.number_of_edges() == 3
    cl = g.to_networkx(edges, mode="clique")
    assert cl.graph["derived_projection"] is True and cl.graph["warnings"]
    assert cl.number_of_edges() == 3


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def test_bounds_and_context_validation() -> None:
    g.check_bounds(g.MAX_DEPTH, g.MAX_EDGES, 1000)
    with pytest.raises(ValidationProblem):
        g.check_bounds(g.MAX_DEPTH + 1, 10, 1000)
    with pytest.raises(ValidationProblem):
        g.check_bounds(2, g.MAX_EDGES + 1, 1000)
    with pytest.raises(ValidationProblem):
        g.check_bounds(2, 10, g.MAX_TIMEOUT_MS + 1)
    with pytest.raises(ValidationProblem):
        GraphContext.evidence(as_of=datetime(2024, 1, 1))  # naive
    with pytest.raises(ValidationProblem):
        GraphContext(mode="release", release_id=uuid4(), as_of=datetime.now(UTC))
    with pytest.raises(ValidationProblem):
        GraphContext.evidence(origin="production")
    with pytest.raises(ValidationProblem):
        g.check_interval((datetime(2024, 1, 1, tzinfo=UTC), datetime(2023, 1, 1, tzinfo=UTC)))


def test_cursor_and_limit() -> None:
    c = encode_cursor(["France", "00000000-0000-0000-0000-000000000001"])
    assert decode_cursor(c, 2) == ["France", "00000000-0000-0000-0000-000000000001"]
    for bad in ("%%%", encode_cursor(["one"]), "e30"):
        with pytest.raises(ValidationProblem):
            decode_cursor(bad, 2)
    assert check_limit(1000) == 1000
    for bad_limit in (0, 1001, True):
        with pytest.raises(ValidationProblem):
            check_limit(bad_limit)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# events: sequence is not causation
# ---------------------------------------------------------------------------


def _te(start: str, end: str | None = None, *, instant: bool = False, su: tuple[str, str] | None = None) -> dict:
    te: dict = {"kind": "instant" if instant else "interval", "start": start, "end": end}
    if su:
        te["start_uncertainty"] = {"earliest": su[0], "latest": su[1]}
    return te


def test_time_bounds_and_relations() -> None:
    exact = TimeBounds.from_extent(_te("2020-03-01", instant=True))
    assert exact.status == "exact" and exact.latest_end == date(2020, 3, 2)
    fuzzy = TimeBounds.from_extent(_te("2020-01-01", instant=True, su=("2020-01-01", "2020-06-30")))
    assert fuzzy.status == "uncertain"
    later = TimeBounds.from_extent(_te("2021-01-01", "2022-01-01"))
    unknown = TimeBounds.from_extent({"kind": "interval", "start": None})
    assert temporal_relation(exact, later) == "before"
    assert temporal_relation(later, exact) == "after"
    assert temporal_relation(exact, fuzzy) == "indeterminate"
    assert temporal_relation(exact, unknown) == "unknown"
    year = TimeBounds.from_extent(_te("2020-01-01", "2021-01-01"))
    assert temporal_relation(exact, year) == "overlaps"
    # half-open: an interval ending on 2021-01-01 precedes one starting that day
    assert temporal_relation(year, later) == "before"


def test_sequence_relations_never_claim_causation() -> None:
    def occ(te: dict) -> Occurrence:
        return Occurrence(
            uuid4(), "e", "t", "1", "real", uuid4(), te, TimeBounds.from_extent(te), {}, {}, datetime.now(UTC), uuid4()
        )

    rels = sequence_relations([occ(_te("2021-01-01", instant=True)), occ(_te("2020-01-01", instant=True))])
    assert len(rels) == 1 and rels[0].relation == "before"
    d = rels[0].as_dict()
    assert d["causal_claim"] is False and "not caused" in d["note"]
