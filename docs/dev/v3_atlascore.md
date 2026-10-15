# V3 ATLASCORE — atlas storage, catalog, snapshots, queries, API, budgets

Owner: agent ATLASCORE. Interface for atlas connector agents: `docs/dev/v3_atlas_interface.md`.

## Components

| Module | Role |
|---|---|
| `src/reva/atlas/tables.py` | `ATLAS_SCHEMA` (fixed long schema), `validate_atlas_frame`, `register_atlas_table` (content-addressed Parquet + manifest via `reva.simulation.datasets.register_dataset`), `country_iso3` / `geo_index` |
| `src/reva/atlas/country_codes.py` | rebuilds `registry/entities/country_codes_extra.json` (eurostat, fips) from archived/quoted provider references |
| `src/reva/atlas/catalog.py` + `registry/atlas/catalog/_schema.json` | concept catalog loader/validator (`<domain>.yaml`) |
| `src/reva/atlas/snapshot.py` | atlas snapshots (record / resolve) |
| `src/reva/atlas/query.py` | DuckDB query layer (best-available cell, per-source, coverage, dossier, map) |
| `src/reva/atlas/sources.py` | `AtlasSourceSpec` + atlas source registry (discovers `reva.adapters.connectors.atlas_*`) |
| `src/reva/atlas/ingest.py` | single budget-metered ingest entry point (`python -m reva.atlas.ingest`, job handler) |
| `src/reva/atlas/budget_merge.py` | merges `registry/atlas/budget_requests/*.yaml` into `registry/policies/budgets.yaml` |
| `src/reva/api/routes/atlas.py` | `GET /v1/atlas/{concepts,countries/{iso3},series,map,coverage}` |

## Storage and provenance

* An atlas table is one `evidence.dataset` row (`concept_id = 'atlas.table'`, `dataset_key = atlas.<source>.<dataset>`,
  `subject_key = <source>.<dataset>`), its Parquet artifact (zstd, exact Arrow schema, rows sorted by
  indicator/geo/period so row order never changes the bytes) and a JSON manifest whose `coverage` lists row counts,
  numeric/text/missing cells, period range, the geo scheme and **every unmapped provider geography with its row count**.
* `register_dataset` checks in the database that every source acquisition exists with the table's origin and that the
  artifacts are verified; the provenance activity links the table to the raw artifacts. Every row also carries
  `raw_sha256` + a JSON `selector` locating the cell in the raw bytes.
* Validation refuses (typed `ValidationProblem`, nothing written): schema/type mismatches, nulls in required columns,
  non-strict decimals (`1,000`, `NaN`, `inf`, `007`, empty), value-less rows without `flags.missing`, invalid JSON
  flags/selectors, empty selectors, `period_start >= period_end`, duplicate cells, raw hashes not declared, `iso3` not a
  crosswalk key or contradicting the geo scheme, and two provider geographies mapping to the same country for the
  same indicator and period (REVA never picks one silently).
* Country keys: the crosswalk (251 entities since crosswalk 1.1.0 / XWALK: all 249 ISO 3166-1 codes, plus `XKX`, `CHI`,
  World Bank codes, not ISO). Taiwan (`TWN`, ISO 3166-1, not listed by UNSD M49) is an entity since 1.1.0 (it was
  absent from the 250-entity ALLC crosswalk, so tables registered before keep TWN rows unmapped until re-registered);
  CHN/TWN carry `aggregation_overlap` (never summed or split), see `docs/dev/v3_xwalk.md`. Extra schemes:
  `country_codes_extra.json` (Eurostat EL/UK/XK from the quoted Eurostat glossary; FIPS 10-4 from the archived GeoNames
  `countryInfo.txt`, sha256 `c55f638c…`) and per-agent `registry/atlas/geo_aliases/*.yaml` (each alias with evidence;
  conflicting aliases refuse to load).

## Atlas snapshots (no schema change)

A snapshot pins, per atlas `dataset_key`, the latest version recorded at or before its cutoff with its origin. It is a
canonical JSON manifest artifact (`atlas-snapshot/1`) plus a `meta.activity` (kind `atlas.snapshot`, inputs = pinned
table artifacts, output = manifest). `create_atlas_snapshot` takes the **exclusive evidence snapshot lock** before
reading its cutoff (like belief publication), so a table registered at or before the cutoff can never be missing.
Readers resolve the latest snapshot (optionally per origin) or a named one (`?snapshot=<sha256>`), re-verify the
manifest bytes, and every API answer names the snapshot. New table versions are invisible until a new snapshot:
`uv run python -m reva.atlas.snapshot --origin live [--warm]` (worker credentials).

No migration `0010_atlas.py` was needed: `evidence.dataset` + artifacts + activities carry everything.

## Query layer

Per snapshot, the pinned Parquet files are streamed from the store, **re-hashed against the pinned sha256** and
schema-checked, then loaded once into a cache DuckDB file (`$REVA_ATLAS_CACHE_DIR`, default
`$TMPDIR/reva-atlas-cache`, file lock against concurrent builds, two newest kept): table `series` (integer id per
source/dataset/indicator), `cells` sorted by (country, series, period) for country reads and `cells_s` sorted by
(series, year, country) for maps/coverage. Tampered bytes → `IntegrityFailure`, no file is produced.

* **Best-available cell** per (concept, iso3, calendar year): the lowest-rank series of the concept's `source_order`
  that reports a value (numeric or text). Missing-flagged rows never win; nothing is blended or imputed; a cell nobody
  reports is absent (`countries_without_value` on maps). Each cell names source/dataset/indicator, provider code,
  unit, flags, raw sha256 + selector, table artifact + dataset key, its rank and how many declared sources report it.
* "Year" = calendar-year cells (`[Jan 1, Jan 1 next year)`); other periods appear in per-source views
  (`/series` → `per_source`), and `non_annual_cells` is counted in dossiers.
* Rows carrying a `concept_id` hint but not listed in `source_order` appear in `/series` per-source views with
  `in_source_order: false` and never become best values.

### Performance (10.1 M synthetic rows, 1,000 concepts × 2 sources, 250 countries × 45 years)

`uv run pytest -m perf tests/performance/test_atlas_perf.py -s` on the shared 4-core container (load average 9–12 from
other agents):

| step | time |
|---|---|
| snapshot DuckDB build (once per snapshot) | 50–80 s (275 MB) |
| country dossier, all 1,000 concepts, latest + full series (query layer) | 480–670 ms (target < 1 s) |
| world map (concept, year) | 17–26 ms (target < 500 ms) |
| series (concept, country) | 22–32 ms |
| coverage by country (concept) | 16–28 ms |

The HTTP dossier adds pydantic serialisation of every series point; use `?series=false` or `?domain=` for light
responses. Build the query file ahead of traffic with `--warm`.

## API (`src/reva/api/routes/atlas.py`, read scope)

* `GET /v1/atlas/concepts?domain&subdomain&q&limit&offset` — catalog (labels fr/en, definition, unit, source order,
  `review_status: provider_declared_not_human_reviewed`), invalid catalog files reported (`catalog_errors`).
* `GET /v1/atlas/countries/{iso3}?domain&series&origin&snapshot` — dossier: per domain, concepts with latest
  best value + annual series, concepts without data.
* `GET /v1/atlas/series?concept&iso3` — best annual series + every source's cells + declared sources without data.
* `GET /v1/atlas/map?concept&year` — best value per country, countries without value, counts by source.
* `GET /v1/atlas/coverage?domain&concept` — per concept/source coverage; with `concept`, per-country matrix.
* No snapshot → 404 `atlas_snapshot_unavailable` (typed, never an empty success); unknown country/concept → 404;
  fixture-origin snapshots carry the `fixture_data` warning. `contracts/openapi.json` regenerated (additive only).

## Budgets

`budget_merge.py` (dry run by default, `--write`) merges requests for **registered** sources only (invariant 30
requires the pinned keys to equal `registry/sources/*.yaml`), per source `max(current, max request)` capped at
5,000/day, pre-V3 limits never lowered; daily totals = 1.2.0 base + merged limits / declared bytes, capped at
20,000 requests and 3 GiB/day (raw downloads are archived on a ~20 GB disk). Each merge bumps `budgets-1.3.x` and the
digest is re-pinned in `src/reva/autonomy/policy.py` with a history note.

## Requests for other owners

1. **Belief releases pin atlas tables** (`src/reva/simulation/datasets.py`, `ELIGIBLE_SQL`, SIMFIX/lead): eligible
   datasets are not filtered by concept, so every belief release would pin every atlas table (`atlas.table`) as a
   dataset input. Requested: `AND concept_id <> 'atlas.table'` in `ELIGIBLE_SQL` (atlas layers stay out of reviewed
   releases; atlas snapshots pin them instead).
2. Seed (`src/reva/cli/seed.py`, GOV): after the `builtin_connectors` loop add
   `from reva.atlas.sources import register_atlas_connector_versions; register_atlas_connector_versions(conn, store, act)`.
3. Invariant 30 (`tests/invariants/test_invariant_30_budgets.py`): add to `BUDGETED`
   `"src/reva/atlas/ingest.py": ("reserve_ingest(", "metered(", "settle_metered(", "except BudgetExhausted")`.
   Atlas modules that still build their own clients must move to `run.http`.
4. CLI/jobs (lead/HOOK): `reva atlas ingest <source> [--dataset] [--fixture]` → `reva.atlas.ingest.main`,
   `reva atlas snapshot [--origin] [--warm]` → `reva.atlas.snapshot.main`; job kind `atlas_ingest` →
   `reva.atlas.ingest:handle_atlas_ingest` (and `atlas_snapshot` if scheduled snapshots are wanted; `ops.submit_job`
   must allow the job types).
5. NLQA's `llm.nlqa` request (an LLM token budget) needs a ledger function for `llm:nlqa:*` (migration); not merged.

## Tests

`tests/unit/test_atlas_tables.py`, `tests/unit/test_atlas_catalog.py` (also validates every registered catalog file),
`tests/integration/test_atlas_api.py` (registration, idempotency, refusals, snapshots, pinning, tamper, API),
`tests/integration/test_atlas_ingest.py` (registry, metered live run, budget refusal), `tests/performance/test_atlas_perf.py`.
