/**
 * Causality fixtures shaped like CAUSCORE's routes (docs/dev/v3_causal_interface.md: claim JSON, chain JSON),
 * used by vitest and by the Playwright specs (ui/e2e/v3_causal*.spec.ts) through route interception.
 *
 * TEST DATA ONLY: every record is labelled origin "test_fixture"; statement ids, revisions, dataset rows and
 * sha256 are synthetic ("FIXTURE"); entities other than the focal event use provider-scoped keys
 * ("fixture:…"), which the UI never links to Wikidata. Mechanism quotes and citations are placeholders written
 * for the test, not quotations of real sources. Grades are given as the grader would compute them from the
 * fixture's own tier / temporal / corroboration inputs.
 */

const REC = '2026-10-16T12:00:00Z';
const SHA = 'f'.repeat(64);

export function envelope<T>(data: T, warnings: { code: string; message: string }[] = []) {
  return { data, context_mode: 'evidence', origin: 'test_fixture', modality: 'reported_observation', knowledge_cutoff: null, warnings, next_cursor: null };
}

type Ref = { id: string; label: string; kind?: string };
const WW1: Ref = { id: 'Q361', label: 'Première Guerre mondiale', kind: 'event' };
const SARAJEVO: Ref = { id: 'fixture:sarajevo_1914', label: 'Attentat de Sarajevo', kind: 'event' };
const JULY: Ref = { id: 'fixture:icb:crisis:july_1914', label: 'Crise de juillet 1914 (déclencheur codé)', kind: 'event' };
const ALLIANCES: Ref = { id: 'fixture:alliances', label: 'Système d’alliances européen', kind: 'other' };
const NAVAL: Ref = { id: 'fixture:naval_race', label: 'Course aux armements navals', kind: 'event' };
const IMPERIALISM: Ref = { id: 'fixture:imperialism', label: 'Rivalités impériales', kind: 'other' };
const VERSAILLES: Ref = { id: 'fixture:versailles', label: 'Traité de Versailles', kind: 'treaty' };
const REVOLUTION: Ref = { id: 'fixture:russian_revolution', label: 'Révolution russe', kind: 'event' };
const ARMISTICE: Ref = { id: 'fixture:armistice_1918', label: 'Armistice de 1918 (issue codée)', kind: 'event' };

type W = { from: string; to: string; precision: number; basis: string } | null;
const day = (iso: string, basis = 'P585'): W => ({ from: iso, to: iso, precision: 11, basis });
const year = (y: number, basis = 'P580'): W => ({ from: `${y}-01-01`, to: `${y}-12-31`, precision: 9, basis });

interface ClaimOpts {
  key: string;
  cause: Ref;
  effect: Ref;
  relation: string;
  labelFr: string;
  lane: string;
  tier: 'T1' | 'T2' | 'T3' | 'T4';
  grade: string | null;
  rules?: string[];
  temporal?: 'consistent' | 'overlapping' | 'inconsistent' | 'undetermined';
  causeStart?: W;
  effectStart?: W;
  corroboration?: number;
  corroborating?: string[];
  contestedBy?: string[];
  mechanism?: string | null;
  stance?: 'asserts' | 'denies';
  dataset?: { source_key: string; dataset: string; revision: string; selector: Record<string, unknown>; codebook?: Record<string, unknown>; citation?: Record<string, string> };
  refs?: number;
}

let row = 40;
export function claim(o: ClaimOpts) {
  row += 1;
  const wd = !o.dataset;
  const stmt = `Q361$FIXTURE-${row}`;
  const source = wd
    ? {
        source_key: 'wikidata',
        tier: o.tier,
        dataset: null,
        source_revision: '2250000000',
        statement_key: `wikidata:${stmt}`,
        statement_id: stmt,
        statement_row_id: row,
        selector: { entity: o.effect.id, property: 'P828', statement_id: stmt },
        raw_sha256: SHA,
        references: Array.from({ length: o.refs ?? (o.tier === 'T3' ? 1 : 0) }, () => ({ P854: ['https://example.org/fixture-reference'] })),
        citation: null,
        codebook: null,
        verification: 'community_edited_not_verified',
      }
    : {
        source_key: o.dataset!.source_key,
        tier: o.tier,
        dataset: o.dataset!.dataset,
        source_revision: o.dataset!.revision,
        statement_key: null,
        statement_id: null,
        statement_row_id: null,
        selector: o.dataset!.selector,
        raw_sha256: SHA,
        references: [],
        citation: o.dataset!.citation ?? null,
        codebook: o.dataset!.codebook ?? null,
        verification: 'source_reported_not_verified',
      };
  return {
    claim_key: o.key,
    row_id: row,
    cause: o.cause,
    effect: o.effect,
    relation: o.relation,
    relation_label: { en: o.relation.replace(/_/g, ' '), fr: o.labelFr },
    family: o.relation === 'prevented_or_delayed' ? 'prevention' : 'cause',
    lane: o.lane,
    polarity: null,
    stance: o.stance ?? 'asserts',
    asserted_on: 'effect',
    source,
    mechanism: o.mechanism ?? null,
    scope: {},
    qualifiers: {},
    grade:
      o.grade === null
        ? null
        : {
            grade: o.grade,
            rules_version: '1',
            rules_sha256: SHA,
            rule_ids: o.rules ?? [`tier.${o.tier}`, `temporal.${o.temporal ?? 'consistent'}`, `grade.${o.grade}`],
            reasons: [{ rule_id: `grade.${o.grade}`, en: `fixture grade ${o.grade}`, fr: `grade ${o.grade} de la grille (donnée de test)` }],
            temporal_check: o.temporal ?? 'consistent',
            temporal: { cause_start: o.causeStart ?? null, effect_start: o.effectStart ?? year(1914) },
            corroboration: o.corroboration ?? 1,
            corroborating_claim_keys: o.corroborating ?? [],
            upstream_groups: [],
            contested_by: o.contestedBy ?? [],
            graded_at: REC,
          },
    review_status: 'unreviewed',
    wording_fr: `Selon ${wd ? 'Wikidata' : o.dataset!.source_key} (donnée de test), ${o.cause.label} ${o.labelFr} ${o.effect.label} ; REVA n’établit pas de causalité.`,
    origin: 'test_fixture',
    recorded_at: REC,
  };
}

const ICB = {
  source_key: 'icb.data',
  dataset: 'icb2',
  revision: 'v16-FIXTURE',
  selector: { file: 'icb2-fixture.csv', row: 2, columns: ['TRIGGR'] },
  codebook: { name: 'International Crisis Behavior, actor-level codebook (fixture)', version: '16', url: 'https://example.org/codebook' },
};

export const C_SARAJEVO = claim({
  key: 'kg:wikidata:Q361$FIXTURE-sarajevo',
  cause: SARAJEVO,
  effect: WW1,
  relation: 'immediate_cause',
  labelFr: 'cause immédiate de',
  lane: 'immediate',
  tier: 'T3',
  grade: 'B',
  rules: ['tier.T3', 'temporal.consistent', 'corroboration.multi', 'contest.none', 'grade.B.t3_corroborated'],
  causeStart: day('1914-06-28'),
  effectStart: day('1914-07-28', 'P580'),
  corroboration: 2,
  corroborating: ['ds:icb.data:icb2:2:trigger'],
  refs: 2,
});
export const C_JULY = claim({
  key: 'ds:icb.data:icb2:2:trigger',
  cause: JULY,
  effect: WW1,
  relation: 'coded_trigger',
  labelFr: 'est le déclencheur codé de',
  lane: 'coded',
  tier: 'T1',
  grade: 'A',
  rules: ['tier.T1', 'temporal.consistent', 'corroboration.multi', 'contest.none', 'grade.A.t1t2_consistent_corroborated'],
  causeStart: day('1914-07-23'),
  effectStart: day('1914-07-28', 'P580'),
  corroboration: 2,
  mechanism: 'FIXTURE codebook definition: trigger = the specific act, event or situation that catalyzed the crisis.',
  dataset: ICB,
});
export const C_ALLIANCES = claim({
  key: 'ds:fixture.scholarly:refs:alliances',
  cause: ALLIANCES,
  effect: WW1,
  relation: 'underlying_cause',
  labelFr: 'cause profonde de',
  lane: 'underlying',
  tier: 'T2',
  grade: 'B',
  rules: ['tier.T2', 'temporal.consistent', 'corroboration.single', 'contest.none', 'grade.B.t1t2'],
  causeStart: year(1882),
  mechanism: 'FIXTURE quotation: the alliance commitments turned a local conflict into a general war.',
  dataset: { source_key: 'fixture.scholarly', dataset: 'refs', revision: '1', selector: { reference: 'fixture-ref-1' }, citation: { doi: '10.0000/fixture.alliances' } },
});
export const C_NAVAL = claim({
  key: 'kg:wikidata:Q361$FIXTURE-naval',
  cause: NAVAL,
  effect: WW1,
  relation: 'contributing_factor',
  labelFr: 'facteur contributif de',
  lane: 'contributing',
  tier: 'T4',
  grade: 'D',
  causeStart: year(1898),
  refs: 0,
});
export const C_IMPERIALISM = claim({
  key: 'kg:wikidata:Q361$FIXTURE-imperialism',
  cause: IMPERIALISM,
  effect: WW1,
  relation: 'underlying_cause',
  labelFr: 'cause profonde de',
  lane: 'underlying',
  tier: 'T3',
  grade: 'E',
  rules: ['tier.T3', 'temporal.undetermined', 'corroboration.single', 'contest.contested', 'grade.E.contested_uncorroborated'],
  temporal: 'undetermined',
  contestedBy: ['ds:fixture.scholarly:refs:imperialism_denied'],
});
export const C_IMPERIALISM_DENIED = claim({
  key: 'ds:fixture.scholarly:refs:imperialism_denied',
  cause: IMPERIALISM,
  effect: WW1,
  relation: 'underlying_cause',
  labelFr: 'cause profonde de',
  lane: 'underlying',
  tier: 'T2',
  grade: 'B',
  stance: 'denies',
  temporal: 'undetermined',
  dataset: { source_key: 'fixture.scholarly', dataset: 'refs', revision: '1', selector: { reference: 'fixture-ref-2' }, citation: { isbn: '978-0-00-000000-0' } },
});
export const C_TEMPORAL_BAD = claim({
  key: 'kg:wikidata:Q361$FIXTURE-versailles-cause',
  cause: VERSAILLES,
  effect: WW1,
  relation: 'has_cause',
  labelFr: 'a causé',
  lane: 'general',
  tier: 'T4',
  grade: 'E',
  rules: ['tier.T4', 'temporal.inconsistent', 'grade.E.temporal_inconsistent'],
  temporal: 'inconsistent',
  causeStart: day('1919-06-28', 'P585'),
  refs: 0,
});
export const C_UNGRADED = claim({
  key: 'kg:wikidata:Q361$FIXTURE-ungraded',
  cause: { id: 'fixture:balkan_wars', label: 'Guerres balkaniques', kind: 'war' },
  effect: WW1,
  relation: 'has_cause',
  labelFr: 'a causé',
  lane: 'general',
  tier: 'T3',
  grade: null,
});

export const E_VERSAILLES = claim({
  key: 'kg:wikidata:Q361$FIXTURE-versailles',
  cause: WW1,
  effect: VERSAILLES,
  relation: 'has_effect',
  labelFr: 'a pour effet',
  lane: 'general',
  tier: 'T3',
  grade: 'C',
  causeStart: day('1914-07-28', 'P580'),
  effectStart: day('1919-06-28'),
});
export const E_REVOLUTION = claim({
  key: 'kg:wikidata:Q361$FIXTURE-revolution',
  cause: WW1,
  effect: REVOLUTION,
  relation: 'contributing_factor',
  labelFr: 'facteur contributif de',
  lane: 'contributing',
  tier: 'T3',
  grade: 'B',
  causeStart: day('1914-07-28', 'P580'),
  effectStart: year(1917),
  corroboration: 2,
});
export const E_ARMISTICE = claim({
  key: 'ds:icb.data:icb2:2:outcome',
  cause: WW1,
  effect: ARMISTICE,
  relation: 'coded_outcome',
  labelFr: 'a pour issue codée',
  lane: 'coded',
  tier: 'T1',
  grade: 'B',
  causeStart: day('1914-07-28', 'P580'),
  effectStart: day('1918-11-11'),
  dataset: { ...ICB, selector: { file: 'icb2-fixture.csv', row: 2, columns: ['OUTCOM'] } },
});

export const CAUSES = [C_SARAJEVO, C_JULY, C_ALLIANCES, C_NAVAL, C_IMPERIALISM, C_IMPERIALISM_DENIED, C_TEMPORAL_BAD, C_UNGRADED];
export const EFFECTS = [E_VERSAILLES, E_REVOLUTION, E_ARMISTICE];

/** GET /v1/causal/events/Q361 as reva.causal.graph.event() shapes it (context = KG statements, not causal). */
export const EVENT_WW1 = envelope({
  id: 'Q361',
  event: { id: 'Q361', label: 'World War I', label_fr: 'Première Guerre mondiale', kind: 'war' },
  dates: {
    start: { from: '1914-07-28', to: '1914-07-28', basis: 'P580', precision: 11, times: ['+1914-07-28T00:00:00Z'] },
    end: { from: '1918-11-11', to: '1918-11-11', basis: 'P582', precision: 11, times: ['+1918-11-11T00:00:00Z'] },
  },
  causes: { cause: { A: 1, B: 3, D: 1, E: 2, ungraded: 1 } },
  effects: { cause: { B: 2, C: 1 } },
  context: [
    ctx('Q361', 'P276', 'lieu', 'location', { id: 'fixture:europe', label: 'Europe' }),
    ctx('Q361', 'P710', 'participant', 'participant', { id: 'fixture:france', label: 'France' }),
    ctx('Q361', 'P710', 'participant', 'participant', { id: 'fixture:germany', label: 'German Empire', label_fr: 'Empire allemand' }),
  ],
  context_truncated: false,
  context_note: 'Context edges (part of, follows, participants, places) are not causal and are never counted.',
  candidates: 2,
  candidates_note: 'Unsourced candidates (text-mined T5 / model-proposed T6), quarantined.',
  note: 'Causal links are claims made by the named sources; REVA does not establish causation.',
  origins: ['test_fixture'],
});

function ctx(event: string, property: string, fr: string, en: string, other: Record<string, string>) {
  return {
    subject: { id: event, label: 'World War I', label_fr: 'Première Guerre mondiale', kind: 'war' },
    property,
    label: { en, fr },
    object: { ...other, kind: null },
    source_key: 'wikidata',
    statement_key: `wikidata:${event}$FIXTURE-${property}-${other.id}`,
    origin: 'test_fixture',
    causal: false,
  };
}

export const CAUSES_WW1 = envelope({ event: { id: 'Q361', label: 'Première Guerre mondiale' }, claims: CAUSES });
export const EFFECTS_WW1 = envelope({ event: { id: 'Q361', label: 'Première Guerre mondiale' }, claims: EFFECTS });

/** GET /v1/causal/events/Q361/why as reva.causal.graph.why() shapes it (lanes as a list; dissent = denials + contested). */
export const WHY_WW1 = envelope({
  id: 'Q361',
  label: 'World War I',
  lanes: [
    { lane: 'immediate', label: { en: 'immediate causes', fr: 'causes immédiates' }, claims: [C_SARAJEVO] },
    { lane: 'underlying', label: { en: 'underlying causes', fr: 'causes profondes' }, claims: [C_ALLIANCES, C_IMPERIALISM] },
    { lane: 'contributing', label: { en: 'contributing factors', fr: 'facteurs contributifs' }, claims: [C_NAVAL] },
    { lane: 'general', label: { en: 'causes', fr: 'causes' }, claims: [C_TEMPORAL_BAD, C_UNGRADED] },
    { lane: 'coded', label: { en: 'coded triggers', fr: 'déclencheurs codés' }, claims: [C_JULY] },
  ],
  mechanisms: [],
  dissent: [C_IMPERIALISM_DENIED, C_IMPERIALISM],
  truncated: false,
  summary_fr: '7 affirmation(s) causale(s) sourcée(s) sur Première Guerre mondiale ; chacune est attribuée à sa source. REVA n’établit pas de causalité.',
});

export const CHAIN_SARAJEVO_VERSAILLES = envelope({
  from: 'fixture:sarajevo_1914',
  to: 'fixture:versailles',
  found: true,
  max_hops: 4,
  min_grade: 'D',
  paths: [
    {
      length: 2,
      chain_grade: 'C',
      weakest_hop: 1,
      hops: [
        { from: SARAJEVO, to: WW1, hop_grade: 'B', claims: [C_SARAJEVO] },
        { from: WW1, to: VERSAILLES, hop_grade: 'C', claims: [E_VERSAILLES] },
      ],
      wording: 'Composition by REVA of 2 separately asserted claims; chain grade = weakest hop (C).',
    },
  ],
  note: 'composition by REVA of separately asserted claims; chain grade = weakest hop',
  note_fr: 'composition par REVA d’affirmations causales énoncées séparément ; note de la chaîne = maillon le plus faible.',
  search: { levels_expanded: 2, edges_scanned: 14, truncated: false, truncation_reasons: [], elapsed_ms: 3, time_budget_ms: 5000 },
});

export const CHAIN_NONE = envelope({
  from: { id: 'Q361', label: 'World War I', label_fr: 'Première Guerre mondiale', kind: 'war' },
  to: { id: 'fixture:unrelated', label: 'fixture:unrelated', label_fr: null, kind: null },
  found: false,
  paths: [],
  note_fr: 'composition par REVA d’affirmations causales énoncées séparément ; note de la chaîne = maillon le plus faible.',
  not_found_reason: 'no causal chain within max_hops among claims graded at least D',
  search: { truncated: false, ungraded_skipped: 0 },
});

export const CANDIDATES_WW1 = envelope({
  event: 'Q361',
  label: 'non sourcé — candidats',
  candidates: [
    {
      candidate_key: 'cand:causcand.patterns:1',
      cause: { id: 'fixture:nationalism', label: 'Nationalismes' },
      effect: WW1,
      relation: 'has_cause',
      tier: 'T5',
      extractor: { id: 'causcand.patterns', version: '1' },
      source: { source_key: 'wikipedia.fr', source_revision: '999000111', selector: { page: 'FIXTURE', section: 'Causes', sentence: 3 }, raw_sha256: SHA },
      source_sentence: 'FIXTURE sentence: nationalism is often cited among the causes of the war.',
      label: 'unsourced candidate (non sourcé — candidat)',
      promotion_status: 'not_promoted',
      origin: 'test_fixture',
    },
    {
      candidate_key: 'cand:model:2',
      cause: { id: 'fixture:militarism', label: 'Militarisme' },
      effect: WW1,
      relation: 'contributing_factor',
      tier: 'T6',
      extractor_id: 'causcand.model',
      extractor_version: '0',
      source_key: 'wikipedia.fr',
      source_revision: '999000111',
      source_sentence: 'FIXTURE sentence proposed by a model.',
      promotion_status: 'not_promoted',
      origin: 'test_fixture',
    },
  ],
});

export const NOT_FOUND_ROUTE = { detail: 'Not Found' };
export const UNKNOWN_EVENT = { title: 'Not Found', status: 404, detail: 'unknown event', code: 'not_found' };

const prov = (key: string, property: string) => ({
  statement_key: key,
  property,
  source_key: 'wikidata',
  source_revision: '2250000000',
  raw_sha256: SHA,
  rank: 'normal',
  origin: 'test_fixture',
  verification: 'community_edited_not_verified',
  derivation: null,
});
const tv = (time: string, precision: number, key: string, property: string) => ({ time, precision, lo: time.slice(1, 11), hi: time.slice(1, 11), provenance: prov(key, property) });

/** GET /v1/events/Q361 (agent EVENTS, src/reva/api/routes/events.py EventCard), trimmed to what the screen reads. */
export const EVENTS_CARD_WW1 = envelope({
  id: 'Q361',
  label: 'World War I',
  labels: { en: 'World War I', fr: 'Première Guerre mondiale' },
  descriptions: { fr: 'conflit mondial (donnée de test)' },
  kind: 'event',
  dataset_kind: null,
  types: {
    is_event: true,
    basis: 'wikidata_p31',
    primary: [{ id: 'world_war', label_en: 'world war', label_fr: 'guerre mondiale', parent: 'war' }],
    secondary: [],
    rank_rule: 'fixture',
    matches: [],
    p31_unmatched: [],
    class_search_truncated: false,
  },
  interval: {
    start: tv('+1914-07-28T00:00:00Z', 11, 'wikidata:Q361$FIXTURE-P580', 'P580'),
    end: tv('+1918-11-11T00:00:00Z', 11, 'wikidata:Q361$FIXTURE-P582', 'P582'),
    point_in_time: null,
    inception: null,
    dissolved: null,
    episode_end: null,
    earliest: '1914-07-28',
    latest: '1918-11-11',
    basis: 'source_dates',
    consistency: 'consistent',
    alternatives: [],
    conflicting_values: false,
    notes: [],
  },
  places: [{ relation: 'location', place: { id: 'fixture:europe', label: 'Europe', label_fr: 'Europe', kind: 'place' }, value: null, valid_from: null, valid_to: null, provenance: prov('wikidata:Q361$FIXTURE-P276', 'P276') }],
  participants: [
    {
      participant: { id: 'fixture:france', label: 'France', label_fr: 'France', kind: 'country' },
      value: null,
      role: { id: 'belligerent', label_en: 'belligerent', label_fr: 'belligérant' },
      side: 'Alliés',
      property: 'P710',
      direction: 'out',
      role_qualifiers: [],
      valid_from: null,
      valid_to: null,
      statement_count: 1,
      statements: [{ ...prov('wikidata:Q361$FIXTURE-P710-1', 'P710'), point_in_time: null, coded: null }],
    },
  ],
  structure: [],
  causal_statements: [],
  same_as: [],
  coded: [],
  sources: ['wikidata'],
  entity_provenance: null,
  truncated: false,
  notes: [],
  view_version: 'fixture',
  taxonomy_sha256: SHA,
});
