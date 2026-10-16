# V3 — A-TREATIES: international law commitments of every country (UNTC, OHCHR)

Agent A-TREATIES, 2026-10-16. Spec: `docs/roadmap/v3_world_graph.md`, interface `docs/dev/v3_atlas_interface.md` §7.
One live ingest of both sources into a per-session throwaway database (`tests/integration/test_atlas_treaties_live.py`,
`pytest -m "live and db"`, database dropped afterwards). The dev database `reva` was never touched.

## 1. What exists

| Piece | Path |
|---|---|
| UNTC connector (`atlas.untc`, 55 treaties, atlas + KG) | `src/reva/adapters/connectors/atlas_treaties.py` |
| OHCHR dashboard connector (`atlas.ohchr`, 18 core human rights instruments, atlas) | `src/reva/adapters/connectors/atlas_ohchr.py` |
| Sources | `registry/sources/untc.yaml` (`untc.mtdsg`), `registry/sources/ohchr_indicators.yaml` (`ohchr.indicators`) |
| Catalog (219 concepts, domain `treaties`, theme `law`) | `registry/atlas/catalog/treaties.yaml` |
| Geography aliases (scheme `untc`: 197 UNTC participant names; scheme `ohchr`: ISO3 + `PLS`→PSE) | `registry/atlas/geo_aliases/a_treaties.yaml` |
| Treaty → Wikidata QID mapping (verified entries only) | `registry/entities/treaty_qids.json` |
| Budget request (merged by ATLASCORE in budgets-1.3.6) | `registry/atlas/budget_requests/a_treaties.yaml` |
| Fixtures (real 2026-10-16 responses, trimmed; see manifest notes) | `tests/fixtures/source/atlas.untc/` (0.7 MB: 4 pages + terms), `tests/fixtures/source/atlas.ohchr/` (0.35 MB: 20 calls, 12 areas) |
| Tests | `tests/unit/test_atlas_treaties.py` (17, offline), `tests/integration/test_atlas_treaties_ingest.py` (2, throwaway DB, fixture mode), `tests/integration/test_atlas_treaties_live.py` (2, `live`) |

Run: `uv run python -m reva.atlas.ingest atlas.untc [--option treaties=XVIII-10,XXVII-7-d] [--fixture]`,
`uv run python -m reva.atlas.ingest atlas.ohchr [--fixture]`. Both fetch only through `run.get` (GET), archive every
raw response (`run.archive`) **after** the whole run parsed (layout drift archives nothing), register with
`run.register`; `atlas.untc` then loads the KG with `reva.kg.model.load_batch` when the `kg` schema exists.

## 2. Sources, access, licences (checked 2026-10-16)

| Source | Access | Licence | Decision |
|---|---|---|---|
| **UN Treaty Collection** (MTDSG status pages, depositary = UN Secretary-General) | No bulk/machine-readable export. One HTML page per treaty: `Pages/ViewDetails.aspx?src=TREATY&mtdsg_no=<n>&chapter=<c>`, some treaties on `ViewDetailsII` (`Temp=mtdsg2`: V-2, V-3, XXI-6) or `ViewDetailsIII` (`Temp=mtdsg3`: XXIII-1, XXVII-7) — the catalog stores the template so no redirect is followed. 55 pages, 0.13–3.3 MB (25.4 MB), sequential, ≥ 3 s apart. | **UN Terms of Use — not an open licence**: "personal, non-commercial use, without any right to resell or redistribute them or to compile or create derivative works therefrom" (terms page fetched, checked and archived on every run). | ingested, `open_licence: false`, flagged for maintainer review (internal research use; no redistribution without UN permission). The facts (State X deposited instrument Y on date Z) are public acts notified by the depositary. |
| **OHCHR Status of Ratification Dashboard** | The dashboard's own JSON API (`indicators.ohchr.org/webapi/dashboarddata/GetRatifications`, `GetCountries`, `GetCountriesForRatification?ratificationId=1..18`), GET, no key; 20 requests, 3.6 MB. | OHCHR copyright page is behind a Cloudflare challenge (HTTP 403, not readable); OHCHR is UN Secretariat → UN Terms of Use applied conservatively: **not open**. | ingested, `open_licence: false`. Adds reservations/declarations texts and individual-complaints / inquiry-procedure acceptance; no KG (the depositary record from UNTC is the KG source). |
| **ILO NORMLEX** (ILO conventions ratifications) | `normlex.ilo.org` and `www.ilo.org/dyn/normlex/...` answer a Cloudflare block page ("Sorry, you have been blocked", HTTP 403) to non-browser clients. | ILO copyright (not checked: page unreachable) | **skipped** — not circumvented. No `atlas_normlex.py` written (no data, no empty module). Alternative: ILO could be asked for a data export; ILOSTAT (A-ILO) has no ratification data. |
| **UNODA treaty database** (NPT, BWC — depositaries US/UK/Russia, not the UN SG) | `treaties.unoda.org` answers `202` + `x-amzn-waf-action: challenge` (AWS WAF bot challenge). | — | **skipped**: NPT and BWC are not covered. |
| **CITES** (depositary Switzerland), **Ramsar** (depositary UNESCO) | `cites.org` and `ramsar.org` answer HTTP 403 (bot protection). | — | **skipped**. Geneva Conventions (depositary Switzerland, ICRC database) not attempted. |

## 3. Parsing (what is read, never inferred)

* **Participant table** (`…_tblgrid`): participant name exactly as UNTC writes it (footnote superscripts removed and
  kept as note numbers), signature date (or "Succession to signature (d)"), consent instrument date and type from the
  column header's **own legend** (`Approval(AA), Acceptance(A), Accession(a), Succession(d), Ratification` → the uncoded
  term is the default; a page whose legend has no uncoded term, e.g. the 1967 Refugee Protocol, refuses uncoded
  cells), an optional "Provisional application(n)" column (Cluster Munitions), and whether the name links to
  declarations/reservations. **Square brackets** = UNTC lists the dates of a participant that has withdrawn.
* **Provider counts**: "Signatories : n. Parties : m" — for all 55 treaties the parsed listed consents equal the
  provider's party count and the parsed signatures equal its signatory count (reported per run, tested).
* **Withdrawals/denunciations** (29 events): (1) footnote tables `Participant / Date of notification / Date of effect`
  (Rome Statute: Burundi, Philippines; South Africa, Gambia, Hungary with `(Withdrawn)` = notification revoked; Niger,
  Burkina Faso, Mali, Venezuela, Chad, Nauru notified in 2026 with effect in 2027; Kyoto: Canada); (2) footnote
  sentences "On <date>, the Government of <State> notified/deposited … withdraw from / denounce … with effect from /
  took effect (for X) on <date>" (regex, flagged `basis: note_text`; reservation/declaration withdrawals excluded),
  attributed to a participant referencing the note. Bracketed participants without a stated effect date: **Jamaica**
  (ICCPR-OP1, notification 1997-10-23 only).
* **Historical successor-state actions** are recorded as UNTC states them: the table's participant name and date
  (e.g. ICCPR: "Russian Federation" listed with the USSR's 1968 signature and 1973 ratification dates and no note on
  that page — continuity is explained in UNTC's general "Historical Information"; "Czech Republic" and "Slovakia"
  listed with successions `d` of 22 Feb 1993 / 28 May 1993 and note 7 "Czechoslovakia had signed and ratified the
  Convention on 7 October 1968 and 23 December 1975…"), every referenced footnote kept verbatim as an atlas row `<mtdsg>|note=<n>` (≤ 4000 chars, truncation flagged) and
  as `untc_notes` qualifier in the KG. Former States themselves (USSR, Yugoslavia, Czechoslovakia, GDR) appear in UNTC
  only inside notes, so no rows are created for them.

## 4. Outputs (live run 2026-10-16 18:38 UTC)

| Output | Content |
|---|---|
| atlas `untc.treaty_status` | **23,129 rows**, 198 participants (197 crosswalk entities + European Union, kept unmapped), 55 treaties. Indicators `<mtdsg>\|action=signature\|consent\|provisional_application\|withdrawal_notification\|withdrawal_effect` (day periods, `value_text` = instrument term), `<mtdsg>\|status` (as listed on the retrieval day: `party`, `signatory`, `withdrawn`, … with `scheduled_withdrawal_effect`), `<mtdsg>\|note=<n>`. Flags: provider text, legend code, bracketed, note numbers, declarations/reservations link. |
| atlas `ohchr.hr_ratification` | **8,735 rows**, 199 areas × 18 instruments: `<ACR>\|field=status\|signature\|consent\|denunciation\|individual_complaints\|inquiry_procedure\|reservations\|declarations`; ERU (EU) unmapped. |
| KG (`source_key untc.mtdsg`) | **13,664 statements, 0 rejected**, 56 entities: `P1891` signatory (treaty → State, point in time = signature date) **5,219**; `untc:party` (start = consent instrument date, end = date of effect of a withdrawal stated by UNTC) **8,389**; `untc:former_party` 1 (Jamaica/ICCPR-OP1: bracketed, no effect date); `untc:mtdsg_number` 55. States are Wikidata QIDs via `registry/entities/country_qids.json` (249 resolved; `iso3:PSE` node for Palestine, not in that file), the EU is `untc:participant:european_union`. Treaties are QIDs from `treaty_qids.json` when verified, else `untc:treaty:<mtdsg>` entities (labels en/fr). |

Budget: UNTC reserved 59 / used 56 requests (25.4 MB); OHCHR reserved 22 / used 20 (3.6 MB); policy budgets-1.3.6, 0 refused.
Statement ids (`sig:<mtdsg>:<participant>`, `party:<mtdsg>:<participant>`) do not depend on the subject/object node, so
a later run with more QIDs is a new revision of the same statements; a full live run retracts statements no longer built.

**Links**: LINKS' `co_signatory` derivation (`kg_shared_subject` over P1891 on the treaty) applies directly to these
statements ("signataires du même traité"). A `co_party` link type over `untc:party` (overlapping party intervals,
treaty as `via`) is requested below — `registry/links/link_types.yaml` is LINKS-owned.

## 5. Coverage (treaties × States, UNTC status on 2026-10-16)

Parsed counts are identical in the 00:10 UTC capture used for development and the 18:38 UTC live run. "Major
states not party": among USA, CHN, RUS, IND, BRA, FRA, GBR, DEU, JPN, IDN, PAK, NGA, SAU, IRN, ISR, TUR, EGY, ZAF;
`(s)` = signatory only, `(w)` = withdrawn, none = no action listed by UNTC (as reported, not interpreted).

| MTDSG | Treaty | Participants | Parties (UNTC listed / provider count) | Signatories only | Withdrawn | Major states not party (status) |
|---|---|---|---|---|---|---|
| IV-1 | Genocide Convention (1948) | 155 | 154 / 154 | 1 | 0 | JPN, IDN |
| IV-2 | International Convention on the Elimination of All Forms of Racial Discrimination | 185 | 182 / 182 | 3 | 0 |  |
| IV-3 | International Covenant on Economic, Social and Cultural Rights | 178 | 174 / 174 | 4 | 0 | USA(s), SAU |
| IV-3-a | Optional Protocol to the ICESCR | 55 | 31 / 31 | 24 | 0 | USA, CHN, RUS, IND, BRA, GBR, JPN, IDN, PAK, NGA, SAU, IRN, ISR, TUR, EGY, ZAF |
| IV-4 | International Covenant on Civil and Political Rights | 181 | 175 / 175 | 6 | 0 | CHN(s), SAU |
| IV-5 | Optional Protocol to the ICCPR (individual communications) | 123 | 116 / 116 | 4 | 3 | USA, CHN, IND, GBR, JPN, IDN, PAK, NGA, SAU, IRN, ISR, EGY |
| IV-12 | Second Optional Protocol to the ICCPR (abolition of the death penalty) | 92 | 92 / 92 | 0 | 0 | USA, CHN, RUS, IND, JPN, IDN, PAK, NGA, SAU, IRN, ISR, EGY |
| IV-7 | International Convention on the Suppression and Punishment of the Crime of Apartheid | 111 | 110 / 110 | 1 | 0 | USA, BRA, FRA, GBR, DEU, JPN, IDN, SAU, ISR, TUR |
| IV-8 | Convention on the Elimination of All Forms of Discrimination against Women | 191 | 189 / 189 | 2 | 0 | USA(s), IRN |
| IV-8-b | Optional Protocol to CEDAW | 127 | 116 / 116 | 11 | 0 | USA, CHN, IND, JPN, IDN(s), PAK, SAU, IRN, ISR, EGY |
| IV-9 | Convention against Torture | 180 | 176 / 176 | 4 | 0 | IND(s), IRN |
| IV-9-b | Optional Protocol to the Convention against Torture | 107 | 96 / 96 | 11 | 0 | USA, CHN, RUS, IND, JPN, IDN, PAK, SAU, IRN, ISR, EGY |
| IV-11 | Convention on the Rights of the Child | 197 | 196 / 196 | 1 | 0 | USA(s) |
| IV-11-b | Optional Protocol to the CRC on children in armed conflict | 180 | 173 / 173 | 7 | 0 | IRN(s) |
| IV-11-c | Optional Protocol to the CRC on the sale of children, child prostitution and child pornography | 186 | 178 / 178 | 8 | 0 |  |
| IV-11-d | Optional Protocol to the CRC on a communications procedure | 69 | 54 / 54 | 15 | 0 | USA, CHN, RUS, IND, GBR, JPN, IDN, PAK, NGA, SAU, IRN, ISR, EGY, ZAF |
| IV-13 | International Convention on the Protection of the Rights of All Migrant Workers and Members of Their Families | 71 | 60 / 60 | 11 | 0 | USA, CHN, RUS, IND, BRA, FRA, GBR, DEU, JPN, PAK, SAU, IRN, ISR, ZAF |
| IV-15 | Convention on the Rights of Persons with Disabilities | 196 | 194 / 194 | 2 | 0 | USA(s) |
| IV-15-a | Optional Protocol to the CRPD | 131 | 110 / 110 | 21 | 0 | USA, CHN, RUS, IND, JPN, IDN, PAK, IRN, ISR, EGY |
| IV-16 | International Convention for the Protection of All Persons from Enforced Disappearance | 117 | 78 / 78 | 39 | 0 | USA, CHN, RUS, IND(s), GBR, IDN(s), PAK, SAU, IRN, ISR, TUR, EGY |
| V-2 | Convention relating to the Status of Refugees (1951) | 146 | 146 / 146 | 0 | 0 | USA, IND, IDN, PAK, SAU |
| V-5 | Protocol relating to the Status of Refugees (1967) | 147 | 147 / 147 | 0 | 0 | IND, IDN, PAK, SAU |
| V-3 | Convention relating to the Status of Stateless Persons (1954) | 102 | 99 / 99 | 1 | 1 | USA, CHN, RUS, IND, JPN, IDN, PAK, SAU, IRN, EGY, ZAF |
| V-4 | Convention on the Reduction of Statelessness (1961) | 85 | 82 / 82 | 3 | 0 | USA, CHN, RUS, IND, FRA(s), JPN, IDN, PAK, SAU, IRN, ISR(s), TUR, EGY, ZAF |
| III-3 | Vienna Convention on Diplomatic Relations | 193 | 193 / 193 | 0 | 0 |  |
| III-6 | Vienna Convention on Consular Relations | 187 | 183 / 183 | 4 | 0 | ISR(s) |
| XXIII-1 | Vienna Convention on the Law of Treaties | 134 | 119 / 119 | 15 | 0 | USA(s), IND, FRA, IDN, PAK(s), IRN(s), ISR, TUR, ZAF |
| XXI-6 | United Nations Convention on the Law of the Sea | 184 | 172 / 172 | 12 | 0 | USA, IRN(s), ISR, TUR |
| XXI-10 | Agreement on Marine Biological Diversity of Areas beyond National Jurisdiction (BBNJ) | 150 | 104 / 104 | 46 | 0 | USA(s), RUS, IND(s), PAK(s), NGA(s), SAU, IRN, ISR, EGY(s), ZAF(s) |
| XVIII-10 | Rome Statute of the International Criminal Court | 156 | 125 / 125 | 29 | 2 | USA(s), CHN, RUS(s), IND, IDN, PAK, SAU, IRN(s), ISR(s), TUR, EGY(s) |
| XVIII-11 | International Convention for the Suppression of the Financing of Terrorism | 193 | 191 / 191 | 2 | 0 |  |
| XVIII-12 | UN Convention against Transnational Organized Crime | 196 | 195 / 195 | 1 | 0 |  |
| XVIII-12-a | Protocol to Prevent, Suppress and Punish Trafficking in Persons | 186 | 185 / 185 | 1 | 0 | IRN |
| XVIII-14 | UN Convention against Corruption | 194 | 193 / 193 | 1 | 0 |  |
| VI-19 | UN Convention against Illicit Traffic in Narcotic Drugs and Psychotropic Substances (1988) | 192 | 192 / 192 | 0 | 0 |  |
| IX-4 | WHO Framework Convention on Tobacco Control | 189 | 183 / 183 | 6 | 0 | USA(s), IDN |
| XXVI-2 | Convention on Certain Conventional Weapons | 132 | 128 / 128 | 4 | 0 | IDN, NGA(s), IRN, EGY(s) |
| XXVI-3 | Chemical Weapons Convention | 194 | 193 / 193 | 1 | 0 | ISR(s), EGY |
| XXVI-4 | Comprehensive Nuclear-Test-Ban Treaty | 188 | 179 / 179 | 9 | 0 | USA(s), CHN(s), RUS(s), IND, PAK, SAU, IRN(s), ISR(s), EGY(s) |
| XXVI-5 | Anti-Personnel Mine Ban Convention (Ottawa) | 167 | 162 / 162 | 0 | 5 | USA, CHN, RUS, IND, PAK, SAU, IRN, ISR, EGY |
| XXVI-6 | Convention on Cluster Munitions | 125 | 112 / 112 | 12 | 1 | USA, CHN, RUS, IND, BRA, IDN(s), PAK, SAU, IRN, ISR, TUR, EGY |
| XXVI-8 | Arms Trade Treaty | 143 | 118 / 118 | 25 | 0 | USA(s), RUS, IND, IDN, PAK, SAU, IRN, ISR(s), TUR(s), EGY |
| XXVI-9 | Treaty on the Prohibition of Nuclear Weapons | 101 | 75 / 75 | 26 | 0 | USA, CHN, RUS, IND, BRA(s), FRA, GBR, DEU, JPN, PAK, SAU, IRN, ISR, TUR, EGY |
| XXVII-2 | Vienna Convention for the Protection of the Ozone Layer | 198 | 198 / 198 | 0 | 0 |  |
| XXVII-2-a | Montreal Protocol on Substances that Deplete the Ozone Layer | 198 | 198 / 198 | 0 | 0 |  |
| XXVII-3 | Basel Convention on Hazardous Wastes | 193 | 191 / 191 | 2 | 0 | USA(s) |
| XXVII-7 | UN Framework Convention on Climate Change | 198 | 198 / 198 | 0 | 0 |  |
| XXVII-7-a | Kyoto Protocol | 194 | 192 / 192 | 1 | 1 | USA(s) |
| XXVII-7-d | Paris Agreement | 198 | 194 / 194 | 3 | 1 | USA(w), IRN(s) |
| XXVII-8 | Convention on Biological Diversity | 197 | 196 / 196 | 1 | 0 | USA(s) |
| XXVII-8-a | Cartagena Protocol on Biosafety | 181 | 173 / 173 | 8 | 0 | USA, RUS, ISR |
| XXVII-10 | UN Convention to Combat Desertification | 197 | 197 / 197 | 0 | 0 |  |
| XXVII-14 | Rotterdam Convention (prior informed consent) | 172 | 169 / 169 | 3 | 0 | USA(s), EGY |
| XXVII-15 | Stockholm Convention on Persistent Organic Pollutants | 191 | 186 / 186 | 5 | 0 | USA(s), ISR(s) |
| XXVII-17 | Minamata Convention on Mercury | 167 | 154 / 154 | 13 | 0 | RUS(s), EGY |

States party to at least one of the 55 treaties: **197 of 251 crosswalk entities** (all UN members + Holy See,
State of Palestine, Cook Islands, Niue); the 54 others are territories/dependencies (and Taiwan, Kosovo) that UNTC
does not list as participants (their parent State's territorial application is in notes). Most treaties joined
(of 55): Uruguay, Ecuador 55; Costa Rica 54; Benin, Paraguay, Honduras, Panama 53. Fewest among UN members: **USA 21**,
Bhutan, Holy See 24, DPRK 25, Micronesia, Brunei 26. Major States: USA 21, IRN 29, ISR 31, IND 33, SAU 33, PAK 34,
EGY 34, IDN 35, RUS 37, CHN 38, JPN 42, TUR 45, GBR 48, ZAF 48, NGA 49, BRA 50, FRA 50, DEU 52.

Notable as reported by UNTC: the United States is listed with bracketed Paris Agreement acceptance (withdrawal effective
2026-01-27, notified 2025-01-27; the 2019–2020 withdrawal is in the same note) and a UNFCCC withdrawal notified
2026-02-27 with effect 2027-02-27 (still a party on the retrieval day); Estonia, Finland, Latvia, Lithuania and Poland
withdrew from the Ottawa Convention (effects 2025-12-27 to 2026-02-20), Lithuania from Cluster Munitions (2025-03-06);
six States notified Rome Statute withdrawals in 2026 (effect 2027); Hungary revoked its 2025 notification.

### OHCHR vs UNTC (per-source, nothing blended)

Party sets compared on the 18 instruments (OHCHR `state_party` vs UNTC listed consent): OHCHR additionally lists
**Greenland** (GRL) as a State party for 14 instruments (UNTC lists Denmark's territorial applications in notes);
OHCHR still lists **Belarus, Jamaica, Trinidad and Tobago** as parties to ICCPR-OP1 although UNTC brackets them
(denunciations 2023, 1997, 2000); OHCHR lags recent actions (UNTC-only: CRC-OP-IC Kazakhstan, Seychelles; CRPD
Bhutan, Solomon Islands; CRPD-OP Georgia; OP-CAT Congo). OHCHR `LastSynced` per instrument ranges 2023-02-21 – 2026-09-29.
`StatePartyType` (undocumented) was decoded against UNTC ICCPR: 0 accession (99/99), 1 succession (8/8),
2 ratification (67/67), 3 none (7/7); all consent dates agree.

## 6. Treaty QIDs (`registry/entities/treaty_qids.json`)

**3 of 55 verified**: UNFCCC → Q208645, Kyoto Protocol → Q47359, Paris Agreement → Q21707860 (English label or alias
equals the UNTC title; item has P31; revision ids and raw response sha256 in the file). Method: Wikidata's search
endpoints (CirrusSearch on `api.php` and the REST search) were under machine-wide 429 cool-downs for the whole session
(other agents' traffic), so a search-based run (55 queries) did not progress (3 queries in ~15 min) and was stopped.
Instead 30 agent-proposed candidate QIDs were fetched with `Special:EntityData` through WDHARVEST's locked, budgeted
client (`reva.kg.harvest.budgeted_client`, mode `linked_data`) and checked: **26 were wrong items** (e.g. a dam, a
village, a shark) and were rejected, 1 is missing — which is exactly why nothing is mapped without verification. No
Wikidata identifier keyed by the MTDSG number was found (the Paris Agreement item has a UNTC `showDetails` URL in P973,
keyed by UNTC's objid). Unmapped treaties are `untc:treaty:<mtdsg>` KG entities (en/fr labels); statement ids do not
depend on the subject, so mapping more QIDs later makes new revisions of the same statements. The live load above ran
before the 3 QIDs existed (all treaty subjects were `untc:` nodes); the fixture tests run with the mapping.

## 7. Gaps and what would fill them

* **ILO conventions (NORMLEX)**: blocked by Cloudflare for non-browser clients — not ingested. Fill: an ILO data export
  or permission/allow-listing for a documented crawler; Wikidata has no systematic per-State ratification statements.
* **NPT, BWC** (UNODA database behind an AWS WAF challenge), **CITES**, **Ramsar**, **Geneva Conventions** (non-UN
  depositaries; CITES/Ramsar sites answer 403): not covered.
* **Entry into force per State** is not on the status pages (only the treaty's general entry into force): `untc:party`
  starts at the deposit date (qualifier `start_basis` says so).
* **Withdrawal dates from notes** are parsed with two bounded sentence patterns + footnote tables; a note phrased
  otherwise leaves a bracketed participant with `untc:former_party` (no end date; today: Jamaica/ICCPR-OP1 only).
  Re-accessions after a denunciation (Trinidad and Tobago 1998, Guyana 1999, Bhutan/UNCAC 2023) are read from the
  table only (the latest listed date), earlier periods stay in the `withdrawal_events` qualifier.
* **Reservations/declarations text** comes from OHCHR for the 18 instruments only; for the other UNTC treaties the
  atlas/KG carry a boolean (participant name linked to declarations) — the UNTC declaration texts are in the archived
  pages (`dgDec` table) but not parsed.
* **Treaty QIDs**: 52 of 55 unmapped (see §6).

## 8. Requests to other owners

1. **LINKS** (`registry/links/link_types.yaml`): `co_signatory` already applies (P1891 on the treaty, here with exact
   signature dates). Please add a `co_party` link type over `untc:party` (two States with overlapping party intervals
   on the same treaty, `via` = treaty, temporal = interval overlap; hub damping for near-universal treaties such as
   UNFCCC/Montreal with 198 parties) and allow-list `untc:party`/`untc:former_party` as statement edges
   ("partie à" / "party to").
2. **KGHIST / WDHARVEST**: when `history.treaties` is harvested, map Wikidata treaty items to MTDSG numbers by label
   (or add candidates to `registry/entities/treaty_qids.json` with the same verification rule); re-running `atlas.untc`
   then re-keys the subjects. WDHARVEST's `country_qids.json` has no entry for PSE (State of Palestine): its statements
   use `iso3:PSE`.
3. **Lead / maintainer**: both sources are **not openly licensed** (UN Terms of Use): keep the tables and statements
   internal; review before any redistribution.
4. **Budget note**: one stopped Wikidata search run (SIGTERM) may have left an unsettled `wikidata.*` reservation in
   the harvest ledger (`reva_kg_wdharvest`, run id prefix `kgatreaties-`); it only reduces that day's remaining
   allowance.
