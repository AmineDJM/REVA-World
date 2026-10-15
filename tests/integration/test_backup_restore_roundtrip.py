"""Small end-to-end backup -> restore round trip into a FRESH database AND a FRESH
object-store directory, through the real scripts (scripts/backup.sh, restore.sh).

A dedicated throwaway source database is created (migrated, seeded, one captured
fixture connector ingested, one test_fixture belief release published) so the
session ``db`` fixture is not modified. Verified on the restored copy:

* logical manifest identical (alembic revision, per-table row counts and content
  digests, artifact list, releases) including the security catalog: append-only
  triggers, RLS flags and policies, SECURITY DEFINER functions, normalised grants;
* every artifact is backed by an object in the fresh store whose bytes hash to
  meta.artifact.sha256 with the recorded size; the sealed release manifests
  re-verify; a corrupted object is detected;
* the runtime roles can log in and are still refused writes to immutable tables;
  ``reva verify`` passes against the restored database;
* restore refuses a non-empty target unless ``--force --confirm-database <name>``
  (and refuses --force in production);
* no credential from the environment appears in any backup file, including the
  decrypted dump rendered as SQL and the decrypted objects archive.

Skips (with the reason) when pg_dump/pg_restore >= the server's major version are
not available (locally, via REVA_PG_BINDIR, or inside REVA_PG_CONTAINER).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
br = importlib.import_module("backup_restore")


def _server_major(engine: Engine) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SHOW server_version_num")).scalar_one()) // 10000


def _pg_tools_env(server_major: int) -> dict[str, str]:
    """Where pg_dump/pg_restore come from; skip with the precise reason otherwise."""
    tools = ("pg_dump", "pg_restore")
    bindir = os.environ.get("REVA_PG_BINDIR")
    if bindir:
        missing = [t for t in tools if not (Path(bindir) / t).is_file()]
        if missing:
            pytest.skip(f"REVA_PG_BINDIR={bindir} does not contain {', '.join(missing)}")
        return {"REVA_PG_BINDIR": bindir}
    container = os.environ.get("REVA_PG_CONTAINER", "reva-db")
    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run(
            [docker, "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            return {"REVA_PG_CONTAINER": container}
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        pytest.skip(
            f"{', '.join(missing)} not found on PATH and no REVA_PG_BINDIR / running REVA_PG_CONTAINER: "
            "cannot exercise backup/restore"
        )
    for t in tools:
        out = subprocess.run([shutil.which(t) or t, "--version"], capture_output=True, text=True, check=True).stdout
        major = int(out.split(")")[1].strip().split(".")[0])
        if major < server_major:
            pytest.skip(
                f"local {t} {major} is older than the PostgreSQL {server_major} server "
                "(set REVA_PG_BINDIR or REVA_PG_CONTAINER to client tools >= the server version)"
            )
    return {}


@dataclass
class Source:
    name: str
    url: str
    settings: Any
    root: Path
    release_manifest_sha256: str


@pytest.fixture(scope="module")
def tools_env(db) -> dict[str, str]:
    """Checked before any database is created, so a skip costs nothing."""
    return _pg_tools_env(_server_major(db.owner))


@pytest.fixture(scope="module")
def source(db, tools_env: dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Iterator[Source]:
    """A dedicated source database + object root (the session db stays untouched)."""
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    name = f"reva_bkrt_{uuid.uuid4().hex[:10]}"
    admin = create_engine(db.settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp("bk_objects_src")
    url = db.settings.migration_url().set(database=name).render_as_string(hide_password=False)
    settings = db.settings.model_copy(
        update={"reva_migration_database_url": SecretStr(url), "reva_db_name": name, "reva_object_root": root}
    )
    engines: list[Engine] = []
    try:
        for attempt in range(3):  # concurrent sessions may race on cluster-global role DDL
            try:
                bootstrap(settings)
                break
            except Exception:
                if attempt == 2:
                    raise
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        worker = make_engine("worker", settings)
        engines.append(worker)
        seed(worker, store)
        conn = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(conn, FixtureHttp(conn.describe().name), store, worker, persist_cursor=False))
        rel = publish_release(worker, store, cutoff=None, origin=Origin.test_fixture, environment="test")
        yield Source(name, url, settings, root, rel.manifest_sha256)
    finally:
        for e in engines:
            e.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _run(
    script: str, args: list[str], env: dict[str, str], cwd: Path, *, ok: bool = True
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if ok:
        assert proc.returncode == 0, proc.stderr[-4000:]
    return proc


def _secrets(settings: Any) -> list[str]:
    vals = [
        settings.migration_url().password,
        settings.reva_api_db_password.get_secret_value(),
        settings.reva_worker_db_password.get_secret_value(),
        settings.reva_sim_db_password.get_secret_value(),
    ]
    if settings.reva_session_secret is not None:
        vals.append(settings.reva_session_secret.get_secret_value())
    return [v for v in vals if v]


def test_backup_restore_round_trip_fresh_db_and_object_store(
    db, tools_env: dict[str, str], source: Source, tmp_path: Path
) -> None:
    s = source.settings
    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "REVA_ENV": "test",
        "REVA_PYTHON": sys.executable,
        "REVA_OBJECT_BACKEND": "filesystem",
        "REVA_S3_PREFIX": s.reva_s3_prefix,
        "REVA_DB_HOST": s.reva_db_host,
        "REVA_DB_PORT": str(s.reva_db_port),
        "REVA_API_DB_PASSWORD": s.reva_api_db_password.get_secret_value(),
        "REVA_WORKER_DB_PASSWORD": s.reva_worker_db_password.get_secret_value(),
        "REVA_SIM_DB_PASSWORD": s.reva_sim_db_password.get_secret_value(),
        **tools_env,
    }
    encrypt = bool(shutil.which("age") and shutil.which("age-keygen"))
    args = ["--out", str(tmp_path / "backups"), "--include-objects"]
    identity = tmp_path / "age.key"
    if encrypt:
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
        pub = next(x.split(": ", 1)[1] for x in identity.read_text().splitlines() if x.startswith("# public key: "))
        args += ["--recipient", pub]

    # --- backup -----------------------------------------------------------------------
    backup_env = {
        **base_env,
        "REVA_MIGRATION_DATABASE_URL": source.url,
        "REVA_DB_NAME": source.name,
        "REVA_OBJECT_ROOT": str(source.root),
    }
    out = json.loads(_run("backup.sh", args, backup_env, tmp_path).stdout)
    bdir = Path(out["path"])
    record = json.loads((bdir / "backup.json").read_text())
    suffix = ".age" if encrypt else ""
    assert sorted(p.name for p in bdir.iterdir()) == sorted(
        ["SHA256SUMS", "backup.json", "objects.jsonl", f"reva.dump{suffix}", f"objects.tar{suffix}"]
    )
    assert subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=bdir, check=False).returncode == 0
    assert record["objects_archive"]["members"] == record["objects"]["count"] > 0
    assert record["objects"]["artifacts_verified"] == record["objects"]["artifacts"] > 0
    manifest_src = record["logical_manifest"]
    counts = manifest_src["catalog"]["counts"]
    assert counts["triggers"] > 0 and counts["policies"] > 0 and counts["rls_tables"] > 0 and counts["grants"] > 0

    # --- no secret anywhere in the backup set (decrypted dump rendered as SQL too) -------
    secrets = [x.encode() for x in _secrets(s)]
    plain: dict[str, bytes] = {p.name: p.read_bytes() for p in bdir.iterdir()}
    for name in (f"reva.dump{suffix}", f"objects.tar{suffix}"):
        data = plain[name]
        if encrypt:
            data = subprocess.run(
                ["age", "-d", "-i", str(identity), str(bdir / name)], capture_output=True, check=True
            ).stdout
        plain[f"{name} (plaintext)"] = data
    tools = br.PgTools(tools_env.get("REVA_PG_CONTAINER"), tools_env.get("REVA_PG_BINDIR"))
    plain["dump as SQL"] = subprocess.run(
        tools.argv("pg_restore", ["--file=-"], []),
        input=plain[f"reva.dump{suffix} (plaintext)"],
        capture_output=True,
        check=True,
    ).stdout
    assert b"CREATE TABLE evidence.observation" in plain["dump as SQL"]
    assert b"CREATE ROLE" not in plain["dump as SQL"]  # roles are cluster-global, not in the dump
    leaks = {n: i for n, b in plain.items() for i, sec in enumerate(secrets) if sec in b}
    assert leaks == {}, f"secret values found in backup files: {sorted(leaks)}"
    # ... and the backup-time scanner actually catches one.
    assert br.files_containing_secrets([bdir / "backup.json"], [source.name]) == ["backup.json"]

    # --- restore into a fresh database and a fresh, empty object root -----------------
    target = f"{source.name}_r"
    admin = create_engine(db.settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{target}"'))
    target_url = db.settings.migration_url().set(database=target)
    fresh_root = tmp_path / "objects_restored"
    fresh_root.mkdir()
    restore_env = {
        **base_env,
        "REVA_RESTORE_DATABASE_URL": target_url.render_as_string(hide_password=False),
        "REVA_OBJECT_ROOT": str(fresh_root),
    }
    if encrypt:
        restore_env["REVA_BACKUP_AGE_IDENTITY"] = str(identity)
    engines: list[Engine] = []
    try:
        proc = _run("restore.sh", [str(bdir), "--restore-objects", "--verify-objects"], restore_env, tmp_path)
        res = json.loads(proc.stdout)
        assert not any(sec in (proc.stdout + proc.stderr).encode() for sec in secrets)
        assert res["matches_backup"] and res["differences"] == []
        assert res["objects_restored"]["objects"] == record["objects_archive"]["members"]
        obj = res["objects"]
        assert obj["artifacts_verified"] == obj["artifacts"] == record["objects"]["artifacts"]
        assert obj["missing"] == obj["mismatched"] == obj["corrupt_objects"] == obj["unbacked_artifacts"] == []
        assert obj["relocated_locations"] == obj["locations"]  # historical URIs point at the source root
        assert res["releases"]["releases"] == res["releases"]["verified"] >= 1
        assert source.release_manifest_sha256 in {r["manifest_sha256"] for r in manifest_src["releases"]}

        # Independent re-computation: alembic revision, row counts, security catalog.
        conn = br.connect(target_url, autocommit=True)
        try:
            restored = br.logical_manifest(conn)
        finally:
            conn.close()
        assert restored["schema_version"] == manifest_src["schema_version"]
        assert {k: v["rows"] for k, v in restored["tables"].items()} == {
            k: v["rows"] for k, v in manifest_src["tables"].items()
        }
        assert br.diff_manifests(manifest_src, restored) == []

        # Runtime roles (cluster-global; passwords re-applied by bootstrap) work and stay confined.
        settings_r = s.model_copy(update={"reva_db_name": target, "reva_migration_database_url": None})
        api_r, worker_r, owner_r = (
            make_engine("api", settings_r),
            make_engine("worker", settings_r),
            create_engine(target_url),
        )
        engines += [api_r, worker_r, owner_r]
        with api_r.connect() as c:
            assert c.execute(text("SELECT count(*) FROM belief.release")).scalar_one() >= 1
        with pytest.raises(DBAPIError), worker_r.begin() as c:
            c.execute(text("UPDATE evidence.observation SET recorded_at = recorded_at"))
        with pytest.raises(DBAPIError, match="Immutable scientific record"), owner_r.begin() as c:
            c.execute(text("UPDATE belief.release SET method = method"))

        # `reva verify` (the CLI) passes against the restored database and fresh store.
        verify_env = {k: v for k, v in restore_env.items() if k != "REVA_RESTORE_DATABASE_URL"}
        verify_env["REVA_DB_NAME"] = target
        v = subprocess.run(
            [sys.executable, "-m", "reva.cli.main", "verify"],
            env=verify_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert v.returncode == 0, v.stderr[-2000:]
        verdict = json.loads(v.stdout)
        assert verdict["ok"] and verdict["db_worker"]["schema_version"] == manifest_src["schema_version"]

        # A corrupted object in the restored store is detected.
        victim = next(p for p in sorted(fresh_root.rglob("*")) if p.is_file() and ".staging" not in p.parts)
        victim.chmod(0o644)
        victim.write_bytes(victim.read_bytes() + b"x")
        conn = br.connect(target_url, autocommit=True)
        try:
            bad = br.check_locations(
                conn,
                br.object_inventory(s.model_copy(update={"reva_object_root": fresh_root})),
                s.model_copy(update={"reva_object_root": fresh_root}),
            )
        finally:
            conn.close()
        assert bad["corrupt_objects"] and not br._locations_ok(bad)

        # A non-empty target is refused unless explicitly forced with the database name.
        before = restored["manifest_sha256"]
        for extra, msg in (
            ([], "not empty"),
            (["--force"], "--confirm-database"),
            (["--force", "--confirm-database", "other"], "--confirm-database"),
        ):
            p = _run("restore.sh", [str(bdir), *extra], restore_env, tmp_path, ok=False)
            assert p.returncode != 0 and msg in p.stderr
        conn = br.connect(target_url, autocommit=True)
        try:
            assert br.logical_manifest(conn)["manifest_sha256"] == before  # untouched
        finally:
            conn.close()
        os.environ["REVA_TEST_BKRT_URL"] = target_url.render_as_string(hide_password=False)
        try:
            with pytest.raises(br.BackupError, match="refused in production"):
                br.restore(
                    bdir,
                    url_env="REVA_TEST_BKRT_URL",
                    tools=tools,
                    identity=str(identity) if encrypt else None,
                    run_bootstrap=False,
                    verify_objects=False,
                    force=True,
                    confirm_database=target,
                    environment="production",
                )
        finally:
            del os.environ["REVA_TEST_BKRT_URL"]
        forced = json.loads(
            _run(
                "restore.sh",
                [str(bdir), "--force", "--confirm-database", target, "--skip-bootstrap"],
                restore_env,
                tmp_path,
            ).stdout
        )
        assert forced["matches_backup"] and "evidence" in forced["forced_overwrite"]["dropped_schemas"]
    finally:
        for e in engines:
            e.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)'))
        admin.dispose()
