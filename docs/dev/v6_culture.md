# V6 — V6CULTURE: culture at depth with open data (music, books, films, museums, heritage)

Agent V6CULTURE, 2026-10-17. Status: two new atlas sources (MusicBrainz statistics, Open Library subjects + KG
authors) with captured fixtures, offline + throwaway-DB tests, budgets merged (`budgets-1.3.29`); six Wikidata
specs + property requests + relation families handed to WDOPS; live runs queued under the dev-DB lock with a
≥ 5 GB free-disk guard (§6). No Wikidata query was made by this agent.

| File | Role |
|---|---|
| `src/reva/adapters/connectors/atlas_musicbrainz.py` | source `atlas.musicbrainz_stats` → atlas `musicbrainz.country_stats`, `musicbrainz.language_stats` |
| `src/reva/adapters/connectors/atlas_openlibrary.py` | source `atlas.openlibrary_subjects` → atlas `openlibrary.subjects` + KG (`openlibrary.api`) |
| `registry/sources/musicbrainz_stats.yaml`, `registry/sources/openlibrary.yaml` | source entries (rights, hosts, definitions, what is not used) |
| `registry/atlas/catalog/culture_media.yaml` | 5 provider-declared concepts (theme `culture`) for the MusicBrainz per-country counts |
| `registry/atlas/budget_requests/v6culture.yaml` | `musicbrainz.stats` 4/day, `openlibrary.api` 500/day (merged: `budgets-1.3.29`, digest pinned in `policy.py`) |
| `registry/kg/queries/culture/v6/*.yaml` | 6 Wikidata specs (owner V6CULTURE, ids `culture.v6_*`) for WDOPS |
| `registry/kg/property_requests/v6culture.yaml` | 16 properties to allow-list (P57, P58, P86, P136, P364, P1411, P2047, P2130, P2142, P272, P840, P345, P434, P648, P264, P1174) |
| `registry/kg/relations_v4.d/v6culture.yaml` | relation families of the culture predicates (none causal) |
| `tests/unit/test_atlas_v6culture.py` (14), `tests/integration/test_atlas_v6culture_ingest.py` (2, `db`) | parsers, flags, layout drift, KG records, specs, families; fixture ingest + idempotent re-run |
| `tests/fixtures/source/atlas.musicbrainz_stats/`, `…/atlas.openlibrary_subjects/` | live captures 2026-10-17 through SafeHttpClient (MB: labelled excerpts, `<script>` removed — the pages embed a third-party Mapbox token; 84 kB / 152 kB) |

## 1. Sources researched (2026-10-17)

| Domain | Source | Licence / terms (verbatim where it matters) | Result |
|---|---|---|---|
| Music | **MusicBrainz statistics pages** (`musicbrainz.org/statistics/countries`, `/statistics/languages-scripts`) | Core data CC0: "The core data of the database is licensed under the CC0, which is effectively placing the data into the Public Domain"; supplementary data (tags, ratings, annotations) CC BY-NC-SA 3.0 — not used. The statistics pages carry no separate licence; they count core entities (`licence_basis = counts_of_cc0_core_data`) | **connector + fixtures + tests**; 2 requests per run (~0.7 MB) |
| Music | MusicBrainz `/ws/2` JSON web service (search counts per country × year) | same | **not usable from this machine**: every call answered HTTP 503 `{"error": "The MusicBrainz web server is currently busy. Please try again later."}` (4 attempts ≥ 3 s apart, generic project UA, 2026-10-17) — the shared egress IP is over MusicBrainz's per-IP limit. Not worked around. |
| Music | MusicBrainz JSON/PostgreSQL dumps | CC0 core | not used: several GB (disk) |
| Books | **Open Library** subjects/authors JSON API | "The Internet Archive does not assert any new copyright or other proprietary rights over any of the material in the Open Library database. There may be existing rights issues on some contributions and in some jurisdictions." — **not an explicit open licence** → `licence_review = required` on every cell/statement | **connector + fixtures + tests** |
| Film | IMDb Non-Commercial Datasets (`datasets.imdbws.com`) | "Subsets of IMDb data are available for access to customers for personal and non-commercial use. You can hold local copies of this data, and it is subject to our terms and conditions." (data.imdb.com/non-commercial-datasets, read 2026-10-17) | **not used** (non-commercial; owner decision). Only the IMDb *ID* (Wikidata P345, CC0) is requested as a join key. |
| Film | Wikidata films | CC0, community-edited | spec `culture.v6_films` for WDOPS (§3) |
| Film | European Audiovisual Observatory / UIS feature films | see `v3_a_culture.md` (no open licence / not in the UIS API) | not available; UIS indoor cinemas are in `atlas.uis_culture` |
| Heritage | UNESCO World Heritage (`unesco_whc.whc001` + KG), Intangible Cultural Heritage (`unesco_ich.ich001` + KG) | CC BY-SA 4.0 | **already loaded** in the dev DB (KGCULT, A-CULTURE) — nothing to add |
| Culture statistics | UNESCO UIS Data API, theme CULTURE (`atlas.uis_culture`, 30 indicators) | CC BY-SA 4.0 | connector exists (A-CULTURE); **was not in the dev DB** — queued live (§6) |
| Sport | Olympic medal tables (`atlas.olympics_medals`) | CC BY-SA 4.0 (Wikipedia) | connector exists; **atlas table and KG were not in the dev DB** (the brief assumed loaded) — queued live (§6). Nobel (`atlas.nobel.laureates`, 2,007 KG statements) is loaded. |
| Museums | Wikidata museums | CC0 | spec `culture.v6_museums` (§3); no open global museum statistics source found (ICOM/EGMUS: no open dataset) |

## 2. Atlas tables (counts by country / by catalogue)

### 2.1 `musicbrainz.country_stats` (geo scheme `iso2`, snapshot `[stats day, +1)`)

Indicators `mb_artists`, `mb_release_events`, `mb_labels`, `mb_events`, `mb_places` (concepts
`culture_media.mb_*`). Definitions from the MusicBrainz server (`lib/MusicBrainz/Server/Data/Statistics.pm`):

* `count.artist.country` — "Distribution of artists per country": the artist's area **or the country containing
  it** (area containment); same rule for labels, places, events (area of the event or of its place).
* `count.release.country` — "Distribution of releases per country": once per **release-event country**. Digital
  releases list ~200 storefront countries, so tiny territories show tens of thousands of releases (Kiribati 35,563,
  Tokelau 27,876, `[Worldwide]` 2,027,237 on 2026-10-17): flag `release_events_not_origin`; the concept is named
  "release events", not "music from the country".

Live page on 2026-10-17 ("Last updated: 2026-10-17"): 259 codes → 249 map to crosswalk countries; 9 kept unmapped
and never given to a successor (`SU`, `YU`, `CS`, `XC`, `XG`, `XE` Europe, `XW` Worldwide, `XK`, `AN`), plus
"Unknown country" (`mb:null`) with **1,518,606 artists — half of the Artists column**: these tables measure what
volunteers have catalogued, not national output. Expected size: 1,295 rows. Examples (artists / release events /
labels / events / places): US 365,206 / 879,390 / 49,485 / 41,375 / 25,489; Japan 121,220 / 392,850 / 13,568 /
7,038 / 3,489; France 72,489 / 199,931 / 9,416 / 4,538 / 3,920; Brazil 22,156 / 86,057 / 1,781 / 591 / 521;
Nigeria 2,265 / 41,306 / 210 / 6 / 20. Layout checks: the page's "Total" column must equal the sum of the five
columns (not stored); the flag code must equal the statistic code; an empty cell is no row (never 0).

### 2.2 `musicbrainz.language_stats` (provider_geo `world`)

`mb_releases_by_language|language=<ISO 639-3>`, `mb_works_by_lyrics_language|language=…`,
`mb_releases_by_script|script=<ISO 15924>`: 391 languages, 52 scripts (expected 834 rows). English 3,701,389
releases / 693,947 works; unknown language 830,655 / 1,024,486; Latin script 4,647,893 releases. Language codes
are ISO 639-3 — the join to KG language entities is Wikidata P220 (KGCULT's `culture.languages`), an identifier.

### 2.3 `openlibrary.subjects` (provider_geo `world`: the Open Library catalogue)

Subjects = the 70 subjects Open Library links from its own *Browse Subjects* page (provider's selection, documented;
no REVA judgement). Per subject (`/subjects/<key>.json?details=true&limit=1`, definitions from
`openlibrary/plugins/worksearch/subjects.py`): `ol_works`, `ol_ebooks` (snapshot), `ol_works_with_edition_language`
(MARC codes, **top 25 only** — absent ≠ 0, flag `facet_top_25_only`), `ol_works_with_edition_in_year` (a work counts
in every year one of its editions was published — `counted_per_edition_year`; years ≤ 1000 or > next year dropped by
Open Library). Fixture figures (2026-10-17): Poetry 113,550 works (30,089 ebooks, editions in 561 distinct years,
eng 63,738 / spa 6,588 / fre 6,130); Film 2,182; place:Brazil 111,263 (por 97,592). Live index (`live_index`).

No country-year table for books or films: Open Library has no country of publication facet in this API, and
mapping its place subjects ("place:great_britain", "place:england") to countries would be a name match — refused.

## 3. KG

### 3.1 Open Library (`source_key openlibrary.api`, loaded by `atlas.openlibrary_subjects`)

Bounded by construction: ≤ 70 subjects + ≤ 350 authors (the 5 authors with most works per subject, from Open
Library's own author facet). Entities `openlibrary:subject.<key>` (kind `book_subject`), `openlibrary:<OLID>`
(kind `author`: persons **and corporate authors**). Statements: `openlibrary:has_works_in_subject` (author →
subject; qualifier `works_in_subject`, snapshot date), `openlibrary:wikidata_id` / `viaf_id` / `isni` (exactly as
Open Library's `remote_ids` publish them), `openlibrary:birth_date` / `death_date` (Open Library's free text,
never parsed). Redirect records are not entities. Biographies, photos and covers are never stored.

Examples (fixture, throwaway DB): `openlibrary:OL28294A` "Publius Vergilius Maro" —has_works_in_subject (335)→
`openlibrary:subject.poetry`; OL28294A —wikidata_id→ "Q1398" (Virgil), viaf 8194433, birth "15 October 70 BCE".
`openlibrary:OL79796A` "Brazil" (a corporate author: 1,730 works in place:Brazil) —wikidata_id→ "Q155" — the
identifier Open Library publishes; REVA merges nothing. With WDOPS's `culture.v6_openlibrary_author_ids` (P648 on
Wikidata people) the two directions of the bridge are both source statements; V4LINK's identity rules can then
link them (never by name).

### 3.2 Wikidata specs for WDOPS (`registry/kg/queries/culture/v6/`, schema- and harvester-valid)

| Spec | Purpose | Selection (size bound, not a ranking) | `max_rows` |
|---|---|---|---|
| `culture.v6_films` | seed | non-deprecated P31 ∈ film, feature, animated, animated feature, documentary, TV film; ≥ 40 sitelinks; split by QID prefix | 15,000 |
| `culture.v6_literary_works` | seed | literary work, novel, poem, play, short story, epic poem; ≥ 30 sitelinks | 10,000 |
| `culture.v6_music_artists` | seed | humans, P106 ∈ 10 music occupations, with P434 (MusicBrainz id), ≥ 40 sitelinks | 20,000 |
| `culture.v6_musical_groups` | seed | musical group / rock band / ensemble / duo with P434, ≥ 30 sitelinks | 6,000 |
| `culture.v6_openlibrary_author_ids` | statements (P648) | humans ≥ 40 sitelinks carrying an Open Library id | 30,000 |
| `culture.v6_museums` | seed | museum / art / science / national / history museum, art gallery; ≥ 20 sitelinks | 8,000 |

Properties kept: films P495 P577 P57 P58 P86 P136 P364 P166 P1411 P2047 P2130 P2142 P272 P840 P921 P144 P345;
works P50 P577 P571 P495 P407 P136 P166 P648 P921 P840 P144 P135; artists P27 P19 P20 P569 P570 P106 P136 P264
P166 P1411 P1412 P434 P463 P135 P737 P800; groups P495 P740 P571 P576 P136 P264 P166 P1411 P434 P527 P737; museums
P17 P131 P625 P571 P1174 P1435 P127 P112. 16 of them await allow-listing (`property_requests/v6culture.yaml`); the
harvester validator reports only those (test `test_wikidata_specs_valid_and_pending_properties_requested`).
Box office / cost / visitors are reported figures with units and point-in-time qualifiers: never converted, summed
or compared across dates by the harvest.

### 3.3 Relation families (`relations_v4.d/v6culture.yaml`)

`produced_by`: P50 author, P57 director, P58 screenwriter, P86 composer, P272 production company (meets the
V4INDUSTRY company layer through the company's QID). `located_in`: P495 country of origin, P740 location of
formation. `context`: P136 genre, P364 original language, P407 language of work, P840 narrative location (where
the story is set — never a production place), P144 based on, P166 award, P1411 nomination, P264 record label,
`openlibrary:has_works_in_subject`. None is causal.

Path shapes once WDOPS has harvested (shapes only, no harvested example yet): film —P495→ country; film —P57→ director —P27→ country; film —P364→ language ←P37— country (KGCULT); film —P272→ company (V4INDUSTRY). Book side, already loadable: `openlibrary:OL28294A` —`openlibrary:wikidata_id`→ "Q1398" and (spec 5) Q1398 —P648→ "OL28294A": two published identifiers, one hop each.

## 4. Missing, and why

* **Releases per country × year (MusicBrainz)** — needs `/ws/2` search counts (one request per country-year, ≤ 1/s);
  503 "busy" from this IP. Run from an IP not shared with other MusicBrainz clients (bigger machine), see §6.
* **Films/books per country × year** — no open, licensed source with country of production (IMDb non-commercial;
  EAO no open licence; UIS film statistics not in the API). A count of Wikidata films by P495 × P577 would be a
  REVA aggregation of community-edited data (incomplete coverage); not produced. If wanted, WDOPS/QLever could run
  it as an *attributed* aggregate table flagged `community_edited_counts` (owner decision).
* **Museum attendance by country** — no open dataset (EGMUS: Europe only, no licence statement); Wikidata P1174 per
  museum via the spec.
* **Music charts, streaming, sales** — IFPI/Billboard/Spotify: proprietary.

## 5. Owner decisions

1. **Open Library licence**: "no new rights asserted" is not an explicit licence → confirm use of counts, names
   and identifiers (flagged `licence_review = required`).
2. **IMDb datasets**: personal and non-commercial use only → not loaded; keep it that way unless a commercial
   licence is obtained.
3. **MusicBrainz `/ws/2`** from a dedicated IP (or a MetaBrainz commercial/replication arrangement) for
   per-year release counts; MusicBrainz also asks clients to send a contact in the User-Agent — REVA sends none
   by policy (the statistics pages work without it).
4. **MusicBrainz pages embed a third-party (Mapbox) access token** in inline scripts. Fixtures have `<script>`
   removed; the raw archive (`var/objects`) keeps the provider bytes unchanged — exclude or scrub before
   publishing raw-object tars (e.g. `reva-data-*` branches), where GitHub secret scanning may flag it.
5. Films-by-country-year from Wikidata as an attributed, community-edited aggregate: yes/no.

## 6. Live runs and commands

Free disk was 4.3–4.9 GB during development (below the 5 GB floor), then 6.0 GB after DISKZ; the live chain was
queued with a per-ingest guard (`scratchpad/v6c/scripts/live.sh`: `reva seed`, then each ingest only if ≥ 5 GB free,
all under `flock /tmp/reva-devdb.lock`). Its outcome is reported in §7. To (re)run anywhere:

```bash
flock /tmp/reva-devdb.lock uv run reva seed
for s in atlas.musicbrainz_stats atlas.openlibrary_subjects atlas.uis_culture atlas.olympics_medals; do
  flock /tmp/reva-devdb.lock uv run reva atlas ingest $s
done
uv run reva atlas ingest atlas.openlibrary_subjects --option subjects=poetry,film --option authors_per_subject=3  # partial
uv run python -m reva.kg.harvest merge-property-requests          # WDOPS: registry/kg/property_requests/v6culture.yaml
uv run python -m reva.kg.harvest run registry/kg/queries/culture/v6 --via api   # WDOPS
uv run pytest tests/unit/test_atlas_v6culture.py tests/integration/test_atlas_v6culture_ingest.py
```

Raw archive per full refresh: ~0.7 MB (MusicBrainz) + ~3 MB (Open Library) + ~0.9 MB (UIS) + ~1.5 MB (Olympics).

## 7. Live outcome

(filled in below when the queued chain finished)
