# V6SIGNALS — daily world event/news signals (GDELT), as predictors only

Agent V6SIGNALS, 2026-10-17. Roadmap: `docs/roadmap/v6_plan.md` (V6SIGNALS row). Rules: V4 common rules (no
fabricated values, typed Missing, provenance to raw bytes, licence per value, ≤ 100 MB raw archive for V6 agents,
no live ingest below 5 GB free disk).

> **Every value here is a media-coverage signal, not an event count.** GDELT machine-codes news articles. A
> "riot in X" record is a sentence that GDELT's coder read as a riot located in X — possibly a duplicate,
> mis-geolocated, about an older event, or not an event at all. The tables measure what the monitored news media
> published. They are predictors for the forecast registry. They are never evidence that an event happened, and
> they are never a count of events. Each row carries
> `flags.signal_kind = "media-coverage signal, not event counts"`. The coverage note of each table starts with
> `MEDIA-COVERAGE SIGNAL, NOT EVENT COUNTS`.

## 0. For PRED-CONFLICT and PRED-FIN (read this first)

| Atlas table | Grain | Built by | What to use |
|---|---|---|---|
| **`gdelt.events_cd`** | country (FIPS 10-4 `provider_geo`, `iso3` via crosswalk) × UTC day | `atlas.gdelt_events` (source key `gdelt.events2`) | Conflict: `records_quad4` (material conflict), `records_quad3` (verbal conflict), `records_root14` PROTEST, `records_root17` COERCE, `records_root18` ASSAULT, `records_root19` FIGHT, `records_root20` mass violence, `goldstein_mean`, `avgtone_mean`. Normalise by the same row's `records_all` or the day's `_ALL` / `records_all`. Finance: `avgtone_mean`, `goldstein_mean`, `records_all` for a country. |
| **`gdelt.doc_theme_d`** | world (`provider_geo` `ALL_SOURCES`) × UTC day, since 2017-01-01 | `atlas.gdelt_doc` (source key `gdelt.doc2`) | Finance: `articles_econ_inflation`, `articles_econ_interest_rates`, `articles_econ_centralbank`, `articles_econ_stockmarket`, `articles_econ_currency_exchange_rate`, `articles_econ_debt`, `articles_econ_bankruptcy`, `articles_econ_oilprice`, `articles_econ_trade_dispute`, `articles_epu_uncertainty`, `articles_epu_cats_monetary_policy` and their `tone_*`. Conflict: `articles_armedconflict`, `articles_kill`, `articles_terror`, `articles_protest`, `articles_military`, `articles_rebellion`, `articles_unrest_belligerent`, `articles_ceasefire`, `articles_sanctions`, `articles_refugees` and `tone_*`. Divide `articles_<t>` by `monitored_<t>` (the API's daily total) to get a share. |

* **No look-ahead.** `events_cd` day *d* uses only the GDELT update stamped *d* 12:00:00 UTC. It is aggregated by
  `DATEADDED` (when GDELT first saw the record) and never by the event date, so it can be used from *d* 12:15 UTC.
  `doc_theme_d` keeps complete UTC days only and can be used from *d+1*. `flags.as_of` is the fetch date. The DOC
  API may change past values, and a re-fetch that changes a value registers a new table version (vintage).
* **Zeros.** In `events_cd`, a count of zero is no row. The day's `_ALL` row (`records_all`) shows that the update
  file was read. When `_ALL` is present, a missing country/indicator is an observed zero for the sampled update. A
  day whose update file was unavailable has an `_ALL` row with `value = null` and `flags.missing`, so treat that
  day as missing. In `doc_theme_d`, a day with `norm = 0` (no monitoring) is no row, and a tone value is no row on
  a day with no matching article.
* **Noise.** `events_cd` is a **1/96 time-slot sample**: about 1,200 to 2,100 records per day across about 75 to
  85 countries. For small countries the counts are tiny and noisy, so use rolling windows and shares, not single
  days.
* **Catalog concepts** (provider-declared, 58): `registry/atlas/catalog/media_signals.yaml`, ids
  `media.daily.gdelt_*` (themes: `politics` for conflict, `finance` for economy/markets, `society`).
* **Registry questions** must not ask about these values as outcomes of real events. They are only covariates.

## 1. What was built

| Piece | File |
|---|---|
| Connector (two atlas sources in one module) | `src/reva/adapters/connectors/atlas_gdelt.py` (`atlas.gdelt_events` 1.0.1, `atlas.gdelt_doc` 1.0.1 (1.0.0 was seeded from an earlier draft that flagged reused archive files)) |
| Source registry (rights, verbatim terms quote) | `registry/sources/gdelt_events2.yaml`, `registry/sources/gdelt_doc2.yaml` (`lineage_family: gdelt`) |
| Atlas concepts (58) | `registry/atlas/catalog/media_signals.yaml` (domain `media_signals`) |
| Budgets | `registry/atlas/budget_requests/v6signals.yaml` → `gdelt.events2` 1600/day, `gdelt.doc2` 300/day (merged in budgets-1.3.26 / 1.3.28; file now budgets-1.3.29, digest re-pinned in `src/reva/autonomy/policy.py`) |
| Unit tests (8, offline) | `tests/unit/test_atlas_gdelt.py` |
| Integration test (throwaway DB, fixture mode, archive reuse, idempotence) | `tests/integration/test_atlas_gdelt_ingest.py` |
| Fixtures (excerpts of the real 2026-10-17 bytes, licence per file) | `tests/fixtures/source/atlas.gdelt_events/` (2 update files re-zipped with 128 to 133 verbatim records each, the about page as visible text, the full CAMEO lookup; 2026-10-14 deliberately absent to test the missing path), `tests/fixtures/source/atlas.gdelt_doc/` (see §6) |

No migration, no KG statement and no causal claim. These are measurements of media coverage.

## 2. Sources and licence

| Source | URL | Licence | Fetched per run |
|---|---|---|---|
| GDELT 2.0 Event Database, 15-minute update files | `https://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip` | GDELT terms of use (below) | 1 update per day × `days` (default 730; ~70–150 KB each, ≈ 80 MB on the first run, then ≈ 1 new file/day because archived files are reused) |
| CAMEO event code lookup | `https://gdeltproject.org/data/lookups/CAMEO.eventcodes.txt` (12 KB) | same | 1 (root labels re-checked at every run; drift fails closed) |
| GDELT DOC 2.0 API timelines | `https://api.gdeltproject.org/api/v2/doc/doc?query=theme:<T>&mode=timelinevolraw\|timelinetone&format=json&startdatetime=…&enddatetime=…` | same | 22 themes × 2 modes (~0.2–0.3 MB each for 2017–2026; ECON_INFLATION volraw 2017-01-01..2026-10-17 = 216 KB, 3,536 daily points, `date_resolution: day`) |
| GKG theme lookup | `https://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT` (2.0 MB) | same | 1 (each requested theme must be listed, so themes are matched by identifier) |
| Terms page | `https://gdeltproject.org/about.html` | — | 1 per source (quotes checked → `report.policy_quotes_found`) |
| Event codebook (definitions quoted, not fetched per run) | `https://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf` | — | read once 2026-10-17 |

Licence, quoted from the archived about page: "The GDELT Project is an open platform for research and analysis
of global society and thus all datasets released by the GDELT Project are available for unlimited and
unrestricted use for any academic, commercial, or governmental use of any kind without fee." and "You may
redistribute, rehost, republish, and mirror any of the GDELT datasets in any form. However, any use or
redistribution of the data must include a citation to the GDELT Project and a link to this website
(https://www.gdeltproject.org/)." Every row carries `flags.licence` and
`flags.attribution = "Source: The GDELT Project (https://www.gdeltproject.org/)."`. There is no non-commercial
clause and no ban on redistribution.

**News content.** The atlas tables store **no article URL, title, actor name or place name**. A unit test checks
this. The `SOURCEURL` column stays only inside the archived raw update file, which the GDELT terms allow
REVA to keep and redistribute with a citation. REVA never fetches the articles themselves.

## 3. Method of `gdelt.events_cd` (exact)

1. **Window.** `end` defaults to yesterday (UTC). `days` defaults to 730 (allowed range 1..3650). `start` is never
   earlier than 2015-02-19, when the GDELT 2.0 update files begin.
2. **Sample.** For each day, the connector reads one update: `slot`, which defaults to `120000`, the update stamped
   12:00:00 UTC. Using a fixed slot keeps the sampling frame constant from day to day. Each update file is
   immutable, because its name is its time. A file already archived by this connector, with the same request
   fingerprint, is therefore read back from the archive with its own sha256 and retrieval date. It is not
   downloaded again unless `refetch=1`.
3. **Parse.** The file is a zip that must hold exactly one member, `<stamp>.export.CSV`. Records are tab-separated
   with 61 columns. If more than 1% of the records are malformed, the run fails closed. Records whose `DATEADDED`
   differs from the stamp are counted in the report.
4. **Group.** Records are grouped by `ActionGeo_CountryCode`. The codebook defines it as "This is the 2-character
   FIPS10-4 country code for the location." Records without a code are counted in `_ALL/records_no_actiongeo`.
   `iso3` comes only from the `fips` column of the GeoNames crosswalk (`registry/atlas/reference/geonames_countryInfo.txt`),
   never from a name. Codes the crosswalk does not know keep their `provider_geo` and get no `iso3`. In the
   2026-10-13/15/16 updates these were `OS` (oceans) and `RB`, plus `GZ` (Gaza Strip) when it occurs.
5. **Indicators per (country, day).**
   * `records_all`: the number of records.
   * `records_quad1..4`: counts by `QuadClass` ("1=Verbal Cooperation, 2=Material Cooperation, 3=Verbal Conflict,
     4=Material Conflict").
   * `records_root01..20`: counts by CAMEO `EventRootCode`. Labels are verbatim from GDELT's lookup, for example
     `14` PROTEST, `18` ASSAULT, `19` FIGHT, `20` USE UNCONVENTIONAL MASS VIOLENCE.
   * `goldstein_mean`: the mean of `GoldsteinScale` ("a numeric score from -10 to +10, capturing the theoretical
     potential impact that type of event will have on the stability of a country", based on the event type only).
   * `avgtone_mean`: the mean of `AvgTone` ("the average “tone” of all documents containing one or more mentions of
     this event during the 15 minute update in which it was first seen", −100..+100).
   * Both means are arithmetic over the `flags.n` records that carry the field, using exact decimals rounded
     half-even to 4 places.
   * `mentions_sum` and `articles_sum`: the sums of `NumMentions` and `NumArticles`.
6. **Day rows.**
   * `_ALL/records_all` holds all records of the update. `_ALL/records_no_actiongeo` is also emitted.
   * When the update file is unavailable (HTTP 404 or a refused request), the day gets `_ALL/records_all` with no
     value and `flags.missing = "GDELT update file not available (…)"`.
   * The run fails if more than max(2, 20%) of the days are missing.
7. **Flags on every value.**
   * Sampling and source: `signal_kind`, `sampling`, `slot_utc`, `day_basis`, `geo_basis`, `basis`, `n`.
   * Licence and vintage: `licence`, `attribution`, `as_of`, `publication`, `revision_policy`.
   * Fixed values: `precision = day`, `estimate_kind = observation` (an observation of media coverage).
   * Selector: `{file, member, column, where: "ActionGeo_CountryCode=XX"}`.

**Measured on the real files of 2026-10-13/15/16:**

| Update file | Records | With no country | Countries | Malformed | Unknown codes |
|---|---|---|---|---|---|
| 2026-10-13 | 1,186 | 19 | 75 | 0 | 0 |
| 2026-10-15 | 1,784 | 27 | 84 | 0 | 0 |
| 2026-10-16 | 2,085 | 36 | 85 | 0 | 0 |

Across the three files, aggregation produced 2,983 rows in 0.5 s. That is about 1,000 rows per day, so the
default window gives about **0.73 M rows** and **≈ 80 MB raw**. The cap for V6 agents is 100 MB.

**Why not the full daily volume.** One GDELT 1.0 daily events file is about 8.5 MB, and a full day of 2.0 updates
(96 files) is about 10 MB. Two years at full resolution would be about 6–7 GB of raw bytes. That breaks the
100 MB cap and this container's disk. The fixed-slot sample keeps the provenance contract (every value traces to
archived raw bytes) inside the cap. §7 lists the owner options for full volume.

## 4. Method of `gdelt.doc_theme_d`

* One DOC 2.0 request per (theme, mode). The window runs from `doc_start` (default 2017-01-01) to `end` (default
  yesterday). The connector refuses a response whose `query_details.date_resolution` is not `day`. A 2017–2026
  window was verified to come back at day resolution.
* `timelinevolraw` gives `articles_<theme>`, the point's `value` (articles carrying the theme), and
  `monitored_<theme>`, the point's `norm` (all articles monitored that day). Both are integer text, kept as is,
  with nothing derived.
* `timelinetone` gives `tone_<theme>`, the average tone. Numbers are parsed as text, so no float round-trip.
* Themes are 22 GKG identifiers. Each one is checked against `LOOKUP-GKGTHEMES.TXT` before any request, and an
  unknown theme fails closed:
  * Conflict: ARMEDCONFLICT, KILL, TERROR, PROTEST, MILITARY, REBELLION, UNREST_BELLIGERENT, CEASEFIRE, SANCTIONS,
    REFUGEES.
  * Society: FOOD_SECURITY.
  * Finance: ECON_INFLATION, ECON_INTEREST_RATES, ECON_CENTRALBANK, ECON_STOCKMARKET, ECON_CURRENCY_EXCHANGE_RATE,
    ECON_DEBT, ECON_BANKRUPTCY, ECON_OILPRICE, ECON_TRADE_DISPUTE, EPU_UNCERTAINTY, EPU_CATS_MONETARY_POLICY.
  * `COUP` and `ECON_UNEMPLOYMENT` are **not** in the lookup and were not used.
* **Throttling.** The API answers 429 with "Please limit requests to one every 5 seconds or contact … for larger
  queries". The connector handles it this way:
  * The source spaces requests 10 s apart (`min_interval_seconds`).
  * A request refused with 429 is retried up to `doc_retries` (default 4) times, after `doc_cooldown` × attempt
    seconds (default 60).
  * After 6 consecutive refusals the connector stops asking. Every theme it did not get is listed in
    `report.failed` and is never filled.
* **Gaps.** The API's timelines skip days, most likely GDELT processing outages. ECON_INFLATION for
  2026-09-01..10-16 lists 30 of 46 days: 2026-09-14..19, 09-26, 10-03..05, 10-07, 10-09 and 10-11..14 are absent.
  An absent day is no row and is never a zero. The same days are absent for every theme and in the 2017–2026
  response, and the values are identical across the two windows.
* Expected rows for a full run: 22 themes × ~3,500 listed days × (2 + tone) ≈ 0.23 M.

## 5. Live status

**Nothing was ingested live so far.** The rule is no live ingest below 5 GB free disk. Free disk read 4.9, 4.7,
4.3, 5.3, 5.1, 4.8 and 4.6 GB during this session, as other agents wrote to it.

* A first live attempt, while free disk was 5.3 GB, was refused by `_check_registered`. Another agent's
  `reva seed` had registered 1.0.0 from an earlier draft of this module. The connector was then bumped to 1.0.1
  and re-seeded.
* A guarded run waited on the dev-DB lock:
  `flock /tmp/reva-devdb.lock sh -c '<free disk >= 5120 MB> && uv run reva atlas ingest atlas.gdelt_events --option days=730 --option end=2026-10-16'`.
  It re-checks free disk *after* it gets the lock. When it got the lock, 4,312 MB was free, so it **skipped the
  ingest** and nothing was loaded. Re-run the §8 commands when at least 5 GB is free. Its log is
  `/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/ingest_events.log`.
* `atlas.gdelt_doc` was not run live.

Probes of 2026-10-17, from the shared egress, with a generic UA and no personal data:

* `data.gdeltproject.org` answered every update file and lookup with 200.
* `api.gdeltproject.org` (DOC 2.0) answered 429 to most requests and 200 to a few, at intervals of several
  minutes. Expect a live `atlas.gdelt_doc` run from this container to be slow and partial (reported per theme).

## 6. Fixtures

* `atlas.gdelt_events`:
  * Two real update files (2026-10-13 and 2026-10-15, slot 12:00:00). Each keeps up to 25 records verbatim for
    each of UK, US, UP, RS, IS, SY and up to 8 records with no country. The records are re-zipped under the
    original member name. The manifest says "subset".
  * The about page as visible text.
  * The complete CAMEO lookup.
* `atlas.gdelt_doc`:
  * Four complete real API responses, captured 2026-10-17 between 16:39 and 16:51 UTC with the exact connector
    URLs: the themes ECON_INFLATION and ARMEDCONFLICT, modes `timelinevolraw` and `timelinetone`, `doc_start`
    2026-09-01, `end` 2026-10-16 (`run_options.json`). Getting them took 11 requests, because 7 were answered 429.
  * An excerpt of the GKG theme lookup: the 22 declared themes plus the first 5 lines, verbatim.
  * The about page as visible text.

## 7. Gaps and owner decisions

1. **Full-volume events (owner).** The table holds a 1/96 sample. There are two routes to all records per day:
   * Run `atlas.gdelt_events` with more history and slots on a bigger machine. Today the connector takes one
     `slot`. Supporting several slots needs one indicator per slot, or per-day files in a raw bundle, to keep
     provenance exact.
   * Use GDELT's public BigQuery dataset (`gdelt-bq.gdeltv2.events`), which needs a Google Cloud billing account.
     That is an owner decision, so REVA did not use the preconfigured `bq` CLI as a workaround.
2. **DOC API throttling (owner).** The 429 body invites high-volume users to contact GDELT, at the address in that
   body. Alternatively, run from a less shared egress. GDELT also points to its "web ngrams" dataset for
   high-traffic use. That dataset is not evaluated here; it is a bulk dataset and would need a disk check first.
3. **FIPS codes the crosswalk does not map** (`RB`, `OS`, `GZ`, Paracel/Spratly `PF`/`PG` …) keep `provider_geo`
   and get no `iso3`. Mapping them would need a published FIPS 10-4 → ISO table, such as the NGA GEC list, loaded
   as a crosswalk by identifier. REVA does not guess them.
4. **Not used:**
   * Google Trends is not open, so it was skipped as briefed.
   * The GDELT GEO 2.0 API covers only the last 7 days, too short for backtests.
   * The GDELT TV API covers mostly US television stations and was out of scope for country signals.
   * The GKG files are full article-level data, too large for the 100 MB cap.
   * DOC timelines per *source country* (`sourcecountry:` operator) were not built: they would measure who
     publishes, not where the coverage is about, and would cost 2 requests × themes × countries on a throttled
     API.
5. **Skill is not claimed.** Whether these signals add skill is for PRED-CONFLICT and PRED-FIN to measure in the
   forecast registry, against their benchmarks, using as-of vintages.

## 8. Commands to finish (bigger machine or ≥ 5 GB free)

```bash
df -h /                                                     # must show >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed                  # registers atlas.gdelt_events / atlas.gdelt_doc 1.0.1
# events: 730-day backfill (~730 requests, ~80 MB raw); later daily runs fetch ~1 new update file
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gdelt_events
#   options: --option days=1095 --option end=2026-10-16 --option slot=120000 (--option refetch=1 to re-download)
# DOC timelines (46 requests, ~15 MB; slow under 429 throttling, partial results are reported per theme)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gdelt_doc
#   options: --option themes=ECON_INFLATION,ARMEDCONFLICT --option doc_start=2017-01-01 --option doc_retries=6
uv run pytest -q tests/unit/test_atlas_gdelt.py tests/integration/test_atlas_gdelt_ingest.py
```
