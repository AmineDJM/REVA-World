# V3 — KGLANG: languages connect peoples (Glottolog, WALS, Grambank, Wikidata P1394)

Agent KGLANG, 2026-10-16. One live ingest of all four sources into a throwaway database
(`reva_kglang_live_b2`, dropped afterwards, its object store deleted) through the atlas platform
(`reva.atlas.ingest.run_atlas_ingest`, live mode, budget-metered). The dev database `reva` was never touched.
Tests are offline (trimmed fixtures captured from the live responses; every manifest entry says what was trimmed).

## 1. Sources

| Source | Access | Licence (checked every run) | Version loaded | Decision |
|---|---|---|---|---|
| **Glottolog** | Zenodo concept record 3260727 → `api/records/3260727/versions/latest` → the version's single zip (md5 + size checked against the record) | CC BY 4.0 (record `license.id`; anything else blocks the run) | **v5.3**, 2026-03-02, doi:10.5281/zenodo.18840967 (49.6 MB) | KG + atlas table |
| **WALS Online** | Zenodo concept 3606197, same flow | CC BY 4.0 | **v2020.6**, 2026-09-28, doi:10.5281/zenodo.23010827 (12.6 MB) | KG (feature values) |
| **Grambank** | Zenodo concept 7740139, same flow | CC BY 4.0 | **v1.0.3**, 2023-04-19, doi:10.5281/zenodo.7844558 (9.1 MB) | KG (feature values; size permits: +362k statements) |
| **Wikidata P1394** (Glottolog code) | one WDQS query through WDHARVEST's `WikidataClient` (machine-wide lock, Retry-After, raw archived) | CC0 | live, 2026-10-16 (11,963 rows, 5.6 MB) | KG cross-reference edges |

GitHub (codeload/API) is not reachable from this container; Zenodo is the archival home of all three CLDF
datasets anyway and its record gives DOI, version, licence and checksum. zenodo.org reset connections a few times
(SafeHttpClient retries transport failures; the budget request leaves room for a retried run).

Why WDQS and not the Wikibase API for P1394: the Wikibase API has no "items having P1394, with values" listing.
CirrusSearch `haswbstatement:P1394` returns ids only, at most 10,000 per query (11,782 items exist), and the values
would need ~240 full-claims `wbgetentities` calls on large language items (hundreds of MB). One WDQS request
(5.6 MB) under the machine lock is lighter; it waited 820 s for the shared WDQS cool-down set by other agents' 429s.

## 2. Files

| Path | What |
|---|---|
| `src/reva/adapters/connectors/atlas_glottolog.py` | `atlas.glottolog` (KG + atlas) and `atlas.glottolog_wikidata` (P1394 edges); shared Zenodo/CLDF/KG-load helpers used by WALS and Grambank |
| `src/reva/adapters/connectors/atlas_wals.py`, `atlas_grambank.py` | `atlas.wals`, `atlas.grambank` (KG only) |
| `registry/kg/predicates_lang.yaml` | dataset predicates (registered by the connectors via `register_properties`, like `cow:code_denotes` and KGPOLITY's `predicates_polity.yaml`); the file is part of the code digest |
| `registry/sources/{glottolog,wals,grambank}.yaml` | source keys `glottolog.cldf`, `wals.cldf`, `grambank.cldf` |
| `registry/atlas/catalog/languages.yaml` | 8 concepts, domain `languages`, theme `culture` |
| `registry/atlas/budget_requests/kglang.yaml` | 8 + 6 + 6 requests/day; Wikidata step uses WDHARVEST's existing `wikidata.wdqs` budget |
| `registry/kg/queries/culture/glottolog_items.yaml` | WDHARVEST seed spec: every item with P1394 (labels/snapshots for the objects of the P1394 edges; complements KGCULT's ISO/family/ancestor seeds, `depends_on` them) |
| `tests/fixtures/source/atlas.{glottolog,glottolog_wikidata,wals,grambank}/` | trimmed fixtures (≤ 32 kB each) |
| `tests/unit/test_kg_lang.py` (17), `tests/integration/test_kg_lang_ingest.py` (3) | offline tests |

Run: `uv run python -m reva.atlas.ingest atlas.glottolog` (then `atlas.glottolog_wikidata`, `atlas.wals`,
`atlas.grambank`; `--fixture` for the fixtures).

## 3. KG model

Entities `glottolog:<glottocode>` of kind `family` / `language` / `dialect` (all 27,177 languoids; labels en =
Glottolog name; French names only from Glottolog's `names.csv` (Lexvo): one → fr label, several → fr aliases).
Statement ids are stable across releases (`parent:<gc>`, `spoken_in:<gc>:<ISO2>`, `value:<CLDF row id>` …) and
carry no version or line number, so a new release re-stores only what changed, and a fact a release drops is
retracted (`removed_at_source`). Every statement's own reference is its CLDF selector
`{"table": "languages.csv"|"values.csv", "row_id": …, "column"|"parameter": …}`; `raw_sha256` = the archived
release zip; `source_revision` = `<version> doi:<version DOI>` of the release that first recorded it.

| Predicate | Kind | From | Live count |
|---|---|---|---|
| `glottolog:parent` | item | values.csv `classification` (last element) | 26,748 |
| `glottolog:family` | item | languages.csv `Family_ID` | 26,748 |
| `glottolog:in_group` | item | `classification`, languages only: each intermediate group (not parent, not top family), qualifier `steps_up` | 34,584 |
| `glottolog:spoken_in` | item | languages.csv `Countries` (ISO alpha-2 → crosswalk ISO3 → `country_qids.json` QID, else `iso3:<KEY>`) | 11,909 (11,900 to a QID, 9 to `iso3:` nodes; 246 countries) |
| `glottolog:endangerment` | string | values.csv `aes` (+ EGIDS/UNESCO/ElCat equivalences, comment, source ids) | 8,672 |
| `glottolog:iso639_3` | external_id | `ISO639P3code` | 8,184 |
| `glottolog:macroarea` | string | `Macroarea` (families + languages) | 13,735 |
| `glottolog:category` | string | values.csv `category` (families + languages) | 13,471 |
| `glottolog:coordinates` | globe_coordinate | Latitude/Longitude (languages; decimal strings as published) | 8,304 |
| `glottolog:wikidata_item` | item | Wikidata P1394, source key `wikidata.wdqs` (community-edited) | 11,962 |
| `wals:f<feature>` (192) | string | WALS values.csv, value = code name (`SOV`) | 76,475 |
| `grambank:<feature>` (195) | string | Grambank values.csv, value = code description (`absent`, `N-Num`) | 362,025 |

Totals in the live database: 27,194 entities (27,177 Glottolog + 17 `wals:language:*`), **604,833 current
statements**, 0 rejected, 0 conflicting. `kg.statement` 637 MB on disk for the whole throwaway database
(≈ 0.6 kB/statement, as KGCORE measured; Grambank is 60 % of it). Load times on the shared host: Glottolog 65–76 s
(incl. the 50 MB download), WALS 66 s, Grambank 183 s (two batches), Wikidata 922 s (820 s of it waiting for the
shared WDQS cool-down). A second Glottolog run (connector 1.1.0, adding `in_group`) inserted the 34,584 new
statements and found the other 117,771 and all 27,177 entities unchanged.

WALS: 2,662 languages; 17 have no Glottocode in WALS (`alb`, `arr`, `aze`, `bbf`, `biq`, `cln`, `dab`, `gon`,
`gwa`, `jak`, `lcr`, `leg`, `mla`, `mrq`, `ndi`, `tam`, `tsm` → `wals:language:<id>` entities, never mapped by
name); 143 WALS languages share a Glottocode with another WALS language (statements stay distinct, qualifier
`language_id`). Every WALS and Grambank Glottocode exists in Glottolog 5.3. Grambank: 79,638 `?` values (not
decidable from the sources consulted) are not stored, counted in the run report.

Wikidata P1394: 11,962 statements (1 malformed code skipped), 11,878 codes on 11,781 items; 82 codes are on
several items and 169 items carry several codes (all kept, with Wikidata statement id and rank; 44 preferred).
**165 codes on Wikidata are not Glottolog 5.3 languoids** (retired or superseded codes, e.g. `stan1291`,
`indo1326`, `sign1235`): their edges start at bare `glottolog:` nodes; listed in the run report for Wikidata
editors / WDHARVEST.

Typology is stored as literals on purpose: two languages sharing a word order are not "connected" (no path-engine
edge). Neighbours with literals show them on a language page.

## 4. Example chains (live, with citations)

All hops are `glottolog.cldf` statements from Glottolog 5.3 (doi:10.5281/zenodo.18840967, raw sha256
`d302baaf…f063` = the release zip), selector `values.csv <gc>-classification` or `languages.csv <gc>/Countries`.

* **Portuguese genealogy** (`glottolog:parent`, 15 hops): Portuguese → Brazil-Portugal Portuguese →
  Macro-Portuguese → Galician Romance → West Ibero-Romance → Southwestern Shifted Romance → Shifted Western Romance
  → Western Romance → Italo-Western Romance → **Romance** → Imperial Latin → Latinic → Latino-Faliscan →
  **Italic** → Classical Indo-European → **Indo-European**. Shortcuts: `family` Portuguese → Indo-European,
  `in_group` Portuguese → Romance / Italic / ….
* **"Les Brésiliens parlent portugais"**: path engine `Q155 (Brazil) –spoken_in← Portuguese –spoken_in→ Q45
  (Portugal)` (2 hops); `Q155 ← Portuguese –wikidata_item→ Q5146` (Portuguese on Wikidata, P1394 statement
  `Q5146$14E8544F-…`, community-edited); Brazil → Romance and Brazil → Italic in 2 hops (`spoken_in`, `in_group`);
  Brazil → Indo-European in 2 hops (`spoken_in`, `family`; Indo-European has 3,238 incoming statements, above the
  default hub limit, so it is reached as an endpoint but not traversed).
* **Creoles** are only where Glottolog classifies them; REVA adds no lexifier link. Glottolog 5.3 places Haitian
  (`hait1244`, spoken in HT and DO) under Circum-Caribbean French → Macro-French → Central Oïl → Oïl → Gallo-Rhaetian
  → … → Romance: Haiti → Romance is 2 hops (`spoken_in`, `in_group`). Kabuverdianu (`kabu1256`, Cape Verde) sits under
  Galician Romance (the Portuguese branch): Cape Verde → Galician Romance → Portugal is found within 4 hops.
* Brazil in the atlas (Glottolog 5.3 counts): 337 language-level languoids list BR; 269 spoken L1 languages, of
  which 172 living (AES 1–5), 163 endangered (AES 2–5), 97 extinct; 11 sign languages.

## 5. Atlas: `glottolog.languages` (4,636 cells, 244 countries, 0 unmapped)

Exact counts of Glottolog records per country of `Countries` (language-level languoids), period = release day
(2026-03-02), `flags.aggregation = count`, `flags.derived_by`, version and DOI on every cell; selector = criteria +
sha256 of the counted glottocodes (the list itself when ≤ 25). Indicators: `languages_listed`, per category
(`languages_listed|category=…`), spoken L1 per AES (`spoken_l1|aes=…`, incl. `none`), `spoken_l1_living`,
`spoken_l1_endangered`. Catalogued concepts: `languages.languages_listed`, `spoken_l1_listed`,
`living_spoken_l1`, `endangered_spoken_l1`, `not_endangered_spoken_l1`, `nearly_extinct_spoken_l1`,
`extinct_spoken_l1`, `sign_languages_listed`.

| Country | listed | spoken L1 | living | endangered | extinct | sign |
|---|---|---|---|---|---|---|
| PNG | 899 | 869 | 850 | 452 | 19 | 7 |
| IDN | 756 | 727 | 702 | 568 | 25 | 5 |
| NGA | 589 | 550 | 522 | 216 | 28 | 7 |
| IND | 518 | 478 | 450 | 251 | 28 | 4 |
| BRA | 337 | 269 | 172 | 163 | 97 | 11 |
| USA | 323 | 284 | 147 | 131 | 137 | 5 |
| FRA | 37 | 29 | 24 | 11 | 4 | 1 |
| PRT | 11 | 9 | 9 | 3 | 0 | 1 |

These count what Glottolog lists (a language spoken in several countries counts in each; diaspora entries
included where Glottolog records them); they are not speaker numbers and not an official-language count.

## 6. Honesty and limits

* Glottolog's classification is one scholarly classification; contested macro-families are absent because
  Glottolog does not accept them. Pseudo-families (Pidgin, Mixed Language, Sign Language, Unclassifiable,
  Bookkeeping, Artificial Language, Speech Register) are top-level units in Glottolog; their `category` says so,
  and `glottolog:family` to them is not a genealogical claim.
* `spoken_in` is Glottolog's country list, not official status (Wikidata P37 stays KGCULT's) and not speaker counts.
* `glottolog:wikidata_item` is Wikidata's (community-edited) statement, labelled so by the API.
* Atlas cells are REVA counts of Glottolog records, named as such. No imputation; countries with no record have no row.

## 7. Requests to other owners

1. **ATLASOPS** (budgets): merge `registry/atlas/budget_requests/kglang.yaml`
   (`uv run python -m reva.atlas.budget_merge --write`, then re-pin `budgets.yaml`). Until then
   `tests/invariants/test_invariant_30_budgets.py::test_budget_keys_are_registered_sources_and_cover_every_connector`
   fails **only** on `glottolog.cldf`, `wals.cldf`, `grambank.cldf` (registered sources without a pinned budget —
   the merge tool needs the source files to exist first), and a live run of the three is refused (fail closed). The
   live run above used a throwaway-only patch of `load_budget` adding exactly the requested numbers.
2. **LINKS** (`registry/links/link_types.yaml`): language links can use Glottolog edges (no allow-list change needed:
   the predicates are registered by the connector). Suggested additions:
   ```yaml
   - id: shared_language_glottolog
     labels: {fr: "langue parlée dans les deux pays (Glottolog)", en: "language spoken in both (Glottolog)"}
     scope: country_country
     directed: false
     derivation: {method: kg_shared_object, properties: ["glottolog:spoken_in"], sources: [glottolog.cldf], via: language}
     temporal: {kind: none, description: "Glottolog lists are undated (state of the release)"}
     weight: {kind: none}
     honesty: {fr: "relié via : langue parlée dans les deux pays selon Glottolog ({via})", en: "connected via: language spoken in both per Glottolog ({via})"}
     causal: false
     status: derivable
     path_cost: 1.3
   ```
   and for `shared_language_family`, `properties` could add `glottolog:family`, `glottolog:in_group` (Glottolog's
   genealogy, Glottocode-keyed, no upper-ontology noise). In the `property_labels` list:
   `{property: "glottolog:spoken_in", labels: {fr: "parlée dans", en: "spoken in"}, inverse_labels: {fr: "a pour langue parlée", en: "has language spoken"}, path_cost: 1.2}`,
   `{property: "glottolog:family", …, path_cost: 1.5}`, `{property: "glottolog:in_group", …, path_cost: 1.5}`,
   `{property: "glottolog:wikidata_item", …, path_cost: 0.5}`.
3. **WDHARVEST**: run `culture.glottolog_items` (≈ 11.8k items, most already seeded by KGCULT) so the objects of the
   P1394 edges have labels; consider the 165 P1394 values that are not Glottolog 5.3 languoids.
4. **Lead**: the seed registers `atlas.glottolog`/`atlas.glottolog_wikidata` 1.1.0, `atlas.wals`/`atlas.grambank`
   1.0.0 automatically (discovered modules).
