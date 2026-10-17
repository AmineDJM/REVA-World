"""V6 DISKZ: transparent zstd compression of the filesystem object store and its in-place migration.

The object id is always the sha256 of the *raw* bytes; reads decode transparently and re-verify.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import io
import json
import os
import stat
import sys
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest

from reva.adapters.objects import compression as cz
from reva.adapters.objects.common import object_key
from reva.adapters.objects.compress_migrate import compress_object_file, compress_tree, verify_tree
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.kernel.errors import IntegrityFailure

ROOT = Path(__file__).resolve().parents[2]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def json_bytes(n: int = 2000, seed: int = 0) -> bytes:
    rows = [
        {"country": f"C{(i * 7 + seed) % 97:03d}", "year": 1960 + i % 60, "value": (i * 31 + seed) % 1000 / 7}
        for i in range(n)
    ]
    return json.dumps(rows).encode()


def path_of(store: FilesystemArtifactStore, namespace: str, digest: str) -> Path:
    return store.root / object_key(store.prefix, namespace, digest)


def legacy_put(store: FilesystemArtifactStore, data: bytes, namespace: str = "raw") -> Path:
    """Write an object the way the pre-V6 store did (raw bytes, mode 0444)."""
    p = path_of(store, namespace, sha(data))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.chmod(p, 0o444)
    return p


def make_writable(p: Path) -> None:
    os.chmod(p, 0o644)


# --------------------------------------------------------------------------- write path
def test_compressible_object_roundtrip(store: FilesystemArtifactStore) -> None:
    data = json_bytes()
    obj = store.put_bytes(data, "raw", "application/json")
    p = path_of(store, "raw", obj.sha256)
    assert obj.sha256 == sha(data) and obj.byte_size == len(data)  # id/size of the RAW bytes
    assert cz.is_wrapped(p) and store.is_compressed("raw", obj.sha256)
    assert p.stat().st_size < len(data) // 3
    assert store.get("raw", obj.sha256) == data
    assert b"".join(store.open("raw", obj.sha256)) == data
    head = store.head("raw", obj.sha256)
    assert head is not None and head.byte_size == len(data)
    for start, length in ((0, 10), (5, 1000), (len(data) - 3, 100), (len(data) + 5, 10), (1 << 20, 0)):
        assert store.range_read("raw", obj.sha256, start, length) == data[start : start + length]
    assert not (p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    # Idempotent re-put of the same raw bytes reuses (and re-verifies) the compressed object.
    again = store.put_bytes(data, "raw", "application/json")
    assert again.storage_uri == obj.storage_uri and again.byte_size == len(data)


def test_large_object_streams_across_chunks(store: FilesystemArtifactStore) -> None:
    data = json_bytes(60000)  # several MiB, several decode chunks
    assert len(data) > 3 * cz.CHUNK
    obj = store.put(io.BytesIO(data), "normalised", "application/json", max_bytes=len(data))
    assert store.is_compressed("normalised", obj.sha256)
    chunks = list(store.open("normalised", obj.sha256))
    assert len(chunks) > 1 and all(len(c) <= cz.CHUNK for c in chunks) and b"".join(chunks) == data
    mid = len(data) // 2
    assert store.range_read("normalised", obj.sha256, mid - 10, cz.CHUNK + 20) == data[mid - 10 : mid + cz.CHUNK + 10]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(os.urandom(64 * 1024), id="incompressible"),
        pytest.param(gzip.compress(json_bytes(), mtime=0), id="gzip-magic"),
        pytest.param(b"PK\x03\x04" + b"\x00" * 50_000, id="zip-magic"),
        pytest.param(b'{"tiny": true}', id="one-block"),
    ],
)
def test_not_worth_compressing_is_stored_raw(store: FilesystemArtifactStore, data: bytes) -> None:
    obj = store.put_bytes(data, "raw", "application/octet-stream")
    p = path_of(store, "raw", obj.sha256)
    assert not cz.is_wrapped(p) and p.read_bytes() == data
    assert store.get("raw", obj.sha256) == data


def test_raw_bytes_starting_with_magic_are_always_wrapped(store: FilesystemArtifactStore) -> None:
    data = cz.MAGIC + b"\x01\x01not really a header"  # tiny and incompressible, but ambiguous if stored raw
    obj = store.put_bytes(data, "raw", "application/octet-stream")
    p = path_of(store, "raw", obj.sha256)
    assert cz.is_wrapped(p)
    assert store.get("raw", obj.sha256) == data and store.head("raw", obj.sha256).byte_size == len(data)  # type: ignore[union-attr]


def test_compression_off_still_reads_compressed(tmp_path: Path) -> None:
    on = FilesystemArtifactStore(tmp_path / "o")
    off = FilesystemArtifactStore(tmp_path / "o", compress=False)
    a, b = json_bytes(seed=1), json_bytes(seed=2)
    oa = on.put_bytes(a, "raw", "application/json")
    ob = off.put_bytes(b, "raw", "application/json")
    assert on.is_compressed("raw", oa.sha256) and not off.is_compressed("raw", ob.sha256)
    assert off.get("raw", oa.sha256) == a and on.get("raw", ob.sha256) == b


# --------------------------------------------------------------------------- corruption
def _corrupt(p: Path, how: str) -> None:
    make_writable(p)
    b = bytearray(p.read_bytes())
    if how == "flip_payload":
        b[cz.HEADER_SIZE + len(b[cz.HEADER_SIZE :]) // 2] ^= 0xFF
    elif how == "truncate":
        b = b[: len(b) - 7]
    elif how == "append":
        b += b"x"
    elif how == "header_size":
        hdr = cz.parse_header(bytes(b[: cz.HEADER_SIZE]))
        assert hdr is not None
        b[: cz.HEADER_SIZE] = cz.encode_header(hdr.raw_size + 1, hdr.raw_sha256, hdr.payload_size)
    elif how == "header_version":
        b[8] = 99
    elif how == "truncated_header":
        b = b[:20]
    p.write_bytes(bytes(b))


@pytest.mark.parametrize(
    "how", ["flip_payload", "truncate", "append", "header_size", "header_version", "truncated_header"]
)
def test_corrupted_compressed_object_detected(store: FilesystemArtifactStore, how: str) -> None:
    data = json_bytes()
    obj = store.put_bytes(data, "raw", "application/json")
    p = path_of(store, "raw", obj.sha256)
    _corrupt(p, how)
    corrupted = p.read_bytes()
    with pytest.raises(IntegrityFailure):
        store.get("raw", obj.sha256)
    with pytest.raises(IntegrityFailure):
        b"".join(store.open("raw", obj.sha256))
    # Re-putting the true bytes never silently repairs/overwrites: it reports the conflict.
    with pytest.raises(IntegrityFailure):
        store.put_bytes(data, "raw", "application/json")
    assert p.read_bytes() == corrupted
    assert [x for x in store.staging.iterdir()] == []
    vt = verify_tree(store.root, store.prefix)
    assert not vt["ok"] and vt["counts"]["bad"] == 1


def test_wrapper_whose_content_is_another_object_fails_address_check(store: FilesystemArtifactStore) -> None:
    a, b = json_bytes(seed=3), json_bytes(seed=4)
    oa = store.put_bytes(a, "raw", "application/json")
    ob = store.put_bytes(b, "raw", "application/json")
    pa, pb = path_of(store, "raw", oa.sha256), path_of(store, "raw", ob.sha256)
    make_writable(pa)
    pa.write_bytes(pb.read_bytes())  # a valid wrapper, but of other raw bytes
    with pytest.raises(IntegrityFailure):
        store.get("raw", oa.sha256)
    with pytest.raises(IntegrityFailure):
        b"".join(store.open("raw", oa.sha256))


# --------------------------------------------------------------------------- migration
def _mixed_store(tmp_path: Path) -> tuple[FilesystemArtifactStore, dict[str, bytes]]:
    store = FilesystemArtifactStore(tmp_path / "objects")
    objs: dict[str, bytes] = {}
    for i in range(6):  # legacy, compressible
        d = json_bytes(1500, seed=10 + i)
        legacy_put(store, d, "raw" if i % 2 else "normalised")
        objs[sha(d)] = d
    for d in (os.urandom(50_000), gzip.compress(json_bytes(seed=99), mtime=0), b"small"):  # legacy, not worth it
        legacy_put(store, d)
        objs[sha(d)] = d
    for i in range(3):  # new, already compressed on put
        d = json_bytes(1500, seed=50 + i)
        store.put_bytes(d, "raw", "application/json")
        objs[sha(d)] = d
    return store, objs


def _ns_of(store: FilesystemArtifactStore, digest: str) -> str:
    return "raw" if path_of(store, "raw", digest).exists() else "normalised"


def test_migration_dry_run_changes_nothing(tmp_path: Path) -> None:
    store, _ = _mixed_store(tmp_path)
    before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    rep = compress_tree(store.root, store.prefix, dry_run=True)
    d = rep.as_dict()
    assert d["statuses"]["would_compress"] == 6 and d["statuses"]["already_compressed"] == 3
    assert d["bytes_freed"] > 0
    assert {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before


def test_migration_compresses_verifies_and_resumes(tmp_path: Path) -> None:
    store, objs = _mixed_store(tmp_path)
    p0 = path_of(store, "raw", sha(json_bytes(1500, seed=11)))
    st0 = p0.stat()
    log = io.StringIO()
    rep = compress_tree(store.root, store.prefix, log=log)
    d = rep.as_dict()
    assert d["statuses"]["compressed"] == 6
    assert d["statuses"]["already_compressed"] == 3
    assert d["statuses"]["skipped_small"] == 1
    assert d["statuses"].get("skipped_precompressed", 0) + d["statuses"].get("skipped_ratio", 0) == 2
    assert d["bytes_after"] < d["bytes_before"] and not d["corrupt"] and not d["errors"]
    assert len(log.getvalue().splitlines()) == 6
    # Mode and timestamps preserved (orphan GC ages objects by mtime).
    st1 = p0.stat()
    assert cz.is_wrapped(p0) and st1.st_mtime_ns == st0.st_mtime_ns and stat.S_IMODE(st1.st_mode) == 0o444
    for digest, data in objs.items():
        ns = _ns_of(store, digest)
        assert store.get(ns, digest) == data and store.head(ns, digest).byte_size == len(data)  # type: ignore[union-attr]
    assert list(store.staging.iterdir()) == []
    # Re-run = resume: nothing left to do.
    again = compress_tree(store.root, store.prefix).as_dict()
    assert again["statuses"].get("compressed", 0) == 0 and again["statuses"]["already_compressed"] == 9
    vt = verify_tree(store.root, store.prefix)
    assert vt["ok"] and vt["counts"] == {"ok_compressed": 9, "ok_raw": 3}


def test_migration_limit_then_resume(tmp_path: Path) -> None:
    store, _ = _mixed_store(tmp_path)
    assert compress_tree(store.root, store.prefix, limit=2).as_dict()["statuses"]["compressed"] == 2
    assert compress_tree(store.root, store.prefix).as_dict()["statuses"]["compressed"] == 4


def test_migration_never_rewrites_a_corrupt_object(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    good = json_bytes(seed=7)
    p = legacy_put(store, good)
    make_writable(p)
    bad = good.replace(b"C0", b"X0", 1)
    p.write_bytes(bad)
    rep = compress_tree(store.root, store.prefix).as_dict()
    assert rep["statuses"] == {"corrupt": 1} and rep["corrupt"] == [str(p)]
    assert p.read_bytes() == bad and not cz.is_wrapped(p)
    assert list(store.staging.iterdir()) == []


def test_migration_skips_young_and_hardlinked(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    young = legacy_put(store, json_bytes(seed=20))
    linked = legacy_put(store, json_bytes(seed=21))
    os.link(linked, tmp_path / "other_link")
    rep = compress_tree(store.root, store.prefix, min_age_s=3600).as_dict()
    assert rep["statuses"] == {"skipped_young": 1, "skipped_hardlinked": 1} or rep["statuses"] == {"skipped_young": 2}
    assert not cz.is_wrapped(young) and not cz.is_wrapped(linked)
    os.utime(young, (1_000_000_000, 1_000_000_000))
    os.utime(linked, (1_000_000_000, 1_000_000_000))
    rep = compress_tree(store.root, store.prefix, min_age_s=3600).as_dict()
    assert rep["statuses"] == {"compressed": 1, "skipped_hardlinked": 1}


def test_migration_ignores_files_outside_the_layout(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    stray = store.root / "reva" / "raw" / "sha256" / "ab" / "cd" / "notasha"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(json_bytes())
    misplaced = store.root / "reva" / "raw" / "sha256" / "00" / "00" / sha(json_bytes(seed=5))
    misplaced.parent.mkdir(parents=True)
    misplaced.write_bytes(json_bytes(seed=5))
    rep = compress_tree(store.root, store.prefix).as_dict()
    assert rep["statuses"] == {"skipped_layout": 2}
    assert not cz.is_wrapped(stray) and not cz.is_wrapped(misplaced)


def test_migration_does_not_replace_a_file_changed_or_deleted_meanwhile(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    data = json_bytes(seed=30)
    p = legacy_put(store, data)
    common = {"root": store.root, "prefix": store.prefix, "staging": store.staging}

    def swap(path: Path) -> None:  # someone else atomically replaced it (e.g. a concurrent writer)
        tmp = path.with_name("x")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    r = compress_object_file(p, "raw", sha(data), before_replace=swap, **common)  # type: ignore[arg-type]
    assert r["status"] == "changed_during" and not cz.is_wrapped(p) and p.read_bytes() == data

    def gc(path: Path) -> None:  # orphan GC deleted it: the migration must not resurrect it
        make_writable(path)
        path.unlink()

    r = compress_object_file(p, "raw", sha(data), before_replace=gc, **common)  # type: ignore[arg-type]
    assert r["status"] == "vanished" and not p.exists()
    assert list(store.staging.iterdir()) == []


def test_migration_interrupted_before_replace_leaves_original_and_no_temp(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    data = json_bytes(seed=31)
    p = legacy_put(store, data)

    def crash(path: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        compress_object_file(
            p, "raw", sha(data), root=store.root, prefix=store.prefix, staging=store.staging, before_replace=crash
        )
    assert p.read_bytes() == data and list(store.staging.iterdir()) == []
    assert compress_tree(store.root, store.prefix).as_dict()["statuses"] == {"compressed": 1}
    assert store.get("raw", sha(data)) == data


def test_concurrent_reads_during_migration(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    objs = {}
    for i in range(40):
        d = json_bytes(800, seed=100 + i)
        legacy_put(store, d)
        objs[sha(d)] = d
    stop = threading.Event()
    failures: list[str] = []
    reads = [0]

    def reader() -> None:
        reader_store = FilesystemArtifactStore(store.root)
        while not stop.is_set():
            for digest, data in objs.items():
                try:
                    ok = (
                        reader_store.get("raw", digest) == data
                        and b"".join(reader_store.open("raw", digest)) == data
                        and reader_store.head("raw", digest).byte_size == len(data)  # type: ignore[union-attr]
                        and reader_store.range_read("raw", digest, 100, 50) == data[100:150]
                    )
                    if not ok:
                        failures.append(f"wrong bytes {digest}")
                except Exception as e:
                    failures.append(f"{digest}: {type(e).__name__}: {e}")
                reads[0] += 1

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        rep = compress_tree(store.root, store.prefix).as_dict()
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert rep["statuses"] == {"compressed": 40}
    assert failures == [] and reads[0] > 40


# --------------------------------------------------------------------------- consumers
def _br() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        return importlib.import_module("backup_restore")
    finally:
        sys.path.pop(0)


class _Settings:
    reva_object_backend = "filesystem"
    reva_s3_prefix = "reva/"

    def __init__(self, root: Path) -> None:
        self.reva_object_root = root


def test_backup_restore_inventory_archive_and_restore_with_compressed_objects(tmp_path: Path) -> None:
    br = _br()
    store, objs = _mixed_store(tmp_path)
    compress_tree(store.root, store.prefix, limit=3)  # mixed: legacy raw, migrated, put-compressed
    inv = br.object_inventory(_Settings(store.root))
    assert len(inv) == len(objs)
    for item in inv:
        assert item["content_ok"] is True and item["sha256"] == item["address"]
        assert item["bytes"] == len(objs[item["address"]])  # raw size, comparable with meta.artifact sizes
    assert any(i["stored_bytes"] < i["bytes"] for i in inv)
    cheap = br.object_inventory(_Settings(store.root), verify_content=False)
    assert [i["bytes"] for i in cheap] == [i["bytes"] for i in inv]
    # objects.tar holds the RAW bytes (portable to any store, including S3) ...
    tar_path = tmp_path / "objects.tar"
    _, _, count = br.write_objects_archive(store, inv, store.prefix, tar_path, None)
    assert count == len(objs)
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            f = t.extractfile(m)
            assert f is not None and sha(f.read()) == m.name.rsplit("/", 1)[-1]
    # ... and restores into a fresh store (re-compressed where it pays off), verified.
    fresh = FilesystemArtifactStore(tmp_path / "restored")
    res = br.restore_objects_archive(tar_path, None, fresh, fresh.prefix, {})
    assert res["objects"] == len(objs)
    for digest, data in objs.items():
        assert fresh.get(_ns_of(fresh, digest), digest) == data
    # A corrupted compressed object is reported as corrupt by the inventory.
    victim = next(p for p in sorted(fresh.root.rglob("*")) if p.is_file() and cz.is_wrapped(p))
    _corrupt(victim, "append")
    bad = [i for i in br.object_inventory(_Settings(fresh.root)) if i["content_ok"] is False]
    assert [b["key"] for b in bad] == [str(victim.relative_to(fresh.root))]


def test_events_demo_reads_archive_through_the_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.events import demo

    body = json.dumps({"entities": {"Q1": {"id": "Q1"}}}).encode()
    gz = gzip.compress(body, mtime=0)
    store = FilesystemArtifactStore(tmp_path / "kg")
    obj = store.put_bytes(gz, "raw", "application/gzip")
    index = tmp_path / "archive_index.jsonl"
    index.write_text(
        json.dumps(
            {"kind": "entitydata", "ids": ["Q1"], "sha256": obj.sha256, "retrieved_at": "2026-01-01T00:00:00+00:00"}
        )
        + "\n"
    )
    monkeypatch.setattr(demo, "ARCHIVE_ROOT", store.root)
    monkeypatch.setattr(demo, "ARCHIVE_INDEX", index)
    got = demo.archived_entitydata("Q1")
    assert got is not None and got[0] == body and got[1] == obj.sha256
    # Tampered archive bytes are refused instead of being parsed.
    p = path_of(store, "raw", obj.sha256)
    make_writable(p)
    p.write_bytes(gzip.compress(b'{"entities": {}}', mtime=0))
    with pytest.raises(IntegrityFailure):
        demo.archived_entitydata("Q1")
