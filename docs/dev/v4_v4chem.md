# V4CHEM — chemistry & physics of matter (report)

Agent V4CHEM, 2026-10-17. Scope: what things are made of, down to atoms — elements, isotopes, physical constants,
compounds and materials that the rest of REVA references, with sourced composition (`made_of`) and discovery
history. Everything below is what a source states; REVA-computed numbers are flagged `computed_by: reva`.

## Sources

| Source (source key) | URL fetched | Licence (per value) | Fetched (live, dev DB `reva`) |
|---|---|---|---|
| PubChem periodic table (`pubchem.pug`) | `https://pubchem.ncbi.nlm.nih.gov/rest/pug/periodictable/JSON` | NLM / US Government work | 1 request, 44 kB |
| PubChem element pages (`pubchem.pug`) | `.../rest/pug_view/data/element/<Z>/JSON`, Z = 1..118 | per contributor, recorded on every value: IUPAC CIAAW (bylaws), AMDC/IAEA (disclaimer), NIST PML, Jefferson Lab (citation notice), LANL, IUPAC IPTEI (CC BY-NC-ND 4.0, flagged `licence_noncommercial`; no IPTEI section is parsed) | 118 requests, 14.6 MB (with the table) |
| NIST CODATA (`nist.codata`) | `https://physics.nist.gov/cuu/Constants/Table/allascii.txt` | NIST SRD 121: cite "Source: NIST", possible US copyright (15 U.S.C. 290e) → `licence_review` | 1 request, 40.8 kB |
| ChEBI release 256 (`chebi.release`) | `https://ftp.ebi.ac.uk/pub/databases/chebi/ontology/chebi_lite.obo.gz` (7.9 MB), `.../flat_files/chemical_data.tsv.gz` (3.8 MB) | CC BY 4.0 per the ontology header; flat-files README also says CC BY-SA 4.0 (owner to confirm) | 3 requests incl. periodic table, 11.7 MB |
| PubChem compounds (`pubchem.pug`) | `.../rest/pug/substance/sourceid/ChEBI/<ids>/xrefs/RegistryID/JSON`, `.../cids/JSON`, `.../compound/cid/<cids>/property/.../JSON` | NLM; depositor terms may apply | 50 requests (list splitting), 0.1 MB |
| Wikidata (`wikidata.wdqs` / `wikidata.wbapi`, WDHARVEST's harvester) | specs in `registry/kg/queries/chemistry/wikidata/` | CC0, community-edited | not harvested yet (WDQS 429 cool-downs all session) |

Not used, owner decisions:

* **IAEA LiveChart API** (`www-nds.iaea.org/relnsd/v1/data`) answers a **Cloudflare browser challenge** (HTTP 403
  "Just a moment...") to non-browser clients. Not worked around. The same AMDC/NUBASE data (masses, half-lives,
  discovery years, decay modes) come through the PubChem element pages, attributed to "Atomic Mass Data Center
  (AMDC), International Atomic Energy Agency (IAEA)" with AMDC's licence URL. The AMDC mirror
  `amdc.impcas.ac.cn` reset the connection / 404'd for the NUBASE2020 file.
* **mindat.org** needs an account (not used). The IMA list on `rruff.info/ima` redirects to a page without a
  documented licence (not used). Minerals come from ChEBI (193 entities under `mineral`) and the Wikidata spec
  `science.chem_minerals` (mineral species with P274 formula once allow-listed).
* No openly downloadable **crude-oil assay** (paraffins / naphthenes / aromatics per cut) was found with clear
  terms (Equinor/BP/ExxonMobil assay workbooks are on corporate pages without an open licence); the roadmap gives
  crude assays to V4ENERGY. Licence review needed before anyone loads them.

## Connectors (all `atlas_*`, budget-metered through `run.get`, raw bytes archived)

| Connector (version) | Module | Atlas tables | KG |
|---|---|---|---|
| `atlas.chem_elements` 1.0.1 | `src/reva/adapters/connectors/atlas_chem_elements.py` | `pubchem.elements`, `pubchem.nuclides` | `element:<Z>` (kind `chemical_element`), `nuclide:<Z>-<A>[m]` (kind `isotope`) |
| `atlas.chem_codata` 1.0.1 | `atlas_chem_codata.py` | `codata.constants` | `codata:<slug>` (kind `physical_constant`) |
| `atlas.chem_chebi` 1.0.1 | `atlas_chem_chebi.py` | `chebi.composition` | `chebi:<n>` (kinds `compound`, `chemical_class`, `chemical_role`, `mineral`, `chemical_element`, `isotope`) |
| `atlas.chem_pubchem` 1.0.1 | `atlas_chem_pubchem.py` | `pubchem.compounds` | `pubchem:cid:<n>` (kind `compound`) |

Shared helpers `atlas_chem_common.py` (formula parser, exact decimals, KG loader with retraction). Predicates:
`registry/kg/predicates_chem.yaml` (61 dataset predicates, `pubchem:*`, `chebi:*`, `codata:*`, each with its family).
Seeds: `registry/chem/seeds.yaml` (156 ChEBI ids — hydrocarbons and petroleum-fraction reference compounds,
greenhouse gases, fertilisers, minerals/ores, cement/lime, plastics/monomers, metals, drugs for V4HEALTH — plus
role members of greenhouse gas / fossil fuel / fuel / fertilizer and the is_a children of alkane, cycloalkane and
every descendant of mineral). Names in the seed file are only *checked* (a mismatch is reported), never used to map.

Honesty choices worth knowing:

* Atlas rows of these tables are not about a country: `iso3` null, `provider_geo` = `not_geographic` (intrinsic
  properties) or the provider's scope `earth_crust` / `earth_oceans` (abundances). Period = retrieval year (values
  "as published at retrieval", flag `period_semantics`), CODATA = adjustment year (2022).
* Several contributors per value are kept side by side (`atomic_weight|Z=26|provider=ciaaw|jlab|lanl|nist_pml`),
  never averaged or picked. Intervals (`[1.00784, 1.00811]`) stay intervals: no midpoint is invented
  (`pubchem:atomic_weight_interval`, `pubchem:isotopic_abundance_interval`, atlas `value_text`).
* Half-lives are kept verbatim (`5.70 ky ± 0.03`); `pubchem:half_life_seconds` (SI-prefixed seconds, minutes,
  hours, days) and `pubchem:half_life_years` (multiples of NUBASE's year, *not* converted to seconds because the
  year convention is NUBASE's) are unit scalings computed by REVA.
* Abundances: the exponent is read from PubChem's superscript markup (`5.63×10<sup>4</sup>` → 56300 mg/kg).
* Discovery: `pubchem:year_discovered` (periodic table, "Ancient" kept as text) and `pubchem:history_text`
  (verbatim paragraphs from CIAAW, Jefferson Lab and LANL, 409 statements). REVA does **not** extract discoverer
  names from text; structured discoverer / place / date come from Wikidata P61 / P189 / P575
  (`science.chem_element_discovery`).
* `made_of` from formulas: atom counts are the formula's; element symbols resolve to `element:<Z>` through the
  periodic table of the same run (symbols are IUPAC identifiers); `mass_share` = count × atomic mass / formula total,
  6 significant digits, `computed_by: reva` + the periodic table's sha256. Generic formulas (`CH3R`, `(C2H4)n`,
  `[14C]`) are not interpreted (reported as unparsed); such ChEBI entities are kind `chemical_class`.
* KG quantities are plain decimals of ≤ 40+40 digits; 22 CODATA values outside that range (e.g. Planck constant)
  are stored exactly as `{"type": "quantity_scientific", "amount_scientific": "6.62607015e-34"}` (never rounded).
* ChEBI ↔ PubChem: PubChem's own link (ChEBI deposit SID → standardised CID), `pubchem:chebi_deposit`; a ChEBI id
  PubChem refuses (HTTP 400 for the whole list) is isolated by list splitting and reported `not_in_pubchem`.
  Element nodes, ChEBI atoms and PubChem CIDs are never merged.

## Loaded (live, dev DB `reva`, origin `live`)

| What | Entities | Statements | Atlas rows |
|---|---|---|---|
| Elements (PubChem periodic table + 118 pages) | 118 `chemical_element` | 45,101 (`pubchem.pug`) incl. elements + nuclides | `pubchem.elements` 1,555 |
| Nuclides (AMDC via PubChem; CIAAW/NIST abundances) | 5,637 `isotope` | (in the 45,101): 5,637 half-life texts, 4,546 in seconds, 146 in years, 5,317 discovery years, 8,892 decay modes, 6,261 masses, 541 abundances (+36 intervals) | `pubchem.nuclides` 17,168 |
| CODATA 2022 | 355 `physical_constant` | 355 (`nist.codata`) | `codata.constants` 355 |
| ChEBI subset (release 256, seeds file) | 1,356 (`compound`, `chemical_class`, `chemical_role`, `mineral`, `chemical_element`, `isotope`) | 6,281 (`chebi.release`): is_a 2,011, made_of 964, definitions 937, has_role 915, formula/mass/charge ~445 each, has_part 72 | `chebi.composition` 1,408 |
| PubChem compounds for the ChEBI seeds | 110 `compound` | 1,032 (`pubchem.pug`), incl. made_of from formulas and 114 ChEBI→CID links | `pubchem.compounds` 479 |

Element statements by predicate: atomic weight per contributor 378 (+26 intervals), crustal abundance 88,
oceanic abundance 80, history texts 409, year discovered 118, periodic-table properties (symbol, configuration,
state, block, mass, MP/BP, density, radius, IE, EA, electronegativity, oxidation states) 93–118 each.

ChEBI subset composition (release 256): 1,356 entities (seeds 156,
role members 53, mineral/alkane/cycloalkane members 341, relation targets 227, + is_a ancestors), 6,281
statements (is_a 2,011, made_of 964, definitions 937, has_role 915, formula/mass/charge ~445 each, has_part 72),
391 formulas parsed, 57 not interpreted (51 generic, 6 unknown symbols).

## Relation families (`registry/kg/relations_v4.d/v4chem.yaml`, `reva.kg.families check` passes)

`chebi:is_a` → is_a; `chebi:has_part` → has_part; `chebi:has_role` → is_a (subtype has_role, role classification);
`chebi:has_functional_parent|has_parent_hydride|is_conjugate_acid_of|is_conjugate_base_of|is_enantiomer_of|
is_tautomer_of|is_substituent_group_from` → context; `chebi:formula_element`, `pubchem:formula_element` → made_of
(share = qualifier `mass_share`, scale fraction); `pubchem:isotope_of` → is_a; `pubchem:chebi_deposit` → context
(identifier crosswalk); Wikidata P189 location of discovery, P817 decay mode → context. P31/P279/P361/P527/P186/P61
are mapped by V4CORE. Literal predicates (masses, formulas, half-lives, abundances, constants, texts) are values,
not edges, and are deliberately unmapped.

## Examples (ids)

* **What is methane made of** — `chebi:16183` (methane) `chebi:formula_element` → `element:6` (atom_count 1,
  mass_share 0.748675, computed) and → `element:1` (4, 0.251325); `chebi:16183` `chebi:is_a` → `chebi:18310`
  (alkane) `chebi:is_a` → `chebi:24632` (hydrocarbon, ChEBI definition verbatim: "A compound consisting of carbon
  and hydrogen only."); `chebi:16183` `chebi:has_role` → `chebi:76413` (greenhouse gas) and → `chebi:35230`
  (fossil fuel); `pubchem:cid:297` `pubchem:chebi_deposit` → `chebi:16183`, `pubchem:cid:297`
  `pubchem:formula_element` → `element:6`, `element:1`.
* **Iron ore** — `chebi:50818` (hematite, kind mineral) `chebi:is_a` → `chebi:50819` (ferric oxide);
  `chebi:50818` made_of `element:26` (2 atoms, mass share 0.699412) and `element:8` (3, 0.300588);
  `element:26` `pubchem:crustal_abundance` 56300 mg/kg (Jefferson Lab), `pubchem:year_discovered` "Ancient".
* **Uranium-235** — `nuclide:92-235` `pubchem:isotope_of` → `element:92`; half-life "704 My ± 1" (AMDC) =
  704,000,000 y (scaled by REVA); natural abundance 0.007204 (CIAAW, NIST); discovered 1935 (AMDC); decay modes
  "α=100%", "SF=7e-9±0.2%", ... verbatim.
* **Carbon-14** — `nuclide:6-14` half-life "5.70 ky ± 0.03" → 5700 y, decay "β-=100%", discovered 1936.
* **Avogadro constant** — `codata:avogadro_constant` `codata:value` 602214076000000000000000 mol^-1 (exact,
  CODATA 2022).
* **What is petroleum made of** — path, once the Wikidata specs are harvested: petroleum `Q22656` → P527/P186
  (as Wikidata states, `science.chem_petroleum_composition`) → Wikidata hydrocarbon classes → P683 (ChEBI ID,
  `science.chem_compound_identifiers`, which lists the ChEBI class ids too) → `chebi:24632` hydrocarbon /
  `chebi:18310` alkane / `chebi:23453` cycloalkane / `chebi:33658` arene → `chebi:is_a` members (110 alkanes,
  39 cycloalkanes loaded) → `chebi:formula_element` → `element:6` carbon, `element:1` hydrogen (with shares).
  Today the ChEBI → element half of the chain is loaded in the dev DB; the petroleum → class edge waits for the Wikidata harvest
  (no other open source stating petroleum's composition was found; see owner decisions).

## Wikidata specs (`registry/kg/queries/chemistry/wikidata/`, domain `science`; all specs validate)

The harvester checks domain == directory name only one level below the queries root, so the specs live one level
deeper, in `chemistry/wikidata/` with domain `science` (same pattern as `nature/*` with domain `geography`); the
README owner table lists `chemistry/**` -> V4CHEM. (`science/` is KGSCIECO's, whose tests own every spec there.)

| Spec | What |
|---|---|
| `science.chem_petroleum_composition` | P527 / P186 of petroleum (Q22656) and its subclasses |
| `science.chem_element_discovery` | P61 discoverer, P575 time, P189 place, P1086 Z, P246 symbol of every element |
| `science.chem_isotopes` | isotope items (instance of a subclass of Q25276) |
| `science.chem_minerals` | mineral species (Q12089225), split by QID prefix |
| `science.chem_materials_made_from` | P186 / P527 of alloys, steel, plastics, cement, concrete, glass, fertilizer, fuel |
| `science.chem_compound_identifiers` | P683 ChEBI ID of the seed and class ids (QID ↔ chebi bridge) |

Property requests (`registry/kg/property_requests/v4chem.yaml`): P1086, P246, P189, P274, P2067, P2114, P817,
P2374, P1148, P683, P662, P231, P579 (P186 was requested by another agent and is now allow-listed by V4CORE).

## Live runs and what is missing

* Done live: `atlas.chem_codata`, `atlas.chem_elements` (all 118 pages, no missing page, 0 KG rejections).
  Note: the element run was started while `df` showed 4.7 GB free (the gate was read but not enforced by the
  command); it added 14.6 MB of raw bytes. Later runs are gated in the command itself.
* ChEBI subset and PubChem compounds: run live once free disk was back above 5 GB (6.7 GB; the gate is in the
  command): 0 KG rejections. PubChem refused 4 seed ids as unknown ChEBI sources (`not_in_pubchem`: CHEBI:17234
  glucose, CHEBI:18723 nicotine, CHEBI:30187 aluminium oxide, CHEBI:35150 calcium hydroxide — typed missing, likely
  deposited under other ChEBI ids; not guessed) and 3 deposits have no standardised CID (CHEBI:6078 ivermectin, a
  mixture; CHEBI:28984 aluminium atom; CHEBI:33101 nitrogen dioxide).
* Shared-worktree note: the move of the specs to `chemistry/wikidata/` was swept into V4INDUSTRY's commit b41de6c
  (shared git index), and my commit fcba77e carried another agent's staged `nature/ecology -> geography/ecology`
  renames; contents are correct.
* Wikidata: WDQS returned 429 with 15-minute cool-downs for the whole session (all agents share the egress);
  `harvest try` of the petroleum spec waited in the cool-down queue.
* Abundance in the **universe** (and Sun / meteorites): not on the PubChem pages; no open source found (CRC
  Handbook is not open). Missing, not zero.
* Element **discoverer names** as entities: only verbatim texts until Wikidata P61 is harvested.
* Drugs: only the 20 V4HEALTH-relevant seeds; insulin (protein) has no PubChem compound lookup (`pubchem: false`).

## Owner decisions

1. IAEA LiveChart (Cloudflare challenge): ask IAEA NDS for API access/allow-listing, or accept AMDC data via PubChem.
2. Licence reviews: NIST SRD (CODATA), AMDC/IAEA disclaimer, CIAAW bylaws, Jefferson Lab notice (all flagged
   `licence_review`), ChEBI CC BY vs CC BY-SA wording, IUPAC IPTEI CC BY-NC-ND (flagged, not parsed).
3. Crude-oil assays (composition of petroleum by fraction): choose a provider with an open licence (V4ENERGY).
4. mindat.org account (mineral localities, formulas) if wanted.

## Commands to finish (bigger machine / later)

```bash
df -h /                                                     # >= 5 GB free first
# all four ran live on 2026-10-17; refresh monthly (ChEBI releases monthly):
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.chem_chebi      # ~12 MB, 3 requests
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.chem_pubchem    # ~50 requests, < 1 MB
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.chem_elements   # 119 requests, ~15 MB
uv run python -m reva.kg.harvest merge-property-requests                  # WDHARVEST: P1086 P246 P189 P274 ...
uv run python -m reva.kg.harvest run registry/kg/queries/chemistry --via api
uv run pytest tests/unit/test_atlas_chem.py tests/integration/test_atlas_chem_ingest.py
```

Tests: `tests/unit/test_atlas_chem.py` (25 offline parser tests on real captured fixtures),
`tests/integration/test_atlas_chem_ingest.py` (all four connectors in fixture mode on a throwaway database: the
methane → alkane → hydrocarbon chain, made_of counts, the ChEBI → CID link, C-14 half-life, kinds, idempotent
re-run). Fixtures in `tests/fixtures/source/atlas.chem_*` (real responses; element pages and ChEBI files are
verbatim excerpts, each manifest entry gives the sha256 of the full response).
