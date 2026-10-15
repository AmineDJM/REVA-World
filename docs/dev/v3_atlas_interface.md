# V3 Atlas — interface for atlas connector agents (ATLASCORE)

Status: **stable interface** (`src/reva/atlas/tables.py`). Code against it now. Changes will be
additive only. Questions/needs: write them in your report; budgets in
`registry/atlas/budget_requests/<agent>.yaml` (format below).

## 1. Produce an atlas table

One *atlas table* = one provider dataset (`source` × `dataset`) in a fixed long schema, one row per
reported cell. Build a `polars.DataFrame` with exactly these columns (`reva.atlas.tables.ATLAS_SCHEMA`):

| column | type | null? | meaning |
|---|---|---|---|
| `source` | str | no | short provider key, `[a-z0-9][a-z0-9_-]{0,23}`, **no dots** (`wb`, `imf`, `ilo`, `who`, `owid`, `vdem`…) |
| `dataset` | str | no | short dataset key, `[a-z0-9][a-z0-9_-]{0,38}`, no dots (`wdi`, `weo_2025_10`, `ilostat_une`) |
| `indicator` | str | no | provider indicator code, extra dimensions folded in: `UNE_RT\|sex=F\|age=Y15-24` |
| `concept_id` | str | yes | optional hint of the catalog concept (the catalog `source_order` is authoritative) |
| `iso3` | str | yes | crosswalk entity key; fill with `geo_scheme=` or `country_iso3()`; null = unmapped (kept) |
| `provider_geo` | str | no | the provider's own geography code, verbatim |
| `entity_qid` | str | yes | Wikidata QID `Q…` when known |
| `period_start` | date | no | half-open `[start, end)`: year 2020 = `2020-01-01`..`2021-01-01` (`year_period(2020)`) |
| `period_end` | date | no | exclusive end |
| `value` | str | yes | **strict decimal string** as reported: `-12.5`, `3e-4`. No `1,000`, no `NaN`, never a filled zero |
| `value_text` | str | yes | non-numeric reported value (category, `"Yes"`, legal text excerpt ≤ 4000 chars) |
| `unit` | str | no | provider unit as declared (`%`, `current US$`, `persons`, `years`, `1`) |
| `flags` | str (JSON object) | no | provider flags/notes, `{}` if none. A row with **no value and no value_text must carry `{"missing": "<reason>"}`** (`missing_flags(reason)`; reasons: `not_reported`, `confidential`, `not_applicable`, `not_available`, `unreliable`, `provider_missing_code`). Prefer simply omitting cells the provider does not report. |
| `raw_sha256` | str | no | sha256 of the archived raw artifact this cell was parsed from |
| `selector` | str (JSON object) | no | non-empty locator of the cell in that raw artifact: `{"file":"WDICSV.csv","row":1234,"column":"2020"}` |

Uniqueness: `(indicator, provider_geo, period_start, period_end)` is unique per table.
Helpers: `year_period(y)`, `missing_flags(reason, **extra)`, `json_obj(**kw)`, `empty_atlas_frame()`.

## 2. Register it

```python
from reva.atlas.tables import register_atlas_table, country_iso3, year_period, json_obj, missing_flags

summary = register_atlas_table(
    worker_engine, store,
    source="wb", dataset="wdi",
    frame=df,                       # polars.DataFrame in ATLAS_SCHEMA
    raw_sha256s=[raw_sha],          # every raw artifact used; each must be an evidence.acquisition
    parser_version="atlas_wb_wdi/1",
    origin="live",                  # or "test_fixture" (tests)
    coverage_note="WDI bulk CSV; 217 economies + 49 aggregates (aggregates unmapped, kept)",
    geo_scheme="wb",                # optional: map provider_geo -> iso3 through the crosswalk
)
# -> {dataset_id, dataset_key "atlas.wb.wdi", artifact_sha256, manifest_sha256, row_count,
#     created, unmapped_provider_geo [{provider_geo, rows}], coverage {...}}
```

* **Raw bytes first.** Archive each raw download as an `evidence.acquisition` (with your
  connector's spec through `reva.evidence.ingest.publish_acquisition`, zero observation batches is
  fine) with the same `origin`; pass the raw sha256s. Unarchived raw → `ValidationProblem`.
* Validation refuses (typed `ValidationProblem`, nothing written): wrong/missing/extra columns or
  types, nulls in required columns, `source`/`dataset` mismatch, non-strict decimals, value-less rows
  without `flags.missing`, `period_start >= period_end`, invalid JSON flags/selector, duplicate cells,
  `raw_sha256` not in `raw_sha256s`, `iso3` that is not a crosswalk key or disagrees with `geo_scheme`.
* Unmapped provider geographies (aggregates, regions, historical areas, Taiwan — not in the ALLC
  crosswalk) are **kept** with `iso3 = null` and listed in the manifest `coverage.unmapped_provider_geo`.
* Idempotent: identical content → `created: False`. Changed content (new vintage) → a new version
  of `atlas.<source>.<dataset>`; the atlas snapshot uses the latest version recorded at its cutoff.
* Stored as content-addressed Parquet (zstd) + JSON manifest via
  `reva.simulation.datasets.register_dataset` (`evidence.dataset`, `concept_id = "atlas.table"`).
  Keep one table ≤ ~5 M rows; split huge datasets by theme (`wdi_a`, `wdi_b`) if needed.

## 3. Country codes

`country_iso3(code, scheme) -> str | None` returns the crosswalk entity key (ISO 3166 alpha-3; `XKX`
Kosovo and `CHI` Channel Islands are the two non-ISO World Bank entities) or `None`.

| scheme | from | notes |
|---|---|---|
| `iso3` | crosswalk | also accepts `XKX`, `CHI` |
| `iso2` | crosswalk | |
| `m49` (= ISO numeric; `"004"` == `"4"`) | crosswalk | |
| `wb` | crosswalk `worldbank` | aggregates (WLD, EUU, HIC…) → `None` |
| `comtrade` | crosswalk `comtrade_reporter` | FRA = 251 |
| `eurostat` | `country_codes_extra.json` | ISO2 except `EL`→GRC, `UK`→GBR, `XK`→XKX (Eurostat glossary, quoted) |
| `fips` | `country_codes_extra.json` | FIPS 10-4 from archived GeoNames countryInfo |

Providers using ISO3 (ILO, WHO, OWID, IMF WEO, UNDP, V-Dem…) use `iso3`. **Provider-specific
non-ISO codes** (e.g. a Kosovo or "former" code) go into your own file
`registry/atlas/geo_aliases/<agent>.yaml` — never guessed, always with evidence:

```yaml
scheme: owid            # new scheme name you then pass as geo_scheme="owid"
base: iso3              # inherit all crosswalk iso3 codes
aliases:
  - {code: OWID_KOS, iso3: XKX, source: "OWID CO2 dataset row 'Kosovo' iso_code OWID_KOS (raw sha256 …)"}
source: {kind: provider_data, url: "https://…"}
```

## 4. Catalog (`registry/atlas/catalog/<domain>.yaml`)

```yaml
domain: labour
concepts:
  - id: labour.unemployment_rate          # unique across all files; [a-z0-9][a-z0-9_.-]*
    labels: {fr: "Taux de chômage", en: "Unemployment rate"}
    definition: "Share of the labour force without work but available for and seeking employment (ILO)."
    unit: "%"
    domain: labour                         # must equal the file's domain
    subdomain: unemployment
    source_order:                          # best-available order: "<source>.<dataset>.<indicator>"
      - "ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT|sex=SEX_T|age=AGE_YTHADULT_YGE15"
      - "wb.wdi.SL.UEM.TOTL.ZS"
    provider_declared: true                # always true: provider definitions, not human-reviewed
    notes: "ILO modelled estimates differ from national estimates; both shown per source."
```

Schema: `registry/atlas/catalog/_schema.json`. Only list series whose definitions are the same concept
(same unit and definition). The "best available" cell is the first source in `source_order` that
reports the (country, year); it always names its source and flags. No blending, no imputation.

## 5. Budgets (`registry/atlas/budget_requests/<agent>.yaml`)

Only ATLASCORE edits `registry/policies/budgets.yaml`. Request what you need:

```yaml
agent: A-WB
requests:
  - source_key: worldbank.wdi_bulk   # the source_key of your connector's registry/sources entry
    daily_requests: 20
    expected_bytes_per_day: 300000000
    reason: "one bulk ZIP (WDI_CSV.zip ~ 280 MB) + metadata; resumable"
```

ATLASCORE merges requests periodically (conservatively: within the daily total/byte caps), re-pins
`budgets.yaml`, and notes the change in `src/reva/autonomy/policy.py`. Until merged, a live run for
an unlisted source is refused (fail closed) — test with fixtures meanwhile.

## 6. Reading (for LINKS, NLQA, UI3)

`reva.atlas.query` (DuckDB over the snapshot's pinned Parquet files) and `GET /v1/atlas/*`:
`/concepts`, `/countries/{iso3}` (dossier), `/series?concept&iso3`, `/map?concept&year`, `/coverage`.
See `docs/dev/v3_atlascore.md`.
