#!/usr/bin/env python3
"""Logical backup / restore of the REVA database plus object-store inventory.

Subcommands (wrapped by scripts/backup.sh and scripts/restore.sh):

  backup    consistent pg_dump (custom format) taken inside an exported
            REPEATABLE READ snapshot; the logical manifest is computed from the
            *same* snapshot; object-store inventory (sha256 per object) and a
            cross-check of every meta.artifact_location; optional/required age
            encryption; writes backup.json.
  restore   verifies the dump checksum, decrypts if needed, refuses a non-empty
            target, ensures the runtime roles exist, pg_restore --no-owner (ACLs
            kept), re-applies database-level grants, re-runs bootstrap (role
            passwords, ownership verification) and compares the restored logical
            manifest with the one recorded at backup time.
  manifest  print the logical manifest of a database.
  inventory print the object-store inventory.

Credentials come only from environment variables (never argv): the database URL
is read from the variable named by --url-env (default REVA_MIGRATION_DATABASE_URL)
and passed to pg_dump/pg_restore as PG* environment variables, so the password
is not visible in process listings. Set REVA_PG_CONTAINER=<name> to run the
PostgreSQL client tools inside a container (``docker exec``) when the host's
client is older than the server (pg_dump refuses to dump a newer server).

Ownership semantics: the dump records owners and ACLs; restore uses --no-owner,
so the restoring migration owner owns every object (exactly what bootstrap's
verify_ownership requires) while grants to reva_api/reva_worker/reva_sim are
restored. Database-level grants are not part of a non --create dump and are
re-applied explicitly (mirrors migration 0007).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_env import ConfigError, ensure_importable, merged_environ, redact

ensure_importable()

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

SCHEMAS = ("meta", "ontology", "world", "evidence", "belief", "model", "sim", "ops")
RUNTIME_ROLES = ("reva_api", "reva_worker", "reva_sim")
SESSION_GUCS = (
    "SET LOCAL TimeZone = 'UTC'",
    "SET LOCAL DateStyle = 'ISO, YMD'",
    "SET LOCAL IntervalStyle = 'postgres'",
    "SET LOCAL bytea_output = 'hex'",
    "SET LOCAL extra_float_digits = 1",
)


class BackupError(Exception):
    pass


# --- connection helpers ----------------------------------------------------------


def url_from_env(var: str, environ: Mapping[str, str] | None = None) -> Any:
    env = os.environ if environ is None else environ
    raw = env.get(var)
    if not raw:
        raise BackupError(f"{var} is not set")
    return make_url(raw)


def pg_env(url: Any) -> dict[str, str]:
    """libpq environment for the client tools (password never on argv)."""
    out = {
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGDATABASE": url.database or "",
    }
    if url.password:
        out["PGPASSWORD"] = str(url.password)
    sslmode = url.query.get("sslmode")
    if sslmode:
        out["PGSSLMODE"] = str(sslmode)
    return out


def connect(url: Any, **kw: Any) -> psycopg.Connection[Any]:
    e = pg_env(url)
    return psycopg.connect(
        host=e["PGHOST"],
        port=int(e["PGPORT"]),
        user=e["PGUSER"],
        password=e.get("PGPASSWORD"),
        dbname=e["PGDATABASE"],
        sslmode=e.get("PGSSLMODE", "prefer"),
        connect_timeout=10,
        application_name="reva-backup",
        **kw,
    )


class PgTools:
    """Runs pg_dump/pg_restore locally or inside a container (REVA_PG_CONTAINER)."""

    def __init__(self, container: str | None = None, bindir: str | None = None):
        self.container = container
        self.bindir = bindir

    @classmethod
    def from_env(cls) -> PgTools:
        return cls(os.environ.get("REVA_PG_CONTAINER") or None, os.environ.get("REVA_PG_BINDIR") or None)

    def argv(self, tool: str, args: list[str], env_keys: list[str]) -> list[str]:
        if self.container:
            docker = shutil.which("docker") or "docker"
            flags = [x for k in env_keys for x in ("-e", k)]  # values come from our env, not argv
            return [docker, "exec", "-i", *flags, self.container, tool, *args]
        exe = str(Path(self.bindir) / tool) if self.bindir else (shutil.which(tool) or tool)
        return [exe, *args]

    def run(self, tool: str, args: list[str], url: Any, *, stdin: Any = None, stdout: Any = None) -> str:
        env = {**os.environ, **pg_env(url)}
        argv = self.argv(tool, args, sorted(pg_env(url)))
        proc = subprocess.run(
            argv,
            env=env,
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            raise BackupError(f"{tool} failed ({proc.returncode}): {redact(proc.stderr.decode(errors='replace'))}")
        return proc.stdout.decode(errors="replace") if stdout is None and proc.stdout else ""

    def spawn(self, tool: str, args: list[str], url: Any, *, stdin: Any = None, stdout: Any = None) -> _Proc:
        env = {**os.environ, **pg_env(url)}
        return _Proc(tool, self.argv(tool, args, sorted(pg_env(url))), env, stdin=stdin, stdout=stdout)

    def major_version(self, tool: str, url: Any) -> int:
        out = self.run(tool, ["--version"], url)
        # "pg_dump (PostgreSQL) 17.5 (Debian ...)"
        return int(out.split(")")[1].strip().split(".")[0])


# --- streaming (no plaintext dump ever touches disk when encrypting) -----------------

CHUNK = 1 << 20


class _Proc:
    """A child process whose stderr goes to an anonymous temp file (no pipe deadlock)."""

    def __init__(self, name: str, argv: list[str], env: Mapping[str, str], *, stdin: Any = None, stdout: Any = None):
        self.name = name
        self.err = tempfile.TemporaryFile()
        self.p = subprocess.Popen(argv, env=dict(env), stdin=stdin, stdout=stdout, stderr=self.err)

    def finish(self) -> tuple[int, str]:
        rc = self.p.wait()
        self.err.seek(0)
        msg = self.err.read().decode(errors="replace")
        self.err.close()
        return rc, redact(msg.strip())[-2000:]

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()


def _copy(src: Any, dst: Any) -> tuple[str, int]:
    """Copy src to dst (None: discard) in bounded chunks; sha256/bytes of what passed."""
    h = hashlib.sha256()
    n = 0
    while chunk := src.read(CHUNK):
        h.update(chunk)
        n += len(chunk)
        if dst is not None:
            dst.write(chunk)
    return h.hexdigest(), n


def _age() -> str:
    exe = shutil.which("age")
    if not exe:
        raise BackupError("age is required for encryption/decryption but was not found on PATH")
    return exe


def _require_ok(*procs: tuple[_Proc, tuple[int, str]]) -> None:
    bad = [f"{p.name} exited {rc}: {msg}" for p, (rc, msg) in procs if rc != 0]
    if bad:
        raise BackupError("; ".join(bad))


def stream_dump(tools: PgTools, args: list[str], url: Any, out: Path, age_args: list[str] | None) -> tuple[str, int]:
    """pg_dump -> (age ->) out. Returns sha256/bytes of the *plaintext* stream.

    With encryption the plaintext exists only in pipe buffers: pg_dump's stdout is
    copied chunk by chunk into age's stdin and age writes the ciphertext file.
    """
    dump = tools.spawn("pg_dump", args, url, stdout=subprocess.PIPE)
    try:
        if age_args is None:
            with _private_umask(), out.open("wb") as f:
                digest = _copy(dump.p.stdout, f)
            _require_ok((dump, dump.finish()))
            return digest
        with _private_umask():
            enc = _Proc("age", [_age(), "--encrypt", *age_args, "-o", str(out)], os.environ, stdin=subprocess.PIPE)
        try:
            digest = _copy(dump.p.stdout, enc.p.stdin)
        except BrokenPipeError:
            digest = ("", 0)
        finally:
            enc.p.stdin.close()  # type: ignore[union-attr]
        _require_ok((dump, dump.finish()), (enc, enc.finish()))
        return digest
    except BaseException:
        dump.kill()
        out.unlink(missing_ok=True)
        raise


def _plaintext_source(archive: Path, identity: str | None) -> tuple[Any, _Proc | None]:
    if identity is None:
        return archive.open("rb"), None
    dec = _Proc("age", [_age(), "--decrypt", "-i", identity, str(archive)], os.environ, stdout=subprocess.PIPE)
    return dec.p.stdout, dec


def verify_plaintext(archive: Path, identity: str | None) -> tuple[str, int]:
    """Pass 1: authenticate/decrypt to nowhere and hash the plaintext."""
    src, dec = _plaintext_source(archive, identity)
    try:
        digest = _copy(src, None)
    finally:
        src.close()
    if dec is not None:
        _require_ok((dec, dec.finish()))
    return digest


def stream_restore(tools: PgTools, args: list[str], url: Any, archive: Path, identity: str | None) -> tuple[str, int]:
    """Pass 2: (age -d ->) pg_restore via stdin; sha256/bytes of the plaintext fed to it."""
    src, dec = _plaintext_source(archive, identity)
    restore_proc = tools.spawn("pg_restore", args, url, stdin=subprocess.PIPE)
    try:
        try:
            digest = _copy(src, restore_proc.p.stdin)
        except BrokenPipeError:
            digest = ("", 0)
        finally:
            src.close()
        if dec is not None:
            rc, msg = dec.finish()
            if rc != 0:
                raise BackupError(f"age decryption failed during restore: {msg}")
        restore_proc.p.stdin.close()  # type: ignore[union-attr]
        _require_ok((restore_proc, restore_proc.finish()))
        return digest
    except BaseException:
        restore_proc.kill()
        if dec is not None:
            dec.kill()
        raise


def write_checksums(directory: Path, names: list[str]) -> Path:
    """SHA256SUMS (``sha256sum -c`` format) over every file of the backup set."""
    path = directory / "SHA256SUMS"
    with _private_umask():
        path.write_text("".join(f"{_sha256_file(directory / n)}  {n}\n" for n in sorted(names)))
    return path


def verify_checksums(directory: Path) -> list[str]:
    path = directory / "SHA256SUMS"
    if not path.is_file():
        raise BackupError("SHA256SUMS missing: refusing an unverifiable backup")
    names = []
    for line in path.read_text().splitlines():
        digest, _, name = line.partition("  ")
        if not name or "/" in name or name.startswith("."):
            raise BackupError(f"malformed SHA256SUMS line: {line[:80]!r}")
        target = directory / name
        if not target.is_file() or _sha256_file(target) != digest:
            raise BackupError(f"checksum verification failed for {name}: refusing to restore")
        names.append(name)
    if "backup.json" not in names:
        raise BackupError("SHA256SUMS does not cover backup.json")
    return names


# --- logical manifest --------------------------------------------------------------


def _tables(cur: psycopg.Cursor[Any]) -> list[tuple[str, str]]:
    cur.execute(
        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s) "
        "OR (schemaname = 'public' AND tablename = 'alembic_version') ORDER BY 1, 2",
        (list(SCHEMAS),),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def logical_manifest(conn: psycopg.Connection[Any], *, in_transaction: bool = False) -> dict[str, Any]:
    """Deterministic content digest of every REVA table plus key scientific lists.

    Each table digest is sha256 over the sorted per-row sha256 of the row's text
    representation under fixed session settings (UTC, ISO dates, hex bytea), so
    it is independent of physical order, OIDs and storage.
    """
    if not in_transaction:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        with conn.cursor() as cur:
            for stmt in SESSION_GUCS:
                cur.execute(stmt)
            tables: dict[str, dict[str, Any]] = {}
            for schema, table in _tables(cur):
                q = sql.SQL(
                    "SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(h, '' ORDER BY h), ''),"
                    " 'UTF8')), 'hex') FROM (SELECT encode(sha256(convert_to(t::text, 'UTF8')), 'hex') AS h"
                    " FROM {}.{} AS t) AS s"
                ).format(sql.Identifier(schema), sql.Identifier(table))
                cur.execute(q)
                rows, digest = cur.fetchone()  # type: ignore[misc]
                tables[f"{schema}.{table}"] = {"rows": int(rows), "sha256": digest}
            cur.execute("SELECT version_num FROM public.alembic_version")
            version = cur.fetchone()
            cur.execute("SELECT sha256 FROM meta.artifact ORDER BY sha256")
            artifact_shas = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT r.id::text, r.manifest_sha256, r.origin, r.knowledge_cutoff::text, p.sequence,"
                " p.published_at::text FROM belief.release r LEFT JOIN ops.publication p ON p.release_id = r.id"
                " ORDER BY p.sequence NULLS LAST, r.id"
            )
            releases = [
                dict(
                    zip(
                        ("release_id", "manifest_sha256", "origin", "knowledge_cutoff", "sequence", "published_at"),
                        r,
                        strict=True,
                    )
                )
                for r in cur.fetchall()
            ]
    finally:
        if not in_transaction:
            conn.execute("COMMIT")
    body = {
        "schema": "reva-logical-manifest/1",
        "schema_version": version[0] if version else None,
        "tables": tables,
        "artifact_count": len(artifact_shas),
        "artifact_sha256_list_digest": hashlib.sha256("\n".join(artifact_shas).encode()).hexdigest(),
        "releases": releases,
    }
    body["manifest_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


def diff_manifests(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    out = []
    for key in sorted(set(a["tables"]) | set(b["tables"])):
        if a["tables"].get(key) != b["tables"].get(key):
            out.append(f"table {key}: {a['tables'].get(key)} != {b['tables'].get(key)}")
    for key in ("schema_version", "artifact_count", "artifact_sha256_list_digest", "releases"):
        if a.get(key) != b.get(key):
            out.append(f"{key} differs")
    return out


# --- object inventory --------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def object_inventory(settings: Any, *, verify_content: bool = True) -> list[dict[str, Any]]:
    """sha256 inventory of the content-addressed store (key -> sha256, bytes)."""
    prefix = settings.reva_s3_prefix
    items: list[dict[str, Any]] = []
    if settings.reva_object_backend == "filesystem":
        root = Path(settings.reva_object_root).resolve()
        base = root / prefix
        if base.exists():
            for p in sorted(base.rglob("*")):
                if not p.is_file() or ".staging" in p.parts:
                    continue
                key = str(p.relative_to(root))
                actual = _sha256_file(p) if verify_content else None
                items.append(
                    {
                        "key": key,
                        "address": p.name,
                        "sha256": actual,
                        "bytes": p.stat().st_size,
                        "content_ok": None if actual is None else actual == p.name,
                    }
                )
        return items
    from reva.adapters.objects.s3 import S3ArtifactStore

    store = S3ArtifactStore.from_settings(settings)
    paginator = store.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=store.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            address = key.rsplit("/", 1)[-1]
            actual = None
            if verify_content:
                h = hashlib.sha256()
                body = store.client.get_object(Bucket=store.bucket, Key=key)["Body"]
                for chunk in iter(lambda b=body: b.read(1 << 20), b""):
                    h.update(chunk)
                actual = h.hexdigest()
            items.append(
                {
                    "key": key,
                    "address": address,
                    "sha256": actual,
                    "bytes": obj["Size"],
                    "content_ok": None if actual is None else actual == address,
                }
            )
    return items


def _location_key(uri: str, settings: Any) -> str:
    parts = urlsplit(uri)
    if parts.scheme == "file":
        return str(Path(parts.path).resolve().relative_to(Path(settings.reva_object_root).resolve()))
    return parts.path.lstrip("/")


def check_locations(conn: psycopg.Connection[Any], inventory: list[dict[str, Any]], settings: Any) -> dict[str, Any]:
    keys = {i["key"]: i for i in inventory}
    with conn.cursor() as cur:
        cur.execute("SELECT artifact_sha256, storage_uri, verified_byte_size FROM meta.artifact_location")
        rows = cur.fetchall()
    missing, mismatched = [], []
    for sha, uri, size in rows:
        try:
            key = _location_key(uri, settings)
        except ValueError:
            missing.append(uri)
            continue
        item = keys.get(key)
        if item is None:
            missing.append(uri)
        elif item["address"] != sha or item["bytes"] != size or item["content_ok"] is False:
            mismatched.append(uri)
    bad_content = [i["key"] for i in inventory if i["content_ok"] is False]
    return {"locations": len(rows), "missing": missing, "mismatched": mismatched, "corrupt_objects": bad_content}


# --- backup --------------------------------------------------------------------------


def _settings() -> Any:
    from reva.settings import load_settings

    return load_settings()


@contextmanager
def _private_umask() -> Iterator[None]:
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def backup(
    out_dir: Path,
    *,
    url_env: str,
    objects: bool,
    recipients: list[str],
    recipients_file: str | None,
    tools: PgTools,
    environment: str,
) -> dict[str, Any]:
    encrypt = bool(recipients or recipients_file)
    if environment == "production" and not encrypt:
        raise BackupError("production backups must be encrypted: set REVA_BACKUP_AGE_RECIPIENT(S_FILE)")
    if encrypt and not shutil.which("age"):
        raise BackupError("age is required for encryption but was not found on PATH")
    url = url_from_env(url_env)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"reva-backup-{stamp}"
    with _private_umask():
        target.mkdir(parents=True, exist_ok=False)
    timings: dict[str, float] = {}
    conn = connect(url, autocommit=True)
    try:
        server_major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000  # type: ignore[index]
        dump_major = tools.major_version("pg_dump", url)
        if dump_major < server_major:
            raise BackupError(
                f"pg_dump {dump_major} cannot dump server {server_major}; "
                "set REVA_PG_CONTAINER or REVA_PG_BINDIR to version >= server"
            )
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]  # type: ignore[index]
        t0 = time.perf_counter()
        manifest = logical_manifest(conn, in_transaction=True)
        timings["logical_manifest_seconds"] = round(time.perf_counter() - t0, 3)
        age_args: list[str] | None = None
        if encrypt:
            age_args = [x for r in recipients for x in ("-r", r)]
            if recipients_file:
                age_args += ["-R", recipients_file]
        dump_file = target / ("reva.dump.age" if encrypt else "reva.dump")
        t0 = time.perf_counter()
        plain_sha, plain_bytes = stream_dump(
            tools,
            [
                "--format=custom",
                "--compress=6",
                f"--snapshot={snapshot}",
                "--no-password",
                # PostGIS-owned reference data is recreated by CREATE EXTENSION; REVA adds no
                # custom SRIDs (EPSG:4326 only). Restoring it would need the extension owner.
                "--exclude-table-data=public.spatial_ref_sys",
            ],
            url,
            dump_file,
            age_args,
        )
        timings["pg_dump_seconds"] = round(time.perf_counter() - t0, 3)
        conn.execute("COMMIT")
        inventory: list[dict[str, Any]] = []
        locations: dict[str, Any] = {}
        if objects:
            settings = _settings()
            t0 = time.perf_counter()
            inventory = object_inventory(settings)
            locations = check_locations(conn, inventory, settings)
            timings["object_inventory_seconds"] = round(time.perf_counter() - t0, 3)
    finally:
        conn.close()
    inv_path = target / "objects.jsonl"
    with _private_umask():
        inv_path.write_text("".join(json.dumps(i, sort_keys=True) + "\n" for i in inventory))
    record = {
        "schema": "reva-backup/1",
        "created_at": datetime.now(UTC).isoformat(),
        "database": url.database,
        "server_major": server_major,
        "pg_dump_major": dump_major,
        "snapshot": snapshot,
        "dump": {
            "file": dump_file.name,
            "format": "custom",
            "sha256": _sha256_file(dump_file),
            "bytes": dump_file.stat().st_size,
            "plaintext_bytes": plain_bytes,
            "plaintext_sha256": plain_sha,
            "encrypted": encrypt,
            "encryption": "age (X25519 recipients; ChaCha20-Poly1305 authenticated chunks)" if encrypt else None,
        },
        "logical_manifest": manifest,
        "objects": {
            "inventory_file": inv_path.name,
            "inventory_sha256": _sha256_file(inv_path),
            "count": len(inventory),
            **locations,
        }
        if objects
        else None,
        "timings": timings,
    }
    with _private_umask():
        (target / "backup.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    write_checksums(target, [dump_file.name, inv_path.name, "backup.json"])
    if objects and (locations.get("missing") or locations.get("mismatched") or locations.get("corrupt_objects")):
        raise BackupError(f"object store inconsistent with database (see {target / 'backup.json'})")
    record["path"] = str(target)
    return record


# --- restore -------------------------------------------------------------------------

ROLE_SQL = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['reva_api','reva_worker','reva_sim'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT', r);
    END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    CREATE ROLE reva_restricted_deletion NOLOGIN;
  END IF;
END $$;
"""


def _target_is_empty(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname = ANY(%s) "
        "OR (schemaname = 'public' AND tablename = 'alembic_version')",
        (list(SCHEMAS),),
    ).fetchone()
    return bool(row and row[0] == 0)


def restore(
    backup_dir: Path, *, url_env: str, tools: PgTools, identity: str | None, run_bootstrap: bool, verify_objects: bool
) -> dict[str, Any]:
    verify_checksums(backup_dir)  # refuses unverifiable or tampered sets before anything else
    record = json.loads((backup_dir / "backup.json").read_text())
    dump_file = backup_dir / record["dump"]["file"]
    if dump_file.name not in {"reva.dump", "reva.dump.age"} or _sha256_file(dump_file) != record["dump"]["sha256"]:
        raise BackupError("dump checksum mismatch: refusing to restore")
    encrypted = bool(record["dump"]["encrypted"])
    if encrypted and not identity:
        raise BackupError("encrypted dump: pass --identity (age identity file)")
    url = url_from_env(url_env)
    timings: dict[str, float] = {}
    # Pass 1: authenticate (age) and hash the plaintext without writing it anywhere.
    t0 = time.perf_counter()
    plain = verify_plaintext(dump_file, identity if encrypted else None)
    timings["verify_seconds"] = round(time.perf_counter() - t0, 3)
    if plain[0] != record["dump"].get("plaintext_sha256"):
        raise BackupError("decrypted dump does not match its recorded sha256: refusing to restore")
    conn = connect(url, autocommit=True)
    try:
        server_major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000  # type: ignore[index]
        row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        superuser = bool(row and row[0])
        if not _target_is_empty(conn):
            raise BackupError("target database is not empty; restore only into a new, empty database")
        conn.execute(ROLE_SQL)
        restore_major = tools.major_version("pg_restore", url)
        if restore_major < record["pg_dump_major"]:
            raise BackupError("pg_restore is older than the pg_dump that produced the archive")
        # Pass 2: stream straight into pg_restore (one transaction; no plaintext file).
        t0 = time.perf_counter()
        fed = stream_restore(
            tools,
            [
                "--no-owner",
                "--no-comments",
                "--exit-on-error",
                "--single-transaction",
                "--no-password",
                f"--dbname={url.database}",
            ],
            url,
            dump_file,
            identity if encrypted else None,
        )
        timings["pg_restore_seconds"] = round(time.perf_counter() - t0, 3)
        if fed[0] != plain[0]:
            raise BackupError("archive changed between verification and restore; drop the target and retry")
        dbname = sql.Identifier(url.database)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(dbname))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO reva_api, reva_worker, reva_sim").format(dbname))
    finally:
        conn.close()
    bootstrap_report = None
    if run_bootstrap:
        from reva.cli.bootstrap import bootstrap
        from reva.settings import load_settings

        os.environ["REVA_MIGRATION_DATABASE_URL"] = url.render_as_string(hide_password=False)
        os.environ["REVA_DB_NAME"] = str(url.database)
        t0 = time.perf_counter()
        bootstrap_report = bootstrap(load_settings())
        timings["bootstrap_seconds"] = round(time.perf_counter() - t0, 3)
    conn = connect(url, autocommit=True)
    try:
        t0 = time.perf_counter()
        restored = logical_manifest(conn)
        timings["logical_manifest_seconds"] = round(time.perf_counter() - t0, 3)
        objects = None
        if verify_objects:
            settings = _settings()
            objects = check_locations(conn, object_inventory(settings), settings)
    finally:
        conn.close()
    differences = diff_manifests(record["logical_manifest"], restored)
    result = {
        "schema": "reva-restore/1",
        "backup": str(backup_dir),
        "target_database": url.database,
        "server_major": server_major,
        "restored_as_superuser": superuser,
        "logical_manifest_sha256": restored["manifest_sha256"],
        "matches_backup": not differences,
        "differences": differences,
        "bootstrap": bootstrap_report,
        "objects": objects,
        "timings": timings,
    }
    if differences:
        raise BackupError("restored logical manifest differs from backup: " + "; ".join(differences[:10]))
    if objects and (objects["missing"] or objects["mismatched"] or objects["corrupt_objects"]):
        raise BackupError("object store does not contain every restored artifact location")
    return result


# --- CLI -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    try:
        merged, _ = merged_environ()  # local .env is a development convenience; refused in production
    except ConfigError as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 1
    for key, value in merged.items():
        os.environ.setdefault(key, value)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--out", default=os.environ.get("REVA_BACKUP_DIR", "./var/backups"))
    b.add_argument("--url-env", default="REVA_MIGRATION_DATABASE_URL")
    b.add_argument("--no-objects", action="store_true")
    b.add_argument(
        "--recipient", action="append", default=[x for x in [os.environ.get("REVA_BACKUP_AGE_RECIPIENT")] if x]
    )
    b.add_argument("--recipients-file", default=os.environ.get("REVA_BACKUP_AGE_RECIPIENTS_FILE"))
    r = sub.add_parser("restore")
    r.add_argument("backup_dir")
    r.add_argument("--url-env", default="REVA_RESTORE_DATABASE_URL")
    r.add_argument("--identity", default=os.environ.get("REVA_BACKUP_AGE_IDENTITY"))
    r.add_argument("--skip-bootstrap", action="store_true")
    r.add_argument("--verify-objects", action="store_true")
    m = sub.add_parser("manifest")
    m.add_argument("--url-env", default="REVA_MIGRATION_DATABASE_URL")
    sub.add_parser("inventory")
    args = ap.parse_args(argv)
    tools = PgTools.from_env()
    try:
        if args.cmd == "backup":
            out = backup(
                Path(args.out),
                url_env=args.url_env,
                objects=not args.no_objects,
                recipients=args.recipient,
                recipients_file=args.recipients_file,
                tools=tools,
                environment=os.environ.get("REVA_ENV", "local"),
            )
            out = {k: out[k] for k in ("path", "dump", "timings", "objects") if k in out} | {
                "logical_manifest_sha256": out["logical_manifest"]["manifest_sha256"]
            }
        elif args.cmd == "restore":
            out = restore(
                Path(args.backup_dir),
                url_env=args.url_env,
                tools=tools,
                identity=args.identity,
                run_bootstrap=not args.skip_bootstrap,
                verify_objects=args.verify_objects,
            )
        elif args.cmd == "manifest":
            conn = connect(url_from_env(args.url_env), autocommit=True)
            try:
                out = logical_manifest(conn)
            finally:
                conn.close()
        else:
            inv = object_inventory(_settings())
            out = {"count": len(inv), "corrupt": [i["key"] for i in inv if i["content_ok"] is False], "items": inv}
    except (BackupError, subprocess.CalledProcessError, psycopg.Error) as e:
        print(json.dumps({"status": "error", "error": redact(str(e))}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
