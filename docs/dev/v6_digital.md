# V6DIGITAL — the digital world (internet shutdowns, state-sponsored cyber operations, ICT indicators, IXPs)

Agent V6DIGITAL, 2026-10-17. Connectors `atlas.keepiton` and `atlas.cfr_cyber` (fixture-tested, versions registered
in the dev DB). **The live ingest was not run:** free disk was 4.3–4.8 GB (below the 5 GB rule) and stayed at 4.3 GB
after `uv run reva objects compress` (it freed nothing more). The commands to finish the job are in §6.

## 1. Sources: what was checked and what was decided

| Source | Access | Licence (checked 2026-10-17) | Decision |
|---|---|---|---|
| **Access Now #KeepItOn STOP** (internet shutdowns 2016–2025) | public Google Sheet `1DvPAuHNLp5BXGb0nnZDGNoiIwEeu2ogdXEIDvT4Hyfk`, tab "Combined (2016-2025)" (gid 1484157943), linked as "DOWNLOAD OUR DATASET" from the methodology page | **No dataset licence.** Access Now's media-usage page: "Any and all original Media Material on the Access Now website may be freely licensed and distributed at will under the Creative Commons Attribution (CC BY) 4.0 International License, unless otherwise noted". It defines Media Material as images, symbols, icons, graphics and logos. The site Terms of Use otherwise allow "personal, non-commercial use only". | Connector built. Rights recorded as **not open**: redistribution and commercial use not granted (`restricted` on every cell). |
| **CFR Cyber Operations Tracker** (state-sponsored incidents since 2005) | "Download the Data" → `https://static.cfr.org/cyber-operations/data/cyber-operations-incidents.csv` (558 kB, 897 incidents, 2005-08-25 … 2023-12-19, 19 undated; Last-Modified 2025-11-13) | CFR Terms of Use, "All rights reserved": "You agree not to … reproduce, display, publicly perform, distribute, or otherwise use such content or materials in any way for any public or commercial purpose" | Connector built. Rights recorded as **non-commercial, no redistribution**. |
| ITU DataHub (ICT indicators, prices) | `datahub.itu.int` and `api.datahub.itu.int` answer **HTTP 403** (CloudFront) to a non-browser client. The www.itu.int statistics page offers only `ITU_regional_global_Key_ICT_indicator_aggregates_Nov_2025.xlsx` (regional/global aggregates, 110 kB). | ITU website terms: use is allowed "for personal, educational, or non-commercial purposes". "You may not modify, reproduce, distribute … without obtaining prior written permission from ITU." | **Not ingested.** We do not work around the 403. The country series are already in the atlas through the World Bank WDI, which re-publishes them under CC BY 4.0: `wb.it.net.user.zs` (internet users %), `.fe.zs`/`.ma.zs` (female/male), `.user.p2`, `wb.it.net.bbnd(.p2)` (fixed broadband), `wb.it.cel.sets(.p2)` (mobile subscriptions), `wb.it.mlt.main(.p2)` (fixed telephone), `wb.it.net.secr(.p6)` (secure servers), `wb.it.cmp.pcmp.p2`. ITU price baskets are only in DataHub: **owner decision**. |
| PeeringDB (IXPs, facilities) | Public API works (`/api/ix`, `/api/fac`; 1,329 IXPs and 5,885 facilities on the site's statistics) | PeeringDB AUP: "Except for Internet operational purposes approved by PeeringDB, no part of the PeeringDB data may be reproduced, stored in a retrieval system, or transmitted … without prior permission of PeeringDB". "The PeeringDB data may not be passed on in bulk to any other person or organization unless approved by PeeringDB." "Internet research and analysis" is listed as an operational issue, but it still needs PeeringDB's approval. | **Not fetched.** Archiving PeeringDB data is "storing in a retrieval system": **owner decision** (ask PeeringDB). |
| Cloudflare Radar (outages, traffic) | The API needs an account API token ("you must obtain your API token first"). `radar.cloudflare.com/about` answers 403. | Cloudflare's developer docs: "public API (CC BY-NC 4.0 license)" | **Not ingested** (an account is needed and the licence is non-commercial): **owner decision**. |
| Submarine cables | already covered: `atlas.wikidata_cables` (A-INFRA, Wikidata CC0) | — | unchanged |

Bytes fetched while researching (scratchpad only, not archived): ≈ 6 MB. Planned raw archive of one live run
for both sources: ≈ 4.6 MB (well under the 100 MB cap). Budget: `registry/atlas/budget_requests/v6digital.yaml`
→ budgets-1.3.36 (`accessnow.stop` 8/day, `cfr.cyber_operations` 6/day). The merge was pinned in
`src/reva/autonomy/policy.py`; later agents have since merged and re-pinned newer versions.

## 2. What the connectors produce

### `atlas.keepiton` (source key `accessnow.stop`, `src/reva/adapters/connectors/atlas_keepiton.py`)

* **Transport.** The Sheet's `/export?format=csv` answers 307 to a per-request `doc-*.googleusercontent.com` host,
  which cannot be allow-listed. The connector therefore reads the Google Visualization endpoint of the same tab on
  docs.google.com (`/gviz/tq?tqx=out:csv`), plus its JSON header (`tq=limit 0`), which records the column types on
  every run.
* **Known loss on that endpoint.** Columns it types as date or number are start_date, end_date, duration,
  econ_impact and count_year. In those columns, text cells are served empty. Compared with the /export bytes this
  affects 18 cells: 9 econ_impact, 7 duration (e.g. "Curfew Style", "2 hours") and 2 end_date (e.g. "April/2017
  following …"). An empty typed cell is read as "blank or text not served", which is a typed Missing and never
  zero. econ_impact is not loaded.
* **Definitions are quoted** on every run from the archived methodology page: the "Metadata" table, with 45 of 47
  columns described (geo_scope and count_year are not), the shutdown definition ("An internet shutdown is an
  intentional disruption of internet or electronic communications, rendering them inaccessible or effectively
  unusable, for a specific population or within a location, often to exert control over the flow of information.")
  and the instance definition.
* **Instances.** The sheet publishes no identifier. The key is the verbatim values of start_date, country,
  area_name, shutdown_type, affected_network, shutdown_extent, ordered_by, actual_cause and end_date. STOP counts an
  ongoing shutdown again in each year (`count_year`), so rows with identical key values form one instance that
  lists all its count years. On the full sheet: 2,102 rows → **1,896 instances**, 69 multi-year, 3 multi-country.
  Ids can change if Access Now edits a row; this is a documented limitation.
* **Atlas `keepiton.shutdowns_cy`** (2,940 rows on the full sheet): exact counts of STOP rows per country ×
  count_year, `SHUTDOWN_INSTANCES` plus breakdowns `|shutdown_type=`, `|affected_network=`, `|shutdown_extent=`,
  `|ordered_by=`, `|actual_cause=` (label verbatim in flags). Only non-zero cells are stored; an absent cell means
  "none recorded", not a measured zero.
* **Atlas `keepiton.shutdowns`** (1,354 rows): `DURATION_DAYS|instance=<id>` where the duration is numeric, with
  period start_date .. end_date.
* **KG:** 1,896 `keepiton:shutdown:<id>` (kind `internet_shutdown`) plus 7 `keepiton:service:<column>` (kind
  `communication_service`), ≈ 51.8 k statements: P580/P582, P17 (1,871 to reviewed country QIDs; Palestine →
  `iso3:PSE` because it has no reviewed QID; Somaliland stays a coded text field), the coded columns
  (`keepiton:shutdown_type`, `…:ordered_by`, `…:actual_cause`, `…:gov_justification`, `…:shutdown_status`,
  `…:coded_field` with column and quoted definition), `keepiton:duration_days`, `keepiton:service_affected`
  (1,192 links) and source links.

### `atlas.cfr_cyber` (source key `cfr.cyber_operations`, `src/reva/adapters/connectors/atlas_cfr_cyber.py`)

* **Quoted from the archived tracker page:** scope, inclusion rule, completeness disclaimer, glossary definitions
  of the 7 operation types, and the attribution basis: "This data set identifies suspected threat actors and their
  state sponsors based on what the reporting suggests and whether the tools, techniques, and procedures used by the
  threat actor conform to what is known about a state sponsor's preferred methods of intrusion."
* **Sponsor wording.** Every sponsor is worded as **CFR's suspected sponsor**. Each KG edge carries the qualifiers
  `claim: "suspected state sponsor as attributed by CFR"` and `attribution: <the quote>`, and each atlas cell
  carries the same flag. REVA never states attribution as fact.
* **Atlas `cfr_cyber.sponsor_year`** (748 rows): `SUSPECTED_SPONSORED_OPERATIONS` per sponsor × year, plus
  `|type=` and `|victim_category=`. The 19 undated incidents are not counted, and multi-sponsor incidents (12)
  count for each sponsor (flagged).
* **KG:** 897 `cfr_cyber:operation:<id>` (kind `cyber_operation`; id = sha256(Title + Date)[:16]), 8,791 statements:
  P585, `cfr_cyber:suspected_state_sponsor` (877 edges; `iso3:PSE` for the 6 "Palestine, State of" ones), and
  type, victim category, victims, threat actor and sponsor as stated, description, government response and
  sources, all verbatim.
* **Not inferred.** Victim countries are not coded in the CSV (the `Victims` field is free text). Threat actors
  ("APT 28", "Believed to be the work of APT 28") stay text and are never matched by name. The tracker page shows
  2024 incidents that the CSV does not have yet; they are not scraped.

### Geography, events, families

* `registry/atlas/geo_aliases/v6digital.yaml`: schemes `keepiton_country` and `cfr_cyber_country`. Both providers
  publish names only.
  * Each alias is either an exact verbatim match to one crosswalk name field, or one of 9 listed conventional names
    with evidence (Turkey, North/South Korea, Laos, Vietnam, The Philippines, "Congo (Democratic Republic of the)",
    the two "Korea (… of)" forms).
  * Multi-country cells are split by `digital_common.split_countries`, which keeps known names such as
    "Palestine, State of" or "Tanzania, United Republic of" whole. No fuzzy matching.
* `registry/events/taxonomy.yaml` (small appends):
  * types `internet_shutdown` and `cyber_operation`;
  * role `suspected_sponsor`;
  * property role `cfr_cyber:suspected_state_sponsor` → participant/suspected_sponsor;
  * dataset_kinds for both prefixes.
* `registry/kg/predicates_digital.yaml`: 20 predicates.
* `registry/kg/relations_v4.d/v6digital.yaml`:
  * `keepiton:service_affected` → family **context** (subtype `shutdown_affected_service`);
  * P17 keeps its core `located_in`;
  * the sponsor predicate is an event role, not a family (same rule as the UCDP and IFPRI participant predicates).
  * No family is causal. STOP's `actual_cause` ("Protests", "Exam cheating") is Access Now's category of the
    circumstances, not an effect.

## 3. Example links (ids computed from the 2026-10-17 bytes)

* `keepiton:shutdown:5a3f413de70d7732`, a service-based, nationwide shutdown starting 2009-05-24:
  * → P17 → Q794 (Iran);
  * → `keepiton:service_affected` → `keepiton:service:facebook` and `keepiton:service:twitter`;
  * counted by STOP in each year 2016 … 2025 (one instance, 10 count years);
  * `keepiton:shutdown_extent` = "Service-based".
* `keepiton:shutdown:d25514e8ffc2e3b8`:
  * → P17 → Q794, P582 2026-01-08, `keepiton:duration_days` 11;
  * `keepiton:actual_cause` = "Protests", `keepiton:ordered_by` = "Executive government" (Access Now's coding).
* `keepiton:shutdown:c8fd4201562998d6` (2024-07-24): → P17 → Laos (LAO) and Myanmar (Q836); the cell was
  "Laos; Myanmar".
* `cfr_cyber:operation:754b78d03626d4b0` ("Targeting of Indian two-factor authentication tool Kavach", 2023-02-21,
  Espionage):
  * → `cfr_cyber:suspected_state_sponsor` → Q843 (Pakistan), with qualifier `claim` "suspected state sponsor as
    attributed by CFR";
  * threat actor as stated "APT 36".
* `cfr_cyber:operation:5af76b3b8fd43669` ("Targeting of Ukrainian and Polish military personnel", 2022-02-25):
  → suspected sponsors Q184 (Belarus) and Q159 (Russia), per CFR.

## 4. Tests

* `tests/unit/test_v6_digital.py` (7 tests): the splitter, the exact-name schemes, quoted definitions and gviz
  types, instance grouping (a 10-year instance and a same-year duplicate), multi-country counts, the
  Somaliland/Palestine handling, header drift refused, CFR attribution qualifiers and counts, and restricted rights.
* `tests/integration/test_v6digital_ingest.py` (3 tests, test DB): both fixture ingests register their tables and
  are idempotent. The KG loads 15 `internet_shutdown` and 40 `cyber_operation` events with 58 sponsor edges.
  Atlas flags carry the attribution wording.
* Fixtures `tests/fixtures/source/atlas.keepiton/` and `atlas.cfr_cyber/` (≈ 210 kB): subsets of the real
  2026-10-17 bytes (24 STOP rows and 40 CFR rows verbatim; the pages reduced to their visible text; the gviz
  header whole). Each manifest records the live sha256 and size.

## 5. Owner decisions

1. **Access Now STOP licence.** Ask Access Now for an explicit data licence (CC BY 4.0 would make it open). Until
   then the tables and events are restricted: name the source next to any answer and do not redistribute.
2. **CFR tracker.** Ask CFR for permission for public use. The current terms forbid any public or commercial use.
3. **ITU DataHub.** Its API rejects non-browser clients (403), and ITU's terms are non-commercial. The options are
   to ask ITU for API access and a redistribution grant (price baskets, more indicators), or to keep the WB WDI
   copies.
4. **PeeringDB.** Ask for approval under its AUP before archiving IXP and facility data, then a connector of 2
   requests (`/api/ix`, `/api/fac`, country-level counts).
5. **Cloudflare Radar.** It needs an account token and is CC BY-NC 4.0. Decide whether a non-commercial source with
   an account is acceptable.
6. **CFR victim countries.** These are only on the ~900 per-incident HTML pages; fetching them is a 900-request job
   under the same restrictive terms. It was not done.

## 6. Finish later (free disk ≥ 5 GB)

```bash
df -h /                                    # must show >= 5 GB free
uv run reva objects compress               # if below
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.keepiton
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.cfr_cyber
uv run pytest -q tests/unit/test_v6_digital.py tests/integration/test_v6digital_ingest.py
```

Expected results:

* keepiton: 2 tables (≈ 2,940 + 1,354 rows), ≈ 1.9 k entities and 52 k statements.
* cfr_cyber: 1 table (≈ 748 rows), 897 entities and 8.8 k statements.
* Raw ≈ 4.6 MB.

Connector versions `atlas.keepiton` 1.0.0 and `atlas.cfr_cyber` 1.0.0 are already registered (`reva seed`).
