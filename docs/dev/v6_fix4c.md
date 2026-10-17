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
| M8 | ambiguous LEI ↔ Wikidata links kept (193 LEIs on several items, 324 items with several LEIs) | V4LINK's identity rules (`registry/kg/identity_rules_v4.yaml#P1278`, engine `v4link_common.derive_bridges`) applied in `atlas.gleif_wikidata`: best rank only; LEI on several items and item with several LEIs are **not linked** and reported with samples | `atlas_gleif.py` 1.0.3 (`apply_identity_rules`, run option `replay_raw_sha256`) | **done** (load 480, replay of the archived WDQS response, no request): 52,458 links kept (`unchanged`), **1,005 retracted** (`reclassified_by_reva`, basis `raw_unchanged`) |
| L1 | GLEIF parent edges carry the child record's registration status / corroboration as relationship qualifiers; `relationship_type` composed by the loader | the `entities` dataset fetches each child's Level 2 relationship record (`/lei-records/{lei}/{direct,ultimate}-parent-relationship`) and the parent edge carries the record's own `relationship_type`, `relationship_status`, `relationship_registration_status`, `relationship_corroboration_level`, corroboration documents and periods, citing the record's bytes; without the record: `relationship_record: not_fetched` and no relationship fields at all | `atlas_gleif.py` 1.0.3 | **queued** (disk fell to 4.2 GB before the step; code + fixtures + DB test done) |
| L2 | Natural Earth admin-1 zip recorded with NOAA's licence (`meta.artifact.rights`, source key `noaa.ghcnm`) | (a) new spec `atlas.naturalearth_admin1` (source key `naturalearth.admin1_10m`, whose registry entry already says public domain); the GHCN-M run archives the NE zip under that spec, so a new NE file records NE's terms; (b) families API: when a statement's raw artifact was recorded for *another* source, the licence of the statement's own reference is served (basis `statement_reference (raw artifact rights recorded for noaa.ghcnm)`) | `atlas_ghcnm.py` 1.0.2, `kg/family_query.py` | read-side fix effective now; no re-fetch (see below) |
| L4 | FishStat sums include FAO "N" (not significant, < 0.5) cells unflagged | `flags.includes_not_significant`, `not_significant_cells`, `understated_by_less_than` (0.5 × N cells), `all_cells_not_significant` when every summed cell is N; FAO's definition quoted verbatim from `CL_FI_SYMBOL_SDMX.csv` and checked at each run | `atlas_fishstat.py` 1.0.2 | **queued** (disk < 5 GB); verified offline on the archived live release: 26,502 of 136,317 sums include N cells, 2,903 are all-N |
| L5 | OECD How's Life licence review only at table level | every cell carries `flags.licence_review = third_party_upstream_possible` + basis (no observation-level upstream is stated, see below) | `atlas_mind_oecd.py` 1.0.2 | **queued** (disk < 5 GB) |
| L6 | report drift | `v4_v4health.md`: WHO GHE was loaded live by the lead (620,004 rows, 14:16 UTC); `v6_plan.md`: 586 questions / 2,287 forecasts (AUDIT3 census); `v4_v4bio.md`: leftover placeholder line removed (V4BIO finished) | docs | — |
| L7 | ~111k HGNC edges (and others) to never-loaded entities | counted (below): the HGNC targets are **all loaded now** (V4BIO's UniProt / Ensembl / taxa loads); the remaining dangling edges are marked in the families API (`neighbour.entity_loaded = false` + a note) | `kg/family_query.py`, `api/routes/kg_families.py`, contract | count below |
| L9 | 1,657 NOAA STAR sea-level ~10-day cycle means stored as 1-day periods | each row is an interval around the stated decimal-year time (half-way to the neighbouring stated times of the same mission; contiguous, never overlapping), `time_precision: altimetry cycle`, stated time kept in flags + selector | `atlas_climate_indices.py` 1.0.1 | **queued** (disk < 5 GB) |
| §5 | USGS facility countries mapped by name | country = GENC trigraph prefix of the USGS Facility ID (identifier), name kept verbatim and cross-checked; other curated mappings listed for maintainer review in `registry/curation/name_based_mappings.yaml` | `atlas_usgs_facilities.py` 1.0.2 | **queued** (disk < 5 GB) |

## Details

### M8 — LEI ↔ Wikidata (live, `atlas.gleif_wikidata` 1.0.3)

Replay of raw `d99357ad…` (WDQS response of 2026-10-17 14:18:50 UTC, 53,523 rows; 60 invalid LEIs skipped as before):
53,463 candidate P1278 statements → rules → **52,458 linked** (52,458 LEIs, 52,458 items: now one-to-one).
Not linked, reported: 21 not best rank (an item's normal-rank LEI next to a preferred one), 357 statements whose LEI is held
by several items (AUDIT3: 193 LEIs), 626 statements of items holding several LEIs (AUDIT3: 324 items), 0 malformed. Examples: `lei:315700A86BCQXN3PYJ02` on Q102261577 and Q24287827;
`lei:097900CAGQ0000242376` on Q102537253 and Q1660948; in the test capture Siemens AG `lei:W38RGI023J3WT1HWRP32` on
Q16984598 and Q81230. The 1,005 previously current statements were retracted append-only in load 480 with reason
`reclassified_by_reva` / basis `raw_unchanged` (the source still states them; REVA's rule no longer links them);
kept statements are byte-identical (`unchanged`), so their revision and retrieval time are those of the original fetch.
The run report (`build.identity_rules`) lists counts and samples; load parameters record
`identity_rules: registry/kg/identity_rules_v4.yaml#P1278` and `replay_of_archived_raw`. Effect downstream: the
`entities` dataset seeds only unambiguous P1278 LEIs from now on; V4INDUSTRY's QID → LEI → GEM chains only cross
one-to-one bridges.

### L1 — GLEIF parent edges

Live shape checked on Siemens Healthcare (Private) Ltd `lei:529900IR3Y1XBIR1GX29`: the child record says
`registration.corroborationLevel = PARTIALLY_CORROBORATED`, its direct-parent relationship record says
`IS_DIRECTLY_CONSOLIDATED_BY`, `status ACTIVE`, registration `PUBLISHED`, `FULLY_CORROBORATED`
(`ACCOUNTS_FILING`), periods ACCOUNTING / RELATIONSHIP / DOCUMENT_FILING — exactly the confusion AUDIT3 found. From
1.0.3 the parent statement is built from the relationship record (its bytes are the statement's raw, the include
page is a second reference); the end node must equal the parent LEI from `include`, otherwise the record is not used
and counted. Cost: one request per child with an LEI parent (455 on 2026-10-17; option `max_relationship_records`,
default 1,000; `relationship_records=false` turns it off). The 455 current edges keep the 1.0.1 qualifiers until the
next `entities` run (queued, below): that run stores them as new revisions (new Golden Copy revision).

### L2 — Natural Earth rights

`atlas.ghcnm` now returns two specs; the NE zip is archived through `atlas.naturalearth_admin1` (source key
`naturalearth.admin1_10m`, rights from `registry/sources/naturalearth_admin1_10m.yaml`: "Public domain (Natural Earth
terms of use)", "Made with Natural Earth"). `reva seed` registered both versions (1.0.2). In the families API the
16,721 Natural Earth statements now show "Public domain (Natural Earth)" (their own reference) instead of NOAA's terms;
statements of the source that recorded the artifact keep the artifact rights, and a statement without its own licence
keeps the artifact's (possibly more restrictive) rights.

### L4 — FishStat "not significant"

FAO `CL_FI_SYMBOL_SDMX.csv` (in the release zip): N = "Not significant (<0.5)", "Used to indicate a value which is not
a "real" zero (e.g. a result of 0.0004 rounded to zero)." The archived live release has 35,674 N cells, all `0`.
Re-aggregated offline from the archived bytes (`90aee535…`) with 1.0.2: 26,502 of the 136,317 sums contain N cells
(flag `includes_not_significant`, count, `understated_by_less_than` = 0.5 × N), 2,903 sums consist only of N cells
(`all_cells_not_significant`: the 0 means "less than 0.5 per cell", not zero production). Values are unchanged.

### L5 — OECD How's Life per cell

1.0.2 writes `flags.licence_review = "third_party_upstream_possible"` and its basis on every cell of
`oecd.hsl_mind_current` / `oecd.hsl_mind_social_capital` (73,953 cells at the next run). No cell is shown as cleared,
because no observation-level upstream is published (see owner decisions).

### L7 — edges to entities not loaded (live count, `kg.statement_current`, origin live, 2026-10-17 ~20:40 UTC)

| predicate | statements | to unloaded entities | distinct unloaded targets |
|---|---:|---:|---:|
| `hgnc:in_taxon` | 45,277 | 0 | 0 |
| `hgnc:located_on_chromosome` | 45,277 | 0 | 0 |
| `hgnc:encodes` | 20,377 | 0 | 0 |
| `gleif:gem_entity` | 290 | 290 | 290 (`gem:*`, no GEM entity loader) |
| `globi:resolved_within` | 141 | 40 | 13 (GBIF keys outside V4BIO's backbone selection) |
| `mondo:in_taxon` | 2,247 | 148 | 43 (NCBI taxa not loaded) |

The ~111k HGNC edges AUDIT3 found now all resolve: V4BIO loaded `ncbitaxon:9606`, the Ensembl karyotype
(`chromosome:9606:*`) and 20,431 human UniProt proteins after the audit. The remaining 478 dangling edges are shown with
`neighbour.entity_loaded = false` and a note in `/v1/kg/entities/{id}/families`; nothing is invented to fill them.

### L9 — sea level

Each STAR row (one value per altimetry cycle and mission) gets the interval half-way to its neighbours of the same
mission (first/last mirror the single neighbour gap, flagged `interval_edge`); the stated decimal year and its day are
kept in `flags` (`stated_time_decimal_year`, `stated_day`) and the selector; `time_precision: altimetry cycle`;
basis quoted in `flags.period_basis`. The intervals are contiguous and 5–20 days long on the fixture (~9–10 days).
EPA annual rows are unchanged.

### §5 — USGS facility countries

`usgs_myb:country` now comes from the Facility ID's GENC trigraph (e.g. `usgs_myb:MMR0336` → MMR → Q836 Myanmar), with
qualifiers `genc_trigraph`, `basis`, and the verbatim name `usgs_country` ("Burma") as a cross-check only (counter
`country_name_crosscheck_differs`, 0 on the fixture; AUDIT3 found 0 of 12,027 live). The statement id changes to
`fac:<id>:country_by_id`, so the next run retracts the 12,027 name-based statements (`reclassified_by_reva`, same CSV
bytes) and inserts the id-based ones — same answers, by identifier. The other six name-based items of §5 are documented
as curator decisions in `registry/curation/name_based_mappings.yaml` for maintainer review.

### L6 — report drift

`docs/dev/v4_v4health.md` (status table, raw archive line, gaps list) now says WHO GHE was loaded live by the lead
(`atlas.who.ghe`, 620,004 rows, registered 14:16 UTC); `docs/roadmap/v6_plan.md` says 586 sealed questions / 2,287
baseline forecasts (AUDIT3 census, 16:50 UTC). `docs/dev/v4_v4bio.md`: the stray `LIVE_RESULTS_PLACEHOLDER` line
was removed — V4BIO had finished (its live-load section is committed and no V4BIO run was active at 21:20 UTC).

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
* **L7 remaining dangling edges** (`gleif:gem_entity` → `gem:*`, GloBI → GBIF keys outside the backbone selection,
  Mondo `in_taxon` → NCBI taxa not loaded): they wait for the owners' loads; the API now says so per neighbour.
* **§5 curated mappings** (Hofstede aliases, effect-estimate country codes, CHI/XKX by enwiki title, causal markers,
  EML anchor label, Carbon Brief / RESOLVE name-keyed ids) are listed with size and review points in
  `registry/curation/name_based_mappings.yaml` (status `pending_maintainer_review`); none was changed here.
* Two GLEIF relationship-record probes and one OECD structure probe were made with curl (generic project UA, public
  CC0 / OECD endpoints) to learn the response shapes before writing the connector code; the relationship captures
  became fixtures. All DB-loaded bytes came through the metered connector runs.

## Queued live runs (free disk fell to 4.2 GB after the M8 replay; the 5 GB rule stopped the rest)

`reva seed` already registered every new version (gleif 1.0.3, ghcnm + naturalearth_admin1 1.0.2, fishstat 1.0.2,
oecd.wellbeing 1.0.2, climate_indices 1.0.1, usgs_myb_facilities 1.0.2). When `df -h /` shows ≥ 5 GB:
`/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/fix4c_live.sh` (skips the seed and
the replay safely: both are idempotent) or the commands below. Expected raw growth: GLEIF ~22 MB (record pages +
455 relationship records), climate indices ~1.5 MB; FishStat, How's Life and USGS re-fetch the same files
(deduplicated if unchanged).

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
