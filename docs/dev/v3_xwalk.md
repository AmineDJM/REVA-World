# V3 — XWALK: the country crosswalk covers all of ISO 3166-1 (Taiwan added), with UN status

Agent XWALK owns `registry/entities/country_crosswalk.json`, its builder
`src/reva/adapters/connectors/country_crosswalk.py` and `tests/unit/test_country_crosswalk.py`.
Review status: **agent-built, not reviewed by a human maintainer** (`review_status:
machine_derived_pending_maintainer_review`, `review_note` in the file).

## What changed (crosswalk 1.0.0 → 1.1.0)

| | 1.0.0 (V2, ALLC) | 1.1.0 (V3, XWALK) |
|---|---|---|
| entity rule | ISO alpha-3 codes **listed by UNSD M49** + World Bank economies without one | **every ISO 3166-1 code** + World Bank economies without one |
| entities | 250 | **251** |
| ISO 3166-1 entities | 248 | **249** (all officially assigned codes) |
| non-ISO entities | 2 (`XKX` Kosovo, `CHI` Channel Islands: World Bank codes) | 2 (unchanged) |
| World Bank economies | 217 | 217 |
| Comtrade reporters mapped | 219 | 219 |
| Comtrade reporter / partner codes unmapped | 36 / 72 | 36 / **71** (partner 158 now maps to TWN) |
| UN member states / observer states recorded | — | **193 / 2** (`VAT`, `PSE`) |

Only one entity is added: **TWN**. The other entities named in the task — ATA, BVT, HMD, UMI, IOT, ATF,
SGS, CCK, CXR, NFK, PCN, TKL, ESH, VAT, BES, SJM, ALA, GGY, JEY, IMN — were already present (M49 lists
them). The builder now checks that every ISO code M49 lists exists in ISO 3166-1 with the same alpha-2
and numeric code (all 248 agree) and fails closed otherwise.

Every existing entity keeps every field value it had; fields are only added, plus the flag
`aggregation_overlap` on CHN, SRB and XKX (below). Verified by diffing against the 1.0.0 file.
`python -m reva.adapters.connectors.country_crosswalk verify` rebuilds the file from the archives and
compares (`content_sha256 d09e6b0f…`).

## Sources (archived under `registry/reference/`, digests in `manifest.yaml` and in the crosswalk)

| name | archive | origin | sha256 (raw) | retrieved | licence |
|---|---|---|---|---|---|
| `iso3166_1` | `iso-codes_4.20.1_iso_3166-1.json` | Debian **iso-codes 4.20.1** (tag `v4.20.1`, commit `74623235ae99…`, 2026-01-01), `data/iso_3166-1.json` on salsa.debian.org | `f01b812b57fb…c89f` | 2026-10-15T23:12:13Z | LGPL-2.1-or-later |
| `iso3166_1_fr` | `iso-codes_4.20.1_iso_3166-1_fr.po` | same tag, `iso_3166-1/fr.po` | `adda8487a0bb…58d7` | 2026-10-15T23:12:14Z | LGPL-2.1-or-later |
| `unsd_m49_home` | `unsd_m49_home.html.gz` | https://unstats.un.org/unsd/methodology/m49/ (FAQ) | `4d5cabcb10ad…b01d` | 2026-10-15T23:11:35Z | United Nations |
| `un_member_states` | `un_member_states.html.gz` | https://www.un.org/en/about-us/member-states | `9f1d81ab68ed…169e` | 2026-10-15T23:11:41Z | United Nations |
| `un_non_member_states` | `un_non_member_states.html.gz` | https://www.un.org/en/about-us/non-member-states | `516294ce51c3…6cf4` | 2026-10-15T23:11:45Z | United Nations |

The four 1.0.0 sources (M49 overview, Comtrade Reporters/partnerAreas, World Bank country list) are
unchanged. ISO's Online Browsing Platform is not machine-readable; Debian iso-codes is the
long-standing public mirror of the ISO 3166 lists (249 entries in 4.20.1). The HTML pages are stored as
`gzip(raw, mtime=0)`; the digest is of the raw bytes.

## Status fields (copied from sources, never inferred)

Per entity (new in 1.1.0):

* `in_iso3166_1`, `iso_numeric`, `iso_name` — ISO's English short name, verbatim from iso-codes
  (`TWN`: **"Taiwan, Province of China"**; `PSE`: "Palestine, State of"; `VAT`: "Holy See (Vatican City State)").
* `label`, `label_source`, `label_fr` — display label: the M49 name (248 entities, unchanged), else
  the World Bank name (`XKX`, `CHI`, unchanged), else ISO's `common_name` (only `TWN`: **"Taiwan"**,
  `label_fr` **"Taïwan"** = iso-codes' French translation of that common name).
* `un_membership` — `member_state` (193, with `un_name` as written on the UN page and
  `un_admission_date`), `non_member_observer_state` (Holy See, State of Palestine: the UN page's
  "Non-Member States having received a standing invitation to participate as Observers…"), or
  `not_listed` (56 entities: territories, `TWN`, `XKX`, `CHI`, …). UN page names map to ISO codes by
  M49/ISO name; four UN labels need a listed override ("Bahamas (The)", "China (the People's Republic
  of)", "Gambia (Republic of The)", "Guinea Bissau"), and every member card's flag code (an ISO alpha-2
  in the page) must agree with the matched code, or the build fails.
* `aggregation_notes` + flag `aggregation_overlap` — see next section.
* flag `not_listed_in_un_m49` — ISO code that the M49 overview does not list (only `TWN`): no `m49`
  code, region or `un_m49` alias is given.

REVA records no sovereignty judgement of its own: the only status texts are ISO's names, the UN pages'
lists and the UNSD FAQ quoted verbatim.

## CHN/TWN (and SRB/XKX) aggregation semantics — never summed, never split

UNSD M49 FAQ, quoted verbatim in `aggregation_notes` of both entities:

> Q: Why is Taiwan Province of China not shown in the M49? — On the 25th October 1971, the UN General
> Assembly adopted a resolution (2758) to recognize the representatives of the Government of the
> People's Republic of China as the only legitimate representatives of China to the United Nations. As
> a result, within the M49, Taiwan Province of China is considered part of China (numerical code 156).
> However, for strictly statistical purposes, the numerical code 158 can be used to represent this area.

The same FAQ has the same structure for Kosovo (considered part of Serbia 688 within M49; 412 for
statistical purposes), so `SRB`/`XKX` carry the same flag and note. Consequence for data users: a
provider's "China" may or may not include Taiwan (FAO 351 "China" includes it, WPP 156 does not); each
atlas row keeps its provider geography and the provider's territorial definition. **REVA never adds TWN
into CHN nor splits CHN** (`reva_policy` in the note).

## Provider codes for Taiwan

| Provider / scheme | Code | Provider label | 1.1.0 |
|---|---|---|---|
| ISO 3166-1 / `iso3`, `iso2` | TWN / TW | Taiwan, Province of China | → TWN |
| ISO numeric / `m49` scheme (`country_iso3(…, "m49")`) | 158 | (ISO) Taiwan, Province of China | → TWN (via `iso_numeric`, see ATLASOPS note) |
| Comtrade partner | 158 | "Taiwan, Province of China" | → TWN (`comtrade_partner`, informational; bilateral rows keep provider codes) |
| Comtrade reporter / partner | 490 | "Other Asia, nes" | **unmapped** (`nes_area`; not Taiwan by definition) |
| GeoNames FIPS (`country_codes_extra.json`, rebuilt) | TW | Taiwan | → TWN (was in `skipped`) |
| World Bank | — | Taiwan has no WB economy | no `worldbank` code; WGI rows `TWN` map via iso3 |

Atlas schemes built on `iso3`/`iso2`/`m49` pick TWN up automatically. Verified in the unit fixtures:
IMF (WEO/IFS `TWN`), ILOSTAT (`TWN` "Taiwan, China"), V-Dem (`TWN`), Maddison (`TWN`), Gapminder (`twn`),
HYDE (`158`, documented as ISO 3166 numeric), UN WPP/IMS (`158` "China, Taiwan Province of China"),
FAO (area 214, M49 158), WID (`TW`), World Bank WGI (`TWN`), GeoNames, Factbook (FIPS `TW`, alias in
`a_factbook.yaml`). UN SDG / UNSD Statistical Yearbook label 158 "Other non-specified areas (in Eastern
Asia)": these rows also map to TWN, by the code's definition (ISO numeric 158; UNSD's own M49 FAQ:
"158 can be used to represent this area") — unlike 490, whose definition is a residual "nes" area. The
row keeps the provider label in its flags. If the owner prefers label-only mapping for those two UN
products, A-UN can add `exclude: ["158"]` to its `un_m49x` scheme.

Tables registered before 1.1.0 keep `iso3 = null` on Taiwan rows until they are re-registered (a new
table version; identical other content).

## Unmapped provider codes that remain, and why (never forced)

Crosswalk (Comtrade reference lists):

* **Comtrade reporters (36)**: 3 groups (97 European Union, 490 Other Asia nes, 975 ASEAN); 33
  historical codes valid only before 2019 (58 Belgium-Luxembourg, 200 Czechoslovakia, 278/280 the two
  Germanys, 810 USSR, 890 Yugoslavia, 891 Serbia and Montenegro, 530/532 Netherlands Antilles, 736
  former Sudan, the 1974/1977/1980 USA, Panama, Vietnam, Yemen, India codes, French overseas
  departments reported before France 251 absorbed them, …).
* **Comtrade partners (71)**: 0 World; 16 "nes" areas (129, 221, 290, 471, 472, 473, **490**, 492 —
  whose provider ISO3 field says MCO —, 527, 568, 577, 636, 637, 697, 879, 899); 36 historical codes (as
  above plus 457 Sarawak, 647 Ryukyu, 698 Sikkim); 8 ambiguous pairs with one ISO3 and different
  territories (250/251 France, 578/579 Norway, 756/757 Switzerland, 840/842 USA; the reviewed
  `countries.yaml` aliases handle the four V1 countries); 10 codes whose provider ISO3 field is not an
  ISO code: 80 British Antarctic Territories, 488 Midway, 536 Neutral Zone, 837 Bunkers, 838 Free
  Zones, 839 Special Categories, 849 US Misc. Pacific Islands, 872 Wake Island — and 412 Kosovo (`_KS`),
  830 Channel Islands (`_CI`), which name crosswalk entities keyed by World Bank codes but are not
  mapped by the ISO3 rule (bilateral rows keep provider partner codes anyway).
* **World Bank aggregates (79)**: regions, income and lending groups — excluded.

Atlas providers (from the `docs/dev/v3_a_*.md` coverage reports; kept with `iso3 = null`, listed):

* Aggregates and regions: World Bank (49 in WDI), IMF G001/G110/… and department areas, ILO `X..`
  (87), OWID continents/income groups/"(EI)" regions, UN 900/1834…, FAO 351 "China" (incl. Taiwan,
  Hong Kong, Macao — an aggregate, never CHN).
* Historical states: USSR, Czechoslovakia, Yugoslavia, Serbia and Montenegro, East/West Germany,
  Netherlands Antilles, former Sudan/Ethiopia, Prussia, Bavaria, Baden, … (KG historical states).
* Disputed or partial areas: V-Dem `PSE` = "Palestine/West Bank" and `PSG` "Palestine/Gaza" are **not**
  the State of Palestine (`PSE` entity) — both kept unmapped (A-POL); WHR "North Cyprus", "Somaliland
  Region"; LIED "Palestine/West Bank|vdem=128", "Palestine/Gaza|vdem=138", "Somaliland|vdem=139";
  Natural Earth disputed polygons; COW 601 Sahrawi.
* Taiwan in **name-keyed or hand-listed schemes**, which do not follow the crosswalk automatically and
  still say "not a crosswalk entity": A-LAW (SSPTW `taiwan`, Constitute `Taiwan`, CCP `713|Taiwan`),
  A-POL (LIED `Taiwan|vdem=48`, BMR COW `713`), A-QOL (WHR "Taiwan Province of China": `None` in
  `atlas_whr.py`), A-SCI (Nobel "Taiwan" in `atlas_nobel.py`). Each agent may now map them with its own
  evidence (the provider labels them Taiwan); XWALK did not edit those files.

## V1/V2 behaviour

* `worldbank_economies()` and `comtrade_reporters()` are unchanged (TWN has neither), so the
  `worldbank_all` / `comtrade_batch` mapping scopes and the ALLC all-countries runs are identical.
  `countries: ["ALL"]` selects countries with estimates in the release: TWN has none (no WB/Comtrade
  data), so it is not selected; requested explicitly, it is a typed missing-input gap, never a zero.
* `ALIAS_MAPPING_VERSION` stays `crosswalk-1.0.0` (it versions the alias rules, which did not change;
  a bump would duplicate every seeded alias row). The seed fingerprint includes the crosswalk sha256, so
  the next `reva seed` adds the entity TWN (label "Taiwan", alias `iso3166_alpha3:TWN`).

## Changes outside the crosswalk

* `src/reva/atlas/tables.py` (ATLASOPS/ATLASCORE): `geo_index()` also indexes an entity's
  `iso_numeric` into the `m49` scheme with `setdefault` (only TWN 158 differs from `m49`). Backward
  compatible: no existing code changes target.
* `registry/entities/country_codes_extra.json`: rebuilt with the unchanged builder
  (`python -m reva.atlas.country_codes`): FIPS `TW` → TWN.
* Tests that asserted "TWN unmapped": `test_atlas_un.py`, `test_atlas_ilo.py`, `test_atlas_imf.py`,
  `test_atlas_histecon.py`, `test_atlas_pol.py`, `test_atlas_qol_connectors.py` (WID now fetches `TW`);
  integration `test_atlas_api.py` (`countries_total` 251), `test_atlas_pol_ingest.py`,
  `test_atlas_ilo_ingest.py`, `test_atlas_imf_db.py`, `test_atlas_qol_ingest.py` (one more WID raw),
  `test_comtrade_worldbank_all_countries.py` (249 ISO entities seeded).

## Tests

`tests/unit/test_country_crosswalk.py` (14): rebuild equality, sources and version, every ISO code an
entity, TWN fields (verbatim ISO name, neutral label, partner 158 mapped, 490 not), overlap notes on both
sides, UN membership counts and names, fail-closed cases (ISO/M49 disagreement, ISO code missing,
partial UN page, UN name/flag disagreement, missing M49 FAQ), and the atlas geo index (`158`, `TW`
FIPS → TWN; `490` → nothing).
* Docs: `docs/roadmap/v3_world_graph.md`, `docs/dev/v3_atlascore.md`, `docs/dev/v3_atlas_interface.md`,
  `docs/dev/v2_allc.md` (count note).
