"""Bounded lineage locks in evidence ingest (docs/dev/lock_bounds.md).

``publish_acquisition`` serialises writers of the same lineage (source, origin, logical key) with
striped transaction advisory locks: at most ``LINEAGE_LOCK_BUCKETS`` per acquisition, taken once
and in ascending order, namespaced by (source, origin). These tests prove

* a 10,000-observation acquisition holds <= LINEAGE_LOCK_BUCKETS lineage locks (and a bounded total
  of lock-table entries), measured in ``pg_locks`` inside the publication transaction;
* two writers of the same logical key are still serialised and get revisions 1 and 2 (the second
  supersedes the first), and an identical concurrent re-write is ``unchanged``;
* concurrent writers of overlapping key sets in opposite orders never deadlock;
* a writer of another source is not blocked by an in-flight large acquisition;
* the lineage lock key space cannot collide with REVA's fixed bigint advisory keys.

Rows are written under test-only sources that have no eligibility decisions, so they never enter a
release of the shared session database; the 10k-row acquisition is rolled back after measuring.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.postgres import repositories as repo
from reva.cli.seed import register_connector_version
from reva.evidence.ingest import (
    LINEAGE_LOCK_BUCKETS,
    LINEAGE_LOCK_CLASS,
    lineage_lock_id,
    publish_acquisition,
)
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import Precision, TimeExtent
from reva.kernel.uncertainty import UncertaintySpec
from reva.ports.connectors import FetchItem, ObservationBatch, ParsedObservation, QualityReport, RawAcquisition

pytestmark = pytest.mark.db

POP = "demography.population.wdi_midyear_total"
WB = WorldBankConnector().describe()
LINEAGE_LOCKS_SQL = """SELECT count(*) FILTER (WHERE locktype = 'advisory' AND classid = :c AND objsubid = 2),
                              count(*) FILTER (WHERE locktype = 'advisory'), count(*)
                       FROM pg_locks WHERE pid = pg_backend_pid()"""


class _Rollback(Exception):
    """Raised from post_publish to roll the measured acquisition back."""


@pytest.fixture(autouse=True)
def _restore_dirty_nodes(request: pytest.FixtureRequest) -> Iterator[None]:
    if "db" not in request.fixturenames:
        yield
        return
    db = request.getfixturevalue("db")
    with db.worker.connect() as c:
        before = [
            dict(r)
            for r in c.execute(
                text("SELECT node_key, since_sequence, accumulated_error_bound, reason FROM ops.dirty_node")
            ).mappings()
        ]
    yield
    with db.worker.begin() as c:
        c.execute(text("DELETE FROM ops.dirty_node"))
        for r in before:
            c.execute(
                text(
                    """INSERT INTO ops.dirty_node(node_key, since_sequence, accumulated_error_bound, reason)
                   VALUES (:node_key, :since_sequence, CAST(:b AS jsonb), :reason)"""
                ),
                {**r, "b": json.dumps(r["accumulated_error_bound"])},
            )


def _spec(db: Any, which: str) -> Any:
    """A test-only source + connector version with no eligibility decision (never selected)."""
    source_key = f"lockfix.test_source.{which}"
    spec = WB.model_copy(update={"name": f"lockfix.test.connector.{which}", "source_key": source_key})
    with db.owner.begin() as c:  # registry setup is the migration owner's job, as in seeding
        act = repo.simple_activity("registry.seed", "review", "maintainer:lockfix-test", {})
        repo.record_activity(c, act)
        if repo.source_by_key(c, source_key) is None:
            sid = c.execute(
                text(
                    """INSERT INTO evidence.source(source_key, title, provider_url, lineage_family, rights, metadata,
                     provenance_activity_id)
                   VALUES (:k, 'lock bound test source', 'https://example.invalid/', 'lockfix_test',
                           '{"licence":"test"}', '{}', :a) RETURNING id"""
                ),
                {"k": source_key, "a": act.id},
            ).scalar_one()
            c.execute(
                text(
                    """INSERT INTO evidence.source_metadata_version(source_id, version, metadata, rights,
                     provenance_activity_id) VALUES (:s, 1, '{}', '{"licence":"test"}', :a)"""
                ),
                {"s": sid, "a": act.id},
            )
        register_connector_version(c, db.store, spec, act.id)
    return spec


def _source_id(db: Any, spec: Any) -> Any:
    with db.worker.connect() as c:
        return repo.source_by_key(c, spec.source_key)["id"]


def _support(tag: str) -> Support:
    return Support(
        temporal=TimeExtent(kind="instant", precision=Precision.day, start=date(2021, 7, 1), label="2021 midyear"),
        spatial=SpatialRef(scheme="provider_reporter_territory", key="WB:FRA", authority="World Bank"),
        population=PopulationRef(universe="de_facto_residents_wdi", definition_source="lock bound test"),
        aggregation="point_in_time_stock",
        resolution={"lockfix_test": tag},
    )


def _acquisition(
    spec: Any,
    request: str,
    keys: list[str],
    values: dict[str, str] | str,
    *,
    origin: str = "test_fixture",
    tag: str = "t",
) -> tuple[RawAcquisition, list[ObservationBatch]]:
    """One synthetic WDI-shaped acquisition (request fingerprint ``request``) with one observation per key,
    in the given key order."""
    vals = {k: (values if isinstance(values, str) else values[k]) for k in keys}
    rows = [{"suffix": k, "value": v} for k, v in vals.items()]  # each raw_selector resolves to one row
    body = json.dumps({"lockfix": request, "rows": rows}, sort_keys=True).encode()
    item = FetchItem(
        key=f"lockfix:{request}", url=f"https://api.worldbank.org/v2/lockfix/{request}", max_bytes=50_000_000
    )
    raw = RawAcquisition(
        item=item,
        body=body,
        media_type="application/json",
        retrieved_at=datetime.now(UTC),
        response_metadata={"status": 200},
        request_fingerprint=f"lockfix:{request}",
        origin=origin,
    )
    support = _support(tag)
    obs = tuple(
        ParsedObservation(
            subject_provider="worldbank",
            subject_key="FRA",
            concept_id=POP,
            concept_version="1.0.0",
            support=support,
            logical_key=k,
            value={"kind": "decimal", "value": vals[k], "unit_ref": "unit.persons", "basis": {"scale_factor": "1"}},
            uncertainty=UncertaintySpec.unknown("synthetic lock test value"),
            source_flags={"lockfix": tag},
            raw_selector={"match": {"suffix": k}},
        )
        for k in keys
    )
    return raw, [
        ObservationBatch(connector=spec.name, connector_version=spec.version, item_key=item.key, observations=obs)
    ]


def _publish(db: Any, spec: Any, raw: RawAcquisition, batches: list[ObservationBatch], **kw: Any) -> Any:
    return publish_acquisition(db.worker, db.store, spec, raw, batches, [QualityReport(outcome="pass")], **kw)


def _lineage(db: Any, source_id: Any, key: str) -> list[tuple[int, Any, Any, str]]:
    with db.worker.connect() as c:
        rows = c.execute(
            text(
                """SELECT revision_no, id, supersedes_id, value->>'value' FROM evidence.observation
               WHERE source_id = :s AND logical_key = :k ORDER BY revision_no"""
            ),
            {"s": source_id, "k": key},
        ).all()
    return [tuple(r) for r in rows]


def _waiting_lineage_locks(db: Any) -> int:
    with db.owner.connect() as c:
        return int(
            c.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :c AND objsubid = 2 "
                    "AND NOT granted"
                ),
                {"c": LINEAGE_LOCK_CLASS},
            ).scalar_one()
        )


def _wait_for(pred: Any, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# --------------------------------------------------------------------------- bound
def test_ten_thousand_observations_hold_a_bounded_number_of_locks(db: Any) -> None:
    """The live failure: ~8,000 cells, one advisory lock each, exhausted the cluster-wide lock table
    (64 x 100 slots locally). Measured inside the publication transaction, then rolled back."""
    spec = _spec(db, "bound")
    run = uuid.uuid4().hex[:8]
    n = 10_000
    keys = [f"lockfix:{run}:cell:{i:05d}" for i in range(n)]
    raw, batches = _acquisition(spec, f"bound-{run}", keys, {k: str(i + 1) for i, k in enumerate(keys)}, tag=run)
    seen: dict[str, int] = {}

    def probe(conn: Any, summary: Any) -> None:
        seen["inserted"] = summary.inserted
        seen["lineage"], seen["advisory"], seen["all"] = conn.execute(
            text(LINEAGE_LOCKS_SQL), {"c": LINEAGE_LOCK_CLASS}
        ).one()
        seen["max_locks"] = int(conn.execute(text("SHOW max_locks_per_transaction")).scalar_one())
        seen["lock_timeout"] = conn.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
        raise _Rollback

    with pytest.raises(_Rollback):
        summary = _publish(db, spec, raw, batches, post_publish=probe)
        pytest.fail(f"not published (quarantined?): {summary}")
    assert seen["inserted"] == n
    assert 0 < seen["lineage"] <= LINEAGE_LOCK_BUCKETS
    # every advisory lock of the transaction: the lineage buckets + evidence write lock + acquisition
    # lock + object lock of the raw bytes
    assert seen["advisory"] <= LINEAGE_LOCK_BUCKETS + 3, seen
    # all lock-table rows of this backend (relation/index locks, virtualxid, transactionid included)
    assert seen["all"] <= LINEAGE_LOCK_BUCKETS + 200, seen
    assert seen["lock_timeout"] == "5s"  # the bucket wait override did not leak into the rest of the txn
    print(
        f"lock bound: lineage={seen['lineage']} advisory={seen['advisory']} all={seen['all']} "
        f"max_locks_per_transaction={seen['max_locks']}"
    )
    with db.worker.connect() as c:  # rolled back: nothing of the measured acquisition remains
        assert (
            c.execute(
                text("SELECT count(*) FROM evidence.observation WHERE logical_key = :k"), {"k": keys[0]}
            ).scalar_one()
            == 0
        )


def test_lineage_lock_keys_never_collide_with_fixed_keys() -> None:
    """Two-int keys (objsubid 2) and bigint keys (objsubid 1) are disjoint in PostgreSQL; the ids are
    also negative, so even an objid-only pg_locks probe cannot see a small fixed key."""
    src = uuid.uuid4()
    ids = {lineage_lock_id(src, o, f"k{i}") for o in ("live", "test_fixture") for i in range(5000)}
    assert all(-(2**31) <= i < 0 for i in ids)
    assert len({lineage_lock_id(src, "live", f"k{i}") for i in range(5000)}) <= LINEAGE_LOCK_BUCKETS
    assert LINEAGE_LOCK_CLASS not in {7262001, 7262002, 7262011, 7262020, 7262021, 7262031}


# --------------------------------------------------------------------------- same key: serialised
def test_concurrent_writers_of_one_logical_key_are_serialised(db: Any) -> None:
    """Writer B (same source/origin/logical key, different request) blocks on the lineage bucket while
    writer A's transaction is open, then supersedes A's revision: revisions 1 and 2, not a unique-key
    failure. Without the lock, B would read no head and also claim revision 1."""
    spec = _spec(db, "same")
    sid = _source_id(db, spec)
    run = uuid.uuid4().hex[:8]
    key = f"lockfix:{run}:shared"
    inside, release = threading.Event(), threading.Event()
    result: dict[str, Any] = {}

    def hold(conn: Any, summary: Any) -> None:
        inside.set()
        assert release.wait(30)

    def writer(name: str, value: str, **kw: Any) -> None:
        try:
            raw, batches = _acquisition(spec, f"same-{run}-{name}", [key], value, tag=run)
            result[name] = _publish(db, spec, raw, batches, **kw)
        except Exception as e:  # pragma: no cover - reported by the assertion below
            result[f"{name}_error"] = e

    a = threading.Thread(target=writer, args=("a", "100"), kwargs={"post_publish": hold})
    a.start()
    assert inside.wait(30), "writer A did not reach its publication transaction"
    b = threading.Thread(target=writer, args=("b", "200"))
    b.start()
    _wait_for(lambda: _waiting_lineage_locks(db) >= 1, 10, "writer B to wait on the lineage bucket")
    assert b.is_alive() and "b" not in result
    release.set()
    a.join(30)
    b.join(60)
    assert not [k for k in result if k.endswith("_error")], result
    assert result["a"].inserted == 1 and result["b"].inserted == 1
    (r1, id1, sup1, v1), (r2, _id2, sup2, v2) = _lineage(db, sid, key)
    assert (r1, sup1, v1) == (1, None, "100")
    assert (r2, sup2, v2) == (2, id1, "200")

    # an identical value written concurrently by two writers: one revision 3, the other 'unchanged'
    out: dict[str, Any] = {}
    barrier = threading.Barrier(2)

    def same_value(name: str) -> None:
        raw, batches = _acquisition(spec, f"same-{run}-again-{name}", [key], "300", tag=run)
        barrier.wait(10)
        out[name] = _publish(db, spec, raw, batches)

    ts = [threading.Thread(target=same_value, args=(n,)) for n in ("x", "y")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert sorted((s.inserted, s.unchanged) for s in out.values()) == [(0, 1), (1, 0)]
    assert [(r[0], r[3]) for r in _lineage(db, sid, key)] == [(1, "100"), (2, "200"), (3, "300")]


# --------------------------------------------------------------------------- different keys: no deadlock
def test_overlapping_acquisitions_in_opposite_key_orders_do_not_deadlock(db: Any) -> None:
    """Four concurrent acquisitions of one source whose key sets overlap and are iterated in opposite
    orders (each covers every bucket). Per-key locks taken in iteration order would deadlock; the
    ascending, up-front bucket locks serialise them and every revision is correct."""
    spec = _spec(db, "order")
    sid = _source_id(db, spec)
    run = uuid.uuid4().hex[:8]
    shared = [f"lockfix:{run}:s:{i:04d}" for i in range(600)]
    own = {w: [f"lockfix:{run}:{w}:{i:04d}" for i in range(300)] for w in "abcd"}
    orders = {
        "a": shared + own["a"],
        "b": list(reversed(shared)) + own["b"],
        "c": own["c"] + shared[::2] + shared[1::2],
        "d": list(reversed(own["d"] + shared)),
    }
    out: dict[str, Any] = {}
    barrier = threading.Barrier(4)

    def writer(w: str) -> None:
        try:
            raw, batches = _acquisition(spec, f"order-{run}-{w}", orders[w], str("abcd".index(w) + 1), tag=run)
            barrier.wait(10)
            out[w] = _publish(db, spec, raw, batches)
        except Exception as e:  # deadlock_detected / lock timeout would land here
            out[f"{w}_error"] = e

    ts = [threading.Thread(target=writer, args=(w,)) for w in "abcd"]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    assert not [k for k in out if k.endswith("_error")], out
    assert all(out[w].inserted == len(orders[w]) for w in "abcd"), out
    with db.worker.connect() as c:
        revs = c.execute(
            text(
                """SELECT logical_key, array_agg(revision_no ORDER BY revision_no) FROM evidence.observation
               WHERE source_id = :s AND logical_key LIKE :p GROUP BY logical_key"""
            ),
            {"s": sid, "p": f"lockfix:{run}:%"},
        ).all()
    by_key = dict(revs)
    assert all(by_key[k] == [1, 2, 3, 4] for k in shared)  # four writers -> four revisions, none lost
    assert all(by_key[k] == [1] for w in "abcd" for k in own[w])


# --------------------------------------------------------------------------- other sources: no contention
def test_large_acquisition_does_not_block_another_source(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Buckets are namespaced by (source, origin): while a 1,000-key acquisition of source A holds its
    bucket locks, an acquisition of source B with the *same* logical keys commits without waiting.

    A is paused right after taking its buckets (before it touches shared rows such as the support or
    the ``belief:<concept>`` dirty node, which serialise same-concept ingests independently of these
    locks; see docs/dev/lock_bounds.md)."""
    import reva.evidence.ingest as ingest

    spec_a, spec_b = _spec(db, "ns-a"), _spec(db, "ns-b")
    sid_a = _source_id(db, spec_a)
    run = uuid.uuid4().hex[:8]
    keys = [f"lockfix:{run}:k:{i:04d}" for i in range(1000)]
    inside, release = threading.Event(), threading.Event()
    result: dict[str, Any] = {}
    original = ingest.lock_lineages

    def pausing_lock(conn: Any, source_id: Any, origin: str, logical_keys: Any) -> int:
        n = original(conn, source_id, origin, logical_keys)
        if source_id == sid_a and n > 1:
            result["a_buckets"] = n
            inside.set()
            assert release.wait(30)
        return n

    monkeypatch.setattr(ingest, "lock_lineages", pausing_lock)

    def writer_a() -> None:
        try:
            raw, batches = _acquisition(spec_a, f"ns-{run}-a", keys, "1", tag=f"{run}-a")
            result["a"] = _publish(db, spec_a, raw, batches)
        except Exception as e:  # pragma: no cover - reported below
            result["a_error"] = e

    t = threading.Thread(target=writer_a)
    t.start()
    try:
        assert inside.wait(60), "writer A did not take its bucket locks"
        raw, batches = _acquisition(spec_b, f"ns-{run}-b", keys, "2", tag=f"{run}-b")
        started = time.monotonic()
        summary_b = _publish(db, spec_b, raw, batches)
        assert summary_b.inserted == len(keys)
        assert t.is_alive() and "a" not in result  # A still held every one of its buckets
        assert time.monotonic() - started < 30
    finally:
        release.set()
        t.join(60)
    assert "a_error" not in result, result.get("a_error")
    assert result["a"].inserted == len(keys) and result["a_buckets"] <= LINEAGE_LOCK_BUCKETS


# --------------------------------------------------------------------------- belief publication
def test_belief_publication_lock_entries_do_not_grow_with_rows(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-row ``belief.reject_if_published`` trigger (estimates, inputs, release artifacts and the
    pinned world.relation rows) re-takes the *same* release key shared: re-entrant, one lock-table
    entry however many rows. Measured inside a real publication transaction, then rolled back."""
    import asyncio

    import reva.belief.publication as publication
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.belief.publication import publish_release
    from reva.evidence.runner import FixtureHttp, run_connector
    from reva.kernel.context import Origin

    for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
        conn_ = builtin_connectors()[name]
        asyncio.run(run_connector(conn_, FixtureHttp(conn_.describe().name), db.store, db.worker, persist_cursor=False))
    seen: dict[str, int] = {}
    original = publication.pin_release_relations

    def probe(conn: Any, release_id: Any, activity_id: Any) -> int:
        n = original(conn, release_id, activity_id)
        seen["relations"] = n
        seen["rows"] = (
            int(
                conn.execute(
                    text(
                        """SELECT (SELECT count(*) FROM belief.estimate WHERE release_id = :r)
                    + (SELECT count(*) FROM belief.input i JOIN belief.estimate e ON e.id = i.estimate_id
                       WHERE e.release_id = :r)"""
                    ),
                    {"r": release_id},
                ).scalar_one()
            )
            + n
        )
        seen["advisory"], seen["all"] = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE locktype = 'advisory'), count(*) FROM pg_locks "
                "WHERE pid = pg_backend_pid()"
            )
        ).one()
        raise _Rollback

    monkeypatch.setattr(publication, "pin_release_relations", probe)
    with pytest.raises(_Rollback):
        publish_release(db.worker, db.store, cutoff=None, origin=Origin.test_fixture, environment="test")
    print(
        f"belief publication: rows={seen['rows']} relations={seen['relations']} "
        f"advisory={seen['advisory']} all={seen['all']}"
    )
    assert seen["rows"] >= 100
    # evidence snapshot lock, publication lock, the release key, a handful of object locks
    assert seen["advisory"] <= 12, seen
    assert seen["all"] < seen["rows"], seen
