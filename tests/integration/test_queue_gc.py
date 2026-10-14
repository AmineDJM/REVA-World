"""Two-pass orphan GC: an unreferenced staged object is deleted only when older
than 7 days AND confirmed by a second scan; referenced objects never are."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from reva.adapters.postgres import repositories as repo
from reva.kernel.errors import PolicyDenied
from reva.operations.context import JobContext
from reva.operations.gc import gc_scan, handle_gc

pytestmark = pytest.mark.db
EIGHT_DAYS = 8 * 86400


def _age(uri: str, seconds: float) -> None:
    p = Path(uri.removeprefix("file://"))
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_two_pass_orphan_gc(db) -> None:
    tag = uuid.uuid4().hex.encode()
    old_orphan = db.store.put_bytes(b"orphan-staged-" + tag, "raw", "application/octet-stream")
    young_orphan = db.store.put_bytes(b"young-staged-" + tag, "normalised", "application/octet-stream")
    with db.worker.begin() as c:
        sha = repo.store_json_artifact(c, db.store, {"published": tag.decode()}, "belief",
                                       schema_version="test/1")
        ref_uri = repo.artifact_locations(c, sha)[0]["storage_uri"]
    _age(old_orphan.storage_uri, EIGHT_DAYS)
    _age(ref_uri, EIGHT_DAYS)

    first = gc_scan(db.worker, db.store)
    assert Path(old_orphan.storage_uri.removeprefix("file://")).exists()  # one scan is never enough
    assert old_orphan.storage_uri not in first.deleted
    with db.owner.connect() as c:
        scans = c.execute(text("SELECT confirmed_scans FROM ops.gc_candidate WHERE storage_uri = :u"),
                          {"u": old_orphan.storage_uri}).scalar()
        assert scans == 1
        assert c.execute(text("SELECT count(*) FROM ops.gc_candidate WHERE storage_uri = :u"),
                         {"u": ref_uri}).scalar() == 0

    second = gc_scan(db.worker, db.store)
    assert old_orphan.storage_uri in second.deleted
    assert not Path(old_orphan.storage_uri.removeprefix("file://")).exists()
    assert Path(young_orphan.storage_uri.removeprefix("file://")).exists()  # < 7 days old
    assert Path(ref_uri.removeprefix("file://")).exists()  # published objects are never deleted
    assert db.store.get("belief", sha)
    with db.owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ops.gc_candidate WHERE storage_uri = :u"),
                         {"u": old_orphan.storage_uri}).scalar() == 0
        assert c.execute(text("SELECT confirmed_scans FROM ops.gc_candidate WHERE storage_uri = :u"),
                         {"u": young_orphan.storage_uri}).scalar() == 2
        assert c.execute(text("SELECT count(*) FROM ops.audit WHERE action = 'gc.delete_orphan' "
                              "AND target->>'storage_uri' = :u"), {"u": old_orphan.storage_uri}).scalar() == 1


def test_gc_job_refuses_short_retention(db) -> None:
    ctx = JobContext(job_id=uuid.uuid4(), job_type="gc", worker_id="w", fencing_token=1, attempt=1,
                     settings=db.settings, worker_engine=db.worker, sim_engine=None, store=db.store)
    with pytest.raises(PolicyDenied):
        handle_gc({"min_age_days": 1}, ctx)
