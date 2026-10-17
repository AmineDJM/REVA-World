# V6 DISKZ — transparent zstd compression of the object archive

Date: 2026-10-17. Agent: DISKZ. No DB migration (head stays `0019_effect_estimates`), no DB writes.

## Result on the dev archive

| | before | after |
|---|---|---|
| `du -sh var/objects` | 5.1 GB | 3.4 GB |
| allocated bytes of the 2,684 compressed objects | 2.098 GB | 0.259 GB (+0.06 GB second pass) |
| **freed by the migration** | | **≈ 1.90 GB** (1.839 GB pass 1 + 0.059 GB pass 2) |
| `df -h /` free | 4.56 GB (just before pass 1) | 6.34 GB (just after pass 1); other agents keep writing (5.3 GB at report time) |

* `var/objects`: 13,113 objects; 2,845 now stored compressed (incl. new puts by other agents), 10,268 raw.
  Of the raw ones, 7,322 are one-block files (< 4 KiB: nothing to gain), 2,618 already-compressed formats
  (gzip/zip, by magic bytes: the 2.2 GB of zips are the biggest remaining item), 257 failed the ratio test.
  Per namespace freed: raw 1.81 GB (2,435 objects), normalised 22.5 MB (180; parquet mostly already compressed),
  evaluation 3.2 MB.
* `var/kg/objects` (115 MB, almost all gzip Wikidata bodies): 9 objects compressed, ~0 GB to gain.
* `reva objects verify` afterwards: **every** object in both roots decodes and hashes to its address
  (`var/objects` ok_raw 10,268 / ok_compressed 2,845; `var/kg/objects` ok_raw 7,566 / ok_compressed 9); no corrupt
  object, no error, nothing deleted. Raw content 5.42 GB is now held in 3.51 GB allocated.
* Logs: per-object JSON lines (namespace, sha256, before/after bytes) in the DISKZ scratchpad
  (`diskz/migrate_objects.jsonl`, `diskz/migrate_kg.jsonl`).

## Design

* **Object id unchanged**: always the sha256 of the *raw* bytes. Path unchanged (`<prefix><ns>/sha256/aa/bb/<sha>`), so
  `meta.artifact_location.storage_uri`, GC (`list_uris`/`uri_mtime`/`delete_orphan`) and backups keep working.
* **Format** (`src/reva/adapters/objects/compression.py`): a compressed file is
  `MAGIC "\x89RVZ\r\n\x1a\n" | version | codec | raw_size u64 | payload_size u64 | raw_sha256 | one zstd frame`
  (content size + frame checksum). A file without MAGIC is the raw bytes (legacy and not-worth-it objects). Raw bytes
  that themselves start with MAGIC are always stored wrapped, so the marker is unambiguous.
* **Reads** (`FilesystemArtifactStore.get/open/head/range_read`) decode transparently; `get`/`open` always re-verify
  sha256 of the decoded bytes; decoding is bounded (fails past the declared size), truncation/appended bytes are
  caught by `payload_size`; `head` returns the raw size (what `confirm_object_locked` and backups compare).
  `range_read` on a compressed object decodes sequentially (O(start)).
* **Writes** (`put`): spool+hash as before, then compress into `.staging` when it pays (skip already-compressed
  formats by magic bytes, skip one-block files, ratio test on the first MiB at level 3, keep only if it frees ≥ 1
  block and ≥ 10 %), verify the staged wrapper by decoding, then the same atomic `link()` conditional create.
  Reuse of an existing object re-verifies the decoded bytes (raw or wrapped). `compress=False` disables writing
  compressed objects (reads still decode). zstd level 9 (≈ 57 MB/s; level 3/15 differ by ~1 % ratio here).
* **S3 store unchanged** (objects there are raw bytes). Moving filesystem objects to S3 must go through the store API
  (`backup_restore` objects.tar holds raw bytes), never a byte-level sync of `var/objects`.
* Dependency: `zstandard>=0.23` (0.25.0 locked via `uv add`; licence BSD-3-Clause, bundled libzstd BSD).

## Migration command

```
uv run reva objects compress [--dry-run] [--min-saving-pct 10] [--root var/kg/objects] [--namespace raw]
                             [--level 9] [--min-age-minutes 60] [--limit N] [--log FILE]
uv run reva objects verify [--root ...]
```

Per file (`src/reva/adapters/objects/compress_migrate.py`): skip if already wrapped (this makes re-runs resume),
hard-linked, younger than `--min-age-minutes`, outside the content-addressed layout, or not worth it; stream-compress
into `.staging` (same filesystem) while hashing the source — if the source does not hash to its address it is reported
`corrupt` and left untouched; fsync; verify by decoding from disk (sha256 + size); copy mode and atime/mtime (orphan GC
ages by mtime); re-stat the original and only then `os.replace` + directory fsync (if it changed or vanished — e.g. GC
deleted it — the temp file is discarded, never resurrected). Concurrent readers hold either the old inode or the
complete new file. Exit 1 if anything is corrupt/errored. No DB access.

Safety on the shared dev archive: before migrating I checked that every running Python process had started after the
store change (so all readers/writers decode); pass 1 skipped objects modified in the last 60 min, pass 2 those of the
last 10 min.

## Consumers checked (grep for `var/objects`, `var/kg/objects`, `sha256/`, `storage_uri`, `read_bytes`, `rglob`)

* Through the store API already (decode transparently): atlas ingest/fixture replay and the atlas query cache build
  (`store.open`), `kg import-harvest` (`archive.get`), evidence runner, causal candidates, GC, belief releases.
* Fixed: `scripts/backup_restore.py` `object_inventory` (filesystem) now reports sha256/bytes of the *decoded* bytes
  (+ `stored_bytes`); a broken wrapper is `content_ok: false`. objects.tar holds raw bytes; restore re-compresses.
* Fixed: `src/reva/events/demo.py` `archived_entitydata` read `var/kg/objects` files directly; now via
  `FilesystemArtifactStore.get` (decode + sha256 check). `_copy_raw` copies the stored file as is (target store decodes).
* No other code reads object files by path.

## Tests

`tests/unit/test_storage_compression.py` (26 tests): round-trip (get/open/head/range_read, multi-chunk), raw storage
for incompressible/gzip/zip/one-block data, MAGIC-prefixed raw data, compress-off store reading compressed objects,
corruption detection (flipped payload byte, truncation, appended byte, header size/version, truncated header, valid
wrapper of other bytes) incl. no silent repair on re-put, migration dry-run/real/resume/limit, corrupt source never
rewritten, young/hard-linked/out-of-layout skips, changed/deleted-during-migration, interruption before replace,
4 reader threads during migration (no failure), backup_restore inventory + objects.tar + restore with mixed objects,
events demo reader. Also green: `tests/unit/test_storage_fs.py`, `tests/integration/test_storage_publication.py`,
`test_kg_import_harvest_db.py`, `test_backup_restore.py`, `test_backup_restore_roundtrip.py` (52 passed). Full
unit+invariants run: failures only in other agents' in-flight areas (nlqa, registry/budget keys, model registry),
none in object storage. ruff, mypy (new files), import-linter clean; eurostat template closure digest unchanged.

## Remaining / owner decisions

* The biggest remaining item is 2.2 GB of zip archives (deflate inside: not recompressible). Freeing it would need a
  retention decision (e.g. keep only the extracted members actually parsed) — owner decision, not done.
* Periodically re-run `uv run reva objects compress --min-age-minutes 60` (new puts are already compressed; this
  catches objects written by processes that predate the change).
* Files: `src/reva/adapters/objects/{compression,compress_migrate,filesystem}.py`, `src/reva/cli/objects.py`
  (registered in `src/reva/cli/main.py`), `scripts/backup_restore.py`, `src/reva/events/demo.py`, `pyproject.toml`,
  `uv.lock`, `tests/unit/test_storage_compression.py`.
