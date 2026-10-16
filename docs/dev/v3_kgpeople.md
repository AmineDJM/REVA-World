# V3 — KGPEOPLE: the people who connect history (Wikidata query specs)

Owner: agent KGPEOPLE. Files: `registry/kg/queries/people/*.yaml` (14 specs), `registry/kg/queries/people/_recorded/`
(recorded WDQS probe responses), `registry/kg/property_requests/kgpeople.yaml`,
`tests/unit/test_kg_queries_people.py`. Executor and spec format: WDHARVEST (`registry/kg/queries/README.md`,
`_schema.json`, `reva.kg.harvest`). Nothing here writes to a database; the specs are run by WDHARVEST.

## 1. What the specs select

All person seeds are `purpose: seed_entities`, `entity_kind: person`: every selected QID gets a full
`wbgetentities` snapshot whose claims are restricted (`properties:`) to the biography subset of the allow-list:

> P31, P21, **P27 citizenship** (with start/end), P19/P20 birth/death place, **P569/P570** birth/death date,
> P106 occupation, P101 field, **P39 position held** (start/end, replaces/replaced by, series ordinal),
> P102 party, P140 religion, P172 ethnic group, P1412 languages, **P1344 participant in**, P607 conflict,
> P793 significant event, **P800 notable work**, **P737 influenced by**, **P108 employer**, **P463 member of**,
> P166 award, P69 educated at, P135 movement, family (P22, P25, P26, P40, P3373, P53, P97), P551 residence,
> P937 work location, P1066 student of, P802 student, P184 doctoral advisor.

`expand_hops: 1` everywhere: positions, places, parties, works, events and states referenced by those claims
get label snapshots, so the graph can name every hop. Deprecated ranks are excluded in SPARQL (and again by
the harvester); preferred and normal ranks are both kept, so *historical* leaders are not lost behind the
"truthy" current one.

| Spec | Purpose | Selection | Notability bound | `max_rows` |
|---|---|---|---|---|
| `people.country_heads` | statements (subject = state) | P35 head of state / P6 head of government of current & historical states, empires, colonies, dependencies (classes country Q6256, sovereign state Q3624078, historical country Q3024240, state Q7275, empire Q48349, kingdom Q417175, constituent country Q1763527, colony Q133156, dependent territory Q161243, protectorate Q164142), all non-deprecated ranks, start/end/point in time, replaces/replaced by, ordinal | none | 15 000 |
| `people.state_leaders` | seed | humans who are P35/P6 objects of those states, or hold (P39) the office a state declares as head-of-state (P1906) / head-of-government (P1313) office | none | 30 000 |
| `people.monarchs` | seed | holders (P39) of any position whose P31/P279 chain reaches monarch (Q116): kings, queens regnant, emperors, sultans, tsars, pharaohs, "King of Portugal", "Emperor of Brazil" …; plus occupation monarch | none | 25 000 |
| `people.colonial_officials` | seed | holders of the P1906/P1313 office of a colony / dependent territory / protectorate (or subclass), or of a position whose jurisdiction (P1001) is such a territory, or of any position whose P31/P279 chain reaches viceroy (Q244741) or governor-general (Q382844) | none | 25 000 |
| `people.explorers_navigators` | seed | occupation explorer (Q11900058), navigator (Q254651) or conquistador (Q126236), or participant (P1344 / the expedition's P710) in an expedition (Q2401485 or subclass) | ≥ 3 sitelinks | 25 000 |
| `people.military_leaders` | seed | occupation military leader / officer / personnel ≥ 30 sitelinks; or any human with a conflict (P607) ≥ 40 sitelinks | 30 / 40 | 15 000 |
| `people.founders` | seed | object of "founded by" (P112) of any item (states, organisations, parties, companies, religions, cities) | ≥ 15 | 25 000 |
| `people.political_figures` | seed | occupation politician / statesperson / diplomat (split: one occupation per request) | ≥ 40 | 25 000 |
| `people.revolutionaries_activists` | seed | occupation revolutionary / activist / trade unionist (one per request) | ≥ 12 | 20 000 |
| `people.nobel_laureates` | seed | P166 Nobel Prize in Physics, Chemistry, Physiology or Medicine, Literature, Peace; Riksbank Prize in Economics | none | 2 000 |
| `people.scientists_scholars` | seed | 18 scientific/scholarly occupations (two per request) | ≥ 25 | 35 000 |
| `people.writers_artists` | seed | writer, poet, novelist, playwright, essayist, painter, sculptor, composer, architect, film director (one per request) | ≥ 40 | 35 000 |
| `people.religious_figures` | seed | founders (P112) of religions (Q9174 or subclass), every pope (P39 Q19546) and caliph (Q65997), occupation religious leader / theologian / prophet / missionary | ≥ 20 for occupations | 15 000 |
| `people.leader_positions` | seed, `entity_kind: position` | the offices themselves (P1906/P1313 offices of the state classes above + every monarch position), kept with P1001 jurisdiction, P17, P361, inception/dissolution, replaces/replaced by | none | 5 000 |

**People budget.** The person seeds' `max_rows` sum to **277 000** (asserted ≤ 300 000 by the tests). This is an
upper bound on distinct people: the sets overlap heavily (a monarch is often also a state leader, founder and
military leader) and the harvester fetches each QID once. `max_rows` is a *bound*, not a measurement: the
harvester appends `LIMIT max_rows+1` and fails the spec with `row_bound_exceeded` rather than truncating, so an
under-estimate shows up loudly. See §4 for what is measured and what is not.

**Measured sizes** (one recorded WDQS COUNT request, 2026-10-16, `_recorded/counts_1.*`): state_leaders
**14 068** people, monarchs **13 642**, colonial_officials **18 557**, nobel_laureates **1 000**,
leader_positions **1 391** offices, country_heads **3 269** distinct P35/P6 statements (rows can repeat a
statement when it has several qualifier values). The bounds above leave 35–120 % headroom. The thresholded
occupation specs (explorers, military, founders, political, revolutionaries, scientists, writers/artists,
religious) were **not** counted: the shared WDQS daily budget (1 500 requests) was exhausted by then; their
bounds are estimates, enforced by the harvester (fail, never truncate). Expected distinct people overall:
roughly 100k–180k (estimate, not measured).

**Why sitelinks.** The number of Wikimedia sitelinks (Wikipedia articles in other languages, Wikiquote, …) is a
crude, transparent proxy for how widely a person is documented across languages — not a judgement of
importance, and biased towards people documented in large-Wikipedia languages. Rulers, heads of state and
government, colonial officials, monarchs and Nobel laureates have **no** threshold: completeness over time
matters more there than notability. Thresholds are only used for open-ended occupational categories.

## 2. Splitting (≤ 60 s per request)

* Occupation categories use `split: {by: values, source: list}` over their occupation QIDs (1–2 per request) so a
  single request never scans more than one large occupation (politician ≈ the largest).
* State/position/monarch/colonial specs are single requests: they start from a small set of classes or
  positions. Birth-century splitting was considered and rejected for these: a `YEAR(?birth)` filter scans every
  P569 triple per window and silently drops people without a birth date (many medieval and ancient rulers).
* Total WDQS requests for a full people harvest: 3 + 3 + 9 + 10 occupation chunks + 10 single requests = **35 WDQS
  requests**; entity snapshots: about `distinct people / 50` `wbgetentities` calls.

## 3. Anchor people (tests)

`tests/unit/test_kg_queries_people.py` (offline) checks schema/allow-list validity, bounds and the people budget,
that person seeds keep the biography properties, and — from recorded WDQS responses in `_recorded/` — that the 18
anchor people are captured by the seed sets the owner expects:

| Anchor | Expected spec(s) |
|---|---|
| Christopher Columbus, Pedro Álvares Cabral, Vasco da Gama | explorers_navigators |
| Isabella I of Castile, John II of Portugal | monarchs |
| Pedro I of Brazil, Napoleon | monarchs or state_leaders |
| Eisenhower, Kennedy, de Gaulle, Nkrumah, Lumumba, Ho Chi Minh, Nasser, Nehru | state_leaders |
| Simón Bolívar | state_leaders or revolutionaries_activists |
| Marie Curie, Albert Einstein | nobel_laureates |

The recordings are produced through WDHARVEST's locked, budget-metered client (`reva.kg.harvest.budgeted_client`):

* `resolve_*` — English label → human QID for each anchor (the QIDs in the test are those confirmed here, not
  typed from memory; Marie Curie is the one exception, see §4), English labels of every class/occupation/award QID written in a spec, and label lookups of
  candidate classes for later revisions;
* `p2_members_*` (kind `membership`) — each spec's own query (whole split range, whitespace collapsed) with its selected variable
  restricted to the anchor QIDs, several specs per UNION request (GET URL ≤ 7 500 characters). The test rebuilds
  the query from the current specs and requires byte equality, so a spec edit without re-recording fails. `counts_1` (kind `count`) holds the size measurement.
  The recorder was a throw-away script calling the test module's builders (`resolve_query`, `membership_query`,
  `pack_membership`) through `budgeted_client`; the provider body is copied out of the archive (`var/kg/objects`).

## 4. Status, measurements and gaps (honest)

* **Specs**: 14, all valid against WDHARVEST's schema and allow-list (`python -m reva.kg.harvest validate
  registry/kg/queries/people`). Not yet run in full (WDHARVEST runs them).
* **Anchors**: all 18 are captured, each by the expected spec (recorded `p2_members_1/2`, 2026-10-16). They are
  usually selected by several specs, e.g. Columbus: explorers, colonial_officials, founders,
  scientists_scholars; de Gaulle: state_leaders, country_heads (France), monarchs (Co-Prince of
  Andorra is a monarch office), military_leaders, colonial_officials, political_figures, founders.
  country_heads rows link de Gaulle–France, Eisenhower/Kennedy–United States, Nehru–India (and the Dominion of
  India), Nasser, Nkrumah, Bolívar, Napoleon and Isabella I to their states.
* **QID provenance**: 17 anchor QIDs and every class/occupation/award QID used in a spec come from recorded
  WDQS lookups (`resolve_1`: English label -> QID for anchors and candidate classes; QID -> English label for
  classes). Five anchor QIDs I first wrote from memory were wrong and were corrected from the lookup
  (Isabella I Q45859, John II Q217637, Pedro I Q939, Lumumba Q161672, Nasser Q39524); Q15634554 (meant as
  "state with limited recognition") returned no English label and was dropped. **Marie Curie (Q7186)** could
  not be confirmed by a WDQS label lookup (`resolve_2`, `resolve_3` returned no row for three label variants and
  for the QID -> label check; not investigated further because the WDQS budget ran out). The test therefore
  confirms her QID from an independent recorded source, A-SCI's real Nobel Prize API response
  (`tests/fixtures/source/atlas.nobel/`, laureate with English Wikipedia slug `Marie_Curie` -> Q7186), and
  the membership probe shows Q7186 is a human Nobel laureate and scientist. This exception is listed in the test.
* **Wikidata access conditions**: WDQS answered almost every request with HTTP 429 `Retry-After: 1000` (shared
  egress IP and User-Agent across agents). The client first capped Retry-After at 900 s, which let the next
  request out early and renewed the ban; reported, and WDHARVEST fixed it (cap 3600 s + margin). Each probe still
  took 17–35 min of waiting. KGPEOPLE obtained 6 WDQS responses (recorded) plus one WDQS timeout (an empty VALUES
  branch in the probe, since fixed in the probe builder); 429-rejected attempts not counted.
* **Known Wikidata artefacts** (selection is what Wikidata says, not a judgement): class chains pull in
  surprises, e.g. Nkrumah is selected by religious_figures (a class-chain match, not investigated) and
  Eisenhower, Kennedy, Nehru, Nkrumah and Pedro I by writers_artists (they carry a writer-type occupation). Seeds overlap; that is
  expected and harmless (one snapshot per QID).
* **Not done / gaps**: no catch-all "most documented humans" seed (a sitelinks filter over all ~11 M humans does
  not fit in 60 s); no counts for the thresholded specs; birth-century splitting not used (see §2); P945
  allegiance and P241 military branch are not in the person `properties:` lists until WDHARVEST merges them.

## 5. Requests to other agents

* **WDHARVEST**: merge `registry/kg/property_requests/kgpeople.yaml` (P241 military branch; qualifier P1686
  "for work" for award statements). P945 allegiance (already requested by KGHIST) is useful for people too; once
  merged, add P945/P241 to the person `properties:` lists (KGPEOPLE).
* **WDHARVEST / ATLASCORE (budget)**: `wikidata.wbapi` is 4 000 calls/day for all KG domains. A full people
  harvest needs about `distinct people / 50` calls (≈ 2 000–4 000 for 100k–200k people), i.e. one to two days of
  the whole budget if run alone; schedule it across days or raise the quota.
* **KGCORE**: the person snapshots carry P39 with start/end and P1365/P1366 qualifiers; a "who held office X on
  date D" query over `kg.statement` needs an index on (property, object) with the temporal qualifiers.
