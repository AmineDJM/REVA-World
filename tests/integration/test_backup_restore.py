"""Backup -> restore into a new database reconstructs identical logical manifests
and as-of results, and the restored database keeps its immutability guarantees.

Uses the real scripts (scripts/backup.sh, scripts/restore.sh) as subprocesses.
pg_dump/pg_restore must be at least the server's major version: the local
client is often older than PostgreSQL 17, so the tools run inside the database
container (REVA_PG_CONTAINER, default ``reva-db`` when it exists) or come from
REVA_PG_BINDIR. Wall times are printed (run with ``-s``) and attached as junit
properties.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.belief.snapshots import estimates, release_as_of
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
br = importlib.import_module("backup_restore")


def _pg_tools_env() -> dict[str, str]:
    if os.environ.get("REVA_PG_BINDIR"):
        return {"REVA_PG_BINDIR": os.environ["REVA_PG_BINDIR"]}
    container = os.environ.get("REVA_PG_CONTAINER", "reva-db")
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "inspect", container], capture_output=True, check=False).returncode == 0:
        return {"REVA_PG_CONTAINER": container}
    if shutil.which("pg_dump"):
        return {}
    pytest.skip("no PostgreSQL client tools >= server version available (set REVA_PG_CONTAINER/REVA_PG_BINDIR)")


def _db_now(engine: Engine) -> datetime:
    with engine.connect() as c:
        return c.execute(text("SELECT clock_timestamp()")).scalar_one()


def _as_of_results(engine: Engine, times: list[datetime]) -> list[dict[str, Any]]:
    out = []
    with engine.connect() as conn:
        for t in times:
            rel = release_as_of(conn, t, "test_fixture")
            if rel is None:
                out.append({"as_of": t.isoformat(), "release": None})
                continue
            # estimates() orders by (concept, label, period) without a unique tiebreaker,
            # so compare as a set keyed by the immutable estimate id.
            rows = sorted(estimates(conn, rel["id"]), key=lambda r: str(r["id"]))
            out.append(
                {
                    "as_of": t.isoformat(),
                    "release": str(rel["id"]),
                    "manifest_sha256": rel["manifest_sha256"],
                    "sequence": rel["sequence"],
                    "estimates": json.loads(json.dumps(rows, default=str, sort_keys=True)),
                }
            )
    return out


def _manifest(url: str) -> dict[str, Any]:
    conn = br.connect(br.make_url(url), autocommit=True)
    try:
        return br.logical_manifest(conn)
    finally:
        conn.close()


def _run(script: str, args: list[str], env: dict[str, str], cwd: Path) -> tuple[dict[str, Any], float, str]:
    t0 = time.perf_counter()
    proc = subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    wall = time.perf_counter() - t0
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout), wall, proc.stderr


def test_backup_restore_reconstructs_manifests_and_as_of(db, tmp_path: Path, record_property) -> None:
    tools_env = _pg_tools_env()

    async def ingest(name: str) -> None:
        c = builtin_connectors()[name]
        await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)

    t_before = _db_now(db.worker) - timedelta(seconds=1)
    asyncio.run(ingest("worldbank.wdi.indicators"))
    rel1 = publish_release(
        db.worker, db.store, cutoff=_db_now(db.worker), origin=Origin.test_fixture, environment="test"
    )
    t_mid = _db_now(db.worker)
    asyncio.run(ingest("un.comtrade.public_exact"))
    rel2 = publish_release(
        db.worker, db.store, cutoff=_db_now(db.worker), origin=Origin.test_fixture, environment="test"
    )
    t_after = _db_now(db.worker)
    times = [t_before, t_mid, t_after]
    as_of_before = _as_of_results(db.api, times)
    assert as_of_before[1]["release"] == str(rel1.release_id)
    assert as_of_before[2]["release"] == str(rel2.release_id)
    assert as_of_before[1]["estimates"] != as_of_before[2]["estimates"]

    source_url = db.settings.reva_migration_database_url.get_secret_value()
    manifest_before = _manifest(source_url)
    assert manifest_before["artifact_count"] > 0
    shas_before = sorted(r["manifest_sha256"] for r in manifest_before["releases"])
    assert {rel1.manifest_sha256, rel2.manifest_sha256} <= set(shas_before)

    # --- backup (encrypted with a throwaway age identity) ---------------------------
    identity = tmp_path / "age.key"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = next(
        line.split(": ", 1)[1] for line in identity.read_text().splitlines() if line.startswith("# public key: ")
    )
    s = db.settings
    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "REVA_ENV": "test",
        "REVA_PYTHON": sys.executable,
        "REVA_OBJECT_BACKEND": "filesystem",
        "REVA_OBJECT_ROOT": str(s.reva_object_root),
        "REVA_S3_PREFIX": s.reva_s3_prefix,
        "REVA_DB_HOST": s.reva_db_host,
        "REVA_DB_PORT": str(s.reva_db_port),
        "REVA_API_DB_PASSWORD": s.reva_api_db_password.get_secret_value(),
        "REVA_WORKER_DB_PASSWORD": s.reva_worker_db_password.get_secret_value(),
        "REVA_SIM_DB_PASSWORD": s.reva_sim_db_password.get_secret_value(),
        **tools_env,
    }
    backup_env = {**base_env, "REVA_MIGRATION_DATABASE_URL": source_url, "REVA_DB_NAME": db.name}
    out, backup_wall, backup_err = _run(
        "backup.sh", ["--out", str(tmp_path / "backups"), "--recipient", recipient], backup_env, tmp_path
    )
    owner_pw = db.settings.migration_url().password
    assert owner_pw not in json.dumps(out) + backup_err
    bdir = Path(out["path"])
    record = json.loads((bdir / "backup.json").read_text())
    assert record["dump"]["encrypted"] and not (bdir / "reva.dump").exists()
    # No plaintext dump was ever written: the set is exactly ciphertext + metadata.
    assert sorted(p.name for p in bdir.iterdir()) == ["SHA256SUMS", "backup.json", "objects.jsonl", "reva.dump.age"]
    assert subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=bdir, check=False).returncode == 0
    assert record["objects"]["count"] > 0 and record["objects"]["missing"] == []
    assert record["objects"]["corrupt_objects"] == [] and record["objects"]["mismatched"] == []
    # The manifest recorded inside the dump snapshot equals the one computed independently.
    assert record["logical_manifest"]["manifest_sha256"] == manifest_before["manifest_sha256"]
    inventory = [json.loads(line) for line in (bdir / "objects.jsonl").read_text().splitlines()]
    assert all(i["sha256"] == i["address"] for i in inventory)

    # --- restore into a new database ---------------------------------------------------
    target = f"{db.name}_r{uuid.uuid4().hex[:6]}"
    admin = create_engine(db.settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{target}"'))
    target_url = db.settings.migration_url().set(database=target)
    restored_engines: list[Engine] = []
    probe_role = f"reva_restore_probe_{uuid.uuid4().hex[:6]}"
    probe_db = f"{target}_ns"
    try:
        restore_env = {
            **base_env,
            "REVA_RESTORE_DATABASE_URL": target_url.render_as_string(hide_password=False),
            "REVA_BACKUP_AGE_IDENTITY": str(identity),
        }
        # Tampered sets are refused before the target is touched.
        for case in ("checksum", "authenticated"):
            bad = tmp_path / f"tampered-{case}"
            shutil.copytree(bdir, bad)
            data = bytearray((bad / "reva.dump.age").read_bytes())
            data[len(data) // 2] ^= 0x01
            (bad / "reva.dump.age").write_bytes(bytes(data))
            if case == "authenticated":  # attacker also rewrites every checksum, but has no age key
                rec = json.loads((bad / "backup.json").read_text())
                rec["dump"]["sha256"] = br._sha256_file(bad / "reva.dump.age")
                (bad / "backup.json").write_text(json.dumps(rec))
                br.write_checksums(bad, ["reva.dump.age", "objects.jsonl", "backup.json"])
            proc = subprocess.run(
                ["bash", str(SCRIPTS / "restore.sh"), str(bad)],
                env=restore_env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            assert proc.returncode != 0
            assert ("checksum verification failed" if case == "checksum" else "age") in proc.stderr
            with create_engine(target_url).connect() as c:
                n = c.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = ANY(:s)"), {"s": list(br.SCHEMAS)}
                ).scalar_one()
            assert n == 0, f"tampered archive ({case}) touched the target"
        res, restore_wall, restore_err = _run("restore.sh", [str(bdir), "--verify-objects"], restore_env, tmp_path)
        assert owner_pw not in json.dumps(res) + restore_err
        assert res["matches_backup"] and res["differences"] == []
        assert res["timings"]["verify_seconds"] >= 0
        assert res["bootstrap"]["schema_version"] == manifest_before["schema_version"]
        assert res["objects"]["missing"] == [] and res["objects"]["locations"] > 0

        # Independent re-computation on the restored database.
        manifest_after = _manifest(target_url.render_as_string(hide_password=False))
        assert br.diff_manifests(manifest_before, manifest_after) == []
        assert manifest_after["manifest_sha256"] == manifest_before["manifest_sha256"]

        settings_r = db.settings.model_copy(update={"reva_db_name": target, "reva_migration_database_url": None})
        api_r = make_engine("api", settings_r)
        worker_r = make_engine("worker", settings_r)
        owner_r = create_engine(target_url)
        restored_engines += [api_r, worker_r, owner_r]
        # Identical as-of answers through the runtime API login on the restored copy.
        as_of_after = _as_of_results(api_r, times)
        assert as_of_after == as_of_before

        # Immutability and grants survive the restore.
        with pytest.raises(DBAPIError, match="Immutable scientific record"), owner_r.begin() as c:
            c.execute(text("UPDATE belief.release SET method = method WHERE id = :i"), {"i": rel1.release_id})
        with pytest.raises(DBAPIError), worker_r.begin() as c:
            c.execute(text("UPDATE evidence.observation SET recorded_at = recorded_at"))
        with pytest.raises(DBAPIError), worker_r.begin() as c:
            c.execute(text("DELETE FROM meta.artifact"))
        with pytest.raises(DBAPIError), api_r.begin() as c:
            c.execute(
                text(
                    "INSERT INTO ops.publication(release_id, activity_id) SELECT release_id, activity_id "
                    "FROM ops.publication LIMIT 1"
                )
            )
        with owner_r.connect() as c:
            owned = c.execute(
                text(
                    "SELECT count(*) FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace "
                    "JOIN pg_roles r ON r.oid = cl.relowner WHERE n.nspname = ANY(:s) "
                    "AND r.rolname IN ('reva_api','reva_worker','reva_sim')"
                ),
                {"s": list(br.SCHEMAS)},
            ).scalar_one()
            public_connect = c.execute(
                text("SELECT has_database_privilege('public', current_database(), 'CONNECT')")
            ).scalar_one()
        assert owned == 0 and public_connect is False

        # Restore as a NON-superuser that owns the empty target database (as on a
        # managed service). PostGIS is pre-created by an administrator because it is
        # not a trusted extension locally; bootstrap is skipped because altering the
        # cluster-global runtime roles needs privileges this role does not have.
        probe_pw = uuid.uuid4().hex
        with admin.connect() as c:
            c.execute(text(f"CREATE ROLE \"{probe_role}\" LOGIN NOSUPERUSER PASSWORD '{probe_pw}'"))
            c.execute(text(f'CREATE DATABASE "{probe_db}" OWNER "{probe_role}"'))
        with create_engine(db.settings.migration_url().set(database=probe_db)).begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        probe_url = db.settings.migration_url().set(database=probe_db, username=probe_role, password=probe_pw)
        ns_env = {**restore_env, "REVA_RESTORE_DATABASE_URL": probe_url.render_as_string(hide_password=False)}
        res_ns, _, ns_err = _run("restore.sh", [str(bdir), "--skip-bootstrap"], ns_env, tmp_path)
        assert res_ns["matches_backup"] and res_ns["restored_as_superuser"] is False
        assert probe_pw not in json.dumps(res_ns) + ns_err
        api_ns = make_engine("api", settings_r.model_copy(update={"reva_db_name": probe_db}))
        restored_engines.append(api_ns)
        assert _as_of_results(api_ns, times) == as_of_before
        with create_engine(probe_url).connect() as c:
            owners = (
                c.execute(
                    text(
                        "SELECT DISTINCT r.rolname FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace "
                        "JOIN pg_roles r ON r.oid = cl.relowner WHERE n.nspname = ANY(:s)"
                    ),
                    {"s": list(br.SCHEMAS)},
                )
                .scalars()
                .all()
            )
        assert owners == [probe_role]

        timings = {
            "backup_wall_seconds": round(backup_wall, 3),
            "restore_wall_seconds": round(restore_wall, 3),
            "pg_dump_seconds": record["timings"]["pg_dump_seconds"],
            "pg_restore_seconds": res["timings"]["pg_restore_seconds"],
            "bootstrap_seconds": res["timings"].get("bootstrap_seconds"),
            "dump_bytes": record["dump"]["plaintext_bytes"],
            "objects": record["objects"]["count"],
            "tables": len(manifest_before["tables"]),
            "rows": sum(t["rows"] for t in manifest_before["tables"].values()),
            "pg_tools": tools_env or {"local": True},
        }
        for k, v in timings.items():
            record_property(k, v)
        print("\nBACKUP/RESTORE TIMINGS " + json.dumps(timings, sort_keys=True))
    finally:
        for e in restored_engines:
            e.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)'))
            c.execute(text(f'DROP DATABASE IF EXISTS "{probe_db}" WITH (FORCE)'))
            c.execute(text(f'DROP ROLE IF EXISTS "{probe_role}"'))
        admin.dispose()


def test_restore_refuses_unverifiable_sets_and_non_empty_target(db, tmp_path: Path) -> None:
    import hashlib

    bdir = tmp_path / "b"
    bdir.mkdir()
    (bdir / "reva.dump").write_bytes(b"not a dump")
    good = hashlib.sha256(b"not a dump").hexdigest()
    record = {
        "dump": {"file": "reva.dump", "sha256": good, "plaintext_sha256": good, "encrypted": False},
        "pg_dump_major": 17,
    }
    (bdir / "backup.json").write_text(json.dumps(record))
    kwargs = {"tools": br.PgTools(), "identity": None, "run_bootstrap": False, "verify_objects": False}
    with pytest.raises(br.BackupError, match="SHA256SUMS missing"):
        br.restore(bdir, url_env="REVA_RESTORE_DATABASE_URL", **kwargs)
    br.write_checksums(bdir, ["reva.dump", "backup.json"])
    (bdir / "reva.dump").write_bytes(b"not a dumq")
    with pytest.raises(br.BackupError, match="checksum verification failed"):
        br.restore(bdir, url_env="REVA_RESTORE_DATABASE_URL", **kwargs)
    (bdir / "reva.dump").write_bytes(b"not a dump")
    env_key = "REVA_TEST_RESTORE_TARGET_URL"
    os.environ[env_key] = db.settings.reva_migration_database_url.get_secret_value()  # populated test DB
    try:
        with pytest.raises(br.BackupError, match="not empty"):
            br.restore(bdir, url_env=env_key, **kwargs)
    finally:
        del os.environ[env_key]


def test_production_backup_requires_encryption(tmp_path: Path) -> None:
    with pytest.raises(br.BackupError, match="encrypted"):
        br.backup(
            tmp_path,
            url_env="UNSET_URL_VAR",
            objects=False,
            recipients=[],
            recipients_file=None,
            tools=br.PgTools(),
            environment="production",
        )
