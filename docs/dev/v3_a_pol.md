# V3 agent A-POL — politics atlas (V-Dem, LIED, BMR, IPU Parline)

Status 2026-10-15. Connectors `src/reva/adapters/connectors/atlas_{vdem,regimes,ipu}.py` (+ `atlas_rdata.py`, a
stdlib/numpy RData reader), declared as atlas sources `atlas.vdem`, `atlas.lied`, `atlas.bmr`, `atlas.ipu`
(`reva.atlas.sources` discovery, `atlas_ingest(run)`; no HTTP client built in the modules). Catalog
`registry/atlas/catalog/politics.yaml` (111 provider-declared concepts, fr/en). Geo schemes `vdem`, `lied`, `bmr` in
`registry/atlas/geo_aliases/a_pol.yaml`. QID crosswalk `registry/entities/vdem_qids.json` (agent-mapped). Sources
`registry/sources/{vdem,ipu_parline,lied,bmr}.yaml`; budgets `registry/atlas/budget_requests/a_pol.yaml` (merged by
ATLASCORE, budgets-1.3.3).

Run: `uv run python -m reva.atlas.ingest atlas.vdem` (also `atlas.lied`, `atlas.bmr`, `atlas.ipu`; `--fixture` offline).

## Sources and licences

| source | what | licence | access | notes |
|---|---|---|---|---|
| V-Dem v16 Country-Year Full+Others | 1789-2025, 202 units (26 historical states/sub-units) | **CC BY-SA 4.0** (V-Dem FAQ) | V-Dem Institute's own R package `vdemdata` on GitHub (`data/vdem.RData`, `data/codebook.RData`, `DESCRIPTION` = v16.0) | v-dem.net download form requires e-mail, gender and personal-data consent; the agent does not submit the owner's personal data, so the institute's GitHub distribution of the same dataset is used. Share-alike recorded in rights. |
| LIED v6.9 (Skaaning, Gerring, Bartusevicius) | 1789-2025, 244 units incl. colonies | **CC0** (Dataverse record) | Harvard Dataverse doi:10.7910/DVN/WPKNIT, `LIED_6.9.xlsx` | licence + MD5 checked against the dataset record on every run |
| BMR v4.0 (Boix, Miller, Rosato) | 1800-2020, 223 sovereign states | **CC0** (Dataverse record) | Harvard Dataverse doi:10.7910/DVN/FENWWR, `democracy-v4.0.csv` | idem |
| IPU Parline open-data API | 193 parliaments; women since 1945; 2,976 elections | **CC BY-NC-SA 4.0 — NON-COMMERCIAL** (`meta.licence` of every response) | `api.data.ipu.org/v1/{chambers,parliaments,countries,elections}` | connector refuses a response declaring another licence; rights + every catalog concept carry `non_commercial` |

Not ingested (decisions):
* **Polity5** — CSP: "Reproduction or redistribution … prohibited without prior, written permission" (systemicpeace.org/inscrdata.html). Also not taken from V-Dem's re-distributed `e_p_polity`/`e_polity2`. Polity stopped in 2018 anyway.
* **V-Dem "Others" (`e_*`)** — third-party variables (Polity, Freedom House, WGI, UDS, TI CPI, BMR, LIED, economy) are covered by their own licences, not V-Dem's; none loaded. WGI is agent A-WB's; BMR and LIED come from their own CC0 releases.
* **International IDEA voter turnout** — IDEA terms: data may not be used to "benefit commercially" and "any intellectual property rights that arise out of your use will belong to us"; not open. Turnout comes from IPU (parliamentary elections) and V-Dem (`v2eltrnout`, `v2elvaptrn`).
* **CCP characteristics** — no data licence stated; owned by agent A-LAW (`registry/sources/ccp.yaml`, non-commercial). Political-system *de facto* indicators here come from V-Dem/IPU; constitutional (de jure) characteristics stay with A-LAW.

## Atlas tables (live ingest 2026-10-15, throwaway DB, budget-metered: 11 requests, ~85 MB)

| table | rows | indicators | crosswalk countries | content |
|---|---|---|---|---|
| `vdem.indices` | 5,177,398 | 210 | 174 | all `v2x*` V-Dem indices (+ 68% HPD `_codelow/_codehigh`, `_osp` for the 5 unbounded indices), RoW `v2x_regime`/`_amb` (code + label) |
| `vdem.institutions` | 3,674,843 | 184 | 174 | elections, executive (HOS/HOG names/titles as text), legislature, judiciary, parties |
| `vdem.liberties` | 2,890,806 | 109 | 174 | civil liberties, media, civil society, state |
| `lied.lied` | 562,530 | 17 | 194 | lexical index 0-6 / LIED+ 0-7 + 15 components (status on 31 Dec) |
| `bmr.bmr` | 114,780 | 6 | 194 | dichotomous democracy, with female suffrage, transitions, duration |
| `ipu.parline` | 114,236 | 74 | 193 | women/members per chamber (exact validity intervals + `|at=year_end`), electoral system, term, quotas, voting age, structure, political system, suffrage years |
| `ipu.elections` | 16,370 | 36 | 193 | 2,976 elections: dates, turnout (444 with turnout), electors, voters, seats, women elected |

`vdem.indices` is slightly above ATLASCORE's ~5 M guideline (5.18 M; 12.6 MB Parquet); split further on request.
Disk: raw ~85 MB per full run, Parquet ~35 MB; well under the 1.5 GB agent budget.

## Semantics and honesty notes

* **Cells are what the provider reports.** V-Dem doubles are the exact shortest decimal text of the stored values; NA is
  omitted, R `NaN` too. Election variables (V-Dem) only in election years; V-Dem's `fill_vars` is not applied.
* **V-Dem unit continuity is V-Dem's**: `DEU` 1789-1866 = Kingdom of Prussia, `RUS` 1918-1990 = RSFSR/USSR, `MEX` 1789-1820 = Viceroyalty of New Spain, `TUR` = Ottoman Empire, etc. Every V-Dem cell carries `flags.vdem_histname` (V-Dem's name of the polity that year) and, where verified, `entity_qid` of that period (Kingdom of Prussia Q27306, German Empire Q43287, Weimar Q41304, Third Reich Q7318, Russian Empire Q34266, Joseon Q28179, Qing Q8733, …).
* **Palestine**: V-Dem `PSE` is "Palestine/West Bank" (Gaza is `PSG`), so it is *not* mapped to crosswalk `PSE` (State of Palestine); both kept unmapped with QIDs West Bank Q36678 / Gaza Strip Q39760. Hence PSE has no politics cell.
* **Taiwan** (V3 TWNMAP, crosswalk 1.1.0 adds TWN): V-Dem `TWN` (base iso3), LIED `Taiwan|vdem=48` (via V-Dem
  TWN) and BMR `713` ("TAIWAN", abbreviation TWN) now map to TWN; China stays CHN (no double mapping). V-Dem's
  unit is the territory of Taiwan in every year: 1945-1948 rows are V-Dem's "Republic of China [headquartered in
  the mainland]" (`flags.vdem_histname`, entity_qid Q13426199), so the mainland-government years stay flagged.
* **Category labels** (RoW, electoral systems, HOS appointment, LIED levels, transition/breakdown types, BMR) are short forms of the providers' codebook texts written by A-POL; codes outside the codebook fail closed (V-Dem, LIED lexical indices) — except the 10 LIED cells below.
* **LIED v6.9 anomaly**: Botswana 1961-1965 has `legislative_elections` = 2 and `multi-party_legislative_elections` = 2 (description: 0/1 only). Kept as reported, no label, `flags.undocumented_code`; listed in the run summary.
* **V-Dem codebook vs data**: `v2xdd_i_ci` is documented 0-1 but v16 values reach 1.92; unit says so, nothing rescaled. Unbounded indices (`v2x_accountability`, `v2x_veracc`, `v2x_diagacc`, `v2x_horacc`, `v2x_divparctrl`) are labelled as such; the catalog uses V-Dem's own 0-1 `_osp` for accountability.
* **IPU periods**: each value keeps the provider's validity interval `[date_from, date_to+1)`; the current open interval ends at retrieval and is flagged `open_ended`. Calendar-year cells (`|at=year_end`) are a *selection* of the provider's value valid on 31 December (flags `selection`, `provider_interval`), never interpolation; none for a year whose 31 Dec is after retrieval. Undated "current" fields are one cell for the retrieval day (`as_of_retrieval`). Not loaded and counted in the coverage note: 16 null values without reason, 14 overlapping-interval duplicates, 1 future-dated value, 4 suffrage entries without a year. Elections dated after retrieval are flagged `scheduled_after_retrieval`.
* **Turnout**: IPU (voters / registered, parliamentary election per chamber) and V-Dem `v2eltrnout` (national election; executive turnout when same-day) are *different* concepts in the catalog; only women in the lower chamber (IPU year-end + V-Dem `v2lgfemleg`, both "last value of the year") and minimum voting age share a concept.
* **LIED/BMR geography**: LIED keys are `"<countryn>|vdem=<id>"` (neither name nor COW code is unique: COW 325 = Sardinia and Italy; "Vietnam" carries V-Dem ids 35 and 34). BMR keys are COW `ccode`, mapped via BMR's own World Bank code (`abbreviation`, per its codebook) + 10 listed aliases (old WB codes ROM/ZAR/TMP, SWD, ETF, rows without code). West Germany (260) stays unmapped because BMR's ccode 255 also covers 1945 and 1990 (two cells would compete for DEU); USSR, Yugoslavia, Czechoslovakia, GDR, the two Vietnams, South Yemen, Korea before 1910 and the pre-unification German/Italian states stay unmapped (with QIDs where verified).

## QID crosswalk (`registry/entities/vdem_qids.json`, agent-mapped, pending maintainer review)

* 202 V-Dem units with their historical-name periods (exact V-Dem `histname`, years). A period gets a QID only when the
  name is that Wikidata state (86 periods; 56 distinct QIDs); occupations, annexations, colonies, "Part of …", and
  ambiguous names stay null. Crosswalk countries are linked through iso3 (QID from WDHARVEST's `country_qids.json`, not duplicated here).
* Every QID was verified (label, description, inception/dissolution) with one `wbgetentities` call by enwiki title
  through the WDHARVEST locked client (raw sha256 + revision in `evidence`), or reused from A-HISTECON's verified
  `historical_states_qids.json` (marked). Results agree with A-HISTECON wherever both looked (Baden, Bavaria, Saxony, Württemberg, Hanover, …).
* Not found by title (left null): Free City of Hamburg, Saxe-Weimar-Eisenach.
* Atlas rows: `entity_qid` = QID of the V-Dem period containing the year (LIED rows via LIED's own V-Dem id; BMR/LIED units outside V-Dem via `other_units`).

### Historical states and non-crosswalk units (V-Dem)

| V-Dem | name | years | periods with QID |
|---|---|---|---|
| BDN | Baden | 1789-1871 | (no name) 1806-1871 → Q186320 |
| BRW | Brunswick | 1789-1867 | Duchy of Brunswick 1813-1866 → Q326029 |
| BVR | Bavaria | 1789-1871 | Kingdom of Bavaria 1806-1870 → Q154195 |
| DDR | German Democratic Republic | 1949-1990 | German Democratic Republic 1949-1990 → Q16957 |
| HDM | Hesse-Darmstadt | 1789-1871 | Grand Duchy of Hesse-Darmstadt 1806-1866 → Q20135; Grand Duchy of Hesse-Darmstadt as part of North German Confederation 1867-1871 → Q20135 |
| HKS | Hesse-Kassel | 1789-1866 | Electorate of Hesse-Kassel 1803-1805 → Q529605; Electorate of Hesse-Kassel 1813-1865 → Q529605 |
| HRG | Hamburg | 1789-1867 | — |
| HVR | Hanover | 1789-1866 | Kingdom of Hanover 1814-1865 → Q164079 |
| MCL | Mecklenburg Schwerin | 1789-1867 | Grand Duchy of Mecklenburg-Schwerin 1815-1866 → Q158445 |
| MDN | Modena | 1789-1859 | Duchy of Modena and Reggio 1789-1795 → Q252580; Duchy of Modena and Reggio 1814-1847 → Q252580; Duchy of Modena and Reggio 1849-1858 → Q252580 |
| NSS | Nassau | 1806-1866 | Principalities of Nassau-Weilburg and Nassau-Usingen united into the Duchy of Nassau 1806-1866 → Q836680 |
| OLD | Oldenburg | 1789-1867 | Grand Duchy of Oldenburg 1829-1866 → Q693669 |
| PPS | Papal States | 1789-1870 | Papal States 1789-1797 → Q170174; Papal States 1800-1808 → Q170174; Papal States 1814-1869 → Q170174 |
| PRM | Parma | 1789-1859 | Duchy of Parma and Piacenza 1789-1801 → Q165040; Duchy of Parma and Piacenza 1814-1858 → Q165040 |
| PSB | Palestine/British Mandate | 1918-1948 | Palestine/British Mandate 1918-1948 → Q193714 |
| PSE | Palestine/West Bank | 1948-2025 | unit → Q36678 |
| PSG | Palestine/Gaza | 1948-2025 | unit → Q39760 |
| SAX | Saxe-Weimar-Eisenach | 1809-1867 | — |
| SML | Somaliland | 1900-2025 | Republic of Somaliland [independent state] 1991-2025 → Q34754 |
| SPD | Piedmont-Sardinia | 1789-1861 | Kingdom of Piedmont-Sardinia 1789-1792 → Q165154; Kingdom of Piedmont-Sardinia, but with Savoy and Nice annexed by France 1793-1799 → Q165154; Kingdom of Piedmont-Sardinia 1814-1858 → Q165154; Kingdom of Piedmont-Sardinia [incorporates Lombardy] 1859-1859 → Q165154; Kingdom of Piedmont-Sardinia 1861-1861 → Q165154 |
| SXN | Saxony | 1789-1867 | Kingdom of Saxony 1807-1867 → Q153015 |
| TSC | Tuscany | 1789-1861 | Grand Duchy of Tuscany 1789-1800 → Q154849; Grand Duchy of Tuscany 1814-1858 → Q154849 |
| TWN | Taiwan | 1900-2025 | Republic of China [headquartered in the mainland] 1945-1948 → Q13426199; Republic of China [headquartered in Taiwan] 1949-2025 → Q865 |
| TWS | Two Sicilies | 1789-1860 | United with Sicily to form the Kingdom of the Two Sicilies 1816-1847 → Q180393; Kingdom of the Two Sicilies 1849-1860 → Q180393 |
| VDR | Republic of Vietnam | 1802-1975 | Republic of Vietnam (also known as South Vietnam) 1955-1975 → Q180573 |
| WRG | Würtemberg | 1789-1871 | Kingdom of Wurtemberg 1806-1871 → Q159631 |
| YMD | South Yemen | 1900-1990 | People's Republic of South Yemen 1967-1969 → Q199841; People's Democratic Republic of Yemen 1970-1990 → Q199841 |
| ZZB | Zanzibar | 1856-2025 | Sultanate of Zanzibar [independent] 1856-1884 → Q157904; Sultanate of Zanzibar under German protectorate 1885-1889 → Q157904; Sultanate of Zanzibar under British protectorate 1890-1962 → Q157904; Sultanate of Zanzibar [independent] 1963-1963 → Q157904; People's Republic of Zanzibar and Pemba 1964-1964 → Q157535; Zanzibar, semi-autonomous region of Tanzania 1965-2025 → Q1774 |

Other units outside the crosswalk: LIED Newfoundland, Orange Free State (Q218023), Transvaal (Q550374), Natal
(Q1301901), Cape Colony (Q370736), Sahrawi, Tibet (Q2444884), plus LIED's German/Italian states and V-Dem-linked units
(QIDs via V-Dem periods); BMR ccodes 89 (Q190025), 99 (Q199821), 245/267/269/271 German states, 260 (Q713750), 265 (Q16957),
315 (Q33946), 324-337 Italian states, 345 (Q36704), 347 (Q838261), 364 (Q15180), 564, 680 (Q199841), 713 (Q865), 730 (none:
Joseon/Korean Empire split), 816 (Q172640), 817 (Q180573).

## Coverage by country (first year of each source; crosswalk entities)

195 of 250 crosswalk entities have at least one politics source. V-Dem covers 174; LIED 194; BMR 194 (to 2020);
IPU 193. Countries without V-Dem but covered by LIED/BMR/IPU (microstates): AND ATG BHS BLZ BRN DMA FSM GRD KIR KNA LCA
LIE MCO MHL NRU PLW SMR TON TUV VCT WSM. **No politics source**: dependent territories (ABW, AIA, ASM, BMU, CYM, FRO,
GRL, GUM, PRI, REU, …), Antarctica and uninhabited areas, VAT, MAC, CHI, ESH, and PSE (see above) — 55 entities; none of
the four providers codes them. Gaps worth filling elsewhere: V-Dem for microstates → LIED/BMR/IPU (already here);
Freedom House would add territories but its terms are not open.

| iso3 | country | V-Dem first | RoW first | LIED first | BMR first | IPU women (lower) first | IPU elections (lower) |
|---|---|---|---|---|---|---|---|
| AFG | Afghanistan | 1789 | 1900 | 1789 | 1800 | 1965 | 5 |
| AGO | Angola | 1900 | 1900 | 1885 | 1975 | 1980 | 7 |
| ALB | Albania | 1914 | 1914 | 1912 | 1914 | 2014 | 16 |
| AND | Andorra | — | — | 1814 | 1994 | 1993 | 9 |
| ARE | United Arab Emirates | 1971 | 1971 | 1971 | 1971 | 1971 | 5 |
| ARG | Argentina | 1789 | 1900 | 1789 | 1825 | 1946 | 22 |
| ARM | Armenia | 1990 | 1990 | 1991 | 1991 | 1990 | 9 |
| ATG | Antigua and Barbuda | — | — | 1951 | 1982 | 1984 | 10 |
| AUS | Australia | 1789 | 1900 | 1789 | 1901 | 1945 | 22 |
| AUT | Austria | 1789 | 1900 | 1789 | 1800 | 1945 | 17 |
| AZE | Azerbaijan | 1990 | 1990 | 1991 | 1991 | 1990 | 7 |
| BDI | Burundi | 1916 | 1916 | 1916 | 1962 | 1961 | 7 |
| BEL | Belgium | 1789 | 1900 | 1789 | 1830 | 1946 | 17 |
| BEN | Benin | 1900 | 1900 | 1892 | 1960 | 1979 | 13 |
| BFA | Burkina Faso | 1919 | 1919 | 1919 | 1960 | 1970 | 11 |
| BGD | Bangladesh | 1971 | 1971 | 1971 | 1971 | 1973 | 12 |
| BGR | Bulgaria | 1878 | 1900 | 1878 | 1879 | 1945 | 22 |
| BHR | Bahrain | 1900 | 1900 | 1789 | 1971 | 1973 | 7 |
| BHS | Bahamas | — | — | 1789 | 1973 | 1973 | 11 |
| BIH | Bosnia and Herzegovina | 1992 | 1992 | 1990 | 1992 | 1990 | 10 |
| BLR | Belarus | 1990 | 1990 | 1991 | 1991 | 1990 | 9 |
| BLZ | Belize | — | — | 1862 | 1981 | 1984 | 10 |
| BOL | Bolivia (Plurinational State of) | 1825 | 1900 | 1825 | 1825 | 1951 | 13 |
| BRA | Brazil | 1789 | 1900 | 1789 | 1824 | 1945 | 15 |
| BRB | Barbados | 1900 | 1900 | 1839 | 1966 | 1966 | 13 |
| BRN | Brunei Darussalam | — | — | 1789 | 1985 | 2015 | 3 |
| BTN | Bhutan | 1900 | 1900 | 1789 | 1907 | 1953 | 4 |
| BWA | Botswana | 1900 | 1900 | 1885 | 1966 | 1965 | 12 |
| CAF | Central African Republic | 1920 | 1920 | 1894 | 1960 | 1960 | 8 |
| CAN | Canada | 1841 | 1900 | 1841 | 1867 | 1945 | 18 |
| CHE | Switzerland | 1798 | 1900 | 1798 | 1848 | 1971 | 15 |
| CHL | Chile | 1789 | 1900 | 1789 | 1818 | 1945 | 12 |
| CHN | China | 1789 | 1900 | 1789 | 1800 | 1954 | 11 |
| CIV | Côte d’Ivoire | 1900 | 1900 | 1893 | 1960 | 1960 | 13 |
| CMR | Cameroon | 1961 | 1961 | 1884 | 1960 | 1960 | 11 |
| COD | Democratic Republic of the Congo | 1900 | 1900 | 1877 | 1960 | 1960 | 9 |
| COG | Congo | 1903 | 1903 | 1882 | 1960 | 1959 | 12 |
| COL | Colombia | 1810 | 1900 | 1789 | 1832 | 1947 | 17 |
| COM | Comoros | 1900 | 1900 | 1886 | 1975 | 1978 | 11 |
| CPV | Cabo Verde | 1900 | 1900 | 1789 | 1975 | 1975 | 10 |
| CRI | Costa Rica | 1838 | 1900 | 1838 | 1838 | 1949 | 15 |
| CUB | Cuba | 1789 | 1900 | 1789 | 1902 | 1948 | 10 |
| CYP | Cyprus | 1900 | 1900 | 1878 | 1960 | 1960 | 12 |
| CZE | Czechia | 1918 | 1918 | 1918 | 1993 | 1993 | 15 |
| DEU | Germany | 1789 | 1900 | 1789 | 1800 | 1990 | 22 |
| DJI | Djibouti | 1900 | 1900 | 1885 | 1977 | 1977 | 9 |
| DMA | Dominica | — | — | 1924 | 1978 | 1980 | 10 |
| DNK | Denmark | 1789 | 1900 | 1789 | 1800 | 1945 | 21 |
| DOM | Dominican Republic | 1789 | 1900 | 1789 | 1844 | 1947 | 14 |
| DZA | Algeria | 1900 | 1900 | 1789 | 1962 | 1962 | 10 |
| ECU | Ecuador | 1830 | 1900 | 1830 | 1830 | 1948 | 20 |
| EGY | Egypt | 1789 | 1900 | 1789 | 1922 | 1950 | 18 |
| ERI | Eritrea | 1900 | 1900 | 1889 | 1993 | 1994 | 1 |
| ESP | Spain | 1789 | 1900 | 1789 | 1800 | 1946 | 18 |
| EST | Estonia | 1918 | 1918 | 1917 | 1919 | 1990 | 9 |
| ETH | Ethiopia | 1789 | 1900 | 1789 | 1855 | 1957 | 9 |
| FIN | Finland | 1863 | 1900 | 1809 | 1917 | 1945 | 15 |
| FJI | Fiji | 1900 | 1900 | 1874 | 1970 | 1972 | 14 |
| FRA | France | 1789 | 1900 | 1789 | 1800 | 1945 | 14 |
| FSM | Micronesia (Federated States of) | — | — | 1947 | 1992 | 1993 | 16 |
| GAB | Gabon | 1910 | 1910 | 1885 | 1960 | 1961 | 12 |
| GBR | United Kingdom of Great Britain and Northern Ireland | 1789 | 1900 | 1789 | 1800 | 1945 | 15 |
| GEO | Georgia | 1990 | 1990 | 1991 | 1991 | 1990 | 10 |
| GHA | Ghana | 1902 | 1902 | 1902 | 1957 | 1960 | 11 |
| GIN | Guinea | 1900 | 1900 | 1891 | 1958 | 1963 | 9 |
| GMB | Gambia | 1900 | 1900 | 1889 | 1965 | 1966 | 11 |
| GNB | Guinea-Bissau | 1900 | 1900 | 1892 | 1974 | 1972 | 11 |
| GNQ | Equatorial Guinea | 1900 | 1900 | 1900 | 1968 | 1968 | 10 |
| GRC | Greece | 1822 | 1900 | 1822 | 1827 | 1946 | 19 |
| GRD | Grenada | — | — | 1951 | 1974 | 1972 | 10 |
| GTM | Guatemala | 1789 | 1900 | 1789 | 1839 | 1954 | 15 |
| GUY | Guyana | 1900 | 1900 | 1892 | 1966 | 1964 | 13 |
| HKG | China, Hong Kong Special Administrative Region | 1900 | 1900 | — | — | — | — |
| HND | Honduras | 1838 | 1900 | 1838 | 1839 | 1949 | 13 |
| HRV | Croatia | 1941 | 1941 | 1990 | 1991 | 1992 | 11 |
| HTI | Haiti | 1789 | 1900 | 1789 | 1820 | 1946 | 9 |
| HUN | Hungary | 1789 | 1900 | 1867 | 1867 | 1945 | 14 |
| IDN | Indonesia | 1800 | 1900 | 1800 | 1945 | 1950 | 12 |
| IND | India | 1789 | 1900 | 1789 | 1950 | 1951 | 14 |
| IRL | Ireland | 1919 | 1919 | 1918 | 1921 | 1948 | 16 |
| IRN | Iran (Islamic Republic of) | 1900 | 1900 | 1789 | 1800 | 1947 | 15 |
| IRQ | Iraq | 1920 | 1920 | 1920 | 1932 | 1948 | 12 |
| ISL | Iceland | 1900 | 1900 | 1844 | 1918 | 1946 | 17 |
| ISR | Israel | 1948 | 1948 | 1948 | 1948 | 1949 | 19 |
| ITA | Italy | 1862 | 1900 | 1861 | 1861 | 1948 | 15 |
| JAM | Jamaica | 1900 | 1900 | 1884 | 1962 | 1967 | 14 |
| JOR | Jordan | 1922 | 1922 | 1922 | 1946 | 1946 | 10 |
| JPN | Japan | 1789 | 1900 | 1789 | 1800 | 1946 | 19 |
| KAZ | Kazakhstan | 1990 | 1990 | 1990 | 1991 | 1990 | 10 |
| KEN | Kenya | 1900 | 1900 | 1895 | 1963 | 1963 | 12 |
| KGZ | Kyrgyzstan | 1990 | 1990 | 1990 | 1991 | 1990 | 8 |
| KHM | Cambodia | 1900 | 1900 | 1789 | 1955 | 1955 | 9 |
| KIR | Kiribati | — | — | 1892 | 1997 | 1978 | 12 |
| KNA | Saint Kitts and Nevis | — | — | 1937 | 1984 | 1984 | 10 |
| KOR | Republic of Korea | 1789 | 1900 | 1789 | 1948 | 1948 | 15 |
| KWT | Kuwait | 1789 | 1900 | 1789 | 1961 | 1962 | 21 |
| LAO | Lao People's Democratic Republic | 1900 | 1900 | 1789 | 1954 | 1955 | 10 |
| LBN | Lebanon | 1919 | 1919 | 1918 | 1943 | 1947 | 9 |
| LBR | Liberia | 1821 | 1900 | 1821 | 1847 | 1997 | 8 |
| LBY | Libya | 1789 | 1934 | 1795 | 1951 | 1952 | 2 |
| LCA | Saint Lucia | — | — | 1951 | 1979 | 1979 | 11 |
| LIE | Liechtenstein | — | — | 1814 | 1991 | 1986 | 17 |
| LKA | Sri Lanka | 1900 | 1900 | 1789 | 1948 | 1947 | 10 |
| LSO | Lesotho | 1900 | 1900 | 1884 | 1966 | 1965 | 8 |
| LTU | Lithuania | 1918 | 1918 | 1918 | 1920 | 1990 | 9 |
| LUX | Luxembourg | 1915 | 1915 | 1815 | 1867 | 1945 | 12 |
| LVA | Latvia | 1920 | 1920 | 1918 | 1920 | 1990 | 11 |
| MAR | Morocco | 1789 | 1900 | 1789 | 1800 | 1963 | 11 |
| MCO | Monaco | — | — | 1814 | 1994 | 1963 | 12 |
| MDA | Republic of Moldova | 1990 | 1990 | 1991 | 1991 | 1990 | 12 |
| MDG | Madagascar | 1817 | 1900 | 1817 | 1960 | 1960 | 12 |
| MDV | Maldives | 1900 | 1900 | 1789 | 1966 | 1979 | 8 |
| MEX | Mexico | 1789 | 1900 | 1789 | 1822 | 1952 | 20 |
| MHL | Marshall Islands | — | — | 1875 | 1992 | 1991 | 8 |
| MKD | North Macedonia | 1991 | 1991 | 1990 | 1991 | 1990 | 10 |
| MLI | Mali | 1900 | 1900 | 1892 | 1960 | 1964 | 11 |
| MLT | Malta | 1900 | 1900 | 1840 | 1964 | 1966 | 13 |
| MMR | Myanmar | 1789 | 1900 | 1789 | 1948 | 1974 | 4 |
| MNE | Montenegro | 1900 | 1900 | 1789 | 2006 | 2006 | 6 |
| MNG | Mongolia | 1911 | 1911 | 1911 | 1924 | 1951 | 14 |
| MOZ | Mozambique | 1900 | 1900 | 1893 | 1975 | 1977 | 9 |
| MRT | Mauritania | 1904 | 1904 | 1904 | 1960 | 1965 | 8 |
| MUS | Mauritius | 1900 | 1900 | 1885 | 1968 | 1967 | 12 |
| MWI | Malawi | 1900 | 1900 | 1891 | 1964 | 1964 | 13 |
| MYS | Malaysia | 1900 | 1900 | 1874 | 1957 | 1959 | 12 |
| NAM | Namibia | 1900 | 1900 | 1884 | 1990 | 1990 | 8 |
| NER | Niger | 1922 | 1922 | 1922 | 1960 | 1958 | 12 |
| NGA | Nigeria | 1914 | 1914 | 1914 | 1960 | 1992 | 10 |
| NIC | Nicaragua | 1839 | 1900 | 1838 | 1838 | 1948 | 10 |
| NLD | Netherlands (Kingdom of the) | 1789 | 1900 | 1789 | 1815 | 1946 | 18 |
| NOR | Norway | 1807 | 1900 | 1789 | 1900 | 1945 | 15 |
| NPL | Nepal | 1789 | 1900 | 1789 | 1800 | 1952 | 11 |
| NRU | Naoero | — | — | 1888 | 1968 | 1968 | 22 |
| NZL | New Zealand | 1853 | 1900 | 1841 | 1857 | 1946 | 19 |
| OMN | Oman | 1900 | 1900 | 1789 | 1800 | 2003 | 5 |
| PAK | Pakistan | 1947 | 1947 | 1947 | 1947 | 1973 | 11 |
| PAN | Panama | 1903 | 1903 | 1903 | 1903 | 1946 | 10 |
| PER | Peru | 1821 | 1900 | 1789 | 1820 | 1945 | 14 |
| PHL | Philippines | 1900 | 1900 | 1789 | 1935 | 1946 | 22 |
| PLW | Palau | — | — | 1947 | 1995 | 1992 | 8 |
| PNG | Papua New Guinea | 1900 | 1900 | 1884 | 1975 | 1977 | 10 |
| POL | Poland | 1789 | 1918 | 1789 | 1918 | 1947 | 16 |
| PRK | Democratic People's Republic of Korea | 1945 | 1945 | 1948 | 1948 | 1948 | 12 |
| PRT | Portugal | 1789 | 1900 | 1789 | 1800 | 1945 | 21 |
| PRY | Paraguay | 1811 | 1900 | 1811 | 1811 | 1948 | 13 |
| QAT | Qatar | 1900 | 1900 | 1871 | 1971 | 2005 | 5 |
| ROU | Romania | 1831 | 1900 | 1789 | 1859 | 1946 | 13 |
| RUS | Russian Federation | 1789 | 1900 | 1789 | 1800 | 1990 | 14 |
| RWA | Rwanda | 1916 | 1916 | 1916 | 1962 | 1965 | 9 |
| SAU | Saudi Arabia | 1789 | 1900 | 1789 | 1926 | 2005 | 5 |
| SDN | Sudan | 1900 | 1900 | 1899 | 1956 | 1954 | 14 |
| SEN | Senegal | 1904 | 1904 | 1904 | 1960 | 1960 | 15 |
| SGP | Singapore | 1868 | 1900 | 1867 | 1965 | 1963 | 14 |
| SLB | Solomon Islands | 1900 | 1900 | 1893 | 1978 | 1980 | 11 |
| SLE | Sierra Leone | 1900 | 1900 | 1896 | 1961 | 1973 | 10 |
| SLV | El Salvador | 1838 | 1903 | 1838 | 1841 | 1962 | 21 |
| SMR | San Marino | — | — | 1789 | 1800 | 1945 | 13 |
| SOM | Somalia | 1900 | 1900 | 1890 | 1960 | 1960 | 7 |
| SRB | Serbia | 1834 | 1900 | 1804 | 1878 | 2007 | 18 |
| SSD | South Sudan | 2011 | 2011 | 2011 | 2011 | 2011 | 3 |
| STP | Sao Tome and Principe | 1900 | 1900 | 1789 | 1976 | 1975 | 12 |
| SUR | Suriname | 1900 | 1900 | 1863 | 1975 | 1977 | 10 |
| SVK | Slovakia | 1993 | 1993 | 1993 | 1993 | 1992 | 10 |
| SVN | Slovenia | 1989 | 1989 | 1990 | 1991 | 1992 | 10 |
| SWE | Sweden | 1789 | 1900 | 1789 | 1800 | 1948 | 20 |
| SWZ | Eswatini | 1900 | 1900 | 1899 | 1968 | 1972 | 11 |
| SYC | Seychelles | 1903 | 1903 | 1903 | 1976 | 1974 | 11 |
| SYR | Syrian Arab Republic | 1918 | 1918 | 1918 | 1944 | 1947 | 14 |
| TCD | Chad | 1920 | 1920 | 1920 | 1960 | 1962 | 7 |
| TGO | Togo | 1916 | 1916 | 1916 | 1960 | 1961 | 10 |
| THA | Thailand | 1789 | 1900 | 1789 | 1800 | 1945 | 19 |
| TJK | Tajikistan | 1990 | 1990 | 1990 | 1991 | 1990 | 7 |
| TKM | Turkmenistan | 1991 | 1991 | 1990 | 1991 | 1990 | 7 |
| TLS | Timor-Leste | 1900 | 1900 | 1859 | 2002 | 2007 | 6 |
| TON | Tonga | — | — | 1845 | 1970 | 1975 | 16 |
| TTO | Trinidad and Tobago | 1900 | 1900 | 1889 | 1962 | 1962 | 14 |
| TUN | Tunisia | 1789 | 1900 | 1789 | 1956 | 1959 | 16 |
| TUR | Türkiye | 1789 | 1900 | 1789 | 1800 | 1946 | 16 |
| TUV | Tuvalu | — | — | 1975 | 1978 | 1977 | 11 |
| TZA | United Republic of Tanzania | 1915 | 1915 | 1914 | 1961 | 1970 | 12 |
| UGA | Uganda | 1900 | 1900 | 1894 | 1962 | 1962 | 9 |
| UKR | Ukraine | 1990 | 1990 | 1991 | 1991 | 1990 | 8 |
| URY | Uruguay | 1825 | 1900 | 1825 | 1830 | 1946 | 10 |
| USA | United States of America | 1789 | 1900 | 1789 | 1800 | 1946 | 28 |
| UZB | Uzbekistan | 1912 | 1912 | 1990 | 1991 | 1990 | 7 |
| VCT | Saint Vincent and the Grenadines | — | — | 1951 | 1979 | 1979 | 11 |
| VEN | Venezuela (Bolivarian Republic of) | 1789 | 1900 | 1789 | 1830 | 1948 | 13 |
| VNM | Viet Nam | 1945 | 1945 | 1945 | 1976 | 1976 | 15 |
| VUT | Vanuatu | 1900 | 1900 | 1906 | 1980 | 1979 | 13 |
| WSM | Samoa | — | — | 1902 | 1962 | 1964 | 13 |
| XKX | Kosovo | 1999 | 1999 | 2008 | 2012 | — | — |
| YEM | Yemen | 1789 | 1918 | 1789 | 1918 | 1990 | 6 |
| ZAF | South Africa | 1900 | 1900 | 1900 | 1910 | 1945 | 14 |
| ZMB | Zambia | 1911 | 1911 | 1911 | 1964 | 1965 | 13 |
| ZWE | Zimbabwe | 1900 | 1900 | 1895 | 1965 | 1980 | 10 |

(V-Dem first = first year of `v2x_polyarchy`; RoW is coded from 1900. LIED/BMR first years follow the providers'
unit continuity, e.g. DEU from Prussia, RUS through the USSR in LIED; BMR maps USSR/West Germany separately, see above.)

## Tests

* `tests/unit/test_atlas_pol.py` (offline): RData reader round trip (NA vs NaN, ALTREP, truncation, size cap, refusal of
  code objects), V-Dem values/flags/geo/QIDs, LIED/BMR record checks (licence change, MD5 mismatch, partial file),
  IPU licence/truncation refusal, year-end selection, catalog series all resolvable, geo schemes, QID file evidence.
* `tests/integration/test_atlas_pol_ingest.py` (db): the four sources through `run_atlas_ingest(mode="fixture")`,
  rights recorded (CC-BY-SA / CC0 / CC-BY-NC-SA non_commercial), idempotent re-run.
* `tests/integration/test_atlas_pol_live.py` (live+db, deselected): the live run above (4 passed).
* Fixtures are documented subsets of the live captures (`tests/unit/atlas_pol_fixtures.py` says how; manifests carry
  `derived_from_sha256`); `tests/unit/atlas_pol_rdata_writer.py` writes the V-Dem subset bit-for-bit.

## Requests to other agents

* ATLASCORE: `vdem.indices` is 5.18 M rows (split if the 5 M guideline is hard). Consider adding `PSE` (West Bank +
  Gaza) only if a source codes the State of Palestine as a whole — none of these four does.
* WDHARVEST: once `registry/entities/country_qids.json` exists, crosswalk countries get QIDs from it; vdem_qids.json
  deliberately does not duplicate them.
* LINKS/NLQA: HOS/HOG names per year (`vdem.institutions.v2exnamhos`/`v2exnamhog`, text) can be matched to KG people;
  regime transitions (LIED `democratic_transition`/`transition_type`, BMR `democracy_trans`) are dated events.

