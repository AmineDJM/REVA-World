"""R7 property checks for belief selection (src/reva/belief/selection.py).

* Pure part: ``select_report`` over ``eligible_latest``-shaped rows (no database):
  invariance to candidate order, retracted lineages never selected, preference order.
* Revision part: ``eligible_latest`` is SQL (cutoff filter, latest revision per
  lineage, retraction). Its properties are checked against a fresh per-session test
  database (``db`` fixture; never the dev database ``reva``) with few examples.

Tests marked ``xfail(strict=True)`` encode a property the current code violates
(see docs/dev/property_check.md).
"""

from __future__ import annotations

import itertools
import json
import random
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from reva.belief.selection import select_report
from reva.kernel.uncertainty import UncertaintySpec

PURE = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SOURCES = ("src.a", "src.b", "src.c")
CONCEPTS = ("concept.one", "concept.two")
UNKNOWN = UncertaintySpec.unknown("r7").model_dump(mode="json")
_ns = uuid.UUID("7e57ab1e-0000-4000-8000-000000000007")


def _u(tag: str) -> uuid.UUID:
    return uuid.uuid5(_ns, tag)


# --------------------------------------------------------------------------- pure strategies
@st.composite
def candidate_rows(draw: Any) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Rows as eligible_latest returns them: one row per lineage (source, origin, logical_key)."""
    n_est = draw(st.integers(1, 3))
    rows: list[dict[str, Any]] = []
    lineages: set[tuple[str, str, str]] = set()
    for e in range(n_est):
        concept = draw(st.sampled_from(CONCEPTS))
        cv = _u(f"cv:{concept}")
        subject, support = _u(f"subj:{e % 2}"), _u(f"sup:{e}")
        for _ in range(draw(st.integers(1, 5))):
            src = draw(st.sampled_from(SOURCES))
            origin = draw(st.sampled_from(["test_fixture", "live"]))
            lk = f"{src}:{draw(st.sampled_from(['k1', 'k2', 'k3']))}"
            if (src, origin, lk) in lineages:
                continue
            lineages.add((src, origin, lk))
            rows.append({
                "id": _u(f"obs:{e}:{src}:{origin}:{lk}"),
                "subject_id": subject, "concept_version_id": cv, "support_id": support, "concept_id": concept,
                "source_key": src, "lineage_family": draw(st.sampled_from(["fam1", "fam2", None])),
                "origin": origin, "logical_key": lk,
                "value": {"kind": "decimal", "value": draw(st.sampled_from(["100", "100.0", "101", "7"])),
                          "unit_ref": "unit.usd"},
                "uncertainty": UNKNOWN,
                "source_flags": {"flowCode": draw(st.sampled_from(["M", "M", "M", "X"])), "partnerCode": "0"},
                "selection_status": draw(st.sampled_from(["current", "current", "superseded_revision_selected",
                                                          "retracted"])),
                "revision_no": draw(st.integers(1, 3)),
            })
    prefs = {c: draw(st.permutations(SOURCES).map(lambda p: list(p[: draw(st.integers(1, 3))])))
             for c in CONCEPTS}
    return rows, prefs


def _norm(res: Any, *, ordered: bool) -> Any:
    sels = []
    for s in res.selections:
        d = asdict(s)
        if not ordered:
            d["alternatives"] = sorted(json.dumps(a, sort_keys=True, default=str) for a in d["alternatives"])
            d["discrepancy_flags"] = sorted(d["discrepancy_flags"])
        sels.append(d)
    return sels, [w.as_json() for w in res.withheld]


@PURE
@given(candidate_rows(), st.randoms(use_true_random=False))
def test_selection_is_invariant_to_candidate_order(data: Any, rnd: random.Random) -> None:
    rows, prefs = data
    shuffled = rows[:]
    rnd.shuffle(shuffled)
    assert _norm(select_report(shuffled, prefs), ordered=False) == _norm(select_report(rows, prefs), ordered=False)


def _two_retracted_lineages() -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    base = {"subject_id": _u("s"), "concept_version_id": _u("cv"), "support_id": _u("sup"), "concept_id": "c",
            "lineage_family": "f", "origin": "test_fixture", "uncertainty": UNKNOWN, "source_flags": {},
            "value": {"kind": "decimal", "value": "1", "unit_ref": "u"}, "revision_no": 1}
    rows = [
        {**base, "id": _u("cur"), "source_key": "src.a", "logical_key": "a:1", "selection_status": "current"},
        {**base, "id": _u("r1"), "source_key": "src.a", "logical_key": "a:2", "selection_status": "retracted"},
        {**base, "id": _u("r2"), "source_key": "src.b", "logical_key": "b:1", "selection_status": "retracted"},
    ]
    return rows, {"c": ["src.a", "src.b"]}


@pytest.mark.xfail(strict=True, reason="R7-B1: retracted-lineage alternatives and their "
                   "'retracted_lineage_excluded:*' flags are emitted in input-row order, so the stored "
                   "estimate (and release manifest bytes) depend on SQL row order")
@PURE
@given(candidate_rows(), st.randoms(use_true_random=False))
@example(_two_retracted_lineages(), random.Random(0))
def test_selection_output_is_byte_identical_for_any_candidate_order(data: Any, rnd: random.Random) -> None:
    rows, prefs = data
    for perm in (list(reversed(rows)), rnd.sample(rows, len(rows))):
        assert _norm(select_report(perm, prefs), ordered=True) == _norm(select_report(rows, prefs), ordered=True)


@PURE
@given(candidate_rows())
def test_retracted_lineage_is_never_selected_and_preference_is_respected(data: Any) -> None:
    rows, prefs = data
    res = select_report(rows, prefs)
    by_id = {r["id"]: r for r in rows}
    chosen_keys = set()
    for s in res.selections:
        r = by_id[s.observation_id]
        chosen_keys.add((s.subject_id, s.concept_version_id, s.support_id))
        assert r["selection_status"] != "retracted"
        assert s.value == r["value"] and s.uncertainty == r["uncertainty"]
        p = prefs[s.concept_id]
        usable = [c for c in rows if (c["subject_id"], c["concept_version_id"], c["support_id"]) ==
                  (s.subject_id, s.concept_version_id, s.support_id)
                  and c["selection_status"] != "retracted" and c["source_key"] in p]
        assert p.index(r["source_key"]) == min(p.index(c["source_key"]) for c in usable)
        assert ("superseded_revision_selected" in s.discrepancy_flags) == (
            r["selection_status"] == "superseded_revision_selected")
        for a in s.alternatives:
            if a.get("selection_status") == "retracted":
                assert "value" not in a  # a retracted value is never carried forward
    # an estimand whose usable lineages are all retracted yields nothing (no stale fallback)
    groups: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["subject_id"], r["concept_version_id"], r["support_id"]), []).append(r)
    withheld_keys = {(w.subject_id, w.concept_version_id, w.support_id) for w in res.withheld}
    for key, g in groups.items():
        p = prefs.get(g[0]["concept_id"], [])
        if all(c["selection_status"] == "retracted" for c in g if c["source_key"] in p):
            assert key not in chosen_keys and key not in withheld_keys


@PURE
@given(candidate_rows())
def test_retracting_the_selected_lineage_drops_it(data: Any) -> None:
    """Turning the chosen lineage's head into a retraction never re-selects that lineage."""
    rows, prefs = data
    for s in select_report(rows, prefs).selections:
        mutated = [{**r, "selection_status": "retracted"} if r["id"] == s.observation_id else r for r in rows]
        after = {(x.subject_id, x.concept_version_id, x.support_id): x
                 for x in select_report(mutated, prefs).selections}
        x = after.get((s.subject_id, s.concept_version_id, s.support_id))
        assert x is None or x.observation_id != s.observation_id
        if x is not None:
            assert f"retracted_lineage_excluded:{s.source_key}" in x.discrepancy_flags


# --------------------------------------------------------------------------- DB: eligible_latest
POP = "demography.population.wdi_midyear_total"


@pytest.fixture
def _restore_dirty_nodes(db: Any) -> Iterator[None]:
    from sqlalchemy import text

    with db.worker.connect() as c:
        before = [dict(r) for r in c.execute(text(
            "SELECT node_key, since_sequence, accumulated_error_bound, reason FROM ops.dirty_node")).mappings()]
    yield
    with db.worker.begin() as c:
        c.execute(text("DELETE FROM ops.dirty_node"))
        for r in before:
            c.execute(text(
                """INSERT INTO ops.dirty_node(node_key, since_sequence, accumulated_error_bound, reason)
                   VALUES (:node_key, :since_sequence, CAST(:b AS jsonb), :reason)"""),
                {**r, "b": json.dumps(r["accumulated_error_bound"])})


def _now(engine: Any) -> datetime:
    from sqlalchemy import text

    with engine.connect() as c:
        return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def _ingest_revision(db: Any, tag: str, value: str, salt: str) -> None:
    from reva.adapters.connectors.worldbank import WorldBankConnector
    from reva.evidence.ingest import publish_acquisition
    from reva.kernel.support import PopulationRef, SpatialRef, Support
    from reva.kernel.time import Precision, TimeExtent
    from reva.ports.connectors import (
        FetchItem,
        ObservationBatch,
        ParsedObservation,
        QualityReport,
        RawAcquisition,
    )

    wb = WorldBankConnector().describe()
    body = json.dumps({"r7": tag, "value": value, "salt": salt}, sort_keys=True).encode()
    item = FetchItem(key=f"r7:{tag}", url=f"https://api.worldbank.org/v2/r7-test/{tag}", max_bytes=10_000)
    raw = RawAcquisition(item=item, body=body, media_type="application/json", retrieved_at=datetime.now(UTC),
                         response_metadata={"status": 200}, request_fingerprint=f"r7:{tag}", origin="test_fixture")
    support = Support(
        temporal=TimeExtent(kind="instant", precision=Precision.day, start=date(2021, 7, 1), label="2021 midyear"),
        spatial=SpatialRef(scheme="provider_reporter_territory", key="WB:FRA", authority="World Bank"),
        population=PopulationRef(universe="de_facto_residents_wdi", definition_source="r7 synthetic test"),
        aggregation="point_in_time_stock",
        resolution={"r7_test": tag},
    )
    obs = (ParsedObservation(
        subject_provider="worldbank", subject_key="FRA", concept_id=POP, concept_version="1.0.0", support=support,
        logical_key=f"r7:{tag}:a", value={"kind": "decimal", "value": value, "unit_ref": "unit.persons"},
        uncertainty=UncertaintySpec.unknown("synthetic r7 value"), source_flags={"r7": tag},
        raw_selector={"match": {"value": value}},
    ),)
    batch = ObservationBatch(connector=wb.name, connector_version=wb.version, item_key=item.key, observations=obs)
    publish_acquisition(db.worker, db.store, wb, raw, [batch], [QualityReport(outcome="pass")])


def _lineage_row(db: Any, cutoff: datetime, tag: str, *, include_retracted: bool = False) -> dict[str, Any] | None:
    from reva.belief.selection import eligible_latest

    with db.api.connect() as c:
        rows = [r for r in eligible_latest(c, cutoff, ["test_fixture"], [POP], include_retracted=include_retracted)
                if r["logical_key"] == f"r7:{tag}:a"]
    assert len(rows) <= 1
    return rows[0] if rows else None


@pytest.mark.db
@settings(max_examples=6, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(values=st.lists(st.integers(1, 10**6).map(str), min_size=1, max_size=4).filter(
           lambda v: all(a != b for a, b in itertools.pairwise(v))),
       retract=st.sampled_from(["none", "head", "older"]))
@example(values=["10", "20", "30"], retract="older")
@example(values=["10", "20"], retract="head")
def test_eligible_latest_respects_cutoff_and_retraction(db: Any, _restore_dirty_nodes: None, values: list[str],
                                                        retract: str) -> None:
    from sqlalchemy import text

    from reva.evidence.retractions import retract_observation

    event(f"retract={retract} revisions={len(values)}")
    tag = uuid.uuid4().hex[:10]
    t_before = _now(db.worker)
    cutoffs = []
    for i, v in enumerate(values):
        _ingest_revision(db, tag, v, salt=str(i))
        cutoffs.append(_now(db.worker))
    assert _lineage_row(db, t_before, tag) is None  # nothing recorded yet at that cutoff
    for j, t in enumerate(cutoffs):
        row = _lineage_row(db, t, tag)
        assert row is not None
        # the latest revision recorded at/before the cutoff; never a later-recorded one
        assert row["revision_no"] == j + 1 and row["value"]["value"] == values[j]
        assert row["selection_status"] == "current"
    if retract == "none" or (retract == "older" and len(values) < 2):
        return
    with db.worker.connect() as c:
        revs = c.execute(text("SELECT id, revision_no FROM evidence.observation WHERE logical_key = :k "
                              "ORDER BY revision_no"), {"k": f"r7:{tag}:a"}).mappings().all()
    target = revs[-1] if retract == "head" else revs[0]
    with db.owner.begin() as c:  # human-attributed retraction: a reviewer path, not the worker role
        retract_observation(c, target["id"], "r7 property test retraction", actor="human:r7-test")
    t_after = _now(db.worker)
    # a retraction recorded later never changes an earlier as-of answer
    row = _lineage_row(db, cutoffs[-1], tag)
    assert row is not None and row["revision_no"] == len(values)
    row = _lineage_row(db, t_after, tag)
    if retract == "head":
        assert row is None  # the lineage is dropped, never replaced by the superseded value
        flagged = _lineage_row(db, t_after, tag, include_retracted=True)
        assert flagged is not None and flagged["selection_status"] == "retracted"
        assert flagged["id"] == target["id"]
    else:
        assert row is not None and row["revision_no"] == len(values) and row["selection_status"] == "current"
