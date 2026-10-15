#!/usr/bin/env python3
"""Logical backup / restore of the REVA database plus object-store inventory.

Subcommands (wrapped by scripts/backup.sh and scripts/restore.sh):

  backup    consistent pg_dump (custom format) taken inside an exported
            REPEATABLE READ snapshot; the logical manifest is computed from the
            *same* snapshot; object-store inventory (sha256 per object) and a
            cross-check of every meta.artifact_location; optional/required age
            encryption; with --include-objects an objects tar (encrypted alike);
            plaintext metadata files are scanned for environment secrets;
            writes backup.json. The manifest includes the security catalog
            (triggers, RLS policies, SECURITY DEFINER functions, grants).
  restore   verifies the dump checksum, decrypts if needed, refuses a non-empty
            target, ensures the runtime roles exist, pg_restore --no-owner (ACLs
            kept), re-applies database-level grants, re-runs bootstrap (role
            passwords, ownership verification) and compares the restored logical
            manifest with the one recorded at backup time; optionally restores
            the objects archive into the configured store (--restore-objects),
            verifies every artifact and the sealed release manifests
            (--verify-objects); a non-empty target needs --force
            --confirm-database <name> (refused in production).
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
re-applied explicitly from the grants recorded in the backup's catalog manifest
(so roles added by later migrations, e.g. reva_gate, are covered).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
from sqlalchemy.exc import ArgumentError  # noqa: E402

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


class _HashingWriter:
    """Write-only file object that hashes what passes through (tarfile stream sink)."""

    def __init__(self, dst: Any):
        self.dst, self.h, self.n = dst, hashlib.sha256(), 0

    def write(self, b: bytes) -> int:
        self.h.update(b)
        self.n += len(b)
        self.dst.write(b)
        return len(b)

    def flush(self) -> None:
        pass


class _HashingReader:
    """Read-only file object over a byte iterator or a file, hashing what is read."""

    def __init__(self, src: Any):
        self._it = None if hasattr(src, "read") else iter(src)
        self._src = src if hasattr(src, "read") else None
        self._buf = b""
        self.h, self.n = hashlib.sha256(), 0

    def _more(self, size: int) -> bytes:
        if self._src is not None:
            return self._src.read(size)  # type: ignore[no-any-return]
        return next(self._it, b"")  # type: ignore[arg-type]

    def read(self, size: int = -1) -> bytes:
        want = CHUNK if size is None or size < 0 else size
        while len(self._buf) < want:
            chunk = self._more(CHUNK)
            if not chunk:
                break
            self._buf += chunk
        out, self._buf = self._buf[:want], self._buf[want:]
        self.h.update(out)
        self.n += len(out)
        return out

    def drain(self) -> None:
        while self.read(CHUNK):
            pass


def write_objects_archive(
    store: Any, inventory: list[dict[str, Any]], prefix: str, out: Path, age_args: list[str] | None
) -> tuple[str, int, int]:
    """Stream every inventoried object into a tar (age-encrypted when age_args).

    Members are named by store key and their bytes are re-hashed while streaming:
    an object whose content does not match its address fails the backup. Returns
    sha256/bytes of the plaintext tar and the member count.
    """
    import tarfile

    enc = None
    with _private_umask():
        if age_args is None:
            sink: Any = out.open("wb")
        else:
            enc = _Proc("age", [_age(), "--encrypt", *age_args, "-o", str(out)], os.environ, stdin=subprocess.PIPE)
            sink = enc.p.stdin
    w = _HashingWriter(sink)
    count = 0
    try:
        with tarfile.open(fileobj=w, mode="w|", format=tarfile.PAX_FORMAT) as tar:  # type: ignore[call-overload]
            for item in sorted(inventory, key=lambda i: i["key"]):
                namespace, sha = _parse_key(item["key"], prefix)
                src = _HashingReader(store.open(namespace, sha))
                ti = tarfile.TarInfo(item["key"])
                ti.size, ti.mode, ti.mtime, ti.uname, ti.gname = item["bytes"], 0o444, 0, "", ""
                tar.addfile(ti, src)
                src.drain()
                if src.h.hexdigest() != sha or src.n != item["bytes"]:
                    raise BackupError(f"object {item['key']} does not match its address/size")
                count += 1
    except BaseException:
        if enc is not None:
            enc.kill()
        sink.close()
        out.unlink(missing_ok=True)
        raise
    sink.close()
    if enc is not None:
        _require_ok((enc, enc.finish()))
    return w.h.hexdigest(), w.n, count


def restore_objects_archive(
    archive: Path, identity: str | None, store: Any, prefix: str, media_types: Mapping[str, str]
) -> dict[str, Any]:
    """Put every archived object into the configured store (verified, never overwriting).

    Member names are validated against the content-addressed key layout (no path is
    ever taken from the archive); each put re-hashes the bytes and must yield the
    member's address. Existing identical objects are reused (idempotent).
    """
    import tarfile

    src, dec = _plaintext_source(archive, identity)
    reader = _HashingReader(src)
    count = total = 0
    try:
        with tarfile.open(fileobj=reader, mode="r|") as tar:  # type: ignore[call-overload]
            for m in tar:
                if not m.isfile():
                    raise BackupError(f"objects archive member {m.name[:120]!r} is not a regular file")
                namespace, sha = _parse_key(m.name, prefix)
                f = tar.extractfile(m)
                obj = store.put(
                    f, namespace, media_types.get(sha, "application/octet-stream"), max_bytes=max(m.size, 1)
                )
                if obj.sha256 != sha:
                    raise BackupError(f"archived object {m.name[:120]!r} does not hash to its address")
                count += 1
                total += m.size
        reader.drain()
    finally:
        src.close()
    if dec is not None:
        _require_ok((dec, dec.finish()))
    return {"objects": count, "bytes": total, "plaintext_sha256": reader.h.hexdigest()}


# --- secrets ---------------------------------------------------------------------------

_SECRET_NAME = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "ACCESS_KEY")


def secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Secret values present in the environment (DB URL passwords included)."""
    env = os.environ if environ is None else environ
    vals: set[str] = set()
    for k, v in env.items():
        if not v:
            continue
        if k.endswith("DATABASE_URL"):
            try:
                pw = make_url(v).password
            except (ArgumentError, ValueError):
                pw = None  # not a parseable URL: there is no password component to protect
            if pw:
                vals.add(str(pw))
        elif any(s in k for s in _SECRET_NAME) and len(v) >= 8:
            vals.add(v)
    return sorted(vals)


def files_containing_secrets(paths: list[Path], secrets: list[str]) -> list[str]:
    bad = []
    needles = [s.encode() for s in secrets]
    for p in paths:
        data = p.read_bytes()
        if any(n in data for n in needles):
            bad.append(p.name)
    return bad


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
            catalog = catalog_manifest(cur)
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
        "catalog": catalog,
    }
    body["manifest_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


# Security-relevant catalog state that a data-only comparison would miss: a restore
# that silently lost an append-only trigger, an RLS policy or a grant must fail.
# ACL entries are exploded and normalised (grantor and the owner's own implicit
# entry dropped) because restore uses --no-owner: the restoring role becomes owner.
CATALOG_QUERIES: dict[str, str] = {
    "triggers": """
        SELECT n.nspname || '.' || c.relname, t.tgname, t.tgenabled::text, pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal AND n.nspname = ANY(%(s)s) ORDER BY 1, 2""",
    "rls_tables": """
        SELECT n.nspname || '.' || c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(%(s)s) AND (c.relrowsecurity OR c.relforcerowsecurity) ORDER BY 1""",
    "policies": """
        SELECT schemaname || '.' || tablename, policyname, permissive, roles::text, cmd,
               coalesce(qual, ''), coalesce(with_check, '')
        FROM pg_policies WHERE schemaname = ANY(%(s)s) ORDER BY 1, 2""",
    "functions": """
        SELECT p.oid::regprocedure::text, p.prosecdef, coalesce(array_to_string(p.proconfig, ','), ''),
               p.provolatile::text, md5(coalesce(p.prosrc, ''))
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = ANY(%(s)s) ORDER BY 1""",
    "grants": """
        WITH acl AS (
          SELECT 'schema' AS kind, n.nspname AS obj, n.nspowner AS owner,
                 (aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner)))).*
          FROM pg_namespace n WHERE n.nspname = ANY(%(s)s)
          UNION ALL
          SELECT CASE c.relkind WHEN 'S' THEN 'sequence' ELSE 'relation' END, n.nspname || '.' || c.relname,
                 c.relowner,
                 (aclexplode(coalesce(c.relacl, acldefault(CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char",
                                                          c.relowner)))).*
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = ANY(%(s)s) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          UNION ALL
          SELECT 'column', n.nspname || '.' || c.relname || '.' || a.attname, c.relowner, (aclexplode(a.attacl)).*
          FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = ANY(%(s)s) AND a.attacl IS NOT NULL AND NOT a.attisdropped
          UNION ALL
          SELECT 'function', p.oid::regprocedure::text, p.proowner,
                 (aclexplode(coalesce(p.proacl, acldefault('f', p.proowner)))).*
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = ANY(%(s)s)
          UNION ALL
          SELECT 'database', '<current>', d.datdba, (aclexplode(coalesce(d.datacl, acldefault('d', d.datdba)))).*
          FROM pg_database d WHERE d.datname = current_database()
        )
        SELECT acl.kind, acl.obj, CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE r.rolname END,
               acl.privilege_type, acl.is_grantable
        FROM acl LEFT JOIN pg_roles r ON r.oid = acl.grantee
        WHERE acl.grantee <> acl.owner ORDER BY 1, 2, 3, 4""",
    "extensions": "SELECT extname, extversion FROM pg_extension ORDER BY 1",
}


def catalog_manifest(cur: psycopg.Cursor[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, q in CATALOG_QUERIES.items():
        cur.execute(q, {"s": list(SCHEMAS)})
        out[name] = [list(r) for r in cur.fetchall()]
    out["counts"] = {k: len(v) for k, v in out.items()}
    return out


def diff_manifests(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    out = []
    for key in sorted(set(a["tables"]) | set(b["tables"])):
        if a["tables"].get(key) != b["tables"].get(key):
            out.append(f"table {key}: {a['tables'].get(key)} != {b['tables'].get(key)}")
    for key in ("schema_version", "artifact_count", "artifact_sha256_list_digest", "releases"):
        if a.get(key) != b.get(key):
            out.append(f"{key} differs")
    ca, cb = a.get("catalog"), b.get("catalog")
    if ca is not None and cb is not None:  # backups taken before the catalog section have none
        for key in sorted(set(ca) | set(cb)):
            if key == "counts":
                continue
            sa = {json.dumps(x) for x in ca.get(key, [])}
            sb = {json.dumps(x) for x in cb.get(key, [])}
            if sa != sb:
                lost, extra = sorted(sa - sb)[:5], sorted(sb - sa)[:5]
                out.append(f"catalog {key}: missing after restore {lost}; unexpected {extra}")
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
    """Store key (``<prefix><namespace>/sha256/aa/bb/<sha>``) of a recorded location.

    ``meta.artifact_location.storage_uri`` is immutable history: a filesystem
    location keeps the absolute root it was written under, and an S3 location
    its bucket. The application reads objects by (namespace, sha256) from the
    *configured* store, so a restore into a fresh object root or bucket is
    checked by key, not by the historical root.
    """
    parts = urlsplit(uri)
    if parts.scheme == "file":
        path = Path(parts.path)
        try:
            return str(path.resolve().relative_to(Path(settings.reva_object_root).resolve()))
        except ValueError:
            segs = path.parts
            if len(segs) < 5 or segs[-4] != "sha256":
                raise
            return settings.reva_s3_prefix + "/".join(segs[-5:])
    return parts.path.lstrip("/")


def _parse_key(key: str, prefix: str) -> tuple[str, str]:
    """(namespace, sha256) of a store key; raises BackupError on anything else."""
    from reva.adapters.objects.common import object_key

    rest = key[len(prefix) :] if key.startswith(prefix) else None
    segs = rest.split("/") if rest is not None else []
    if len(segs) != 5:
        raise BackupError(f"unexpected object key {key[:120]!r}")
    namespace, sha = segs[0], segs[4]
    try:
        if object_key(prefix, namespace, sha) != key:
            raise ValueError
    except (ValueError, Exception) as e:  # check_namespace raises a typed RevaError
        raise BackupError(f"unexpected object key {key[:120]!r}") from e
    return namespace, sha


def check_locations(conn: psycopg.Connection[Any], inventory: list[dict[str, Any]], settings: Any) -> dict[str, Any]:
    """Every recorded location exists, its bytes hash to its address and size; every
    non-inline artifact is backed by at least one present object of the recorded size."""
    keys = {i["key"]: i for i in inventory}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT l.artifact_sha256, l.storage_uri, l.verified_byte_size, a.byte_size FROM meta.artifact_location l"
            " JOIN meta.artifact a ON a.sha256 = l.artifact_sha256"
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT a.sha256 FROM meta.artifact a WHERE NOT EXISTS"
            " (SELECT 1 FROM meta.artifact_inline i WHERE i.artifact_sha256 = a.sha256)"
        )
        need_object = {r[0] for r in cur.fetchall()}
    missing, mismatched, relocated = [], [], 0
    backed: set[str] = set()
    for sha, uri, size, artifact_size in rows:
        try:
            key = _location_key(uri, settings)
        except ValueError:
            missing.append(uri)
            continue
        if urlsplit(uri).scheme == "file" and not uri.startswith(
            f"file://{Path(settings.reva_object_root).resolve()}/"
        ):
            relocated += 1
        item = keys.get(key)
        if item is None:
            missing.append(uri)
        elif (
            item["address"] != sha
            or item["bytes"] != size
            or item["bytes"] != artifact_size
            or item["content_ok"] is False
        ):
            mismatched.append(uri)
        else:
            backed.add(sha)
    bad_content = [i["key"] for i in inventory if i["content_ok"] is False]
    return {
        "locations": len(rows),
        "relocated_locations": relocated,
        "artifacts": len(need_object),
        "artifacts_verified": len(backed & need_object),
        "unbacked_artifacts": sorted(need_object - backed)[:50],
        "missing": missing,
        "mismatched": mismatched,
        "corrupt_objects": bad_content,
    }


def _locations_ok(res: Mapping[str, Any]) -> bool:
    return not (
        res.get("missing") or res.get("mismatched") or res.get("corrupt_objects") or res.get("unbacked_artifacts")
    )


# --- sealed release manifests ------------------------------------------------------------


def verify_releases(conn: psycopg.Connection[Any], store: Any) -> dict[str, Any]:
    """Re-verify every belief release against its sealed manifest in the object store.

    Per release: the manifest object exists and hashes to ``manifest_sha256``; its
    bytes are the canonical JSON whose logical hash is recorded in meta.artifact;
    origin, method, cutoff and the ontology/model manifest digests equal the release
    row; the estimates Parquet is the release's registered estimates artifact, has
    the row count and logical digest the manifest pins and as many rows as
    belief.estimate; the pinned input observation ids equal belief.input.
    """
    import io

    import pyarrow.parquet as pq

    from reva.kernel.ids import CANONICAL_HASH_VERSION, logical_hash

    def lhash(raw: bytes) -> str:
        return hashlib.sha256(CANONICAL_HASH_VERSION.encode("ascii") + b"\x00" + raw).hexdigest()

    results = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.id::text, r.manifest_sha256, r.ontology_manifest_sha256, r.model_manifest_sha256, r.origin,"
            " r.method, r.knowledge_cutoff, a.logical_sha256 FROM belief.release r"
            " LEFT JOIN meta.artifact a ON a.sha256 = r.manifest_sha256 ORDER BY r.id"
        )
        releases = cur.fetchall()
        for rid, msha, osha, modsha, origin, method, cutoff, recorded_lsha in releases:
            problems: list[str] = []
            try:
                raw = store.get("belief", msha)  # verifies bytes against the address
                doc = json.loads(raw)
            except Exception as e:
                results.append(
                    {"release_id": rid, "ok": False, "problems": [f"manifest unreadable: {type(e).__name__}"]}
                )
                continue
            if lhash(raw) != recorded_lsha:
                problems.append("manifest logical sha256 differs from meta.artifact")
            # canonical_bytes() would re-escape the "$datetime" tags; compare the encoding.
            if json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() != raw:
                problems.append("manifest bytes are not canonical JSON")
            if doc.get("schema") != "reva-belief-release/1":
                problems.append("unexpected manifest schema")
            if doc.get("origin") != origin or doc.get("method") != method:
                problems.append("origin/method differ from release row")
            if doc.get("ontology_manifest_sha256") != osha or doc.get("model_manifest_sha256") != modsha:
                problems.append("ontology/model manifest digests differ from release row")
            if (doc.get("knowledge_cutoff") or {}).get("$datetime") != cutoff.astimezone(UTC).isoformat():
                problems.append("knowledge cutoff differs from release row")
            for sha in (osha, modsha):
                try:
                    store.get("belief", sha)
                except Exception as e:
                    problems.append(f"pinned manifest {sha[:12]} unreadable: {type(e).__name__}")
            est = doc.get("estimates_artifact") or {}
            cur.execute(
                "SELECT artifact_sha256 FROM belief.release_artifact WHERE release_id = %s AND role = 'estimates'",
                (rid,),
            )
            if [r[0] for r in cur.fetchall()] != [est.get("sha256")]:
                problems.append("estimates artifact differs from belief.release_artifact")
            cur.execute("SELECT count(*) FROM belief.estimate WHERE release_id = %s", (rid,))
            n_est = cur.fetchone()[0]  # type: ignore[index]
            try:
                rows = pq.read_table(io.BytesIO(store.get("belief", est["sha256"]))).to_pylist()
                if len(rows) != est.get("row_count") or len(rows) != n_est:
                    problems.append(f"estimate rows {len(rows)} != manifest {est.get('row_count')} / db {n_est}")
                if logical_hash(rows) != est.get("logical_sha256"):
                    problems.append("estimates logical sha256 differs from manifest")
            except Exception as e:
                problems.append(f"estimates artifact unreadable: {type(e).__name__}")
            cur.execute(
                "SELECT DISTINCT i.observation_id::text FROM belief.input i JOIN belief.estimate e"
                " ON e.id = i.estimate_id WHERE e.release_id = %s",
                (rid,),
            )
            if sorted(r[0] for r in cur.fetchall()) != sorted(doc.get("input_observation_ids", [])):
                problems.append("pinned input observations differ from belief.input")
            results.append({"release_id": rid, "ok": not problems, "estimates": n_est, "problems": problems})
    return {"releases": len(results), "verified": sum(r["ok"] for r in results), "details": results}


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
    include_objects: bool = False,
) -> dict[str, Any]:
    encrypt = bool(recipients or recipients_file)
    if include_objects and not objects:
        raise BackupError("--include-objects needs the object inventory (drop --no-objects)")
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
            # the objects archive re-hashes every object it writes; don't download each one twice
            inventory = object_inventory(settings, verify_content=not include_objects)
            locations = check_locations(conn, inventory, settings)
            timings["object_inventory_seconds"] = round(time.perf_counter() - t0, 3)
    finally:
        conn.close()
    objects_archive: dict[str, Any] | None = None
    if include_objects:
        from reva.adapters.objects.factory import build_store

        arch = target / ("objects.tar.age" if encrypt else "objects.tar")
        t0 = time.perf_counter()
        a_sha, a_bytes, a_count = write_objects_archive(
            build_store(settings), inventory, settings.reva_s3_prefix, arch, age_args
        )
        timings["objects_archive_seconds"] = round(time.perf_counter() - t0, 3)
        objects_archive = {
            "file": arch.name,
            "sha256": _sha256_file(arch),
            "bytes": arch.stat().st_size,
            "plaintext_sha256": a_sha,
            "plaintext_bytes": a_bytes,
            "members": a_count,
            "prefix": settings.reva_s3_prefix,
            "encrypted": encrypt,
        }
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
        "objects_archive": objects_archive,
        "timings": timings,
    }
    with _private_umask():
        (target / "backup.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    # The metadata files are plaintext even for encrypted backups: none may carry a
    # credential (DB passwords, S3/API keys, session secret) from the environment.
    leaked = files_containing_secrets([target / "backup.json", inv_path], secret_values())
    if leaked:
        raise BackupError(f"refusing to finish: a secret value appears in {leaked} (no SHA256SUMS written)")
    members = [dump_file.name, inv_path.name, "backup.json"] + ([objects_archive["file"]] if objects_archive else [])
    write_checksums(target, members)
    if objects and (locations.get("missing") or locations.get("mismatched") or locations.get("corrupt_objects")):
        raise BackupError(f"object store inconsistent with database (see {target / 'backup.json'})")
    record["path"] = str(target)
    return record


# --- restore -------------------------------------------------------------------------

ROLE_SQL = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[{roles}]::text[] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT', r);
    END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reva_restricted_deletion') THEN
    CREATE ROLE reva_restricted_deletion NOLOGIN;
  END IF;
END $$;
"""
BASE_ROLES = ("reva_api", "reva_worker", "reva_sim", "reva_registry")
REVA_ROLE_NAME = re.compile(r"^reva_[a-z][a-z0-9_]{0,50}$")


def _recorded_grants(record: Mapping[str, Any]) -> list[list[Any]]:
    return list(((record.get("logical_manifest") or {}).get("catalog") or {}).get("grants") or [])


def restore_roles(record: Mapping[str, Any]) -> list[str]:
    """Roles that must exist before pg_restore re-applies the recorded ACLs: the base runtime/group
    roles, every runtime login role this code knows (``reva.settings.ROLE_LOGIN``) and every
    ``reva_*`` grantee recorded in the backup's catalog (e.g. a role added by a later migration).
    Missing ones are created NOLOGIN (bootstrap provisions logins and passwords)."""
    from reva.settings import ROLE_LOGIN

    roles = set(BASE_ROLES) | set(ROLE_LOGIN.values())
    roles |= {g[2] for g in _recorded_grants(record) if len(g) >= 3 and isinstance(g[2], str)}
    return sorted(r for r in roles if REVA_ROLE_NAME.match(r))


def database_grants(record: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    """Database-level grants to re-apply (not part of a non --create dump): exactly those recorded
    at backup time; for backups without a catalog section, CONNECT for the V1 runtime roles."""
    rec = [
        (str(g[2]), str(g[3]), bool(g[4]))
        for g in _recorded_grants(record)
        if len(g) >= 5 and g[0] == "database" and g[2] != "PUBLIC"
    ]
    if rec or (record.get("logical_manifest") or {}).get("catalog"):
        return sorted(set(rec))
    return [(r, "CONNECT", False) for r in ("reva_api", "reva_worker", "reva_sim")]


def _target_is_empty(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM pg_namespace WHERE nspname = ANY(%s))"
        " + (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version')",
        (list(SCHEMAS),),
    ).fetchone()
    return bool(row and row[0] == 0)


def _drop_reva_objects(conn: psycopg.Connection[Any]) -> list[str]:
    """--force: drop the REVA schemas and alembic_version of the target (one transaction)."""
    present = [r[0] for r in conn.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)", (list(SCHEMAS),))]
    with conn.transaction():
        for schema in present:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.execute("DROP TABLE IF EXISTS public.alembic_version")
    return sorted(present)


def restore(
    backup_dir: Path,
    *,
    url_env: str,
    tools: PgTools,
    identity: str | None,
    run_bootstrap: bool,
    verify_objects: bool,
    restore_objects: bool = False,
    force: bool = False,
    confirm_database: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    verify_checksums(backup_dir)  # refuses unverifiable or tampered sets before anything else
    record = json.loads((backup_dir / "backup.json").read_text())
    dump_file = backup_dir / record["dump"]["file"]
    if dump_file.name not in {"reva.dump", "reva.dump.age"} or _sha256_file(dump_file) != record["dump"]["sha256"]:
        raise BackupError("dump checksum mismatch: refusing to restore")
    encrypted = bool(record["dump"]["encrypted"])
    if encrypted and not identity:
        raise BackupError("encrypted dump: pass --identity (age identity file)")
    archive_rec = record.get("objects_archive")
    archive = None
    if restore_objects:
        if not archive_rec:
            raise BackupError("--restore-objects: this backup has no objects archive (taken without --include-objects)")
        archive = backup_dir / archive_rec["file"]
        if archive.name not in {"objects.tar", "objects.tar.age"} or _sha256_file(archive) != archive_rec["sha256"]:
            raise BackupError("objects archive checksum mismatch: refusing to restore")
    url = url_from_env(url_env)
    env_name = environment if environment is not None else os.environ.get("REVA_ENV", "local")
    if force and env_name == "production":
        raise BackupError("--force is refused in production: restore into a new database and switch over")
    if force and confirm_database != url.database:
        raise BackupError("--force requires --confirm-database equal to the target database name")
    timings: dict[str, float] = {}
    # Pass 1: authenticate (age) and hash the plaintext without writing it anywhere.
    t0 = time.perf_counter()
    plain = verify_plaintext(dump_file, identity if encrypted else None)
    if plain[0] != record["dump"].get("plaintext_sha256"):
        raise BackupError("decrypted dump does not match its recorded sha256: refusing to restore")
    if archive is not None:
        a_plain = verify_plaintext(archive, identity if archive_rec["encrypted"] else None)
        if a_plain[0] != archive_rec["plaintext_sha256"]:
            raise BackupError("decrypted objects archive does not match its recorded sha256: refusing to restore")
    timings["verify_seconds"] = round(time.perf_counter() - t0, 3)
    dropped: list[str] = []
    conn = connect(url, autocommit=True)
    try:
        server_major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000  # type: ignore[index]
        row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        superuser = bool(row and row[0])
        if not _target_is_empty(conn):
            if not force:
                raise BackupError(
                    "target database is not empty; restore only into a new, empty database "
                    "(or pass --force --confirm-database <name> to drop its REVA schemas first)"
                )
            dropped = _drop_reva_objects(conn)
        # role names are validated against REVA_ROLE_NAME (no quotes possible) before rendering
        conn.execute(ROLE_SQL.replace("{roles}", ", ".join(f"'{r}'" for r in restore_roles(record))))
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
        for grantee, privilege, grantable in database_grants(record):
            if privilege not in {"CONNECT", "CREATE", "TEMPORARY"} or not REVA_ROLE_NAME.match(grantee):
                raise BackupError(f"backup records an unexpected database grant: {privilege} to {grantee}")
            conn.execute(
                sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                    sql.SQL(privilege),
                    dbname,
                    sql.Identifier(grantee),
                    sql.SQL(" WITH GRANT OPTION" if grantable else ""),
                )
            )
    finally:
        conn.close()
    bootstrap_report = None
    if run_bootstrap:
        from reva.cli.bootstrap import bootstrap
        from reva.settings import load_settings

        os.environ["REVA_MIGRATION_DATABASE_URL"] = url.render_as_string(hide_password=False)
        os.environ["REVA_DB_NAME"] = str(url.database)
        t0 = time.perf_counter()
        bootstrap_report = bootstrap(load_settings(), seed_registry=False)  # restore reproduces, never re-seeds
        timings["bootstrap_seconds"] = round(time.perf_counter() - t0, 3)
    conn = connect(url, autocommit=True)
    try:
        objects_restored = None
        store = None
        if archive is not None or verify_objects:
            from reva.adapters.objects.factory import build_store

            settings = _settings()
            store = build_store(settings)
        if archive is not None:
            media = dict(conn.execute("SELECT sha256, media_type FROM meta.artifact").fetchall())  # type: ignore[arg-type]
            t0 = time.perf_counter()
            objects_restored = restore_objects_archive(
                archive, identity if archive_rec["encrypted"] else None, store, archive_rec["prefix"], media
            )
            timings["objects_restore_seconds"] = round(time.perf_counter() - t0, 3)
            if objects_restored["plaintext_sha256"] != archive_rec["plaintext_sha256"]:
                raise BackupError("objects archive changed between verification and restore")
        t0 = time.perf_counter()
        restored = logical_manifest(conn)
        timings["logical_manifest_seconds"] = round(time.perf_counter() - t0, 3)
        objects = releases = None
        if verify_objects:
            t0 = time.perf_counter()
            objects = check_locations(conn, object_inventory(settings), settings)
            releases = verify_releases(conn, store)
            timings["verify_objects_seconds"] = round(time.perf_counter() - t0, 3)
    finally:
        conn.close()
    differences = diff_manifests(record["logical_manifest"], restored)
    result = {
        "schema": "reva-restore/1",
        "backup": str(backup_dir),
        "target_database": url.database,
        "server_major": server_major,
        "restored_as_superuser": superuser,
        "forced_overwrite": {"dropped_schemas": dropped} if force else None,
        "logical_manifest_sha256": restored["manifest_sha256"],
        "catalog_counts": restored["catalog"]["counts"],
        "matches_backup": not differences,
        "differences": differences,
        "bootstrap": bootstrap_report,
        "objects_restored": objects_restored,
        "objects": objects,
        "releases": releases,
        "timings": timings,
    }
    if differences:
        raise BackupError("restored logical manifest differs from backup: " + "; ".join(differences[:10]))
    if objects and not _locations_ok(objects):
        raise BackupError(
            "object store does not back every restored artifact: "
            + json.dumps({k: objects[k] for k in ("missing", "mismatched", "corrupt_objects", "unbacked_artifacts")})[
                :2000
            ]
        )
    if releases and releases["verified"] != releases["releases"]:
        bad = [r for r in releases["details"] if not r["ok"]]
        raise BackupError("sealed release manifests do not verify: " + json.dumps(bad)[:2000])
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
        "--include-objects",
        action="store_true",
        help="also copy every object into objects.tar(.age) (needed to rebuild a fresh object store)",
    )
    b.add_argument(
        "--recipient", action="append", default=[x for x in [os.environ.get("REVA_BACKUP_AGE_RECIPIENT")] if x]
    )
    b.add_argument("--recipients-file", default=os.environ.get("REVA_BACKUP_AGE_RECIPIENTS_FILE"))
    r = sub.add_parser("restore")
    r.add_argument("backup_dir")
    r.add_argument("--url-env", default="REVA_RESTORE_DATABASE_URL")
    r.add_argument("--identity", default=os.environ.get("REVA_BACKUP_AGE_IDENTITY"))
    r.add_argument("--skip-bootstrap", action="store_true")
    r.add_argument(
        "--verify-objects",
        action="store_true",
        help="check every artifact against the configured object store and re-verify sealed releases",
    )
    r.add_argument(
        "--restore-objects",
        action="store_true",
        help="put the backup's objects archive into the configured object store first",
    )
    r.add_argument(
        "--force", action="store_true", help="DROP the REVA schemas of a non-empty target first (refused in production)"
    )
    r.add_argument("--confirm-database", default=None, help="required with --force: the target database name")
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
                include_objects=args.include_objects,
            )
            out = {k: out[k] for k in ("path", "dump", "timings", "objects", "objects_archive") if k in out} | {
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
                restore_objects=args.restore_objects,
                force=args.force,
                confirm_database=args.confirm_database,
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
