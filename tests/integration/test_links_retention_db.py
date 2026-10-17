"""Link store retention (agent LINKRET, migration 0017_links_retention, ``reva links prune``) on a dedicated
throwaway database: synthetic materialisations in two lineages (live and fixture), the keep policy and its
reasons, the owner-only prune function, the ``kg.link_prune`` + ``ops.audit`` records, the database guards (never
the served materialisation, no deletion without a prune record, no non-owner path) and the readers that never
serve or reuse a pruned materialisation. The dev database is never touched."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from reva.settings import Settings

pytestmark = pytest.mark.db


@dataclass
class Env:
    owner: Engine
    worker: Engine
    api: Engine


@pytest.fixture(scope="module")
def env() -> Iterator[Env]:
    from reva.adapters.postgres.engine import make_engine
    from reva.adapters.postgres.schema_contract import throwaway_database

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    with throwaway_database(base, prefix="reva_test_linkret") as s:
        engines = [make_engine(k, s) for k in ("migration", "worker", "api")]  # type: ignore[arg-type]
        try:
            yield Env(*engines)
        finally:
            for e in engines:
                e.dispose()


def _materialise(worker: Engine, *, ao: str | None, ko: str | None, n: int, sha: str | None = None) -> int:
    """One synthetic materialisation of ``n`` link rows, written like the materialiser (one worker transaction)."""
    from reva.links.store import METHOD_VERSION, taxonomy_sha256

    if ao is not None and sha is None:
        sha = uuid.uuid4().hex * 2
    with worker.begin() as c:
        mid = int(
            c.execute(
                text(
                    """INSERT INTO kg.link_materialisation (method_version, taxonomy_sha256, atlas_snapshot_sha256,
                           atlas_origin, kg_origin, kg_load_watermark, parameters, link_count)
                       VALUES (:mv, :ts, :sha, :ao, :ko, 7, CAST(:p AS jsonb), :n) RETURNING id"""
                ),
                {
                    "mv": METHOD_VERSION,
                    "ts": taxonomy_sha256(),
                    "sha": sha if ao is not None else None,
                    "ao": ao,
                    "ko": ko,
                    "p": json.dumps({"top_k": 10}),
                    "n": n,
                },
            ).scalar_one()
        )
        c.execute(
            text(
                """INSERT INTO kg.link_row (materialisation_id, link_type, subject, object, directed, temporal,
                       evidence, origin)
                   SELECT :m, 'linkret_test', 'Q' || g, 'Q' || (g + 1000000), false, '{}'::jsonb,
                          jsonb_build_array(jsonb_build_object('statement_id', 'S' || g)), 'live'
                     FROM generate_series(1, :n) g"""
            ),
            {"m": mid, "n": n},
        )
    return mid


def _rows(owner: Engine, mid: int) -> int:
    with owner.connect() as c:
        return int(
            c.execute(text("SELECT count(*) FROM kg.link_row WHERE materialisation_id = :m"), {"m": mid}).scalar_one()
        )


def _fails(engine: Engine, sql: str, match: str, params: dict[str, Any] | None = None) -> None:
    with pytest.raises(DBAPIError, match=match), engine.begin() as c:
        c.execute(text(sql), params or {})


def test_policy_dry_run_prune_audit_and_readers(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.links import retention
    from reva.links.jobs import existing_materialisation
    from reva.links.store import latest_materialisation

    monkeypatch.setattr(retention, "_current_snapshots", lambda conn: {})
    live1 = _materialise(env.worker, ao="live", ko="live", n=40)
    pinned_sha = uuid.uuid4().hex * 2
    live2 = _materialise(env.worker, ao="live", ko="live", n=30, sha=pinned_sha)
    fix1 = _materialise(env.worker, ao="test_fixture", ko="test_fixture", n=20)
    live3 = _materialise(env.worker, ao="live", ko="live", n=25)
    fix2 = _materialise(env.worker, ao="test_fixture", ko="test_fixture", n=10)
    live4 = _materialise(env.worker, ao="live", ko="live", n=15)
    kgonly = _materialise(env.worker, ao=None, ko="live", n=5)  # atlas-less live: served by live_only readers
    referenced = live1

    # a table that pins a materialisation through a foreign key (a future release/pin table)
    with env.owner.begin() as c:
        c.execute(text("CREATE TABLE public.linkret_pin (mid bigint NOT NULL REFERENCES kg.link_materialisation)"))
        c.execute(text("INSERT INTO public.linkret_pin VALUES (:m)"), {"m": referenced})
    (tmp_path / "live-snapshot.pin").write_text(pinned_sha)

    with env.owner.connect() as c:
        plan = retention.plan_prune(c, keep=2, pin_dir=tmp_path)
    kept = plan.kept
    assert "latest" in kept[kgonly] and "latest_live" in kept[kgonly] and "recent" in kept[kgonly]
    assert "recent" in kept[live4] and "latest_lineage:live/live" in kept[live4]
    assert kept[fix2] == ["latest_lineage:test_fixture/test_fixture"]
    assert kept[live2] == ["atlas_pin"]
    assert kept[referenced] == ["fk:public.linkret_pin.mid"]
    assert sorted(m.id for m in plan.prune) == sorted([fix1, live3])

    dry = retention.prune(env.owner, keep=2, dry_run=True, pin_dir=tmp_path)
    assert dry["dry_run"] is True and dry["pruned"] == [] and dry["prune_rows"] == 45
    assert _rows(env.owner, fix1) == 20 and _rows(env.owner, live3) == 25  # nothing written

    with env.owner.connect() as c:
        audits_before = c.execute(text("SELECT count(*) FROM ops.audit WHERE action = 'links.prune'")).scalar_one()
    out = retention.prune(env.owner, keep=2, pin_dir=tmp_path, reason="test retention", vacuum_mode="plain")
    assert {d["materialisation_id"]: d["rows_deleted"] for d in out["pruned"]} == {fix1: 20, live3: 25}
    assert all(d["row_bytes"] > 0 for d in out["pruned"]) and out["pruned_rows"] == 45
    assert out["vacuum"]["mode"] == "plain" and "after" in out["vacuum"]
    assert _rows(env.owner, fix1) == 0 and _rows(env.owner, live3) == 0
    for mid, n in ((live1, 40), (live2, 30), (fix2, 10), (live4, 15), (kgonly, 5)):
        assert _rows(env.owner, mid) == n
    with env.owner.connect() as c:
        # the provenance rows stay; the prune records and audit rows are written
        assert (
            c.execute(
                text("SELECT count(*) FROM kg.link_materialisation WHERE id IN (:a, :b)"), {"a": fix1, "b": live3}
            ).scalar_one()
            == 2
        )
        rec = (
            c.execute(
                text(
                    "SELECT materialisation_id, rows_deleted, reason, policy, recorded_by FROM kg.link_prune ORDER BY 1"
                )
            )
            .mappings()
            .all()
        )
        assert [(r["materialisation_id"], r["rows_deleted"]) for r in rec] == [(fix1, 20), (live3, 25)]
        assert rec[0]["reason"] == "test retention" and rec[0]["policy"]["keep"] == 2
        assert rec[0]["policy"]["kept"][str(live2)] == ["atlas_pin"]
        aud = (
            c.execute(
                text("SELECT actor, target, details FROM ops.audit WHERE action = 'links.prune' ORDER BY recorded_at")
            )
            .mappings()
            .all()
        )
        assert len(aud) - audits_before == 2
        assert aud[-1]["actor"] == "owner:links-prune" and aud[-1]["details"]["rows_deleted"] in (20, 25)
        assert aud[-1]["target"]["run_id"] == out["run_id"]

    # readers: a pruned materialisation is never served nor reused
    with env.api.connect() as c:
        assert latest_materialisation(c)["id"] == kgonly
        assert latest_materialisation(c, live_only=True)["id"] == kgonly
        assert latest_materialisation(c, atlas_origin="test_fixture")["id"] == fix2
        m = latest_materialisation(c, atlas_sha256=None, atlas_origin="live")
        assert m is not None and m["id"] == live4
    with env.worker.connect() as c:
        assert existing_materialisation(c, atlas_sha256=None, kg_origin="live", kg_watermark=7, top_k=10) == kgonly
        sha3 = c.execute(
            text("SELECT atlas_snapshot_sha256 FROM kg.link_materialisation WHERE id = :m"), {"m": live3}
        ).scalar_one()
        assert existing_materialisation(c, atlas_sha256=sha3, kg_origin="live", kg_watermark=7, top_k=10) is None

    # VACUUM FULL rewrites the table (returns the pruned space to the OS); readers still see the kept rows
    full = retention.vacuum(env.owner, "full")
    assert full["mode"] == "full" and full["after"]["link_row_total"] > 0 and "note" not in full
    assert _rows(env.owner, live4) == 15

    # idempotent: a second run prunes nothing new and reports what was pruned before
    again = retention.prune(env.owner, keep=2, pin_dir=tmp_path)
    assert again["pruned"] == [] and sorted(again["already_pruned"]) == sorted([fix1, live3])

    # keep=1 without the pin and the FK reference: only the served/lineage-latest ones remain
    with env.owner.begin() as c:
        c.execute(text("DROP TABLE public.linkret_pin"))
    with env.owner.connect() as c:
        plan1 = retention.plan_prune(c, keep=1, pin_dir=tmp_path / "nopin")
    assert sorted(m.id for m in plan1.prune) == sorted([live1, live2])


def test_database_guards(env: Env) -> None:
    a = _materialise(env.worker, ao="live", ko="live", n=3)
    b = _materialise(env.worker, ao="live", ko="live", n=2)
    run = str(uuid.uuid4())
    call = "SELECT * FROM kg.prune_link_materialisation(:m, CAST(:r AS uuid), 'guard test', '{}'::jsonb)"
    # never the latest of its lineage / the latest live one
    _fails(env.owner, call, "latest of its lineage", {"m": b, "r": run})
    # runtime roles have no path at all
    for e in (env.worker, env.api):
        _fails(e, call, "permission denied", {"m": a, "r": run})
        _fails(e, "DELETE FROM kg.link_row WHERE materialisation_id = :m", "permission denied", {"m": a})
        _fails(
            e,
            "INSERT INTO kg.link_prune(materialisation_id, run_id, rows_deleted, row_bytes, reason) "
            "VALUES (:m, gen_random_uuid(), 0, 0, 'x')",
            "permission denied",
            {"m": a},
        )
    # even the owner cannot delete link rows without a prune record in the same transaction
    _fails(env.owner, "DELETE FROM kg.link_row WHERE materialisation_id = :m", "Immutable scientific record", {"m": a})
    _fails(env.owner, "TRUNCATE kg.link_row", "TRUNCATE forbidden")
    # a prune record without the deletion cannot commit (deferred check)
    _fails(
        env.owner,
        "INSERT INTO kg.link_prune(materialisation_id, run_id, rows_deleted, row_bytes, reason) "
        "VALUES (:m, gen_random_uuid(), 3, 0, 'record only')",
        "link rows remain",
        {"m": a},
    )
    # a prune record cannot overstate the rows
    _fails(
        env.owner,
        "INSERT INTO kg.link_prune(materialisation_id, run_id, rows_deleted, row_bytes, reason) "
        "VALUES (:m, gen_random_uuid(), 4, 0, 'overstated')",
        "records 4 rows; it stored 3",
        {"m": a},
    )
    assert _rows(env.owner, a) == 3
    # UPDATE stays refused, even inside a prune transaction
    with pytest.raises(DBAPIError, match="Immutable scientific record"), env.owner.begin() as c:
        c.execute(text(call), {"m": a, "r": run})
        c.execute(text("UPDATE kg.link_row SET facet = 'x' WHERE materialisation_id = :m"), {"m": b})
    assert _rows(env.owner, a) == 3  # rolled back with it
    # the governed path works; the record is append-only and unique
    with env.owner.begin() as c:
        r = c.execute(text(call), {"m": a, "r": run}).one()
    assert (r.rows_deleted, _rows(env.owner, a)) == (3, 0)
    _fails(
        env.owner,
        "UPDATE kg.link_prune SET reason = 'x' WHERE materialisation_id = :m",
        "Immutable scientific record",
        {"m": a},
    )
    _fails(
        env.owner, "DELETE FROM kg.link_prune WHERE materialisation_id = :m", "Immutable scientific record", {"m": a}
    )
    _fails(env.owner, call, "duplicate key", {"m": a, "r": run})
    # the materialisation row itself stays immutable
    _fails(env.owner, "DELETE FROM kg.link_materialisation WHERE id = :m", "Immutable scientific record", {"m": a})


def test_restricted_deletion_path_still_reaches_link_rows(env: Env) -> None:
    with env.owner.connect() as c:
        member = c.execute(text("SELECT pg_has_role(session_user, 'reva_restricted_deletion', 'SET')")).scalar_one()
    if not member:
        pytest.skip("migration owner is not a member of reva_restricted_deletion in this cluster")
    a = _materialise(env.worker, ao="test_fixture", ko=None, n=2)
    with env.owner.connect() as c:
        tx = c.begin()
        try:
            c.execute(text("SELECT set_config('reva.reviewed_deletion_ticket', 'LINKRET-REVIEWED', true)"))
            c.execute(text("SET LOCAL ROLE reva_restricted_deletion"))
            n = c.execute(text("DELETE FROM kg.link_row WHERE materialisation_id = :m"), {"m": a}).rowcount
            c.execute(text("RESET ROLE"))
            assert n == 2
        finally:
            tx.rollback()
    assert _rows(env.owner, a) == 2


def test_keep_and_vacuum_arguments_are_validated(env: Env) -> None:
    from reva.links import retention

    with env.owner.connect() as c:
        for bad in (0, -1, True):
            with pytest.raises(retention.RetentionError):
                retention.plan_prune(c, keep=bad)  # type: ignore[arg-type]
    with pytest.raises(retention.RetentionError):
        retention.prune(env.owner, vacuum_mode="aggressive")
    with pytest.raises(retention.RetentionError):
        retention.prune(env.owner, reason=" ")
