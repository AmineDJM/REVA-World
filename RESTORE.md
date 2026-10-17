# REVA World — data snapshot 2026-10-17 (container-local state after the LOADALL run)

Code lives on branch `claude/kind-darwin-kyy8re`. The data is split across the orphan branches
`reva-data-20261017-b1` … `-b12`, which hold no code. `PARTS.txt` lists which branch holds which part. This
branch (b1) holds only this file, `MANIFEST.sha256` and `PARTS.txt`.

Contents:

* `reva.dump.part_000…002`: the dev database `reva`. Concatenate the parts to get one
  `pg_dump -Fc -Z 9` file. Migration head 0016_causal_ops. It holds:
  * atlas snapshot `1d9b950f…` (220 tables, 93.5 M rows)
  * the KG (119,640 entities, 2.45 M statements), including the Wikidata harvests imported by
    `reva kg import-harvest`
  * links materialisation 1 (1,147,110 links)
  * 27,791 graded causal claims
* `reva_kg_wdharvest.dump.part_000`, `reva_kg_causwd.dump.part_000`, `reva_causcand.dump.part_000`: the
  harvest and candidate throwaway databases, one part each. The CAUSWD harvest was still running when this
  was taken (01:11 UTC).
* `var_data.tar.part_000…046`: a tar of `var/objects` (archived raw provider bytes and Parquet atlas tables,
  content-addressed) and `var/kg` (Wikidata archive, `archive_index.jsonl`, harvest state, reports), in
  95 MB parts.
* `MANIFEST.sha256`: the checksum of every part.

Not included: the DuckDB query file (rebuildable cache) and the agents' scratch scripts (see the
2026-10-16 snapshot `reva-data-20261016-b1`).

## Restore on any machine

```bash
git clone --single-branch -b claude/kind-darwin-kyy8re https://github.com/AmineDJM/REVA-World.git
cd REVA-World
mkdir -p ../reva-data && for b in $(seq 1 12); do
  git fetch origin reva-data-20261017-b$b && git --work-tree=../reva-data checkout FETCH_HEAD -- . ; done
(cd ../reva-data && sha256sum -c MANIFEST.sha256)
cat ../reva-data/var_data.tar.part_* | tar -xf -          # restores var/objects and var/kg
make up && uv sync
for db in reva reva_kg_wdharvest reva_kg_causwd reva_causcand; do
  cat ../reva-data/$db.dump.part_* > ../reva-data/$db.dump
  docker exec reva-db createdb -U reva_owner $db 2>/dev/null || true
  docker exec -i reva-db pg_restore -U reva_owner -d $db --no-owner < ../reva-data/$db.dump; done
uv run reva db bootstrap            # roles, grants, contract check
uv run reva atlas snapshot --warm   # rebuilds the DuckDB query file (~7.1 GB, ~15 min)
```
