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
| `period_start` | date | no | half-open `[start, end)`: year 2020 = `2020-01-01`..`2021-01-01` (`year_period(2020)`). **Astronomical years** (year 0 = 1 BCE, -9999 = 10000 BCE, as in the KG): build BCE periods with `frame_with_year_periods(rows, year_key=...)` / `year_period_exprs(col)` (Python `date` stops at year 1) |
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

* **Raw bytes first.** Archive each raw download as an `evidence.acquisition` with the same
  `origin` — inside an atlas run simply `run.archive(raw)` (section 7) — and pass the raw sha256s.
  Unarchived raw → `ValidationProblem`.
* Validation refuses (typed `ValidationProblem`, nothing written): wrong/missing/extra columns or
  types, nulls in required columns, `source`/`dataset` mismatch, non-strict decimals, value-less rows
  without `flags.missing`, `period_start >= period_end`, invalid JSON flags/selector, duplicate cells,
  `raw_sha256` not in `raw_sha256s`, `iso3` that is not a crosswalk key or disagrees with `geo_scheme`.
* Unmapped provider geographies (aggregates, regions, historical areas; Taiwan only for tables
  registered before crosswalk 1.1.0, which added `TWN`) are **kept** with `iso3 = null` and listed in the manifest `coverage.unmapped_provider_geo`.
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

## 7. Atlas sources and the single ingest entry point (lead decision, replaces the template workaround)

`ConnectorSpec.template` stays closed (it is inside the digest-pinned autonomy template closure) and
atlas connectors are **not** `builtin_connectors()`. Instead (`src/reva/atlas/sources.py`,
`src/reva/atlas/ingest.py`):

```python
# src/reva/adapters/connectors/atlas_imf.py  (discovered by module name: atlas_*.py, nothing to register)
from reva.atlas.sources import AtlasSourceSpec

def atlas_source() -> AtlasSourceSpec:          # or a list of specs (only this function name counts)
    return AtlasSourceSpec(
        name="atlas.imf", version="1.0.0",       # connector name (atlas.*) + version (bump on change)
        source_key="imf.sdmx",                   # registry/sources/*.yaml key (also the budget key)
        atlas_source="imf",                      # atlas table `source` (no dots)
        provider="International Monetary Fund", licence="...",
        allowed_hosts=("api.imf.org",), datasets=("weo", "bop"), dataset_ids=("IMF.RES:WEO",),
        cadence_seconds=30 * 86400,
        planned_requests=40,                     # reserved before the run; extra requests are topped up or refused
        max_response_bytes=300_000_000,          # <= 1 GiB per response (bulk files); daily bytes still apply
        min_interval_seconds=1.0,                # politeness per host
        parser_version="atlas_imf/1", code_digest="sha256:...",  # optional
        budget_key="",                           # default = source_key; set to share another source's budget
    )

async def atlas_ingest(run) -> dict:             # run: reva.atlas.ingest.AtlasRun
    for ds in ("weo", "bop"):
        if not run.wants(ds):                    # honours --dataset
            continue
        raw = await run.get(url, key=f"{ds}:2025-10", media_types=("application/xml",), headers={...})
        sha = run.archive(raw)                   # evidence.acquisition of connector atlas.imf@1.0.0
        frame = parse(raw.body, sha)             # polars frame in ATLAS_SCHEMA
        run.register(dataset=ds, frame=frame, raw_sha256s=[sha], geo_scheme="iso3", coverage_note="...")
    return {"datasets": [...]}                   # anything JSON-able
```

* **Never build an HTTP client in an atlas module.** `run.http` (and `run.get`) is the
  SafeHttpClient restricted to `allowed_hosts`, wrapped in the budget meter (live), or
  `FixtureHttp("<name>")` replaying `tests/fixtures/source/<name>/manifest.json` (fixture mode).
  Invariant 30 (`test_every_live_provider_request_path_is_budgeted`) flags any module that creates
  a client (`make_live_client(`, `SafeHttpClient(`, `httpx.*Client(`, `urlopen(`) outside a budgeted path.
* Run it: `uv run python -m reva.atlas.ingest atlas.imf [--dataset weo] [--fixture]`
  (`--list` shows every discovered source and modules that failed to import); worker job handler
  `reva.atlas.ingest:handle_atlas_ingest` (payload `{"source", "dataset"?, "mode"}`); requested CLI
  alias `reva atlas ingest <source>` and job kind `atlas_ingest` (lead-owned files).
* Live flow: `reserve_ingest(budget_key, planned_requests)` before any request (refused →
  `BudgetExhausted`, nothing fetched) → metered requests → `settle_metered` with actual usage
  (also on failure). An in-run budget refusal ends the run with `status: "budget_exhausted"`; what was
  archived/registered so far stays (each table registration is atomic).
* Connector versions: atlas specs are registered like built-in connectors (owner role). In tests /
  throwaway databases call `reva.atlas.sources.ensure_registered(owner_engine, store, spec)`; for the
  seeded deployment the lead adds `register_atlas_connector_versions(conn, store, act)` to the seed.
* Tests: see `tests/integration/test_atlas_ingest.py` (fake live client via `http_factory=`).

### 7.1 Additions (ATLASOPS, 2026-10-15)

* `run.get(url, key=, media_types=, headers=, max_bytes=, params=)`: `params` stay on the acquisition's
  item (bookkeeping, not sent). `run.get_range(url, key=, start=, end= | suffix=, if_range=)`: one HTTP
  byte range, metered like `get`, only `206` accepted, fingerprint includes the range, metadata keeps
  `content-range` (WID reads an 882 MB zip this way). Fixture mode serves byte ranges from a manifest
  object `{"kind": "byte_range_archive", "url", "file", "sha256", "etag"}`
  (`reva.atlas.ingest.ArchiveRangeFixtureHttp`).
* `run.post(url, key=, json_body= | content=, content_type=, media_types=, headers=, max_bytes=, params=)`:
  one POST for POST-only APIs (PxWeb of Greenland/Faroe/Kosovo ASK/Åland/Andorra, IRENA, UNECE). Same
  allow-list, pinning, politeness, size caps and retries as GET (`reva.atlas.http_post.safe_post` through the
  run's SafeHttpClient; redirects are not followed), metered on the run's reservation exactly like a GET
  (one request; request + response bytes). The request body (≤ 64 KiB; `json_body` is sent as canonical JSON)
  is archived with the response in `response_metadata.request` (method, content type, body text or base64,
  sha256) and the fingerprint covers it (`POST <url> body:<sha256>`), so each query is its own acquisition.
  Fixture mode replays a FixtureHttp manifest entry whose `request_fingerprint` is that POST fingerprint.
  A live run never POSTs outside the metered client.
* Run options: `run.options` (CLI `--option KEY=VALUE`, job payload `"options"`), e.g. WID
  `countries=FR,DE` for a country-subset run.
* `AtlasRun.offline(spec, http)`: a fetch-only run for parser unit tests (no archive/register).
* `run_atlas_ingest` refuses a source whose connector version is not registered, or is registered with a
  different `code_digest` ("bump the version"): acquisitions always name the code that produced them. The
  seed registers every discovered atlas source; a conflicting one is skipped and logged, never fatal.
* CLI `reva atlas list | ingest <source> [--dataset] [--fixture] [--option] | snapshot [--warm]
  [--skip-unchanged]`; job kinds `atlas_ingest` / `atlas_snapshot` (migration `0010_atlas_ops` adds them to
  `ops.submit_job`), scheduled monthly / daily by the seed (`docs/dev/v3_atlasops.md`).
* Themes: `registry/atlas/themes.yaml` maps every catalog concept to one REVA theme (optional per-concept
  `theme:` field in the catalog schema overrides the rules). Cross-source equivalences ("REVA headline
  concepts") live in `registry/atlas/source_order/<theme>.yaml` (`docs/dev/v3_atlasops.md`).

