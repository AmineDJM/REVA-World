"""Simulation identity and artifact integrity (review R5-02, R5-05, R5-15).

R5-02: ``reva_sim`` could INSERT ``sim.scenario`` directly with a spec that does
not match its ``spec_sha256`` and ``sim.create_scenario`` then returned the
forged row. Now only the API's definer function creates scenarios, the
``sim.check_scenario`` trigger binds ``spec`` to the inline canonical bytes that
``spec_sha256`` hashes, and reuse compares the stored spec under an advisory lock.

R5-15: two concurrent ``create_scenario`` calls for one spec return one id.

R5-05: ``reva_sim`` could pre-register ``meta.artifact`` rows (wrong size, rights,
media type) for predictable hashes, blocking or mislabelling later worker
registrations. Now the simulation role registers metadata only through
``sim.register_artifact``, which derives every field from the bytes, and
``register_artifact`` reports a size mismatch as a typed integrity failure.

Each negative check runs in a rolled-back transaction where it would otherwise
leave rows behind; positive controls show the negative checks fail for the
stated reason.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres import repositories as repo
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import IntegrityFailure
from reva.kernel.ids import canonical_bytes, sha256_bytes
from reva.kernel.time import utc_now
from reva.simulation.spec import CONTRACTS, ScenarioSpec, validate_scenario_spec

pytestmark = pytest.mark.db

CREATE = "SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:test')"


@contextmanager
def tx(engine: Engine) -> Iterator[Connection]:
    """A transaction that is always rolled back."""
    with engine.connect() as c:
        t = c.begin()
        try:
            yield c
        finally:
            if t.is_active:
                t.rollback()


@pytest.fixture(scope="module")
def baseline(db: Any) -> dict[str, Any]:
    wb = builtin_connectors()["worldbank.wdi.indicators"]
    asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), db.store, db.worker, persist_cursor=False))
    rel = publish_release(db.worker, db.store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
    return {"db": db, "release_id": rel.release_id, "cutoff": rel.knowledge_cutoff}


def spec(baseline: dict[str, Any], **over: Any) -> ScenarioSpec:
    data = json.loads((CONTRACTS / "scenario.example.json").read_text())
    data["baseline_release_id"] = str(baseline["release_id"])
    data["knowledge_cutoff"] = baseline["cutoff"].isoformat()
    data["title"] = f"R5 integrity {uuid.uuid4()}"
    data.update(over)
    return validate_scenario_spec(data)


def params(s: ScenarioSpec) -> dict[str, Any]:
    return {"spec": json.dumps(s.as_json()), "b": s.canonical_bytes(), "d": s.digest()}


def forged(s: ScenarioSpec) -> str:
    evil = s.as_json()
    evil["intervention"] = {**evil["intervention"], "import_multiplier": 2.99}
    return json.dumps(evil)


# =========================================================================== R5-02
def test_sim_cannot_squat_scenario_identity(baseline: dict[str, Any]) -> None:
    """The review's reproduction: sim pre-registers the spec's sha and a forged scenario."""
    db = baseline["db"]
    s = spec(baseline)
    sha = sha256_bytes(s.canonical_bytes())
    with tx(db.sim) as c, pytest.raises(DBAPIError, match="permission denied for table artifact"):
        c.execute(text("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, "
                       "rights) VALUES (:s, 'application/json', 1, 'x', 'public_aggregate', '{}')"), {"s": sha})
    with tx(db.sim) as c:
        ctx = c.execute(text("INSERT INTO meta.context(mode, origin, manifest_sha256, knowledge_cutoff) "
                             "VALUES ('simulation', 'test_fixture', :s, now()) RETURNING id"),
                        {"s": baseline_manifest(db, baseline)}).scalar_one()
        with pytest.raises(DBAPIError, match="permission denied for table scenario"):
            c.execute(text("""INSERT INTO sim.scenario(context_id, baseline_release_id, spec, spec_digest,
                                spec_sha256, created_by) VALUES (:c, :r, CAST(:sp AS jsonb), 'x', :s, 'human:admin')"""),
                      {"c": ctx, "r": baseline["release_id"], "sp": forged(s), "s": sha})
    # The API's create_scenario stores the caller's spec and its canonical bytes.
    with db.api.begin() as c:
        sid = c.execute(text(CREATE), params(s)).scalar_one()
    with db.owner.connect() as c:
        row = c.execute(text("SELECT spec, spec_sha256 FROM sim.scenario WHERE id = :i"), {"i": sid}).one()
        inline = c.execute(text("SELECT content FROM meta.artifact_inline WHERE artifact_sha256 = :s"),
                           {"s": sha}).scalar_one()
    assert row.spec == s.as_json() and row.spec_sha256 == sha and bytes(inline) == s.canonical_bytes()


def baseline_manifest(db: Any, baseline: dict[str, Any]) -> str:
    with db.owner.connect() as c:
        return str(c.execute(text("SELECT manifest_sha256 FROM belief.release WHERE id = :r"),
                             {"r": baseline["release_id"]}).scalar_one())


SCENARIO_SQL = """INSERT INTO sim.scenario(context_id, baseline_release_id, spec, spec_digest, spec_sha256,
                    created_by) VALUES (:c, :r, CAST(:sp AS jsonb), 'x', :s, 'human:test') RETURNING id"""


def _register_spec_bytes(c: Connection, s: ScenarioSpec, *, inline: bool = True) -> str:
    sha = sha256_bytes(s.canonical_bytes())
    c.execute(text("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, rights) "
                   "VALUES (:s, 'application/json', :n, 'scenario-spec/1.0.0', 'public_aggregate', '{}')"),
              {"s": sha, "n": len(s.canonical_bytes())})
    if inline:
        c.execute(text("INSERT INTO meta.artifact_inline(artifact_sha256, content) VALUES (:s, :b)"),
                  {"s": sha, "b": s.canonical_bytes()})
    return sha


def _sim_context(c: Connection, sha: str) -> UUID:
    return UUID(str(c.execute(text("INSERT INTO meta.context(mode, origin, manifest_sha256, knowledge_cutoff) "
                                   "VALUES ('simulation', 'test_fixture', :s, now()) RETURNING id"),
                              {"s": sha}).scalar_one()))


def test_scenario_trigger_binds_spec_to_verified_bytes(baseline: dict[str, Any]) -> None:
    """Even the owner (any future definer path) cannot store a spec its sha does not hash."""
    db = baseline["db"]
    s = spec(baseline)
    with tx(db.owner) as c:
        sha = _register_spec_bytes(c, s)
        with pytest.raises(DBAPIError, match="does not match its verified canonical bytes"), c.begin_nested():
            c.execute(text(SCENARIO_SQL), {"c": _sim_context(c, sha), "r": baseline["release_id"],
                                           "sp": forged(s), "s": sha})
        # positive control: the matching spec is accepted
        c.execute(text(SCENARIO_SQL), {"c": _sim_context(c, sha), "r": baseline["release_id"],
                                       "sp": json.dumps(s.as_json()), "s": sha})
    s2 = spec(baseline)
    with tx(db.owner) as c:  # metadata without verified (inline) bytes is not enough
        sha = _register_spec_bytes(c, s2, inline=False)
        with pytest.raises(DBAPIError, match="does not match its verified canonical bytes"):
            c.execute(text(SCENARIO_SQL), {"c": _sim_context(c, sha), "r": baseline["release_id"],
                                           "sp": json.dumps(s2.as_json()), "s": sha})


def test_create_scenario_refuses_to_reuse_a_row_with_a_different_spec(baseline: dict[str, Any]) -> None:
    """Defence in depth: a forged row that bypassed the trigger is never returned."""
    db = baseline["db"]
    s = spec(baseline)
    with tx(db.owner) as c:
        sha = _register_spec_bytes(c, s)
        c.execute(text("ALTER TABLE sim.scenario DISABLE TRIGGER scenario_context"))
        squat = c.execute(text(SCENARIO_SQL), {"c": _sim_context(c, sha), "r": baseline["release_id"],
                                               "sp": forged(s), "s": sha}).scalar_one()
        c.execute(text("ALTER TABLE sim.scenario ENABLE TRIGGER scenario_context"))
        assert squat is not None
        with pytest.raises(DBAPIError, match="with a different spec"):
            c.execute(text(CREATE), params(s))


def test_create_scenario_is_idempotent(baseline: dict[str, Any]) -> None:
    db = baseline["db"]
    s = spec(baseline)
    with db.api.begin() as c:
        first = c.execute(text(CREATE), params(s)).scalar_one()
    with db.api.begin() as c:
        assert c.execute(text(CREATE), params(s)).scalar_one() == first


# =========================================================================== R5-15
def test_concurrent_identical_create_scenario_returns_one_id(baseline: dict[str, Any]) -> None:
    db = baseline["db"]
    s = spec(baseline)
    out: dict[str, Any] = {}

    def second() -> None:
        try:
            with db.api.begin() as c2:
                out["sid"] = c2.execute(text(CREATE), params(s)).scalar_one()
        except Exception as e:  # noqa: BLE001 - reported below
            out["error"] = e

    with db.api.connect() as c1:
        t1 = c1.begin()
        first = c1.execute(text(CREATE), params(s)).scalar_one()
        th = threading.Thread(target=second)
        th.start()
        time.sleep(1.0)
        assert th.is_alive(), "the second creator must wait for the first"
        t1.commit()
    th.join(30)
    assert "error" not in out, out.get("error")
    assert out["sid"] == first
    with db.owner.connect() as c:
        n = c.execute(text("SELECT count(*) FROM sim.scenario WHERE spec_sha256 = :s"),
                      {"s": sha256_bytes(s.canonical_bytes())}).scalar_one()
    assert n == 1


# =========================================================================== R5-05
def _manifest(**extra: Any) -> dict[str, Any]:
    return {"schema": "reva-model-manifest/1",
            "models": [{"model_key": "r5.test", "version": "1", "spec_sha256": uuid.uuid4().hex * 2}], **extra}


def _row(c: Connection, sha: str) -> dict[str, Any]:
    return dict(c.execute(text("SELECT media_type, byte_size, schema_version, privacy_class, rights "
                               "FROM meta.artifact WHERE sha256 = :s"), {"s": sha}).mappings().one())


def test_sim_cannot_preregister_artifact_metadata(baseline: dict[str, Any]) -> None:
    """The review's reproduction: a wrong-size squat used to block the worker forever."""
    db = baseline["db"]
    payload = canonical_bytes(_manifest())
    sha = sha256_bytes(payload)
    with db.sim.connect() as c, pytest.raises(DBAPIError, match="permission denied for table artifact"):
        with c.begin():
            c.execute(text("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, "
                           "privacy_class, rights) VALUES (:s, 'text/html', 1, 'squatted', 'public_aggregate', "
                           "'{\"licence\":\"none\"}')"), {"s": sha})
    obj = db.store.put_bytes(payload, "belief", "application/json")
    with db.worker.begin() as c:  # the legitimate writer is not blocked
        repo.register_artifact(c, obj, schema_version="model-manifest/1", rights={"licence": "CC-BY-4.0"})
    with db.owner.connect() as c:
        assert _row(c, sha) == {"media_type": "application/json", "byte_size": len(payload),
                                "schema_version": "model-manifest/1", "privacy_class": "public_aggregate",
                                "rights": {"licence": "CC-BY-4.0"}}


def test_sim_register_artifact_derives_metadata_from_the_bytes(baseline: dict[str, Any]) -> None:
    db = baseline["db"]
    payload = canonical_bytes({"schema": "reva-simulation-result/1", "run_id": str(uuid.uuid4())})
    with tx(db.sim) as c:
        sha = c.execute(text("SELECT sim.register_artifact(:b)"), {"b": payload}).scalar_one()
        assert sha == sha256_bytes(payload)
        assert _row(c, sha) == {"media_type": "application/json", "byte_size": len(payload),
                                "schema_version": "simulation-result/1", "privacy_class": "public_aggregate",
                                "rights": {"licence": "internal-derived"}}
        # positive control: its own location (R-18 policy) in the scenarios namespace
        c.execute(text("INSERT INTO meta.artifact_location(artifact_sha256, storage_uri, namespace, "
                       "verified_byte_size, checksum_verified_at) VALUES (:s, :u, 'scenarios', :n, now())"),
                  {"s": sha, "u": f"file:///scenarios/{sha}", "n": len(payload)})


@pytest.mark.parametrize("payload,match", [
    (b"\x89PNG raw source bytes", "not a UTF-8 JSON document"),
    (b"[1, 2, 3]", "only simulation result and model manifests"),
    (canonical_bytes({"schema": "reva-ontology-manifest/1"}), "only simulation result and model manifests"),
    (canonical_bytes({"schema": "reva-belief-release/1"}), "only simulation result and model manifests"),
    (canonical_bytes({"data": "worldbank response"}), "only simulation result and model manifests"),
])
def test_sim_register_artifact_refuses_foreign_kinds(baseline: dict[str, Any], payload: bytes, match: str) -> None:
    with tx(baseline["db"].sim) as c, pytest.raises(DBAPIError, match=match):
        c.execute(text("SELECT sim.register_artifact(:b)"), {"b": payload})


def test_api_and_worker_cannot_call_sim_register_artifact(baseline: dict[str, Any]) -> None:
    db = baseline["db"]
    for e in (db.api, db.worker):
        with tx(e) as c, pytest.raises(DBAPIError, match="permission denied for function register_artifact"):
            c.execute(text("SELECT sim.register_artifact(:b)"), {"b": canonical_bytes(_manifest())})


def test_sim_preregistration_of_worker_bytes_does_not_block_or_relabel(baseline: dict[str, Any]) -> None:
    """Sim registers bytes the worker will write (a predictable model manifest) first."""
    db = baseline["db"]
    payload = canonical_bytes(_manifest())
    sha = sha256_bytes(payload)
    with db.sim.begin() as c:
        assert c.execute(text("SELECT sim.register_artifact(:b)"), {"b": payload}).scalar_one() == sha
    obj = db.store.put_bytes(payload, "belief", "application/json")
    with db.worker.begin() as c:
        assert repo.register_artifact(c, obj, schema_version="model-manifest/1",
                                      rights={"licence": "internal-derived"}) == sha
    with db.owner.connect() as c:
        assert _row(c, sha) == {"media_type": "application/json", "byte_size": len(payload),
                                "schema_version": "model-manifest/1", "privacy_class": "public_aggregate",
                                "rights": {"licence": "internal-derived"}}
        assert c.execute(text("SELECT namespace FROM meta.artifact_location WHERE artifact_sha256 = :s"),
                         {"s": sha}).scalars().all() == ["belief"]


def test_register_artifact_reports_size_mismatch_as_integrity_failure(baseline: dict[str, Any]) -> None:
    db = baseline["db"]
    payload = canonical_bytes(_manifest(note="size"))
    obj = db.store.put_bytes(payload, "belief", "application/json")
    with tx(db.worker) as c:
        c.execute(text("INSERT INTO meta.artifact(sha256, media_type, byte_size, schema_version, privacy_class, "
                       "rights) VALUES (:s, 'application/json', 1, 'x', 'public_aggregate', '{}')"),
                  {"s": obj.sha256})
        with pytest.raises(IntegrityFailure, match="byte_size 1"):
            repo.register_artifact(c, obj, schema_version="model-manifest/1", rights={"licence": "x"})
