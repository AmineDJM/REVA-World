# REVA World: data snapshot 2026-10-17c (dev DB after migration 0018, about 50k causal claims, grading v3)

The code is on branch `claude/kind-darwin-kyy8re` (the dumps match commit `38f1c74` or later). The data is in
the orphan branches `reva-data-20261017c-b1` … `-b3`, which hold no code. `PARTS.txt` gives the branch and the
size in bytes of each part. This branch (b1) holds only metadata and small files.

## Contents

| File | Branch | What it is |
|---|---|---|
| `reva.dump.part_000…002` | b2 | dev database `reva`, `pg_dump -Fc -Z 9`, split into 95 MB parts. Migration head `0018_retraction_reasons`. KG: 182,408 entities and 2,676,446 statements. Links: 1,151,759 link rows in 2 materialisations. Causal: 98,924 claim rows, of which 50,245 are current (T1 48,573 / T2 2 / T3 190 / T4 1,480), all graded with rules v3. 5,483 retractions. 103 evidence sources. |
| `reva_kg_causwd.dump.part_000` | b2 | CAUSWD Wikidata causal-harvest database, one part |
| `reva_kg_wdharvest.dump.part_000` | b2 | Wikidata harvest database, one part |
| `var_delta.tar.part_000` | b3 | **incremental** tar of `var/objects` + `var/kg`. It holds only the 1,182 files (93.96 MB) that were created or changed after the previous snapshot's tar was started (ctime ≥ 2026-10-17 01:10 UTC; that tar started at 01:11:54). It also holds the newer versions of the files that are not content-addressed, such as `var/kg/archive_index.jsonl`, `harvest_state`, reports and logs. The list is in `DELTA_FILES.txt`. |
| `FILES.sha256` | b1 | sha256 and path of **every** file in `var/objects` + `var/kg` at this snapshot: 16,156 files, 4,625,061,228 bytes. Use it to check a restore, and as the baseline for the next incremental snapshot. |
| `DELTA_FILES.txt` | b1 | the paths inside `var_delta.tar` |
| `extras.tar.gz` | b1 | current harvester state `var_kg/harvest_state/*.json`, reports `var_kg/reports/*`, the WDRUN logs `var_kg/logs/wdrun_*.log` (each START line shows the spec paths and args of a harvest group; the `groups_*.json` files no longer existed when this snapshot was taken), and scratch scripts worth keeping under `scratch/`: `driver.py` (WDRUN budget watchdog driver), the loadall, dayops and audfix scripts, and the save scripts of this snapshot and the previous one (`scratch/save3/`, `scratch/loadall/save/`) |
| `MANIFEST.sha256` | b1 | sha256 of every part and of `extras.tar.gz` |

**objects = previous snapshot's tar + this delta.** The full `var/objects` + `var/kg` tree is the previous
snapshot's `var_data.tar.part_000…046` (branches `reva-data-20261017-b3` … `-b12`; its `MANIFEST.sha256` and
`PARTS.txt` are on `reva-data-20261017-b1`), with this snapshot's `var_delta.tar` extracted over it.

Not included: the throwaway DBs (`reva_test_*`, `reva_causcand*`, `reva_allc_live_*`, `*_drift*`), and the DuckDB
query file, which is a rebuildable cache.

Secret scan: before the push, the plain-SQL dumps of all three DBs, every file in the delta and everything in
`extras.tar.gz` were checked against the `HARD_PATTERNS` of `tests/security/test_secrets_logs.py`, and checked
for the owner's e-mail address and any gmail.com address. The only match is in the `reva` DB: two HDX download
redirect URLs that contain a provider-issued temporary AWS key id (`ASIA…`). Their signature and token were
already redacted (`***`) at ingest. This is HDX's pre-signed public URL, not a REVA credential.

## Restore on any machine (needs about 12 GB free during the restore)

```bash
git clone --single-branch -b claude/kind-darwin-kyy8re https://github.com/AmineDJM/REVA-World.git
cd REVA-World
D=../reva-data; mkdir -p $D/new $D/old
# 1. this snapshot (3 branches)
for b in 1 2 3; do git fetch --depth 1 origin reva-data-20261017c-b$b &&
  git --work-tree=$D/new checkout FETCH_HEAD -- . ; done
(cd $D/new && sha256sum -c MANIFEST.sha256)
# 2. the base objects tar of the previous snapshot (b1 = manifest, b3..b12 = var_data.tar parts)
for b in 1 $(seq 3 12); do git fetch --depth 1 origin reva-data-20261017-b$b &&
  git --work-tree=$D/old checkout FETCH_HEAD -- . ; done
(cd $D/old && grep var_data.tar MANIFEST.sha256 | sha256sum -c -)
git reset -q                                    # the checkouts above staged files in the code index
# 3. objects: previous full tar, then this delta on top (the delta overwrites the changed files)
cat $D/old/var_data.tar.part_* | tar -xf -
cat $D/new/var_delta.tar.part_* | tar -xf -
sha256sum --quiet -c $D/new/FILES.sha256           # must print nothing
# files from the old tar that no longer exist in this snapshot (normally none):
comm -23 <(find var/objects var/kg -type f | sort) <(awk '{print $2}' $D/new/FILES.sha256 | sort)
# 4. databases
make up && uv sync
for db in reva reva_kg_causwd reva_kg_wdharvest; do
  cat $D/new/$db.dump.part_* > $D/$db.dump
  docker exec reva-db createdb -U reva_owner $db 2>/dev/null || true
  docker exec -i reva-db pg_restore -U reva_owner -d $db --no-owner < $D/$db.dump
  rm $D/$db.dump; done
uv run reva db bootstrap            # roles, grants, contract check (already at head 0018)
uv run reva db verify-schema        # exit 0 = live catalog matches migrations/schema_contract.json
uv run reva atlas snapshot --warm   # rebuilds the DuckDB query file (~7 GB, ~15 min)
# 5. optional: harvester state, reports and scratch scripts
mkdir -p $D/extras && tar -xzf $D/new/extras.tar.gz -C $D/extras
```

On a fresh cluster, `pg_restore` may report errors for GRANT statements whose roles do not exist yet.
`reva db bootstrap` creates those roles and grants them again. Then run `uv run python -m reva.kg.harvest status`
to see the Wikidata harvest state, and `uv run reva causal refresh` to recompute the causal claims if needed.
