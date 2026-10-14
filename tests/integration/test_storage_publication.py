"""Publication protocol fault injection (architecture §8.2).

Bytes are written to the object store first; metadata, observations and outbox
rows commit in one transaction. A failure at any point before commit must leave
at most an unreferenced (staged) object and nothing visible to readers; a
retry must yield exactly one visible acquisition.

Runs against a private database (see tests/fixtures/adversarial/isolated_db.py)
because it publishes observations.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.belief.selection import eligible_latest
from reva.evidence import ingest
from reva.evidence.ingest import acquisition_idempotency_key, publish_acquisition
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import sha256_bytes
from reva.ports.connectors import FetchBudget, ObservationBatch, QualityReport, RawAcquisition

pytestmark = pytest.mark.db

WB = "worldbank.wdi.indicators"


def _helper() -> Any:
    name = "reva_h2_isolated_db"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "fixtures" / "adversarial" / "isolated_db.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def pdb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    with _helper().isolated_db(tmp_path_factory) as d:
        yield d


# --------------------------------------------------------------------------- helpers
def captured(item_key: str, pad: int) -> tuple[Any, RawAcquisition, list[ObservationBatch], list[QualityReport]]:
    """A real WB connector + captured fixture acquisition; ``pad`` trailing spaces make the
    raw bytes unique per test without changing their JSON meaning."""
    conn = builtin_connectors()[WB]
    item = next(i for i in conn.plan(None, FetchBudget()).items if i.key == item_key)
    raw = asyncio.run(conn.fetch(item, FixtureHttp(WB)))
    assert raw.origin == "test_fixture"
    raw = raw.model_copy(update={"body": raw.body + b" " * pad})
    batches = list(conn.parse(raw))
    reports = [conn.validate(b) for b in batches]
    assert all(not r.blocking for r in reports), [r.findings for r in reports]
    return conn, raw, batches, reports


def visible(engine: Engine, raw_sha: str) -> dict[str, int]:
    with engine.connect() as c:
        q = lambda sql: int(c.execute(text(sql), {"s": raw_sha}).scalar_one())  # noqa: E731
        return {
            "artifact": q("SELECT count(*) FROM meta.artifact WHERE sha256 = :s"),
            "location": q("SELECT count(*) FROM meta.artifact_location WHERE artifact_sha256 = :s"),
            "acquisition": q("SELECT count(*) FROM evidence.acquisition WHERE raw_sha256 = :s"),
            "observation": q(
                "SELECT count(*) FROM evidence.observation o JOIN evidence.acquisition a "
                "ON a.id = o.acquisition_id WHERE a.raw_sha256 = :s"
            ),
            "outbox": q(
                "SELECT count(*) FROM ops.outbox WHERE payload->>'acquisition_id' IN "
                "(SELECT id::text FROM evidence.acquisition WHERE raw_sha256 = :s)"
            ),
        }


NOTHING = {"artifact": 0, "location": 0, "acquisition": 0, "observation": 0, "outbox": 0}


def reader_sees(engine: Engine, raw_sha: str) -> int:
    """What belief selection (the reader path) can see from this acquisition."""
    with engine.connect() as c:
        rows = eligible_latest(c, datetime.now(UTC), ["test_fixture", "live"])
        ids = {
            str(r)
            for r in c.execute(
                text(
                    "SELECT o.id FROM evidence.observation o JOIN evidence.acquisition a "
                    "ON a.id = o.acquisition_id WHERE a.raw_sha256 = :s"
                ),
                {"s": raw_sha},
            ).scalars()
        }
    return sum(1 for r in rows if str(r["id"]) in ids)


class FailingStore:
    """Wraps a real store; fails before or during the upload."""

    def __init__(self, inner: FilesystemArtifactStore, mode: str) -> None:
        self.inner, self.mode = inner, mode

    def put_bytes(self, data: bytes, namespace: str, media_type: str) -> Any:
        if self.mode == "before":
            raise OSError("injected: object store unreachable before upload")

        class Broken(io.RawIOBase):
            sent = 0

            def readable(self) -> bool:
                return True

            def read(self, n: int = -1) -> bytes:
                if self.sent >= len(data) // 2:
                    raise ConnectionResetError("injected: connection reset mid-upload")
                chunk = data[self.sent : self.sent + max(1, min(n if n > 0 else 1024, 1024))]
                self.sent += len(chunk)
                return chunk

        return self.inner.put(Broken(), namespace, media_type, max_bytes=len(data) + 1)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def objects(store: FilesystemArtifactStore) -> list[Path]:
    return [p for p in store.root.rglob("*") if p.is_file() and ".staging" not in p.parts]


# --------------------------------------------------------------------------- tests
def test_failure_before_upload_leaves_nothing(pdb: Any) -> None:
    conn, raw, batches, reports = captured("series:SP.POP.TOTL:p1", pad=1)
    sha = sha256_bytes(raw.body)
    before = len(objects(pdb.store))
    with pytest.raises(OSError):
        publish_acquisition(pdb.worker, FailingStore(pdb.store, "before"), conn.describe(), raw, batches, reports)
    assert pdb.store.head("raw", sha) is None
    assert len(objects(pdb.store)) == before
    assert visible(pdb.owner, sha) == NOTHING


def test_failure_mid_upload_leaves_no_object_and_no_rows(pdb: Any) -> None:
    conn, raw, batches, reports = captured("series:SP.POP.TOTL:p1", pad=2)
    sha = sha256_bytes(raw.body)
    before = len(objects(pdb.store))
    with pytest.raises(ConnectionResetError):
        publish_acquisition(pdb.worker, FailingStore(pdb.store, "mid"), conn.describe(), raw, batches, reports)
    assert pdb.store.head("raw", sha) is None
    assert len(objects(pdb.store)) == before
    assert [p for p in pdb.store.staging.iterdir()] == []
    assert visible(pdb.owner, sha) == NOTHING


def test_crash_after_upload_before_commit_is_invisible_then_retry_publishes_once(pdb: Any) -> None:
    conn, raw, batches, reports = captured("series:SP.POP.TOTL:p1", pad=3)
    spec = conn.describe()
    sha = sha256_bytes(raw.body)
    n_obs = sum(len(b.observations) for b in batches)
    assert n_obs == 100

    with pytest.raises(RuntimeError, match="injected fault"):
        publish_acquisition(pdb.worker, pdb.store, spec, raw, batches, reports, fault_after_object_write=True)
    # The staged object exists (an orphan for GC) ...
    head = pdb.store.head("raw", sha)
    assert head is not None and head.byte_size == len(raw.body)
    # ... but nothing references it and readers cannot see anything.
    assert visible(pdb.owner, sha) == NOTHING
    assert reader_sees(pdb.api, sha) == 0

    # Retry (at-least-once delivery) yields exactly one visible acquisition.
    s1 = publish_acquisition(pdb.worker, pdb.store, spec, raw, batches, reports)
    assert not s1.duplicate_acquisition and s1.raw_sha256 == sha and s1.namespace == "raw"
    assert s1.inserted + s1.unchanged == n_obs
    v1 = visible(pdb.owner, sha)
    assert v1["artifact"] == 1 and v1["location"] == 1 and v1["acquisition"] == 1
    assert v1["observation"] == s1.inserted

    # Crash after commit but before the queue acknowledged: the job is re-run.
    s2 = publish_acquisition(pdb.worker, pdb.store, spec, raw, batches, reports)
    assert s2.duplicate_acquisition and s2.acquisition_id == s1.acquisition_id
    assert s2.inserted == 0
    assert visible(pdb.owner, sha) == v1
    with pdb.owner.connect() as c:
        keys = c.execute(
            text("SELECT count(*) FROM evidence.acquisition WHERE idempotency_key = :k"),
            {"k": acquisition_idempotency_key(spec, raw, sha)},
        ).scalar_one()
    assert keys == 1


def test_failure_inside_transaction_rolls_back_everything(pdb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows written in the publication transaction are invisible to other sessions and vanish
    entirely when the transaction fails before commit."""
    conn, raw, batches, reports = captured("series:NY.GDP.MKTP.CD:p1", pad=4)
    spec = conn.describe()
    sha = sha256_bytes(raw.body)
    seen: dict[str, int] = {}
    real_emit = ingest.repo.emit_outbox

    def emit_then_crash(c: Any, event_type: str, payload: Any) -> None:
        real_emit(c, event_type, payload)
        if event_type == "evidence.observations_changed":
            seen["inside"] = int(
                c.execute(
                    text("SELECT count(*) FROM evidence.observation WHERE acquisition_id = :a"),
                    {"a": payload["acquisition_id"]},
                ).scalar_one()
            )
            seen["outside"] = visible(pdb.worker, sha)["observation"]  # another session, same instant
            raise RuntimeError("injected: worker killed before COMMIT")

    monkeypatch.setattr(ingest.repo, "emit_outbox", emit_then_crash)
    with pytest.raises(RuntimeError, match="before COMMIT"):
        publish_acquisition(pdb.worker, pdb.store, spec, raw, batches, reports)
    assert seen["inside"] > 0  # the transaction really had written observations
    assert seen["outside"] == 0  # ... which no other session could see
    assert pdb.store.head("raw", sha) is not None  # staged orphan only
    assert visible(pdb.owner, sha) == NOTHING
    assert reader_sees(pdb.api, sha) == 0

    monkeypatch.setattr(ingest.repo, "emit_outbox", real_emit)
    s = publish_acquisition(pdb.worker, pdb.store, spec, raw, batches, reports)
    assert s.inserted == seen["inside"]
    assert visible(pdb.owner, sha)["acquisition"] == 1
    assert reader_sees(pdb.api, sha) == s.inserted


def test_unresolvable_subject_aborts_whole_acquisition(pdb: Any) -> None:
    conn, raw, batches, reports = captured("series:SP.POP.TOTL:p1", pad=5)
    spec = conn.describe()
    sha = sha256_bytes(raw.body)
    b = batches[0]
    bad_obs = b.observations[-1].model_copy(update={"subject_key": "XKX", "logical_key": "wb:test:XKX:2000"})
    bad = b.model_copy(update={"observations": (*b.observations, bad_obs)})
    with pytest.raises(ValidationProblem, match="no reviewed alias"):
        publish_acquisition(pdb.worker, pdb.store, spec, raw, [bad], reports)
    assert visible(pdb.owner, sha) == NOTHING  # no partially visible acquisition


def test_quarantined_acquisition_is_recorded_but_publishes_no_observations(pdb: Any) -> None:
    conn, raw, batches, _ = captured("series:SP.POP.TOTL:p1", pad=6)
    blocked = [QualityReport(outcome="blocked", findings=(
        {"code": "pagination_truncated", "severity": "blocker", "message": "injected"},))]
    s = publish_acquisition(pdb.worker, pdb.store, conn.describe(), raw, batches, blocked)
    assert s.namespace == "quarantine" and s.inserted == 0
    assert pdb.store.head("quarantine", s.raw_sha256) is not None
    assert pdb.store.head("raw", s.raw_sha256) is None
    v = visible(pdb.owner, s.raw_sha256)
    assert v["acquisition"] == 1 and v["observation"] == 0
    with pdb.owner.connect() as c:
        ev = c.execute(
            text("SELECT event_type FROM ops.outbox WHERE payload->>'acquisition_id' = :a"),
            {"a": str(s.acquisition_id)},
        ).scalars().all()
    assert ev == ["evidence.acquisition_quarantined"]
    assert reader_sees(pdb.api, s.raw_sha256) == 0
