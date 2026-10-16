# REVA World — data snapshot 2026-10-16 (container-local state at end of session)

Code lives on branch `claude/kind-darwin-kyy8re`. This data is split across branches
`reva-data-20261016-b1` … `-bN` (each an orphan branch with no code). Files:

* `reva.dump` — dev database `reva` (pg_dump custom format; migration head 0016_causal_ops; atlas
  snapshot b11f1c51…, KG, links store, causal claims loaded so far).
* `reva_kg_wdharvest.dump`, `reva_kg_causwd.dump`, `reva_causcand.dump` — harvest / causal throwaway DBs
  (Wikidata harvest progress is resumable from them).
* `var_data.tar.part_000…` — tar of `var/objects` (archived raw provider bytes + Parquet atlas tables,
  content-addressed), `var/kg` (Wikidata archive), `var/acceptance`, `var/live`, split in 95 MB parts.
* `scratchpad_scripts_reports.tgz.part_*` (cat them, then tar -xzf) — agents' scratch scripts and reports (large downloads excluded).
* `MANIFEST.sha256` — checksums of every file.

## Restore on any machine

```bash
git clone --single-branch -b claude/kind-darwin-kyy8re https://github.com/AmineDJM/REVA-World.git
cd REVA-World
mkdir -p ../reva-data && for b in 1 2 3 4; do
  git fetch origin reva-data-20261016-b$b && git --work-tree=../reva-data checkout FETCH_HEAD -- . ; done
(cd ../reva-data && sha256sum -c MANIFEST.sha256)
cat ../reva-data/var_data.tar.part_* | tar -xf -          # restores var/objects, var/kg, ...
make up && uv sync
for db in reva reva_kg_wdharvest reva_kg_causwd reva_causcand; do
  docker exec reva-db createdb -U reva_owner $db 2>/dev/null || true
  docker exec -i reva-db pg_restore -U reva_owner -d $db --no-owner < ../reva-data/$db.dump; done
uv run reva db bootstrap      # roles, grants, contract check
uv run reva atlas snapshot --warm   # rebuilds the DuckDB query file (~6.4 GB, ~15 min)
```
