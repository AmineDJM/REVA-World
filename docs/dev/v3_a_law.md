# V3 atlas — laws and legal systems (agent A-LAW)

Status 2026-10-15. Connectors in `src/reva/adapters/connectors/` on the ATLASCORE source platform
(`atlas_source()` + `async atlas_ingest(run)`; every request through `run.get`, budget-metered):
`atlas_wbl.py`, `atlas_constitute.py`, `atlas_ccp.py`, `atlas_factbook.py`, `atlas_ssa_ssptw.py`
(shared helpers `atlas_law_common.py`). Catalog: `registry/atlas/catalog/law.yaml` (40 provider-declared
concepts). Geo schemes: `registry/atlas/geo_aliases/a_law.yaml` (`wbl`, `ssptw`, `constitute`, `ccp`; every
alias names its evidence; manual ones are marked "maintainer review pending"). Sources:
`registry/sources/{worldbank_wbl,constitute,ccp,factbook,ssa_ssptw}.yaml`. Budgets:
`registry/atlas/budget_requests/a_law.yaml`. KG specs (Wikidata legal ages): `registry/kg/queries/society/law_*.yaml`
+ `registry/kg/property_requests/a_law.yaml`.

## Sources, licences and decisions

| Source | What | Licence / terms (checked 2026-10-15) | Decision |
|---|---|---|---|
| World Bank **Women, Business and the Law** | WBL 1.0 panel 1971-2024 (index, 8 topics, 35 questions + legal basis), WBL 2026 (2.0: legal / supportive / enforcement, 268 codes incl. **retirement ages men/women** and legal basis), WBL 1.0 values 2026 | CC BY 4.0 | ingested (3 tables) |
| **Constitute Project** JSON service | constitutions list: in force, year enacted, latest revision, years amended (240 records) | site terms: no fee, **commercial use prohibited**, no open-data licence | metadata list only (no texts, several are copyrighted translations); restriction on every cell |
| **CCP** Chronology v6 / Characteristics v5 | constitutional events 1789-2025; constitutional voting age, capital punishment, same-sex marriage, civil marriage, system year 1789-2023 with article refs | no explicit data licence; citation requested; project web terms prohibit commercial use | ingested; restriction recorded |
| **CIA World Factbook** (CC0 mirror factbook/factbook.json) | legal system, suffrage, constitution history/amendment, citizenship, ICJ/ICC participation (as of 2026-01-22; Factbook discontinued Feb 2026) | public domain / CC0 | ingested (255 profiles, pinned commit) |
| **SSA SSPTW** 2018-2019 (archived, final editions) | per-country old-age program: laws, type, contribution texts, qualifying conditions, pension ages | US Government work (with ISSA) | parser done + fixture-tested; **live ingest pending the budget merge** (see below) |
| Wikidata | age of majority P2997, marriageable age P3000, retirement age P3001, age of consent P2999, age of candidacy P2998 | CC0 | statement specs written; **pending WDHARVEST allow-listing** of the 5 properties |
| JuriGlobe (Ottawa) | legal-system families | no open licence found | not used; Factbook text + keyword mention list instead |
| Amnesty (death penalty), ILGA (same-sex unions) | | copyrighted / restricted | not used; only CCP *constitutional* status is available (statutory status = gap) |

Minimum-wage law is A-ILO's (not here).

## Live ingest (throwaway test DB, `tests/live/test_atlas_law_live.py`, budget-metered)

| Atlas table | rows | countries | period | requests / bytes | unmapped provider geos (kept) |
|---|---|---|---|---|---|
| `wbl.panel_1971_2024` | 861,840 | 189 | report years 1971-2024 (as-of 1970-10-01 … 2023-10-01) | 3 / 12.2 MB (3 tables) | TWN |
| `wbl.wbl2026` | 55,100 | 189 | report years 2024-2026 (LF), 2026 (SF, EP) | | TWN |
| `wbl.wbl1_2026` | 9,310 | 189 | report year 2026 | | TWN |
| `constitute.constitutions` | 1,550 | 194 | snapshot 2026-10-15 | 1 / 0.15 MB | Baden, Bavaria, Germany_Prussia (Taiwan → TWN since crosswalk 1.1.0) |
| `ccp.chronology_v6` | 41,978 | 193 | 1789-2025 | 2 / 119 MB (2 tables) | 32 historical states (Prussia, Austria-Hungary, Yugoslavia, …); 713 Taiwan → TWN since crosswalk 1.1.0 |
| `ccp.characteristics_v5` | 93,296 | 193 | 1789-2023 | | 23 historical states; Taiwan → TWN since crosswalk 1.1.0 |
| `factbook.legal_2026_01` | 2,369 | 240 | as published 2026-01-22 | 255 / 9.7 MB | AT, AX, BQ, CR, DX, EE (EU), IP, JN, TW, UM, WQ |
| `ssa.ssptw_2018_2019` | — | — | — | (probe only, 1 request) | budget 10/day < 192 planned: refused until a_law.yaml is merged |

Disk: raw archives ≈ 142 MB + Parquet (temporary test store); well under the 1.5 GB agent budget.

## Concept coverage (live run; of 250 crosswalk entities; 197 have any legal data)

| Concept | Countries |
|---|---|
| WBL 1.0 index (2026; panel 1971-2024) | 189 / 189 |
| WBL 2026 legal-frameworks index | 189 |
| Full-pension age men / women (WBL 2026, legal basis per cell) | 180 numeric + 9 "NS" (no system) |
| Pension age men/women, SSPTW 2018-19 (parser on all 184 pages, research copy) | 154 derived by rule, 29 text-only; `taiwan` ("Taiwan (China)") → TWN since crosswalk 1.1.0 |
| Constitution in force: year enacted (Constitute) | 187 |
| CCP last "new" constitution / system year | 193 / 191 |
| Constitutional voting age (CCP 2023; numeric 154, label 122 over time) | 192 |
| Constitutional capital punishment / same-sex marriage (CCP) | 192 / 192 |
| Minimum voting age (Factbook suffrage, agent rule) | 227 (suffrage text 233) |
| Legal families mentioned / legal-system text (Factbook) | 221 / 240 |
| Child marriage: legal age ≥ 18 both sexes (WBL 2026) | 189 |

### Gaps among the 197 entities with legal data, and what fills them

* Constitution in force (Constitute) missing: BFA CAF GAB GIN HKG OMN PRI SMR SYR TCD — CCP chronology/characteristics
  cover all but HKG, PRI (not states in CCP); SMR only in characteristics.
* WBL missing: AND CUB LIE MCO NRU PRK TKM TUV (not WBL economies).
* Retirement age (WBL) missing or "NS": AFG AND BGD BTN CUB ERI LBN LIE MCO MMR NRU PRK PSE SOM TKM TUV ZAF —
  SSPTW 2018-19 gives ages for AND BGD BTN CUB LBN LIE MCO MMR TKM ZAF (older edition, first-listed program; e.g.
  Bangladesh is a social pension); still none: AFG ERI NRU PRK PSE SOM TUV.
* Minimum voting age (Factbook rule) missing: ARE ARG BRA ECU PSE (texts are "limited…", "18-70 years of age",
  "voluntary between 16 to 18…": deliberately not derived; texts present except PSE).
* No legal data at all (16): ALA ATA ATF BES BVT CHI GLP GUF HMD IOT MTQ MYT REU SGS SJM UMI (territories/uninhabited).
* Not available from any open source found: **statutory** death-penalty status, statutory same-sex union legality
  (only the constitutional status from CCP), age of majority / marriage age as numbers (Wikidata specs pending),
  drug policy (no open structured source found).

## Provenance and honesty notes

* Every cell: `raw_sha256` of the archived download + `selector` (file / sheet / row / column, CSV line, JSON path,
  or SSPTW page + section + paragraph). Statutory citations: WBL `flags.legal_basis` (verbatim "Legal basis"
  columns, e.g. France full-pension age 67 ← "Code de la Sécurité Sociale, Arts. L351-8 et L161-17-2");
  CCP `flags.constitution_article` + `flags.codebook` (page of codebook_v5.pdf).
* Agent-applied rules carry `flags.transcription = "transcribed by agent A-LAW from <doc>, maintainer review
  pending"` and the source text: CCP code→label tables (codebook pages 11/103/122/140/141), Factbook voting-age
  regex and legal-family keyword mentions, SSPTW pension-age rules (`pension_ages`, conservative: France's
  "legal minimum retirement age is 62 … full pension is 67", occupation-specific ages, "N and M days" → no number).
* Time semantics: WBL report year Y = law as of 1 Oct Y-1 (cell = that day); the WBL 1.0 2026 file's About sheet
  says "October 1, 2026" (after its own Feb 2026 version) — cells follow the report (2025-10-01) and keep the
  discrepancy in flags. SSPTW cells = as-of month of each regional guide (Europe Jan 2018, Asia Jul 2018,
  Africa Jan 2019, Americas Jul 2019). CCP chronology 2020-2025 lists event rows only for most countries:
  a missing year there is "not reported", not "no event".
* WBL 2026 pillar sheets ("I. Legal Frameworks" …) hold `#REF!` in every cached cell: not read (topic sheets used;
  any error cell elsewhere fails the parse).
* CCP names spanning predecessor + successor under one COW code (Russia (Soviet Union), Turkey/Ottoman Empire,
  Italy/Sardinia, Tanzania/Tanganyika, Zimbabwe (Rhodesia), Yemen Arab Republic, DR Vietnam) map to the successor
  with `flags.provider_continuity`; Prussia, Yugoslavia, Serbia and Montenegro, Czechoslovakia, GDR … stay unmapped.

## Per-country table (latest values)

Values from the live atlas tables, except the SSPTW column (parser run over the research copies of the 184 pages
fetched 2026-10-15, not yet an atlas table). "text only" = SSPTW rule not derivable, see the qualifying-conditions text.

| ISO3 | Country | Constitution in force: enacted / latest revision (Constitute) | CCP last new constitution | WBL 1.0 index 2026 | WBL 2.0 LF index 2026 | Full-pension age M/W (WBL 2026, Oct 2025) | Pension age M/W (SSPTW 2018-19, agent rule) | Voting age (Factbook, Jan 2026) | Constitutional voting age (CCP, 2023) | Legal families mentioned (Factbook) |
|---|---|---|---|---|---|---|---|---|---|---|
| ABW | Aruba | – | – | – | – | – | 62y6m/62y6m | 18 | – | civil |
| AFG | Afghanistan | 2004 / – | 2004 | 3.125 | 5 | NS | – | 18 | left explicitly to non-constitutional law | civil; customary; islamic |
| AGO | Angola | 2010 / – | 2010 | 76.25 | 71.599999999999994 | 60/58 | 60/60 | 18 | 18 | civil |
| AIA | Anguilla | – | – | – | – | – | – | 18 | – | common |
| ALA | Åland Islands | – | – | – | – | – | – | – | – | – |
| ALB | Albania | 1998 / 2016 | 1998 | 91.25 | 78.099999999999994 | 65/61.8 | 65/60y8m | 18 | 18 | civil; customary |
| AND | Andorra | 1993 / – | 1993 | – | – | – | 65/65 | 18 | adult/majority/age of majority | civil; customary; religious |
| ARE | United Arab Emirates | 1971 / 2009 | – | 73.75 | 53.58 | 60/60 | – | – | Not Applicable | civil; islamic |
| ARG | Argentina | 1853 / 1994 | 1853 | 82.5 | 80.78 | 65/60 | text only | – | Not Applicable | civil |
| ARM | Armenia | 1995 / 2020 | 1995 | 90.625 | 75 | 63/63 | 63/63 | 18 | 18 | civil |
| ASM | American Samoa | – | – | – | – | – | – | 18 | – | common; customary |
| ATA | Antarctica | – | – | – | – | – | – | – | – | – |
| ATF | French Southern Territories | – | – | – | – | – | – | – | – | – |
| ATG | Antigua and Barbuda | 1981 / – | 1981 | 71.25 | 56.03 | 65/65 | 62/62 | 18 | 18 | common |
| AUS | Australia | 1901 / 1985 | 1901 | 96.875 | 91.88 | 67/67 | 65y6m/65y6m | 18 | Not Specified | common |
| AUT | Austria | 1920 / 2013 | 1934 | 96.875 | 90.88 | 65/61 | 65/60 | 16 | 18 | civil |
| AZE | Azerbaijan | 1995 / 2016 | 1995 | 85 | 72.180000000000007 | 65/64.5 | 63y6m/60y6m | 18 | Not Specified | civil |
| BDI | Burundi | 2018 / – | 2018 | 76.25 | 52.68 | 60/60 | 60/60 | 18 | 18 (2017) | civil; customary |
| BEL | Belgium | 1831 / 2025 | 1831 | 100 | 91.05 | 66/66 | 65/65 | 18 | 18 | civil |
| BEN | Benin | 1990 / – | 1990 | 83.75 | 67.599999999999994 | 60/60 | 60/60 | 18 | 18 | civil; customary |
| BES | Bonaire, Sint Eustatius and Saba | – | – | – | – | – | – | – | – | – |
| BFA | Burkina Faso | – | 1991 | 85.625 | 67.400000000000006 | 56/56 | text only | 18 | Not Applicable (2021) | civil; customary |
| BGD | Bangladesh | 1972 / 2018 | 1972 | 49.375 | 34.380000000000003 | NS | 65/62 | 18 | 18 | common; islamic; religious |
| BGR | Bulgaria | 1991 / 2015 | 1991 | 90.625 | 88.65 | 64.7/62.3 | 64y1m/61y2m | 18 | 18 | civil |
| BHR | Bahrain | 2002 / 2017 | 2002 | 65 | 43.15 | 60/60 | 60/55 | 20 | left explicitly to non-constitutional law | civil; common; customary; islamic |
| BHS | Bahamas | 1973 / – | 1973 | 81.25 | 64.88 | 65/65 | 65/65 | 18 | Not Applicable | common |
| BIH | Bosnia and Herzegovina | 1995 / 2009 | 1995 | 81.875 | 77.650000000000006 | 65/65 | – | 18 | Not Applicable | civil |
| BLM | Saint Barthélemy | – | – | – | – | – | – | 18 | – | civil |
| BLR | Belarus | 1994 / 2004 | 1994 | 75.625 | 70.430000000000007 | 63/58 | text only | 18 | 18 | civil |
| BLZ | Belize | 1981 / 2022 | 1981 | 80 | 64.150000000000006 | 65/65 | 65/65 | 18 | 18 | common |
| BMU | Bermuda | – | – | – | – | – | 65/65 | 18 | – | common |
| BOL | Bolivia (Plurinational State of) | 2009 / – | 2009 | 88.75 | 76.3 | 58/56 | 60/60 | 18 | 18 | civil; religious |
| BRA | Brazil | 1988 / 2017 | 1988 | 81.875 | 82.03 | 65/62 | text only | – | other, please specify in the comments section | civil |
| BRB | Barbados | 1966 / 2026 | 1966 | 80 | 66.099999999999994 | 67/67 | 67/67 | 18 | Not Specified | common |
| BRN | Brunei Darussalam | 1959 / 2006 | – | 53.125 | 38.58 | 60/60 | 60/60 | 18 | Not Applicable | common; islamic |
| BTN | Bhutan | 2008 / – | 2008 | 75 | 53.6 | NS | 56/56 | 18 | 18 | civil; religious |
| BVT | Bouvet Island | – | – | – | – | – | – | – | – | – |
| BWA | Botswana | 1966 / 2021 | 1966 | 66.875 | 51.1 | 65/65 | 65/65 | 18 | 18 | civil; common; customary |
| CAF | Central African Republic | – | 2023 | 77.5 | 53.48 | 60/60 | 60/60 | 18 | 18 | civil |
| CAN | Canada | 1867 / 2011 | 1867 | 100 | 87.93 | 65/65 | 65/65 | 18 | Not Specified | civil; common |
| CCK | Cocos (Keeling) Islands | – | – | – | – | – | – | 18 | – | common |
| CHE | Switzerland | 1999 / 2014 | 1999 | 94.375 | 83.55 | 65/65 | 65/64 | 18 | 18 | civil |
| CHI | Channel Islands | – | – | – | – | – | – | – | – | – |
| CHL | Chile | 1980 / 2021 | 1980 | 80 | 79.8 | 65/60 | text only | 18 | 18 | civil |
| CHN | China | 1982 / 2018 | 1982 | 78.125 | 70 | 60.2/50.3 | text only | 18 | 18 | civil; socialist |
| CIV | Côte d’Ivoire | 2016 / 2020 | 2016 | 95 | 78.25 | 60/60 | 60/60 | 18 | 18 | civil |
| CMR | Cameroon | 1972 / 2008 | 1972 | 60 | 44.48 | 60/60 | 60/60 | 20 | 20 | civil; common; customary |
| COD | Democratic Republic of the Congo | 2005 / 2011 | 2005 | 78.75 | 59.5 | 60/60 | 60/60 | 18 | 18 | civil; customary |
| COG | Congo | 2015 / – | 2015 | 61.25 | 50.23 | 60/60 | 57/57 | 18 | 18 | civil; customary |
| COK | Cook Islands | – | – | – | – | – | – | 18 | – | common |
| COL | Colombia | 1991 / 2015 | 1991 | 84.375 | 81.05 | 62/57 | 62/57 | 18 | 18 | civil |
| COM | Comoros | 2018 / – | 2018 | 65 | 34.380000000000003 | 58/58 | – | 18 | Not Specified | civil; customary; islamic; religious |
| CPV | Cabo Verde | 1980 / 1992 | 1980 | 86.25 | 75.03 | 65/60 | 65/60 | 18 | adult/majority/age of majority | civil |
| CRI | Costa Rica | 1949 / 2020 | 1949 | 91.875 | 84.78 | 65/65 | text only | 18 | 18 | civil |
| CUB | Cuba | 2019 / – | 2019 | – | – | – | 65/60 | 16 | other, please specify in the comments section | civil |
| CUW | Curaçao | – | – | – | – | – | – | 18 | – | civil |
| CXR | Christmas Island | – | – | – | – | – | – | 18 | – | – |
| CYM | Cayman Islands | – | – | – | – | – | – | 18 | – | common |
| CYP | Cyprus | 1960 / 2013 | 1960 | 96.875 | 88.35 | 65/65 | 65/65 | 18 | 18 | civil; common |
| CZE | Czechia | 1993 / 2013 | 1993 | 93.75 | 86.6 | 64.6/63.2 | 63y2m/62y8m | 18 | 18 | civil; socialist |
| DEU | Germany | 1949 / 2014 | 1949 | 100 | 85.43 | 66/66 | 65y7m/65y7m | 18 | 18 | civil |
| DJI | Djibouti | 1992 / 2010 | 1992 | 71.25 | 55.33 | 60/60 | 60/60 | 18 | adult/majority/age of majority | civil; customary; islamic; religious |
| DMA | Dominica | 1978 / 2014 | 1978 | 62.5 | 59.03 | 65/65 | 64/64 | 18 | 18 | common |
| DNK | Denmark | 1953 / – | 1953 | 100 | 88.35 | 67/67 | 65/65 | 18 | left explicitly to non-constitutional law | civil |
| DOM | Dominican Republic | 2015 / – | 2015 | 86.25 | 76.83 | 60/60 | 60/60 | 18 | 18 | civil |
| DZA | Algeria | 2020 / – | 2020 | 60.625 | 45.53 | 60/55 | 60/55 | 18 | Not Applicable | civil; islamic |
| ECU | Ecuador | 2008 / 2021 | 2008 | 92.5 | 88.63 | 60/60 | text only | – | other, please specify in the comments section | civil |
| EGY | Egypt | 2014 / 2019 | 2014 | 61.875 | 48.5 | 60/60 | text only | 18 | left explicitly to non-constitutional law | civil; islamic; religious |
| ERI | Eritrea | 1997 / – | 1997 | 69.375 | 50.28 | NS | – | 18 | 18 | civil; customary; islamic; religious |
| ESH | Western Sahara | – | – | – | – | – | – | – | – | – |
| ESP | Spain | 1978 / 2011 | 1978 | 100 | 97.93 | 67/67 | 65y6m/65y6m | 18 | Not Specified | civil |
| EST | Estonia | 1992 / 2015 | 1992 | 97.5 | 86.28 | 65/65 | 63y6m/63y6m | 18 | 18 | civil |
| ETH | Ethiopia | 1994 / – | 1994 | 80 | 57.2 | 60/60 | 60/60 | 18 | 18 | civil |
| FIN | Finland | 1999 / 2018 | 1999 | 97.5 | 87.5 | 65/65 | 65/65 | 18 | 18 | civil |
| FJI | Fiji | 2013 / – | 2013 | 83.125 | 63.75 | 55/55 | text only | 18 | 18 | common |
| FLK | Falkland Islands (Malvinas) | – | – | – | – | – | – | 18 | – | common |
| FRA | France | 1958 / 2024 | 1958 | 100 | 92.93 | 67/67 | text only | 18 | adult/majority/age of majority | civil |
| FRO | Faroe Islands | – | – | – | – | – | – | 18 | – | – |
| FSM | Micronesia (Federated States of) | 1978 / 1990 | – | 61.25 | 46.68 | 65/65 | 65/65 | 18 | 18 | common; customary |
| GAB | Gabon | – | 2024 | 91.875 | 74.08 | 57/57 | 55/55 | 18 | 18 | civil; customary |
| GBR | United Kingdom of Great Britain and Northern Ireland | 1215 / 2013 | – | 97.5 | 88.98 | 66/66 | text only | 18 | 18 | common |
| GEO | Georgia | 1995 / 2018 | 1995 | 88.125 | 76.680000000000007 | 65/60 | 65/60 | 18 | 18 | civil |
| GGY | Guernsey | – | – | – | – | – | 65/65 | 16 | – | civil; common; customary |
| GHA | Ghana | 1992 / 1996 | 1992 | 80.625 | 66.72 | 60/60 | 60/60 | 18 | 18 | common; customary |
| GIB | Gibraltar | – | – | – | – | – | – | 18 | – | – |
| GIN | Guinea | – | 2025 | 76.25 | 61.88 | 55/55 | 55/55 | 18 | adult/majority/age of majority | civil |
| GLP | Guadeloupe | – | – | – | – | – | – | – | – | – |
| GMB | Gambia | 1996 / 2018 | 1996 | 72.5 | 57.5 | 60/60 | 60/60 | 18 | 18 | common; customary; islamic |
| GNB | Guinea-Bissau | 1984 / 1996 | 1984 | 51.875 | 48.75 | 60/60 | 60/60 | 18 | 18 | civil; customary |
| GNQ | Equatorial Guinea | 1991 / 2012 | 1991 | 58.125 | 49.15 | 60/60 | 60/60 | 18 | Not Applicable | civil; customary |
| GRC | Greece | 1975 / 2019 | 1975 | 100 | 91.25 | 67/67 | text only | 17 | left explicitly to non-constitutional law | civil |
| GRD | Grenada | 1973 / 1992 | 1974 | 80.625 | 72.72 | 62/62 | 60/60 | 18 | Not Applicable | common |
| GRL | Greenland | – | – | – | – | – | – | 18 | – | – |
| GTM | Guatemala | 1985 / 1993 | 1985 | 73.75 | 63.13 | 60/60 | 60/60 | 18 | Not Applicable | civil |
| GUF | French Guiana | – | – | – | – | – | – | – | – | – |
| GUM | Guam | – | – | – | – | – | – | 18 | – | common |
| GUY | Guyana | 1980 / 2016 | 1980 | 89.375 | 72.72 | 60/60 | 65/65 | 18 | 18 | civil; common |
| HKG | China, Hong Kong Special Administrative Region | – | – | 91.875 | 76.05 | 65/65 | 65/65 | 18 | – | civil; common; customary |
| HMD | Heard Island and McDonald Islands | – | – | – | – | – | – | – | – | – |
| HND | Honduras | 1982 / 2013 | 1982 | 75 | 71.13 | 65/60 | 65/60 | 18 | 18 | civil |
| HRV | Croatia | 1991 / 2013 | – | 93.75 | 94.4 | 65/63.8 | 65/62 | 18 | 18 | civil |
| HTI | Haiti | 1987 / 2012 | 1987 | 61.25 | 51.73 | 55/55 | 55/55 | 18 | 21 | civil |
| HUN | Hungary | 2011 / 2016 | 2011 | 93.75 | 84.18 | 65/65 | 63y6m/63y6m | 18 | adult/majority/age of majority | civil |
| IDN | Indonesia | 1945 / 2002 | 1949 | 70.625 | 56.2 | 59/59 | 56/56 | 17 | Not Applicable | civil; customary |
| IMN | Isle of Man | – | – | – | – | – | text only | 16 | – | – |
| IND | India | 1949 / 2023 | 1949 | 71.25 | 57.93 | 58/58 | 58/58 | 18 | 18 | common |
| IOT | British Indian Ocean Territory | – | – | – | – | – | – | – | – | – |
| IRL | Ireland | 1937 / 2019 | 1937 | 100 | 92.1 | 66/66 | text only | 18 | 18 | common; customary |
| IRN | Iran (Islamic Republic of) | 1979 / 1989 | 1979 | 31.25 | 26.25 | 60/55 | 60/55 | 18 | left explicitly to non-constitutional law | islamic; religious |
| IRQ | Iraq | 2005 / – | 2005 | 50.625 | 39.17 | 63/58 | – | 18 | Not Applicable | civil; islamic |
| ISL | Iceland | 1944 / 2013 | 1944 | 97.5 | 86.05 | 67/67 | 67/67 | 18 | 18 | civil |
| ISR | Israel | 1958 / 2013 | 1958 | 80.625 | 80.33 | 67/63.3 | 70/68 | 18 | 18 | common; religious |
| ITA | Italy | 1947 / 2020 | 1947 | 97.5 | 92.93 | 67/67 | 66y7m/66y7m | 18 | adult/majority/age of majority | civil |
| JAM | Jamaica | 1962 / 2023 | 1962 | 74.375 | 61.85 | 65/65 | 65/65 | 18 | 21 | common |
| JEY | Jersey | – | – | – | – | – | text only | 16 | – | – |
| JOR | Jordan | 1952 / 2016 | 1952 | 61.875 | 52.5 | 60/55 | 60/55 | 18 | Not Applicable | common; islamic |
| JPN | Japan | 1946 / – | 1946 | 81.25 | 80.83 | 65/65 | 65/65 | 18 | Not Applicable | civil |
| KAZ | Kazakhstan | 2026 / – | 1995 | 75.625 | 73 | 63/61 | 63/58y6m | 18 | Not Specified | civil |
| KEN | Kenya | 2010 / – | 2010 | 83.75 | 68.75 | 60/60 | 70/70 | 18 | 18 | common; customary; islamic |
| KGZ | Kyrgyzstan | 2021 / – | 2021 | 86.25 | 79.599999999999994 | 63/58 | 63/58 | 18 | 18 (2020) | civil |
| KHM | Cambodia | 1993 / 2008 | 1993 | 84.375 | 62.73 | 60/60 | text only | 18 | 18 | civil; common; customary; socialist |
| KIR | Kiribati | 1979 / 2018 | 1979 | 76.25 | 55.9 | 50/50 | 65/65 | 18 | 18 | common; customary |
| KNA | Saint Kitts and Nevis | 1983 / – | 1983 | 71.25 | 60.45 | 62/62 | 62/62 | 18 | 18 | common |
| KOR | Republic of Korea | 1948 / 1987 | 1948 | 88.125 | 85.63 | 60/60 | 61/61 | 18 | Not Specified | civil |
| KWT | Kuwait | 1962 / – | 1962 | 41.25 | 27.2 | 55/50 | text only | 21 | left explicitly to non-constitutional law | civil; common; islamic |
| LAO | Lao People's Democratic Republic | 1991 / 2025 | 1991 | 82.5 | 73.03 | 60/55 | 60/55 | 18 | 18 | civil |
| LBN | Lebanon | 1926 / 2004 | – | 58.75 | 46.78 | NS | 60/60 | 21 | 21 | civil; islamic; religious |
| LBR | Liberia | 1986 / – | 1986 | 81.25 | 58.95 | 60/60 | 60/60 | 18 | 18 | common; customary |
| LBY | Libya | 2011 / 2012 | 1951 | 50 | 35.83 | 65/60 | text only | 18 | Not Applicable | – |
| LCA | Saint Lucia | 1978 / – | – | 83.75 | 64.83 | 65/65 | 65/65 | 18 | other, please specify in the comments section | common |
| LIE | Liechtenstein | 1921 / 2020 | 1921 | – | – | – | 65/65 | 18 | 18 | civil |
| LKA | Sri Lanka | 1978 / 2015 | 1978 | 65.625 | 47.78 | 55/50 | 55/50 | 18 | 18 | civil; common; customary |
| LSO | Lesotho | 1993 / 2025 | 1993 | 90 | 66.72 | 70/70 | 70/70 | 18 | 18 | common |
| LTU | Lithuania | 1992 / 2022 | 1992 | 93.75 | 91.2 | 64.8/64.7 | 63y8m/62y4m | 18 | 18 | civil |
| LUX | Luxembourg | 1868 / 2009 | 1868 | 100 | 89.18 | 65/65 | 65/65 | 18 | 18 | civil |
| LVA | Latvia | 1922 / 2018 | 1940 | 100 | 82.3 | 65/65 | 63y3m/63y3m | 18 | 18 | civil; socialist |
| MAC | China, Macao Special Administrative Region | – | – | – | – | – | – | 18 | – | civil |
| MAF | Saint Martin (French Part) | – | – | – | – | – | – | 18 | – | civil |
| MAR | Morocco | 2011 / – | 2011 | 75.625 | 60.95 | 60/60 | 60/60 | 18 | adult/majority/age of majority | civil; islamic |
| MCO | Monaco | 1962 / 2002 | 1962 | – | – | – | 65/65 | 18 | 18 | civil |
| MDA | Republic of Moldova | 1994 / 2024 | 1994 | 90.625 | 85.8 | 63/61.5 | text only | 18 | 18 | civil |
| MDG | Madagascar | 2010 / – | 2010 | 81.25 | 64.930000000000007 | 60/60 | 60/60 | 18 | Not Specified | civil; customary |
| MDV | Maldives | 2008 / – | 2008 | 73.75 | 50.8 | 65/65 | – | 18 | 18 | common; islamic |
| MEX | Mexico | 1917 / 2015 | 1917 | 88.75 | 82.55 | 65/65 | 68/68 | 18 | 18 | civil |
| MHL | Marshall Islands | 1979 / 1995 | – | 65.625 | 51.48 | 63/63 | 61/61 | 18 | 18 | common; customary |
| MKD | North Macedonia | 1991 / 2019 | 1991 | 85 | 83.53 | 64/62 | – | 18 | 18 | civil |
| MLI | Mali | 2023 / – | 2023 | 72.5 | 50.33 | 60/60 | 55/55 | 18 | adult/majority/age of majority | civil; customary |
| MLT | Malta | 1964 / 2016 | 1964 | 91.25 | 88.35 | 64/64 | text only | 18 | other, please specify in the comments section | civil; common |
| MMR | Myanmar | 2008 / 2015 | 2008 | 61.875 | 53.55 | NS | 60/60 | 18 | 18 | common; customary |
| MNE | Montenegro | 2007 / 2013 | 2007 | 85 | 84.63 | 66/63 | – | 18 | 18 | civil |
| MNG | Mongolia | 1992 / 2001 | 1992 | 90.625 | 76.05 | 62/57 | – | 18 | 18 | civil; socialist |
| MNP | Northern Mariana Islands | – | – | – | – | – | – | 18 | – | – |
| MOZ | Mozambique | 2004 / 2007 | 2004 | 79.375 | 68.55 | 60/55 | 60/55 | 18 | Not Applicable | civil; customary |
| MRT | Mauritania | 1991 / 2012 | 1991 | 51.25 | 33.5 | 63/63 | 60/60 | 18 | adult/majority/age of majority | civil; islamic |
| MSR | Montserrat | – | – | – | – | – | – | 18 | – | common |
| MTQ | Martinique | – | – | – | – | – | – | – | – | – |
| MUS | Mauritius | 1968 / 2016 | 1968 | 89.375 | 82.3 | 65/65 | 60/60 | 18 | 18 | civil; common |
| MWI | Malawi | 1994 / 2017 | 1994 | 80 | 62.48 | 50/50 | 50/50 | 18 | 18 | common; customary |
| MYS | Malaysia | 1957 / 2025 | 1957 | 60.625 | 49.6 | 60/60 | 55/55 | 18 | 21 | common; customary; islamic |
| MYT | Mayotte | – | – | – | – | – | – | – | – | – |
| NAM | Namibia | 1990 / 2014 | 1990 | 83.125 | 70.28 | 60/60 | 60/60 | 18 | 18 | civil; customary |
| NCL | New Caledonia | – | – | – | – | – | – | 18 | – | civil |
| NER | Niger | 2010 / 2017 | 2010 | 53.75 | 38.08 | 60/60 | 60/60 | 18 | 18 | civil; customary; islamic |
| NFK | Norfolk Island | – | – | – | – | – | – | 18 | – | common |
| NGA | Nigeria | 1999 / 2023 | 1999 | 69.375 | 51.1 | 50/50 | 50/50 | 18 | 18 | common; islamic |
| NIC | Nicaragua | 1987 / 2014 | 1987 | 86.25 | 70.03 | 60/60 | 60/60 | 16 | other, please specify in the comments section | civil |
| NIU | Niue | – | – | – | – | – | – | 18 | – | common |
| NLD | Netherlands (Kingdom of the) | 1814 / 2008 | 1814 | 100 | 86.05 | 67/67 | 66/66 | 18 | 18 | civil |
| NOR | Norway | 1814 / 2016 | – | 96.875 | 88.13 | 67/67 | text only | 18 | 18 | civil; common; customary |
| NPL | Nepal | 2015 / 2016 | 2015 | 75.625 | 57.93 | 60/60 | 58/58 | 18 | 18 | common; religious |
| NRU | Naoero | 1968 / 2025 | 1968 | – | – | – | – | 20 | 20 | common; customary |
| NZL | New Zealand | 1852 / 2014 | – | 97.5 | 90 | 65/65 | 65/65 | 18 | Not Applicable | common |
| OMN | Oman | – | 2021 | 48.75 | 36.67 | 60/55 | 60/55 | 21 | Not Applicable (2020) | islamic |
| PAK | Pakistan | 1973 / 2018 | 1973 | 58.75 | 46.68 | 60/55 | 60/55 | 18 | 18 | common; islamic |
| PAN | Panama | 1972 / 2004 | 1972 | 79.375 | 75.33 | 62/57 | 62/57 | 18 | 18 | civil |
| PCN | Pitcairn | – | – | – | – | – | – | 18 | – | – |
| PER | Peru | 1993 / 2023 | 1993 | 95 | 88.65 | 65/65 | 65/65 | 18 | 18 | civil |
| PHL | Philippines | 1987 / – | 1987 | 81.875 | 72.53 | 65/65 | 60/60 | 18 | 18 | civil; common; customary; islamic |
| PLW | Palau | 1981 / 1992 | – | 56.25 | 43.75 | 63/63 | 60/60 | 18 | 18 | civil; common; customary |
| PNG | Papua New Guinea | 1975 / 2016 | 1975 | 60 | 47.5 | 55/55 | 55/55 | 18 | 18 | common; customary |
| POL | Poland | 1997 / 2009 | 1997 | 93.75 | 83.33 | 65/60 | 65/60 | 18 | 18 | civil |
| PRI | Puerto Rico | – | – | 83.75 | 79.97 | 66.8/66.8 | – | 18 | – | civil |
| PRK | Democratic People's Republic of Korea | 1972 / 2016 | 1972 | – | – | – | – | 17 | other, please specify in the comments section | civil; socialist |
| PRT | Portugal | 1976 / 2005 | 1976 | 100 | 91.05 | 66.6/66.6 | 66y4m/66y4m | 18 | 18 | civil |
| PRY | Paraguay | 1992 / 2011 | 1992 | 94.375 | 80.2 | 60/60 | 60/60 | 18 | 18 | civil |
| PSE | State of Palestine | 2003 / 2005 | – | 26.25 | 22.83 | NS | – | – | – | – |
| PYF | French Polynesia | – | – | – | – | – | – | 18 | – | – |
| QAT | Qatar | 2003 / – | 2003 | 35.625 | 26.28 | 60/60 | 60/60 | 18 | Not Applicable | civil; islamic |
| REU | Réunion | – | – | – | – | – | – | – | – | – |
| ROU | Romania | 1991 / 2003 | 1991 | 90.625 | 90.7 | 65/61.4 | 65/60y9m | 18 | 18 | civil |
| RUS | Russian Federation | 1993 / 2014 | 1993 | 73.125 | 72.3 | 63/58 | 60/55 | 18 | 18 | civil |
| RWA | Rwanda | 2003 / 2015 | 2003 | 91.875 | 71.47 | 60/60 | text only | 18 | Not Specified | civil; customary |
| SAU | Saudi Arabia | 1992 / 2013 | 1992 | 73.75 | 56.23 | 60/60 | 58/53 | 18 | Not Applicable | customary; islamic |
| SDN | Sudan | 2019 / 2020 | 1998 | 32.5 | 24.75 | 65/65 | 65/65 | 17 | Not Specified | common; islamic |
| SEN | Senegal | 2001 / 2016 | 2001 | 69.375 | 52.9 | 60/60 | 55/55 | 18 | 18 | civil |
| SGP | Singapore | 1963 / 2016 | – | 82.5 | 66.680000000000007 | 63/63 | text only | 21 | Not Applicable | common |
| SGS | South Georgia and the South Sandwich Islands | – | – | – | – | – | – | – | – | – |
| SHN | Saint Helena | – | – | – | – | – | – | 18 | – | common |
| SJM | Svalbard and Jan Mayen Islands | – | – | – | – | – | – | – | – | – |
| SLB | Solomon Islands | 1978 / 2018 | 1978 | 56.875 | 46.53 | 50/50 | 50/50 | 21 | 18 | common; customary |
| SLE | Sierra Leone | 1991 / 2022 | 1991 | 92.5 | 71.88 | 60/60 | 60/60 | 18 | 18 | common; customary |
| SLV | El Salvador | 1983 / 2014 | 1983 | 88.75 | 78.45 | 60/55 | 60/55 | 18 | 18 | civil; common |
| SMR | San Marino | – | – | 91.25 | 78.55 | 66/66 | 65/65 | 18 | – | civil |
| SOM | Somalia | 2012 / – | 2012 | 55.625 | 38.33 | NS | – | 18 | 18 (2011) | civil; customary; islamic |
| SPM | Saint Pierre and Miquelon | – | – | – | – | – | – | 18 | – | civil |
| SRB | Serbia | 2006 / – | 2006 | 93.75 | 81.88 | 65/63.8 | 65/62 | 18 | 18 | civil |
| SSD | South Sudan | 2011 / 2013 | – | 73.75 | 62.1 | 65/65 | – | 18 | – | – |
| STP | Sao Tome and Principe | 1975 / 2003 | 1975 | 80.625 | 67.930000000000007 | 62/57 | 62/62 | 18 | 18 | civil; customary |
| SUR | Suriname | 1987 / 1992 | 1987 | 85 | 71.22 | 60/60 | text only | 18 | 18 | civil |
| SVK | Slovakia | 1992 / 2017 | – | 87.5 | 86.73 | 63.3/62.3 | text only | 18 | Not Specified | civil |
| SVN | Slovenia | 1991 / 2021 | – | 100 | 94.38 | 65/65 | 65/65 | 18 | 18 | civil |
| SWE | Sweden | 1974 / 2012 | 1974 | 100 | 87.3 | 63/63 | text only | 18 | 18 | civil; customary |
| SWZ | Eswatini | 2005 / – | 2005 | 46.25 | 35.35 | 50/50 | 60/60 | 18 | 18 | civil; common; customary |
| SXM | Sint Maarten (Dutch part) | – | – | – | – | – | – | 18 | – | civil; common |
| SYC | Seychelles | 1993 / 2025 | 1993 | 79.375 | 69.58 | 65/65 | 63/63 | 18 | 18 | civil; common; customary |
| SYR | Syrian Arab Republic | – | 2012 | 40 | 34.15 | 60/55 | 60/55 | 18 | 18 (2011) | civil; islamic |
| TCA | Turks and Caicos Islands | – | – | – | – | – | – | 18 | – | civil; common |
| TCD | Chad | – | 2023 | 66.25 | 51.4 | 60/60 | 60/60 | 18 | 18 | civil; customary |
| TGO | Togo | 2024 / – | 2024 | 97.5 | 79.33 | 60/60 | 60/60 | 18 | 18 | customary |
| THA | Thailand | 2017 / – | 2017 | 78.125 | 69.400000000000006 | 55/55 | 55/55 | 18 | 18 | civil; common |
| TJK | Tajikistan | 1994 / 2016 | 1994 | 78.75 | 70.22 | 63/58 | 63/58 | 18 | 18 | civil |
| TKL | Tokelau | – | – | – | – | – | – | 21 | – | common |
| TKM | Turkmenistan | 2008 / 2016 | 2008 | – | – | – | 62/57 | 18 | 18 (2007) | civil; islamic |
| TLS | Timor-Leste | 2002 / – | 2002 | 86.25 | 68.2 | 60/60 | – | 17 | other, please specify in the comments section | civil |
| TON | Tonga | 1875 / 2025 | – | 61.25 | 39.799999999999997 | 60/60 | – | 21 | 21 | common |
| TTO | Trinidad and Tobago | 1976 / 2007 | 1976 | 75 | 66.88 | 65/65 | text only | 18 | 18 | common |
| TUN | Tunisia | 2022 / – | 2022 | 64.375 | 46.38 | 60/60 | 60/60 | 18 | 20 (2010) | civil; islamic |
| TUR | Türkiye | 1982 / 2017 | 1982 | 82.5 | 81.180000000000007 | 60/58 | 60/58 | 18 | 18 | civil |
| TUV | Tuvalu | 2023 / – | 2023 | – | – | – | – | 18 | 18 | common; customary |
| TZA | United Republic of Tanzania | 1977 / 2005 | 1977 | 81.25 | 66.849999999999994 | 60/60 | 60/60 | 18 | 18 | common |
| UGA | Uganda | 1995 / 2017 | 1995 | 80.625 | 64.13 | 55/55 | 55/55 | 18 | 18 | common; customary |
| UKR | Ukraine | 1996 / 2019 | 1996 | 85 | 80.63 | 60/60 | text only | 18 | 18 | civil |
| UMI | United States Minor Outlying Islands | – | – | – | – | – | – | – | – | – |
| URY | Uruguay | 1966 / 2004 | 1966 | 88.75 | 82.93 | 65/65 | 60/60 | 18 | 18 | civil |
| USA | United States of America | 1789 / 1992 | 1789 | 91.25 | 87.58 | 66.8/66.8 | 66/66 | 18 | 18 | civil; common |
| UZB | Uzbekistan | 2023 / – | 2023 | 82.5 | 82.1 | 60/55 | 60/55 | 18 | 18 | civil |
| VAT | Holy See | – | – | – | – | – | – | – | – | religious |
| VCT | Saint Vincent and the Grenadines | 1979 / – | 1979 | 68.125 | 58.58 | 64/64 | 62/62 | 18 | 18 | common |
| VEN | Venezuela (Bolivarian Republic of) | 1999 / 2009 | 1999 | 85 | 74.8 | 60/55 | 60/55 | 18 | 18 | civil |
| VGB | British Virgin Islands | – | – | – | – | – | 65/65 | 18 | – | common |
| VIR | United States Virgin Islands | – | – | – | – | – | – | 18 | – | common |
| VNM | Viet Nam | 1992 / 2013 | 1992 | 88.125 | 86.73 | 61.3/56.7 | 60/55 | 18 | 18 | civil |
| VUT | Vanuatu | 1980 / 2023 | 1980 | 55.625 | 45.48 | 60/60 | 55/55 | 18 | 18 | common; customary |
| WLF | Wallis and Futuna Islands | – | – | – | – | – | – | 18 | – | civil |
| WSM | Samoa | 1962 / 2025 | 1962 | 75 | 54.28 | 55/55 | 65/65 | 21 | Not Applicable | common; customary |
| XKX | Kosovo | 2008 / 2020 | 2008 | 91.875 | 88.75 | 65/65 | – | 18 | 18 | civil |
| YEM | Yemen | 1991 / 2015 | 1991 | 26.875 | 19.100000000000001 | 60/55 | 60/55 | 18 | 18 | common; customary; islamic |
| ZAF | South Africa | 1996 / 2012 | 1996 | 88.125 | 76.349999999999994 | NS | 60/60 | 18 | 18 | civil; common; customary |
| ZMB | Zambia | 1991 / 2016 | 1991 | 83.75 | 76.680000000000007 | 60/60 | 55/55 | 18 | 18 | common; customary |
| ZWE | Zimbabwe | 2013 / 2021 | 2013 | 86.875 | 62.3 | 60/60 | 60/60 | 18 | 18 | civil; common; customary |

## Open items / requests

1. **ATLASCORE**: merge the updated `ssa.ssptw` budget (220/day; was a 10/day probe) — then
   `uv run python -m reva.atlas.ingest atlas.ssa_ssptw` (192 requests) registers `ssa.ssptw_2018_2019`.
2. **WDHARVEST**: allow-list P2997, P3000, P3001, P2999, P2998 and qualifier P642 (`registry/kg/property_requests/a_law.yaml`);
   `registry/kg/queries/society/law_*.yaml` then validate (currently fail only on the allow-list).
   The README owner table should list `society/law_*` → A-LAW (README is WDHARVEST's).
3. **Lead (seed)**: `register_atlas_connector_versions` hook (ATLASCORE's) covers the 5 A-LAW sources once added.
4. Maintainer review: manual geo aliases (continuity mappings, Palestine/PSE, Eswatini …), CCP codebook label
   tables, Factbook and SSPTW derivation rules.
5. Rights: Constitute and CCP are non-commercial; if REVA is ever used commercially these two tables need the
   project's permission or removal.
6. Shared scratch dir: another agent deleted my research downloads under the shared scratchpad mid-run (`dl/`, `wbl/`);
   fixtures and archives were unaffected.
