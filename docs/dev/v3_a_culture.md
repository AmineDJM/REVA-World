# V3 — A-CULTURE: culture and soft power (UNESCO heritage, UIS culture, Olympic medals)

Agent A-CULTURE, 2026-10-16. Status: four atlas sources declared through ATLASCORE's protocol
(`atlas_source()` / `atlas_ingest(run)`, fetched only via `run.get`, GET only, budget-metered live), catalog,
small recorded fixtures, offline + DB tests, and one live run of every source into throwaway databases
(the `db` fixture's `reva_test_*`, dropped afterwards; never the dev DB `reva`).

| file | role |
|---|---|
| `src/reva/adapters/connectors/atlas_culture.py` | sources `atlas.unesco_ich` (ICH lists -> atlas + KG), `atlas.unesco_whc_links` (World Heritage List -> KG only), `atlas.uis_culture` (UIS CULTURE theme -> atlas) |
| `src/reva/adapters/connectors/atlas_olympics.py` | source `atlas.olympics_medals` (Olympic medal tables 1896-2026 -> atlas + KG) |
| `registry/sources/unesco_ich.yaml`, `registry/sources/wikipedia_olympic_medal_tables.yaml` | new source entries (rights, hosts, coverage); `unesco.whc` (KGCULT) and `unesco.uis` (A-HEALTHEDU) are reused as budget keys |
| `registry/atlas/catalog/culture.yaml` | +46 provider-declared concepts (6 ICH, 30 UIS culture, 10 Olympics) after KGCULT's 7 World Heritage concepts |
| `registry/atlas/geo_aliases/a_culture.yaml` | scheme `ioc_noc`: 206 current IOC NOC codes + 9 former codes of the same state -> crosswalk entities |
| `registry/atlas/budget_requests/a_culture.yaml` | `unesco.ich` 4/day, `unesco.whc` +4/day, `unesco.uis` +4/day, `wikipedia.olympic_medal_tables` 12/day |
| `tests/unit/test_atlas_culture.py` (16), `tests/integration/test_atlas_culture_ingest.py` (4, `db`), `tests/integration/test_atlas_culture_live.py` (4, `live and db`, deselected by default) | |
| `tests/fixtures/source/atlas.{unesco_ich,unesco_whc_links,uis_culture,olympics_medals}/` | real captures or labelled excerpts of real captures (manifest `note`, `excerpt_of_sha256`); 40-272 kB each |

## 1. Sources researched (licences checked 2026-10-16)

| Domain | Source | Licence / terms | Result |
|---|---|---|---|
| World Heritage List | UNESCO open-data portal `data.unesco.org` dataset `whc001` | CC BY-SA 4.0 (dataset record) | **already ingested by KGCULT** (`atlas_unesco_whc.py`, atlas table `unesco_whc.whc001`: counts per State Party). A-CULTURE adds the **KG statements** (`atlas.unesco_whc_links`, reuses KGCULT's parser). `whc.unesco.org` syndication XML/JSON sits behind a Cloudflare browser challenge (KGCULT, 2026-10-15) and is not used. |
| Intangible Cultural Heritage | `data.unesco.org` dataset `ich001` ("Intangible Heritage List") | CC BY-SA 4.0 (dataset record) | **ingested** (atlas + KG) |
| UNESCO UIS culture statistics | UIS Data API, theme CULTURE (`api.uis.unesco.org`) | CC BY-SA 4.0 (UIS terms, as A-HEALTHEDU's source entry) | **ingested** as a separate dataset `uis.culture` in A-CULTURE's own module (A-HEALTHEDU's `atlas_uis.py` untouched; its `parse_version`/`parse_data` are reused) |
| Olympic medals | English Wikipedia medal-table articles, template `{{Medals table}}` | CC BY-SA 4.0 (REST page metadata of every article); each table cites the IOC medal table / Official Report | **ingested** (atlas + KG), flagged community-edited |
| Olympic medals (rejected) | olympics.com (IOC) | all rights reserved, no open data | not used |
| | Olympedia (OlyMADMen) | no licence stated on the site (about/FAQ) -> all rights reserved | not used |
| | Kaggle "120 years of Olympic history" and similar | unlicensed scrape of Olympedia/sports-reference; provenance not an official source | not used |
| | Wikidata | no per-Games, per-NOC medal counts (only athlete-level P166) | not used for counts; Games QIDs come from Wikipedia `pageprops.wikibase_item` |
| FIFA/Coca-Cola World Ranking | inside.fifa.com | FIFA Terms of Service §3: content "may not be used, reproduced, distributed … or otherwise exploited for any other purposes than for accessing and using it on the FIFA Digital Platforms"; no data download | **skipped** |
| Tourism | UN Tourism (UNWTO) | copyright, data licensing on request (A-SOCIETY's finding) | **skipped**; WDI copies `ST.INT.ARVL/DPRT/RCPT.CD` come with A-WB |
| Cinema / film | European Audiovisual Observatory (LUMIERE admissions database, yearbooks) | no open licence statement found (LUMIERE home page; the Observatory legal-notice page returned HTTP 403) | **skipped**; UIS indoor-cinema counts (which partly come from the EAO Yearbook, footnoted per cell) are ingested |
| Film production | UIS feature-film statistics | not exposed by the UIS Data API in the February 2026 release (theme CULTURE has 30 indicators: cinemas, cultural employment, heritage expenditure) | not available |
| Cuisine (Michelin), music charts | Michelin Guide, IFPI / Billboard / official chart companies | proprietary, no open dataset | **not available** (Wikidata P2012 "cuisine" per country is KGCULT's `culture.country_cuisines` spec) |
| Creative Cities Network | `data.unesco.org` `cce001` | CC BY-SA 4.0 | **skipped**: no country field (`postal_address.country` null); mapping city -> country would be a guess. The `contact_*` fields hold personal contact data and must never be requested. |
| Hague 1954 protected properties | `data.unesco.org` `chp001` (175 records, CC BY-SA 4.0) | | candidate, not done (low priority) |
| Other UNESCO designations | `mab001` biosphere reserves (797), `gbs001`, `mhm001` national heritage laws (2,939) | CC BY-SA 4.0 | candidates for later (same Opendatasoft pattern) |

## 2. What was loaded (live runs, 2026-10-16)

All runs through `run_atlas_ingest(mode="live")` -> `reserve_ingest` -> metered SafeHttpClient -> `settle_metered`;
budgets overlaid in memory from the budget request (pinned policy `budgets-1.3.5` unchanged). Reserved = consumed,
0 refused.

| Source | Requests | Bytes | Atlas table | Rows | Countries (crosswalk) | Periods | KG |
|---|---:|---:|---|---:|---:|---|---|
| `atlas.unesco_ich` | 2 | 0.46 MB | `unesco_ich.ich001` | 1,652 | 157 | 2008-2025 (+ snapshot at revision 2026-10-15) | 852 entities, 2,244 statements, 0 rejected |
| `atlas.unesco_whc_links` | 2 | 0.74 MB | — (KG only) | — | — | — | 1,273 entities, 3,918 statements, 0 rejected |
| `atlas.uis_culture` | 2 | 0.88 MB | `uis.culture` | 3,923 | 148 | 1995-2024 | — |
| `atlas.olympics_medals` | 8 | 1.51 MB | `olympics.medal_tables` | 7,655 | 142 | 1896-2026 (55 Games) | 23 entities, 1,952 statements, 0 rejected |

Parquet: ICH `8fab3b2b…`, UIS `6356f111…`, Olympics `fa6dfc78…`. Raw + Parquet ≈ 4 MB per full refresh.
The Olympics run took ~8 min (10 s politeness interval; Wikimedia's per-IP throttling answered 429 to exploratory
requests the same day — a 429 ends the run before anything is registered).

### 2.1 ICH (`ich001`, revision 2026-10-15T02:07:24Z)

849 elements: 716 Representative List (`RL`), 90 Urgent Safeguarding List (`USL`), 43 Register of Good Safeguarding
Practices (`Art18`); 157 States (all ISO alpha-2 codes map); 103 multinational elements (falconry: 24 States);
729 distinct country pairs share at least one element (most: UAE-Oman 13, Azerbaijan-Türkiye 11, UAE-Saudi Arabia 10).
136 elements name 229 associated World Heritage properties.

* Atlas indicators (per submitting State): `ich_elements_total`, `ich_elements_rl`, `ich_elements_usl`,
  `ich_elements_gsp`, `ich_elements_multinational` (snapshot `[revision day, +1)`) and
  `ich_elements_inscribed_in_year` (calendar year of `inscription_year`).
* **Limits (flagged):** one inscription year per element and the *current* States list: a State that joined an
  element through a later extension is counted in the element's original year
  (`country_may_have_joined_by_extension`). The 2008 count includes the Masterpieces proclaimed 2001-2005 and
  incorporated into the Representative List in 2008. Removed/transferred elements are not distinguishable.

### 2.2 World Heritage KG (`whc001`, revision 2026-10-15T02:07:43Z)

1,273 properties; 51 transboundary (up to 18 States: Beech Forests of the Carpathians); 280 distinct country pairs
co-inscribed (most: Belgium-France 5; Austria-Germany, France-Germany, Switzerland-Italy 4). Statements per property:
`P1435` -> Q9259 (World Heritage Site) with start = inscription year and qualifiers (category, criteria, transboundary,
in danger + since, extension years), `P757` id literal (join key with KGCULT's Wikidata-harvested items), one `P17`
per State Party.

### 2.3 UIS culture (`uis.culture`, data version `20260507-91260335`, February 2026 release)

| group | indicators | countries | years |
|---|---:|---:|---|
| indoor cinemas (`220174`) | 1 | 123 | 1995-2017 |
| cultural employment (`CE.*`, ILOSTAT-based) | 21 | 74 | 2010-2016 |
| SDG 11.4.1 heritage expenditure per capita (`HEXP*`, constant 2017 PPP$) | 8 | 79 | 2017-2024 |

31 records have magnitude `NA` (not applicable) with a served value of 0: stored as value-less cells
(`flags.missing = not_applicable`, `provider_value` kept) — never a filled zero. 49 `NIL` (nil or negligible) keep
the provider's 0. Qualifiers (`UIS_EST`, `NAT_EST`) and footnotes (e.g. "Source: European Audiovisual Observatory
Yearbook") are kept per cell.

### 2.4 Olympic medal tables (`olympics.medal_tables`)

55 Games (Summer 1896-2024, Winter 1924-2026), 1,900 NOC rows, 142 crosswalk countries. Indicators per season:
`gold|silver|bronze` (template values verbatim), `total` (sum, `aggregation=sum`, computed by REVA), `games_host` (1).
Every cell carries the page revision id and `verification = community_edited_cites_ioc`.

* **Historical NOCs kept unmapped** (never given to successors): URS, EUN, GDR, FRG, EUA, TCH, YUG, SCG, BOH, ANZ,
  RU1 (the 1908/1912 templates key the Russian Empire as `RUS` but display `RU1`: the displayed code is used),
  ZZX (mixed teams 1896-1924), IOP, IOA, ROC (Republic of China 1956-1976 **and** ROC athletes 2020-2022: the code is
  ambiguous, so two KG entities `ioc:ROC.1956-1976` / `ioc:ROC.2020-2022`), OAR, AIN (2024: `skip_AIN`, listed outside
  the official ranking, flagged), EOR, BWI, RAU, AHO — 21 codes, 471 rows.
* **IOC code ≠ ISO code**: no `base` scheme; e.g. IOC `CHI` = Chile (crosswalk `CHI` = Channel Islands), `GER` = DEU,
  `TPE` = TWN (Chinese Taipei). NOC codes map by IOC continuity: the territory at the time may differ (GER 1896-1936,
  AUT/HUN before 1918) — every mapped row says so in `flags.iso3_via`.
* Tokyo 2020 is dated 2021 (`held_in`). Only medal-winning NOCs appear: no row = no medal **or** no participation.
* Hosts: one per table (template `host`/`host_<NOC>`); the 1956 Stockholm equestrian events are not marked, so no
  Sweden-1956 co-host link is asserted. The KG has 52 host statements (Games -> `P17` -> country); Moscow 1980 (URS),
  Munich 1972 (FRG) and Sarajevo 1984 (YUG) have hosts only as unmapped atlas cells.

## 3. KG statements (for LINKS)

| source_key | statements | shape |
|---|---|---|
| `unesco.whc` | 3,918 | `whc:<id_no>` —P17→ country (×States Parties); —P1435→ Q9259 (start = inscription year); —P757→ id |
| `unesco.ich` | 2,244 | `ich:<ref>` —P17→ country (×submitting States); —P1435→ `unesco:ich_list:<RL\|USL\|Art18>` (start = year); —`unesco:associated_whc_property`→ `whc:<id_no>` |
| `wikipedia.olympic_medal_tables` | 1,952 | country QID (or `ioc:<code>`) —P1344 participant in→ Games QID (point in time = year held; medal counts as qualifiers); Games QID —P17→ host country |

* Country objects come only from `registry/entities/country_qids.json` (WDHARVEST). Palestine (`PSE`) has no QID there
  (ambiguous on Wikidata): its 9 ICH and 6 WHC country statements are reported (`countries_without_qid`), not loaded.
  The Old City of Jerusalem has no State Party code in the List: no country statement.
* Dataset predicate registered by A-CULTURE: `unesco:associated_whc_property` (item; inverse
  `associated_ich_element`). P17/P1435/P757/P1344 use WDHARVEST's allow-list definitions.
* Example paths: France —P17⁻¹→ `whc:1567` (Funerary and memory sites of the First World War) —P17→ Belgium;
  France —P17⁻¹→ `ich:2106` (art of dry stone walling, 13 States) —P17→ Italy. **"Portugal et Brésil — co-inscrits"
  is not true per source**: no World Heritage property and no ICH element is shared by Portugal and Brazil (2026-10-15
  revisions); Argentina-Brazil share one transboundary property (Jesuit Missions of the Guaranis).
* Entities written: only namespaced ones (`whc:*`, `ich:*`, `unesco:ich_list:*`, `ioc:*`); never Q-entities
  (WDHARVEST's snapshots stay authoritative for Wikidata items).

## 4. Requests to other agents / lead

1. **ATLASCORE:** merge `registry/atlas/budget_requests/a_culture.yaml` (new keys `unesco.ich`,
   `wikipedia.olympic_medal_tables`; increments on `unesco.whc` and `unesco.uis`). Until merged,
   `test_invariant_30_budgets.py::test_budget_keys_are_registered_sources_and_cover_every_connector` lists the two new
   source keys (alongside other agents' pending keys); `test_every_live_provider_request_path_is_budgeted` passes
   (no client is built in A-CULTURE modules).
2. **WDHARVEST / KGCULT:** a `whc:<id_no>` ↔ Wikidata item join is possible on P757 (both sides carry it); a
   `same_as`-type predicate is not created here. Snapshot the 55 Games QIDs (subjects/objects of
   `wikipedia.olympic_medal_tables` statements) if not already harvested.
3. **LINKS:** country-country edges can be derived from shared `whc:*` / `ich:*` subjects (P17) and from the Games
   hubs (P1344, ≤ ~200 participants per Games).
4. **Lead:** the seed registers the four sources automatically (discovered `atlas_*` modules).

## 5. Run it

```
uv run python -m reva.atlas.ingest atlas.unesco_ich [--fixture]
uv run python -m reva.atlas.ingest atlas.unesco_whc_links [--fixture]
uv run python -m reva.atlas.ingest atlas.uis_culture [--fixture]
uv run python -m reva.atlas.ingest atlas.olympics_medals [--fixture] [--option games=1960s,2026w]
uv run pytest -m "live and db" tests/integration/test_atlas_culture_live.py --basetemp=<scratch>   # throwaway DB
```
