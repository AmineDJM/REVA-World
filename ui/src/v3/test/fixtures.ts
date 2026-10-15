/**
 * V3 API fixtures shaped like the real routes (src/reva/api/routes/{kg,atlas,links,ask}.py).
 * Used by vitest and by the Playwright specs (ui/e2e/v3*.spec.ts) through route interception.
 *
 * TEST DATA ONLY: every record is labelled origin "test_fixture"; statement GUIDs and revision ids
 * are synthetic ("FIXTURE"); entities whose Wikidata id is not certain use provider-scoped keys
 * ("fixture:…"), which the UI never links to Wikidata. Values are illustrative, not sourced.
 */

const RETRIEVED = '2026-10-10T12:00:00Z';

export function envelope<T>(data: T, extra: Record<string, unknown> = {}) {
  return {
    data,
    context_id: null,
    context_mode: 'evidence',
    origin: 'test_fixture',
    modality: 'reported_observation',
    knowledge_cutoff: null,
    warnings: [{ code: 'community_edited', message: 'Wikidata statements are community-edited and are not verified by REVA.' }],
    next_cursor: null,
    ...extra,
  };
}

let row = 1000;
type T = { time: string; precision: number } | null;

export function stmt(
  subject: [string, string],
  pid: string,
  labelFr: string,
  object: [string, string] | null,
  opts: { start?: T; end?: T; pit?: T; value?: Record<string, unknown>; causal?: boolean; labelEn?: string; source?: string } = {},
) {
  row += 1;
  const year = (t: T) => (t ? t.time.slice(0, t.time.startsWith('-') ? 5 : 4) : null);
  const vf = opts.start ? `${year(opts.start)}-01-01` : opts.pit ? `${year(opts.pit)}-01-01` : null;
  const vt = opts.end ? `${year(opts.end)}-12-31` : opts.pit ? `${year(opts.pit)}-12-31` : null;
  const source = opts.source ?? 'wikidata';
  return {
    row_id: row,
    subject: { qid: subject[0], label: subject[1] },
    property: { pid, label_en: opts.labelEn ?? labelFr, label_fr: labelFr, reva_predicate: labelFr.replace(/\W+/g, '_'), causal_assertion: !!opts.causal },
    object: object ? { qid: object[0], label: object[1] } : null,
    value: opts.value ?? null,
    start: opts.start ?? null,
    end: opts.end ?? null,
    point_in_time: opts.pit ?? null,
    time_calendars: null,
    valid_from: vf,
    valid_to: vt,
    qualifiers: {},
    provenance: {
      source_key: source,
      statement_id: `${subject[0].startsWith('Q') ? subject[0] : 'Q1'}$FIXTURE-${row}`,
      statement_key: `${source}:${subject[0]}:${pid}:${row}`,
      source_revision: '2250000000',
      raw_sha256: 'a'.repeat(64),
      retrieved_at: RETRIEVED,
      recorded_at: RETRIEVED,
      load_id: 1,
      origin: 'test_fixture',
      rank: 'normal',
      references: row % 2 ? [{ P854: 'https://example.org/ref' }] : [],
      verification: source.startsWith('wikidata') ? 'community_edited_not_verified' : 'source_reported_not_verified',
    },
  };
}

const y = (yr: number, precision = 9): T => ({ time: `${yr < 0 ? '-' : ''}${String(Math.abs(yr)).padStart(4, '0')}-01-01T00:00:00Z`, precision });
const d = (iso: string): T => ({ time: `${iso}T00:00:00Z`, precision: 11 });

export const BRA: [string, string] = ['Q155', 'Brazil'];
export const POR: [string, string] = ['Q5146', 'Portuguese'];
export const PRT: [string, string] = ['Q45', 'Portugal'];
export const COL: [string, string] = ['Q7322', 'Christopher Columbus'];
export const UN: [string, string] = ['Q1065', 'United Nations'];
export const TORD: [string, string] = ['fixture:tordesillas', 'Treaty of Tordesillas'];
export const INDEP: [string, string] = ['fixture:independance_bresil', 'Independence of Brazil'];

export function entity(qid: string, kind: string | null, fr: string, en: string, desc = '') {
  return {
    qid,
    snapshot: 1,
    kind,
    labels: { fr, en },
    descriptions: desc ? { fr: desc } : {},
    aliases: {},
    sitelinks: 120,
    provenance: {
      source_key: 'wikidata',
      source_revision: '2250000000',
      content_sha256: 'b'.repeat(64),
      retrieved_at: RETRIEVED,
      recorded_at: RETRIEVED,
      load_id: 1,
      raw_sha256s: ['c'.repeat(64)],
      origin: 'test_fixture',
      verification: 'community_edited_not_verified',
    },
    statements_out: 42,
    statements_in: 17,
    degree_capped_at: 100000,
  };
}

export const ENTITIES: Record<string, ReturnType<typeof entity>> = {
  Q155: entity('Q155', 'country', 'Brésil', 'Brazil', 'pays d’Amérique du Sud'),
  Q5146: entity('Q5146', 'language', 'portugais', 'Portuguese', 'langue romane'),
  Q45: entity('Q45', 'country', 'Portugal', 'Portugal', 'pays d’Europe du Sud'),
  Q7322: entity('Q7322', 'person', 'Christophe Colomb', 'Christopher Columbus', 'navigateur'),
  Q1065: entity('Q1065', 'organisation', 'Organisation des Nations unies', 'United Nations'),
  'fixture:tordesillas': entity('fixture:tordesillas', 'treaty', 'traité de Tordesillas', 'Treaty of Tordesillas'),
  'fixture:independance_bresil': entity('fixture:independance_bresil', 'event', 'indépendance du Brésil', 'Independence of Brazil'),
};

export const NEIGHBOURS_Q155 = envelope({
  qid: 'Q155',
  label: 'Brazil',
  edges: [
    { ...stmt(BRA, 'P37', 'langue officielle', POR), direction: 'out' },
    { ...stmt(BRA, 'P463', 'membre de', UN, { start: d('1945-10-24') }), direction: 'out' },
    { ...stmt(PRT, 'P530', 'relations diplomatiques', BRA), direction: 'in' },
    { ...stmt(INDEP, 'P17', 'pays', BRA, { pit: d('1822-09-07') }), direction: 'in' },
  ],
});

export const NEIGHBOURS_Q5146 = envelope({
  qid: 'Q5146',
  label: 'Portuguese',
  edges: [{ ...stmt(PRT, 'P37', 'langue officielle', POR), direction: 'in' }],
});

export const TIMELINE_Q155 = envelope({
  qid: 'Q155',
  label: 'Brazil',
  truncated: false,
  events: [
    { ...stmt(BRA, 'P571', 'date de fondation', null, { value: { type: 'time', time: '1822-09-07T00:00:00Z', precision: 11 } }), sort_date: '1822-09-07', dated_by: 'time_value', direction: 'out' },
    { ...stmt(INDEP, 'P17', 'pays', BRA, { pit: d('1822-09-07') }), sort_date: '1822-09-07', dated_by: 'validity_qualifiers', direction: 'in' },
    { ...stmt(BRA, 'P463', 'membre de', UN, { start: d('1945-10-24') }), sort_date: '1945-10-24', dated_by: 'validity_qualifiers', direction: 'out' },
    { ...stmt(BRA, 'P35', 'chef de l’État', ['fixture:president', 'Président (fixture)'], { start: y(2023) }), sort_date: '2023-01-01', dated_by: 'validity_qualifiers', direction: 'out' },
    { ...stmt(BRA, 'P1365', 'remplace', ['fixture:colonia', 'Brésil colonial'], { end: y(1815, 9), start: y(1500, 7) }), sort_date: '1450-01-01', dated_by: 'validity_qualifiers', direction: 'out' },
  ],
});

export const SEARCH_BRESIL = envelope(
  {
    query: 'Brés',
    mode: 'prefix',
    results: [
      { qid: 'Q155', kind: 'country', sitelinks: 300, origin: 'test_fixture', match: 'prefix', matched: [{ lang: 'fr', label: 'Brésil', alias: false }], labels: { fr: 'Brésil', en: 'Brazil' } },
      { qid: 'fixture:independance_bresil', kind: 'event', sitelinks: 40, origin: 'test_fixture', match: 'substring', matched: [], labels: { fr: 'indépendance du Brésil' } },
    ],
  },
  { context_mode: 'registry', modality: 'metadata', warnings: [] },
);

function ev(subject: string, pid: string, sid: number) {
  return { kind: 'kg_statement', source: 'wikidata', statement_id: `${subject.startsWith('Q') ? subject : 'Q1'}$FIXTURE-L${sid}`, subject, pid, source_revision: '2250000001', raw_sha256: 'd'.repeat(64), retrieved_at: RETRIEVED, origin: 'test_fixture', cite: `wikidata:${subject}$FIXTURE-L${sid} rev 2250000001 [fixture]` };
}

export const LINKS_PATH_COL_BRA = envelope(
  {
    query: { from: 'Q7322', to: 'Q155', k: 3, max_hops: 4, as_of: null },
    found: true,
    reason: null,
    truncated: false,
    explored_nodes: 412,
    labels: {
      Q7322: { fr: 'Christophe Colomb', en: 'Christopher Columbus' },
      'fixture:tordesillas': { fr: 'traité de Tordesillas', en: 'Treaty of Tordesillas' },
      Q45: { fr: 'Portugal', en: 'Portugal' },
      Q155: { fr: 'Brésil', en: 'Brazil' },
      'fixture:colonia': { fr: 'Brésil colonial' },
      'fixture:voyage1492': { fr: 'premier voyage de Colomb' },
      'fixture:colonisation': { fr: 'colonisation européenne des Amériques' },
    },
    disclaimer: { fr: 'Chemin de connexions (« relié via »), pas une explication causale. Chaque étape cite sa source.', en: 'A path of connections, not a causal explanation.' },
    paths: [
      {
        nodes: ['Q7322', 'fixture:tordesillas', 'Q45', 'Q155'],
        hop_count: 3,
        cost: 3.2,
        text: { fr: [], en: [] },
        hops: [
          { from: 'Q7322', to: 'fixture:tordesillas', kind: 'statement', type: 'P1344', forward: true, causal: false, temporal: { kind: 'point', point: '1494-06-07' }, evidence: [ev('Q7322', 'P1344', 1)], sources: ['Wikidata (community-edited)'], alternatives: 0, text: { fr: 'Christophe Colomb — participant à → traité de Tordesillas (1494-06-07) [wikidata:…]', en: '…' } },
          { from: 'fixture:tordesillas', to: 'Q45', kind: 'statement', type: 'P1891', forward: true, causal: false, temporal: { kind: 'point', point: '1494-06-07' }, evidence: [ev('Q1', 'P1891', 2)], sources: ['Wikidata (community-edited)'], alternatives: 1, text: { fr: 'traité de Tordesillas — signataire → Portugal (1494-06-07) [wikidata:…]', en: '…' } },
          { from: 'Q45', to: 'Q155', kind: 'link', type: 'former_colony_of', forward: false, causal: false, temporal: { kind: 'interval', start: '1500', end: '1822' }, via: ['fixture:colonia'], evidence: [ev('Q155', 'P1365', 3), ev('Q1', 'P17', 4)], sources: ['Wikidata (community-edited)'], alternatives: 0, text: { fr: 'Portugal — ancienne puissance coloniale de → Brésil (1500–1822) [wikidata:…]', en: '…' } },
        ],
      },
      {
        nodes: ['Q7322', 'fixture:voyage1492', 'fixture:colonisation', 'Q155'],
        hop_count: 3,
        cost: 5.1,
        text: { fr: [], en: [] },
        hops: [
          { from: 'Q7322', to: 'fixture:voyage1492', kind: 'statement', type: 'P793', forward: true, causal: false, temporal: { kind: 'undated' }, evidence: [ev('Q7322', 'P793', 5)], sources: [], alternatives: 0, text: { fr: 'Christophe Colomb — événement clé → premier voyage de Colomb (non daté dans la source) [wikidata:…]', en: '…' } },
          { from: 'fixture:voyage1492', to: 'fixture:colonisation', kind: 'statement', type: 'P1542', forward: true, causal: true, temporal: { kind: 'point', point: '1492' }, evidence: [ev('Q1', 'P1542', 6)], sources: [], alternatives: 0, text: { fr: 'selon Wikidata (contribution communautaire), premier voyage de Colomb a pour effet colonisation européenne des Amériques (1492) [wikidata:…]', en: '…' } },
          { from: 'fixture:colonisation', to: 'Q155', kind: 'statement', type: 'P17', forward: false, causal: false, temporal: { kind: 'undated' }, evidence: [ev('Q1', 'P17', 7)], sources: [], alternatives: 0, text: { fr: 'colonisation européenne des Amériques — pays → Brésil (non daté dans la source) [wikidata:…]', en: '…' } },
        ],
      },
    ],
  },
  { warnings: [{ code: 'path_not_causal', message: 'A path of connections, not a causal explanation.' }, { code: 'community_edited', message: 'Wikidata statements are community-edited.' }] },
);

export const LINKS_PATH_NONE = envelope({ query: {}, found: false, reason: 'no_path_within_max_hops', truncated: true, explored_nodes: 9000, labels: {}, disclaimer: null, paths: [] });

function hop(from: [string, string], to: [string, string], pid: string, rel: string, opts: { causal?: boolean; forward?: boolean } = {}) {
  const forward = opts.forward ?? true;
  const s = stmt(forward ? from : to, pid, rel, forward ? to : from, { causal: opts.causal, pit: y(1494) });
  return {
    ...s,
    from: { qid: from[0], label: from[1] },
    to: { qid: to[0], label: to[1] },
    direction: forward ? 'forward' : 'inverse',
    relation_label_en: rel,
    relation_label_fr: rel,
    dated: true,
    time_status: 'dated',
    wording: opts.causal ? `${from[1]} — ${rel} — ${to[1]}: a causal relation asserted by wikidata` : `${from[1]} is connected via '${rel}' to ${to[1]}`,
    causal_assertion: opts.causal ? { asserted_by: 'wikidata', statement_key: s.provenance.statement_key } : null,
  };
}

export const KG_PATH_COL_BRA = envelope({
  from: 'Q7322',
  to: 'Q155',
  from_label: 'Christopher Columbus',
  to_label: 'Brazil',
  found: true,
  note: 'A path is not a cause.',
  community_note: 'Wikidata statements are community-edited and are not verified by REVA.',
  search: { max_hops: 4, truncated: false, hubs_skipped: [] },
  paths: [
    { length: 2, origins: ['test_fixture'], wording: 'connected', hops: [hop(COL, TORD, 'P1344', 'participant à'), hop(TORD, BRA, 'P1542', 'a pour effet', { causal: true })] },
  ],
});

// ------------------------------------------------------------------ atlas

const SNAP = { snapshot_sha256: 'e'.repeat(64), origin: 'test_fixture', knowledge_cutoff: '2026-10-01T00:00:00Z', tables: 3, rows: 1200 };

export function concept(id: string, fr: string, unit: string, domain: string, source_order: string[], subdomain: string | null = null, value_kind = 'decimal') {
  return { id, labels: { fr, en: fr }, definition: `Définition fournisseur de ${fr}.`, unit, domain, subdomain, value_kind, source_order, provider_declared: true, review_status: 'provider_declared', notes: null, tags: [], catalog_file: `${domain}.yaml` };
}

export const C_UNEMP = concept('labour.unemployment_rate', 'Taux de chômage', '%', 'labour', ['ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT', 'wb.wdi.SL.UEM.TOTL.ZS'], 'unemployment');
export const C_MINWAGE = concept('labour.minimum_wage_monthly', 'Salaire minimum mensuel', 'USD', 'labour', ['ilo.ilostat_mw.EAR_INEE_NOC_NB'], 'wages');
export const C_LIFE = concept('health.life_expectancy', 'Espérance de vie à la naissance', 'years', 'health', ['who.gho.WHOSIS_000001', 'wb.wdi.SP.DYN.LE00.IN']);
export const C_LEGAL = concept('law.legal_system', 'Système juridique', '1', 'law', ['law.jurisworld.SYSTEM'], null, 'text');

export const CONCEPTS = { data: [C_UNEMP, C_MINWAGE, C_LIFE, C_LEGAL], total: 4, domains: { labour: 2, health: 1, law: 1 }, catalog_errors: [], warnings: [] };

export function cell(iso3: string, year: number, value: string | null, opts: { text?: string; flags?: Record<string, unknown>; series?: string; unit?: string } = {}) {
  const series = opts.series ?? 'ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT';
  const [source, dataset, ...ind] = series.split('.');
  return {
    iso3,
    year,
    value,
    value_text: opts.text ?? null,
    unit: opts.unit ?? '%',
    source,
    dataset,
    indicator: ind.join('.'),
    series,
    provider_geo: iso3,
    entity_qid: null,
    period_start: `${year}-01-01`,
    period_end: `${year + 1}-01-01`,
    flags: opts.flags ?? {},
    provenance: { raw_sha256: 'f'.repeat(64), selector: { file: 'fixture.csv', row: 12, column: String(year) }, table_artifact_sha256: '1'.repeat(64), dataset_key: `atlas.${source}.${dataset}`, dataset_id: 'ds-1' },
    source_rank: 0,
    sources_reporting: 1,
  };
}

export function mapResponse(year: number) {
  const k = year - 2020;
  return {
    data: {
      concept: C_UNEMP,
      year,
      values: [
        cell('FRA', year, (7.9 - k * 0.2).toFixed(1)),
        cell('BRA', year, (13.8 - k * 1.5).toFixed(1)),
        cell('USA', year, '3.6', { flags: { estimated: true } }),
        cell('DEU', year, '3.1', { series: 'wb.wdi.SL.UEM.TOTL.ZS' }),
        cell('ZAF', year, '29.8'),
      ],
      countries_with_value: 5,
      countries_without_value: ['PRK', 'ERI', 'SOM'],
      by_source: { 'ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT': 4, 'wb.wdi.SL.UEM.TOTL.ZS': 1 },
    },
    snapshot: SNAP,
    notes: ['countries_without_value: no declared source reports the cell (absent, not zero).'],
    warnings: [{ code: 'fixture_data', message: 'Test fixture snapshot' }],
  };
}

export const COVERAGE_UNEMP = {
  data: [{ concept_id: 'labour.unemployment_rate', domain: 'labour', countries: 5, cells: 15, year_min: 2020, year_max: 2022, by_source: [] }],
  by_country: [{ iso3: 'FRA', years: 3, year_min: 2020, year_max: 2022, best_sources_used: ['ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT'] }],
  snapshot: SNAP,
  countries_total: 250,
  catalog_errors: [],
  warnings: [],
};

export const COVERAGE_ANY = { ...COVERAGE_UNEMP, by_country: null };

export const DOSSIER_FRA = {
  data: {
    iso3: 'FRA',
    label: 'France',
    concepts_with_data: 3,
    concepts_without_data: 1,
    non_annual_cells: 0,
    domains: [
      {
        domain: 'labour',
        concepts: [
          {
            concept_id: C_UNEMP.id,
            labels: C_UNEMP.labels,
            unit: '%',
            subdomain: 'unemployment',
            latest: cell('FRA', 2022, '7.3'),
            years: 3,
            year_min: 2020,
            year_max: 2022,
            sources_used: ['ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT'],
            series: [2020, 2021, 2022].map((yy, i) => ({ year: yy, value: ['8.0', '7.9', '7.3'][i]!, value_text: null, series: 'ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT', flags: {} })),
          },
        ],
        without_data: [C_MINWAGE.id],
      },
      {
        domain: 'health',
        concepts: [
          {
            concept_id: C_LIFE.id,
            labels: C_LIFE.labels,
            unit: 'years',
            subdomain: null,
            latest: cell('FRA', 2021, '82.3', { series: 'who.gho.WHOSIS_000001', unit: 'years', flags: { estimated: true } }),
            years: 2,
            year_min: 2020,
            year_max: 2021,
            sources_used: ['who.gho.WHOSIS_000001'],
            series: null,
          },
        ],
        without_data: [],
      },
      {
        domain: 'law',
        concepts: [
          { concept_id: C_LEGAL.id, labels: C_LEGAL.labels, unit: '1', subdomain: null, latest: cell('FRA', 2020, null, { text: 'droit civil', series: 'law.jurisworld.SYSTEM', unit: '1' }), years: 1, year_min: 2020, year_max: 2020, sources_used: ['law.jurisworld.SYSTEM'], series: null },
        ],
        without_data: [],
      },
    ],
  },
  snapshot: SNAP,
  notes: ['Atlas cells are what each source reports.'],
  catalog_errors: [],
  warnings: [],
};

export const LINKS_FRA = envelope({
  qid: 'Q142',
  query: 'FRA',
  truncated: false,
  labels: { Q142: { fr: 'France' }, Q29: { fr: 'Espagne' }, Q183: { fr: 'Allemagne' }, Q458: { fr: 'Union européenne' } },
  links: [
    { subject: 'Q142', object: 'Q29', link_type: 'border_wikidata', directed: false, temporal: { kind: 'undated' }, via: [], evidence: [ev('Q142', 'P47', 20)], sources: ['Wikidata (community-edited)'] },
    { subject: 'Q142', object: 'Q183', link_type: 'co_member', directed: false, temporal: { kind: 'interval', start: '1958-01-01' }, via: ['Q458'], evidence: [ev('Q142', 'P463', 21), ev('Q183', 'P463', 22)], sources: ['Wikidata (community-edited)'] },
  ],
  link_types: { border_wikidata: { status: 'derivable', derived: 1 }, co_member: { status: 'derivable', derived: 1 }, bilateral_trade: { status: 'awaiting_source', derived: 0 } },
  derivation: {},
});

export const TIMELINE_Q142 = envelope({
  qid: 'Q142',
  label: 'France',
  truncated: false,
  events: [{ ...stmt(['Q142', 'France'], 'P463', 'membre de', UN, { start: d('1945-10-24') }), sort_date: '1945-10-24', dated_by: 'validity_qualifiers', direction: 'out' }],
});

export const ASK_BRESIL = {
  question_sha256: '0'.repeat(64),
  language: 'fr',
  plan: { planner: 'keyword_fallback', language: 'fr', operations: [{ id: 'op1', op: 'kg_statements', subject: 'Q155', property: 'P37' }], rejected: [] },
  restatement: 'Langue officielle (P37) du Brésil (Q155)',
  llm_restatement: '',
  answer_text: 'La langue officielle du Brésil est le portugais.',
  sentences: [
    { id: 's1', text: 'Selon Wikidata, la langue officielle du Brésil est le portugais.', kind: 'fact', citations: ['E1'], citation_refs: ['E1: wikidata Q155$FIXTURE-1'] },
    { id: 's2', text: 'Aucune source chargée ne donne le nombre de locuteurs en 2024.', kind: 'missing', citations: ['E2'], citation_refs: ['E2: consulted un.wpp'] },
    { id: 's3', text: 'Le Brésil est relié au Portugal via « langue officielle » ; ce lien n’est pas une cause.', kind: 'path', citations: ['E1', 'E3'], citation_refs: [] },
  ],
  evidence: [
    { id: 'E1', kind: 'kg_statement', citation: { source: 'wikidata', statement_id: 'Q155$FIXTURE-1', revision: '2250000000', url: 'https://www.wikidata.org/wiki/Q155#Q155$FIXTURE-1' }, origin: 'test_fixture' },
    { id: 'E2', kind: 'absence', citation: { source: 'atlas' }, sources_consulted: ['un.wpp', 'wb.wdi'] },
    { id: 'E3', kind: 'kg_statement', citation: { source: 'wikidata', statement_id: 'Q45$FIXTURE-2', revision: '2250000000' }, origin: 'test_fixture' },
  ],
  unsupported: [{ text: 'nombre de locuteurs', reason: 'not in catalog' }],
  planner: 'keyword_fallback',
  provider: null,
  model: null,
  model_requested: null,
  prompt_version: 'nlqa-1',
  prompt_sha256: null,
  catalog_digest: 'x',
  usage: {},
  rephrase: {},
  warnings: ['llm_not_enabled_for_questions: keyword planner used'],
  latency_ms: 12.5,
};

/** A tiny display geometry collection (rectangles) shaped like GET /v1/locations. */
function rect(key: string, name: string, x0: number, y0: number, x1: number, y1: number, i: number) {
  return {
    type: 'Feature',
    id: `geo-${i}`,
    geometry: { type: 'MultiPolygon', coordinates: [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]] },
    properties: { feature_key: `${key}:${i}`, entity_id: `ent-${key}`, entity_label: name, match_status: 'reviewed_match', name, dataset_sha256: '2'.repeat(64), display_only: true },
  };
}

export const LOCATIONS = envelope(
  {
    type: 'FeatureCollection',
    display_note: 'Display geometry only',
    truncated: false,
    features: [
      rect('FRA', 'France', -5, 42, 8, 51, 1),
      rect('DEU', 'Germany', 6, 47, 15, 55, 2),
      rect('BRA', 'Brazil', -74, -33, -35, 5, 3),
      rect('USA', 'United States', -125, 25, -67, 49, 4),
      rect('ZAF', 'South Africa', 16, -35, 33, -22, 5),
      rect('PRK', 'North Korea', 124, 38, 131, 43, 6),
      rect('ERI', 'Eritrea', 36, 12, 43, 18, 7),
      rect('SOM', 'Somalia', 41, -2, 51, 12, 8),
      rect('-99', 'Somaliland', 43, 8, 48, 11, 9),
    ],
  },
  { context_mode: 'registry', modality: 'metadata' },
);

export const NOT_FOUND_ROUTE = { type: 'about:blank', title: 'Not Found', status: 404, detail: 'Not Found', code: 'not_found' };
