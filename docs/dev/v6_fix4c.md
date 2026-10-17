# V6 FIX4C — AUDIT3 fixes: M8, L1, L2, L4, L5, L6, L7, L9 and the §5 name-mapping inventory

Agent FIX4C, 2026-10-17. Input: `docs/dev/review_v4_v5_audit.md` (AUDIT3) §§ M8, L1, L2, L4, L5, L6, L7, L9, §5, and
the reports of V4INDUSTRY, V4LINK, V4CLIMATE, V4ECO, V4MIND, V4ENERGY, V4BIO. Out of scope (other fixers):
`src/reva/predict/**`, `src/reva/effects/**`, `src/reva/estimation/**` (FIX4B), health ontology connectors (FIX4A).
Every fix has a test; connector versions are bumped; DB changes are append-only (re-ingest; retractions carry the
loader's derived reason). Live runs were made under `flock /tmp/reva-devdb.lock` with free disk checked ≥ 5 GB before
each step (5.2–5.3 GB).

## Summary

| id | defect (AUDIT3) | fix | code / version | live |
|---|---|---|---|---|
| M8 | ambiguous LEI ↔ Wikidata links kept (193 LEIs on several items, 324 items with several LEIs) | V4LINK's identity rules (`registry/kg/identity_rules_v4.yaml#P1278`, engine `v4link_common.derive_bridges`) applied in `atlas.gleif_wikidata`: best rank only; LEI on several items and item with several LEIs are **not linked** and reported with samples | `atlas_gleif.py` 1.0.3 (`apply_identity_rules`, run option `replay_raw_sha256`) | LIVE_M8 |
| L1 | GLEIF parent edges carry the child record's registration status / corroboration as relationship qualifiers; `relationship_type` composed by the loader | the `entities` dataset fetches each child's Level 2 relationship record (`/lei-records/{lei}/{direct,ultimate}-parent-relationship`) and the parent edge carries the record's own `relationship_type`, `relationship_status`, `relationship_registration_status`, `relationship_corroboration_level`, corroboration documents and periods, citing the record's bytes; without the record: `relationship_record: not_fetched` and no relationship fields at all | `atlas_gleif.py` 1.0.3 | LIVE_L1 |
| L2 | Natural Earth admin-1 zip recorded with NOAA's licence (`meta.artifact.rights`, source key `noaa.ghcnm`) | (a) new spec `atlas.naturalearth_admin1` (source key `naturalearth.admin1_10m`, whose registry entry already says public domain); the GHCN-M run archives the NE zip under that spec, so a new NE file records NE's terms; (b) families API: when a statement's raw artifact was recorded for *another* source, the licence of the statement's own reference is served (basis `statement_reference (raw artifact rights recorded for noaa.ghcnm)`) | `atlas_ghcnm.py` 1.0.2, `kg/family_query.py` | read-side fix effective now; no re-fetch (see below) |
| L4 | FishStat sums include FAO "N" (not significant, < 0.5) cells unflagged | `flags.includes_not_significant`, `not_significant_cells`, `understated_by_less_than` (0.5 × N cells), `all_cells_not_significant` when every summed cell is N; FAO's definition quoted verbatim from `CL_FI_SYMBOL_SDMX.csv` and checked at each run | `atlas_fishstat.py` 1.0.2 | LIVE_L4 |
| L5 | OECD How's Life licence review only at table level | every cell carries `flags.licence_review = third_party_upstream_possible` + basis (no observation-level upstream is stated, see below) | `atlas_mind_oecd.py` 1.0.2 | LIVE_L5 |
| L6 | report drift | `v4_v4health.md`: WHO GHE was loaded live by the lead (620,004 rows, 14:16 UTC); `v6_plan.md`: 586 questions / 2,287 forecasts (AUDIT3 census); `v4_v4bio.md` placeholder: see below | docs | — |
| L7 | ~111k HGNC edges (and others) to never-loaded entities | counted (below): the HGNC targets are **all loaded now** (V4BIO's UniProt / Ensembl / taxa loads); the remaining dangling edges are marked in the families API (`neighbour.entity_loaded = false` + a note) | `kg/family_query.py`, `api/routes/kg_families.py`, contract | count below |
| L9 | 1,657 NOAA STAR sea-level ~10-day cycle means stored as 1-day periods | each row is an interval around the stated decimal-year time (half-way to the neighbouring stated times of the same mission; contiguous, never overlapping), `time_precision: altimetry cycle`, stated time kept in flags + selector | `atlas_climate_indices.py` 1.0.1 | LIVE_L9 |
| §5 | USGS facility countries mapped by name | country = GENC trigraph prefix of the USGS Facility ID (identifier), name kept verbatim and cross-checked; other curated mappings listed for maintainer review in `registry/curation/name_based_mappings.yaml` | `atlas_usgs_facilities.py` 1.0.2 | LIVE_S5 |

## Details

LIVE_DETAILS

## Tests

* `tests/unit/test_v4industry.py` (+3: identity rules on the real capture — Siemens AG's LEI `W38RGI023J3WT1HWRP32`
  is on Q16984598 and Q81230 → not linked; synthetic ambiguity/rank cases; parent edges from relationship records vs
  the child record, e.g. child `PARTIALLY_CORROBORATED` vs relationship `FULLY_CORROBORATED`).
* `tests/integration/test_v4industry_ingest.py` (+1: pre-fix load then replay of the same archived bytes → 2
  retractions `reclassified_by_reva` / basis `raw_unchanged`, 0 inserts; relationship-record qualifiers in the DB).
  Fixtures: two complete live GLEIF relationship records (CC0) added to `tests/fixtures/source/atlas.gleif/`.
* `tests/unit/test_fix4c.py` (families rights choice; FishStat N flags).
* `tests/integration/test_v4climate_ingest.py` (NE artifact rights = Natural Earth, source key
  `naturalearth.admin1_10m`, no NOAA text).
* `tests/unit/test_atlas_mind.py` (per-cell licence review), `tests/unit/test_atlas_climate_indices.py` (STAR
  intervals contiguous, 5–20 days, stated day inside), `tests/unit/test_atlas_v4energy.py` (country by GENC prefix,
  0 name cross-check differences), `tests/integration/test_kg_families_api.py` (`entity_loaded`), contract
  `contracts/openapi.json` + `ui/src/api/openapi.gen.ts` regenerated (`FamilyNeighbourRef`).
* ruff, ruff format, mypy clean on every changed module; budget invariant and atlas catalog tests pass.

## Not done / owner decisions

* **L2 existing artifact.** `meta.artifact` is content-addressed and append-only: the NE zip already archived
  (efc59726…) keeps the NOAA rights row; re-fetching the same bytes cannot change it. The served licence is corrected
  by the families API rule above; a new Natural Earth release will be archived under NE's own terms. No GHCN-M
  re-run was made (60 MB, nothing would change in the KG).
* **L5 per measure.** Neither the SDMX-CSV rows nor the data structure (`DSD_HSL` 1.1, probed with
  `references=all`) carry an observation-level source; the OECD documents upstream surveys per measure in its
  referential metadata (`MSD_REF_METADATA`) and publications. Narrowing the reservation to the Gallup-sourced
  measures needs that metadata archived and reviewed (owner decision 2 of V4MIND stands).
* **L6 V4BIO placeholder.** V4BIO's report has its live section but still contains the line
  `LIVE_RESULTS_PLACEHOLDER`; a V4BIO Wikidata run (`atlas.bio_wikidata`) was still in progress during FIX4C, so the
  file was left to V4BIO, as instructed.
* **L7 remaining dangling edges** (`gleif:gem_entity` → `gem:*`, GloBI → GBIF keys outside the backbone selection,
  Mondo `in_taxon` → NCBI taxa not loaded): they wait for the owners' loads; the API now says so per neighbour.
* **§5 curated mappings** (Hofstede aliases, effect-estimate country codes, CHI/XKX by enwiki title, causal markers,
  EML anchor label, Carbon Brief / RESOLVE name-keyed ids) are listed with size and review points in
  `registry/curation/name_based_mappings.yaml` (status `pending_maintainer_review`); none was changed here.
* Two GLEIF relationship-record probes and one OECD structure probe were made with curl (generic project UA, public
  CC0 / OECD endpoints) to learn the response shapes before writing the connector code; the relationship captures
  became fixtures. All DB-loaded bytes came through the metered connector runs.

## Commands (to refresh on a bigger machine)

```bash
cd /home/user/REVA-World
flock /tmp/reva-devdb.lock uv run reva seed
# M8 without a WDQS request (re-derives from the archived 2026-10-17 response):
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gleif_wikidata \
    --option replay_raw_sha256=d99357ad16d5342bc8c5d03d186a0c13f818bb086ffd6de142fc5708ee5abffd
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gleif --dataset entities      # + relationship records
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.fishstat
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.oecd.wellbeing
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.climate_indices
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.usgs_myb_facilities
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.naturalearth_admin1          # next NE release only
uv run pytest -q tests/unit/test_fix4c.py tests/unit/test_v4industry.py tests/integration/test_v4industry_ingest.py
```
