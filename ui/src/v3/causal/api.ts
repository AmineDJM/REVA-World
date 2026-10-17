/**
 * Causality client (agent CAUSCORE, src/reva/api/routes/causal.py; contract: docs/dev/v3_causal_interface.md;
 * design: docs/roadmap/v3_causality.md):
 *   GET /v1/causal/events/{id}             event view (kind/type, date windows with precision, places, participants, context)
 *   GET /v1/causal/events/{id}/causes      sourced causal claims whose effect is the event
 *   GET /v1/causal/events/{id}/effects     sourced causal claims whose cause is the event
 *   GET /v1/causal/events/{id}/why         causes by lane (immediate / underlying / contributing / general / coded / …), dissent
 *   GET /v1/causal/chain?from&to           compositions of distinct claims (≤ 6 hops), chain grade = weakest hop
 *   GET /v1/causal/candidates?event=       quarantined candidate space (text-mined T5 / model-proposed T6)
 *
 * Schemas are tolerant about *shape* (envelope or bare data, aliases, extra fields pass through) and strict
 * about *honesty*: a causal claim must name its asserting source and a claim tier T1–T4; its grade is A–E or
 * explicitly absent ("not graded yet", shown as such). Otherwise the response is a ContractError and nothing
 * is drawn. A candidate (T5/T6) never parses as a claim, a claim never parses as a candidate, and a candidate
 * never carries a grade.
 */
import { z } from 'zod';
import { JsonRecord, parseWith, warningsFr } from '../api/common';
import { request, type Query } from '../../api/http';

const enc = encodeURIComponent;
type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => !!v && typeof v === 'object' && !Array.isArray(v);

// ------------------------------------------------------------------ primitives

const optStr = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : typeof v === 'number' ? String(v) : null);

export type TimePoint = { time: string; precision: number | null } | null;

/**
 * A time at its own precision, from any of: {time, precision} (KG), {from, to, precision, basis} (a date window,
 * CAUSCORE), {date}, a bare ISO / astronomical-year string, or a number (a year). Anything else → null.
 */
export function timeOf(v: unknown, precision: number | null = null): TimePoint {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number' && Number.isFinite(v)) return { time: String(v), precision: 9 };
  if (typeof v === 'string') return { time: v, precision };
  if (!isRec(v)) return null;
  const t = optStr(v.time) ?? optStr(v.from) ?? optStr(v.date) ?? optStr(v.value);
  if (!t) return null;
  return { time: t, precision: typeof v.precision === 'number' ? v.precision : precision };
}

export interface Interval {
  start: TimePoint;
  end: TimePoint;
}

/** Interval from {start,end} (possibly nested under time/dates/interval), start_time/end_time, point_in_time or date. */
function intervalOf(o: Rec): Interval {
  const nested = [o.dates, o.time, o.interval].find((x) => isRec(x) && ('start' in x || 'end' in x)) as Rec | undefined;
  const src = nested ?? o;
  const precision = typeof src.precision === 'number' ? src.precision : null;
  const p = (k: string) => timeOf(src[k], precision);
  const start = p('start') ?? p('start_time') ?? p('begin') ?? p('point_in_time') ?? p('date') ?? (typeof o.time === 'string' ? timeOf(o.time, precision) : null);
  const end = p('end') ?? p('end_time');
  return { start, end };
}

function labelOf(o: Rec, fallback: string): string {
  const labels = isRec(o.labels) ? o.labels : {};
  for (const v of [o.label_fr, labels.fr, o.label, o.label_en, labels.en, o.name, ...Object.values(labels)]) {
    if (typeof v === 'string' && v.trim()) return v;
  }
  return fallback;
}

export interface NodeRef extends Interval {
  id: string;
  label: string;
  type: string | null;
}

/** An event or entity reference: a bare id, or {id|qid|event_id|key, label|labels, type|kind, dates}. */
export function nodeRefOf(v: unknown): NodeRef {
  if (!isRec(v)) {
    const id = v === null || v === undefined ? '' : String(v);
    return { id, label: id, type: null, start: null, end: null };
  }
  const id = String(v.id ?? v.qid ?? v.event_id ?? v.entity_id ?? v.key ?? '');
  const type = optStr(v.type) ?? optStr(v.event_type) ?? optStr(v.kind);
  return { id, label: labelOf(v, id), type, ...intervalOf(v) };
}

// ------------------------------------------------------------------ tiers, grades, checks

export const ALL_TIERS = ['T1', 'T2', 'T3', 'T4', 'T5', 'T6'] as const;
export type Tier = (typeof ALL_TIERS)[number];
const normTier = (v: unknown) => (typeof v === 'number' ? `T${v}` : typeof v === 'string' ? v.trim().toUpperCase().replace(/^TIER[_ -]?/, 'T') : null);

export const GRADES = ['A', 'B', 'C', 'D', 'E'] as const;
export type Grade = (typeof GRADES)[number];
export const gradeOf = (v: unknown): Grade | null => {
  const g = typeof v === 'string' ? v.trim().toUpperCase() : null;
  return g && (GRADES as readonly string[]).includes(g) ? (g as Grade) : null;
};

export const TEMPORAL = ['consistent', 'overlapping', 'inconsistent', 'undetermined'] as const;
export type TemporalCheck = (typeof TEMPORAL)[number] | 'unknown';

/** The time an endpoint was compared by (grading v2): an event start window, or an entity's whole existence span. */
export interface ComparedTime {
  kind: 'event_start' | 'entity_existence' | string;
  from: string | null;
  to: string | null;
  /** entity existence: inception (P571) start and dissolution end (null = still exists / not stated) */
  inception: string | null;
  end: string | null;
  basis: string | null;
}

function comparedTimeOf(v: unknown): ComparedTime | null {
  if (!isRec(v)) return null;
  const inc = isRec(v.inception) ? v.inception : null;
  const end = isRec(v.end) ? v.end : null;
  return {
    kind: optStr(v.kind) ?? 'event_start',
    from: optStr(v.from),
    to: optStr(v.to),
    inception: inc ? optStr(inc.from) : null,
    end: end ? (optStr(end.to) ?? optStr(end.from)) : null,
    basis: optStr(v.basis) ?? (inc ? optStr(inc.basis) : null),
  };
}

/** Known server reasons of a temporal check, in French (the server's text is kept when unknown). */
const TEMPORAL_REASON_FR: [RegExp, string][] = [
  [/entity dated by inception/i, 'entité datée par sa création (P571), pas un événement : toute sa durée d’existence est comparée, pas sa date de fondation'],
  [/claim time qualifier|claim'?s own time/i, 'date portée par la déclaration elle-même (qualificatif temporel)'],
  [/no start date in the KG for the cause and the effect/i, 'ni la cause ni l’effet n’ont de date de début dans le graphe de connaissances'],
  [/no start date in the KG for the cause/i, 'la cause n’a pas de date de début dans le graphe de connaissances'],
  [/no start date in the KG for the effect/i, 'l’effet n’a pas de date de début dans le graphe de connaissances'],
];
const reasonFr = (r: string | null) => (r ? (TEMPORAL_REASON_FR.find(([re]) => re.test(r))?.[1] ?? r) : null);

/**
 * Who the source says makes the claim (grading v2, CAUSFIX H3): Wikidata P3680 "statement supported by" (Wikidata
 * records that a party asserts it; Wikidata does not assert it itself), P5102 "nature of statement" (shown
 * verbatim), and a source's own doubtful coding (`scope.source_validity[].certainty = doubtful`).
 */
export interface Attribution {
  supportedBy: NodeRef[];
  nature: NodeRef[];
  doubtful: { code: string | null; label: string | null; field: string | null }[];
  note: string | null;
}

function attributionOf(o: Rec): Attribution | null {
  const a = isRec(o.attribution) ? o.attribution : {};
  const refs = (v: unknown) => (Array.isArray(v) ? v.map(nodeRefOf).filter((r) => r.id) : []);
  const scope = isRec(o.scope) ? o.scope : {};
  const sv = [...(Array.isArray(a.source_validity) ? a.source_validity : []), ...(Array.isArray(scope.source_validity) ? scope.source_validity : [])].filter(isRec);
  const seen = new Set<string>();
  const doubtful = sv
    .filter((x) => String(x.certainty ?? '').toLowerCase() === 'doubtful')
    .map((x) => ({ code: optStr(x.code) ?? optStr(x.value), label: optStr(x.label_fr) ?? optStr(x.label) ?? optStr(x.label_en), field: optStr(x.field) ?? optStr(x.variable) }))
    .filter((x) => {
      const k = JSON.stringify(x);
      return seen.has(k) ? false : (seen.add(k), true);
    });
  const out = { supportedBy: refs(a.supported_by), nature: refs(a.nature_of_statement), doubtful, note: optStr(a.note_fr) ?? optStr(a.note_en) };
  return out.supportedBy.length || out.nature.length || out.doubtful.length || out.note ? out : null;
}

/** The asserting source of a claim (nested `source` / `asserting_source` object, or flat fields of the claim). */
export interface AssertingSource {
  key: string;
  label: string | null;
  dataset: string | null;
  revision: string | null;
  statementId: string | null;
  rowSelector: string | null;
  rawSha256: string | null;
  citation: string | null;
  codebook: string | null;
  url: string | null;
  references: number | null;
  verification: string | null;
  /** Wikidata property the statement was made with (P828, P1542, P1478, P1536 …), from the selector */
  property: string | null;
}

/** {"doi": "10.1/x"} → "doi: 10.1/x"; {"file": "a.csv", "row": 4} → "file: a.csv · row: 4". */
function flat(v: unknown): string | null {
  if (typeof v === 'string') return v || null;
  if (typeof v === 'number') return String(v);
  if (isRec(v)) {
    const parts = Object.entries(v)
      .filter(([, x]) => x !== null && x !== undefined && x !== '')
      .map(([k, x]) => `${k}: ${Array.isArray(x) ? x.map(String).join(', ') : isRec(x) ? JSON.stringify(x) : String(x)}`);
    return parts.length ? parts.join(' · ') : null;
  }
  return null;
}

function sourceOf(o: Rec): AssertingSource | null {
  const nested = o.asserting_source ?? o.source;
  const s = isRec(nested) ? nested : o;
  const pick = (...ks: string[]) => {
    for (const k of ks) {
      const v = flat(s[k]);
      if (v) return v;
    }
    return null;
  };
  const key = typeof nested === 'string' ? nested : pick('source_key', 'key', 'source', 'name');
  if (!key) return null;
  const refs = s.references;
  const cb = s.codebook;
  return {
    key,
    label: pick('label', 'source_label', 'title'),
    dataset: pick('dataset', 'dataset_key'),
    revision: pick('source_revision', 'revision', 'dataset_revision'),
    statementId: pick('statement_id', 'statement_guid'),
    rowSelector: s.statement_id ? null : pick('row_selector', 'selector', 'row'),
    rawSha256: pick('raw_sha256', 'sha256'),
    citation: pick('citation', 'reference', 'doi', 'isbn'),
    codebook: isRec(cb) ? [optStr(cb.name), optStr(cb.version) && `version ${String(cb.version)}`, optStr(cb.term)].filter(Boolean).join(', ') || null : flat(cb),
    url: pick('url', 'href'),
    references: Array.isArray(refs) ? refs.length : typeof refs === 'number' ? refs : null,
    verification: pick('verification'),
    property: isRec(s.selector) ? optStr(s.selector.property) : null,
  };
}

function idList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => (isRec(x) ? String(x.claim_key ?? x.claim_id ?? x.id ?? '') : String(x))).filter(Boolean);
}

function mechanismOf(o: Rec): { text: string; lang: string | null } | null {
  const m = o.mechanism ?? o.mechanism_quote;
  if (typeof m === 'string' && m.trim()) return { text: m, lang: null };
  if (isRec(m)) {
    const t = m.text ?? m.quote;
    if (typeof t === 'string' && t.trim()) return { text: t, lang: optStr(m.lang) };
  }
  return null;
}

/** Shared normalisation of claims and candidates (everything but tier/grade). */
function baseOf(o: Rec, ctx: z.RefinementCtx) {
  const id = String(o.claim_key ?? o.claim_id ?? o.candidate_key ?? o.candidate_id ?? o.id ?? o.row_id ?? '');
  if (!id) ctx.addIssue({ code: 'custom', message: 'identifiant d’affirmation manquant' });
  const cause = nodeRefOf(o.cause ?? o.cause_id);
  const effect = nodeRefOf(o.effect ?? o.effect_id);
  if (!cause.id || !effect.id) ctx.addIssue({ code: 'custom', message: 'cause et effet requis' });
  const rel = o.relation ?? o.relation_type ?? o.predicate;
  const relation = typeof rel === 'string' ? rel : isRec(rel) ? String(rel.id ?? rel.key ?? '') : '';
  if (!relation) ctx.addIssue({ code: 'custom', message: 'relation causale manquante' });
  const rl = o.relation_label;
  return {
    id,
    cause,
    effect,
    relation,
    lane: optStr(o.lane),
    relationLabel: optStr(o.relation_label_fr) ?? (isRec(rl) ? (optStr(rl.fr) ?? optStr(rl.en)) : optStr(rl)),
    polarity: optStr(o.polarity),
    stance: (o.stance === 'denies' ? 'denies' : 'asserts') as 'asserts' | 'denies',
    source: sourceOf(o),
    mechanism: mechanismOf(o),
    scope: isRec(o.scope) ? o.scope : null,
    origin: optStr(o.origin),
    serverWording: optStr(o.wording_fr) ?? optStr(o.wording),
  };
}

// ------------------------------------------------------------------ claims

export const CausalClaim = JsonRecord.transform((o, ctx) => {
  const base = baseOf(o, ctx);
  if (!base.source) ctx.addIssue({ code: 'custom', message: 'source affirmante manquante : une affirmation causale sans source n’est jamais affichée' });
  const srcRec = isRec(o.source) ? o.source : isRec(o.asserting_source) ? o.asserting_source : {};
  const tierRaw = srcRec.tier ?? o.source_tier ?? o.tier;
  const tier = normTier(tierRaw);
  if (!tier || !['T1', 'T2', 'T3', 'T4'].includes(tier)) ctx.addIssue({ code: 'custom', message: `niveau de source T1–T4 attendu pour une affirmation causale (reçu ${JSON.stringify(tierRaw ?? null)})` });
  // grade: an object (CAUSCORE), a letter, or null = not graded yet (shown as such, never invented)
  const g = o.grade !== undefined ? o.grade : o.evidence_grade;
  const gr: Rec = isRec(g) ? g : {};
  const gradeRaw = isRec(g) ? g.grade : g;
  const grade = gradeOf(gradeRaw);
  if (gradeRaw !== null && gradeRaw !== undefined && grade === null) ctx.addIssue({ code: 'custom', message: `grade de preuve A–E attendu (reçu ${JSON.stringify(gradeRaw)})` });
  const tRaw = gr.temporal_check ?? o.temporal_check ?? (typeof o.temporal === 'string' ? o.temporal : undefined);
  const tStatus = typeof tRaw === 'string' && (TEMPORAL as readonly string[]).includes(tRaw.toLowerCase()) ? (tRaw.toLowerCase() as TemporalCheck) : 'unknown';
  const tw: Rec = isRec(gr.temporal) ? gr.temporal : isRec(o.temporal) ? o.temporal : {};
  // date windows the check used fill in the dates of cause/effect when the refs carry none
  const cause = base.cause.start ? base.cause : { ...base.cause, start: timeOf(tw.cause_start) };
  const effect = base.effect.start ? base.effect : { ...base.effect, start: timeOf(tw.effect_start) };
  const corr = gr.corroboration ?? o.corroboration;
  const corroboration = {
    count: typeof corr === 'number' ? corr : isRec(corr) && typeof corr.count === 'number' ? corr.count : null,
    claims: idList(gr.corroborating_claim_keys ?? (isRec(corr) ? corr.claims : undefined)),
    groups: Array.isArray(gr.upstream_groups) ? gr.upstream_groups.length : null,
  };
  const reasons = Array.isArray(gr.reasons)
    ? gr.reasons.filter(isRec).map((r) => ({ rule: optStr(r.rule_id) ?? '', text: optStr(r.fr) ?? optStr(r.en) ?? '' }))
    : [];
  const entityDated = (Array.isArray(tw.entity_dated_by_inception) ? tw.entity_dated_by_inception : []).map(String).filter((x) => x === 'cause' || x === 'effect') as ('cause' | 'effect')[];
  const inceptionReason = reasons.find((r) => r.rule === 'temporal.entity_dated_by_inception')?.text ?? null;
  const rl = o.relation_inverse_label;
  return {
    ...base,
    cause,
    effect,
    source: base.source!,
    tier: (tier ?? 'T4') as Tier,
    grade,
    gradeRules: idList(gr.rule_ids ?? gr.rules ?? o.grade_rules),
    gradeReasons: reasons,
    gradeVersion: optStr(gr.rules_version),
    temporal: {
      status: tStatus,
      // the server's reason (e.g. an entity dated by its inception, compared through its existence span)
      detail: reasonFr(optStr(tw.reason) ?? optStr(gr.temporal_detail)) ?? (entityDated.length ? inceptionReason : null),
      rule: optStr(tw.rule),
      entityDated,
      causeTime: comparedTimeOf(tw.cause_time),
      effectTime: comparedTimeOf(tw.effect_time),
    },
    attribution: attributionOf(o),
    assertedOn: o.asserted_on === 'cause' || o.asserted_on === 'effect' ? (o.asserted_on as 'cause' | 'effect') : null,
    inverseLabel: isRec(rl) ? (optStr(rl.fr) ?? optStr(rl.en)) : optStr(rl),
    corroboration,
    contestedBy: idList(gr.contested_by ?? o.contested_by),
    contests: idList(o.contests),
    reviewStatus: optStr(o.review_status),
  };
});
export type CausalClaim = z.output<typeof CausalClaim>;

export const CausalCandidate = JsonRecord.transform((o, ctx) => {
  const base = baseOf(o, ctx);
  const tierRaw = o.tier ?? o.source_tier ?? (isRec(o.source) ? o.source.tier : undefined);
  const tier = normTier(tierRaw);
  if (tier !== 'T5' && tier !== 'T6') ctx.addIssue({ code: 'custom', message: `un candidat doit être de niveau T5/T6 (reçu ${JSON.stringify(tierRaw ?? null)})` });
  for (const k of ['grade', 'evidence_grade']) if (o[k] !== undefined && o[k] !== null) ctx.addIssue({ code: 'custom', message: 'un candidat ne porte jamais de grade de preuve' });
  const ex = isRec(o.extractor) ? o.extractor : {};
  return {
    ...base,
    tier: (tier ?? 'T6') as Tier,
    extractor: optStr(ex.id) ?? optStr(o.extractor_id) ?? optStr(o.extractor),
    extractorVersion: optStr(ex.version) ?? optStr(o.extractor_version),
    sentence: optStr(o.source_sentence) ?? optStr(o.sentence) ?? optStr(o.text),
    sourceRevision: optStr(o.source_revision) ?? base.source?.revision ?? null,
    selector: flat(o.selector) ?? (isRec(o.source) ? flat(o.source.selector) : null),
    promotionStatus: optStr(o.promotion_status) ?? 'not_promoted',
  };
});
export type CausalCandidate = z.output<typeof CausalCandidate>;

// ------------------------------------------------------------------ envelopes

/** Server warnings the screen already states in French (claim ≠ causation banner, chain banner, candidate label). */
const SAID_ON_SCREEN = new Set(['claims_not_causation', 'chain_is_composition', 'unsourced_candidates']);

/** Envelope-or-bare: `{data, warnings}` (src/reva/api/schemas.py Envelope) or the data itself. */
function unwrap(body: unknown): { data: unknown; warnings: string[] } {
  if (isRec(body) && 'data' in body) {
    const ws = Array.isArray(body.warnings) ? body.warnings.filter(isRec).filter((w) => typeof w.code === 'string') : [];
    const shown = ws.filter((w) => !SAID_ON_SCREEN.has(String(w.code)));
    return { data: body.data, warnings: warningsFr(shown.map((w) => ({ code: String(w.code), message: String(w.message ?? '') }))) };
  }
  return { data: body, warnings: [] };
}

async function get(path: string, query?: Query, signal?: AbortSignal) {
  const res = await request<unknown>('GET', path, { query, signal });
  return unwrap(res.body);
}

const ClaimList = z.array(CausalClaim);

/** The claim array of a list response: bare array, or under claims / causes / effects / items. */
function claimsIn(data: unknown, keys: string[]): unknown {
  if (Array.isArray(data)) return data;
  if (isRec(data)) for (const k of keys) if (Array.isArray(data[k])) return data[k];
  return data;
}

// ------------------------------------------------------------------ event

export interface ContextEdge {
  relation: string;
  property: string | null;
  other: NodeRef;
  /** the event is the statement's subject (event —relation→ other) or its object (other —relation→ event) */
  eventIsSubject: boolean;
}

const sumCounts = (v: unknown, skip?: string): number | null => {
  if (typeof v === 'number') return v;
  if (!isRec(v)) return null;
  let n = 0;
  for (const [k, x] of Object.entries(v)) if (k !== skip) n += typeof x === 'number' ? x : (sumCounts(x) ?? 0);
  return n;
};
/** The server counts coded outcomes apart (family bucket "outcome", APIFIX): never causes or consequences. */
const outcomeCount = (...sides: unknown[]): number => sides.reduce<number>((n, v) => n + (isRec(v) ? (sumCounts(v.outcome) ?? 0) : 0), 0);

/** Context (non-causal) relations giving the event's places and participants (KG statements). */
const PLACE_PIDS = new Set(['P276', 'P17', 'P30', 'P131']);
const PARTICIPANT_PIDS = new Set(['P710', 'P1344']);

export const CausalEvent = JsonRecord.transform((o0, ctx) => {
  const o: Rec = isRec(o0.event) ? { ...o0, ...o0.event, id: o0.id ?? o0.event.id } : o0;
  const id = String(o.id ?? o.qid ?? o.event_id ?? '');
  if (!id) ctx.addIssue({ code: 'custom', message: 'identifiant d’événement manquant' });
  const refs = (v: unknown) => (Array.isArray(v) ? v.map(nodeRefOf).filter((r) => r.id) : []);
  const ctxRaw = [o.context, o.context_edges, o.context_relations].find(Array.isArray) as unknown[] | undefined;
  const context: ContextEdge[] = (ctxRaw ?? []).filter(isRec).map((c) => {
    const subj = c.subject !== undefined ? nodeRefOf(c.subject) : null;
    const obj = c.object !== undefined ? nodeRefOf(c.object) : null;
    const eventIsSubject = !!subj && subj.id === id;
    const other = subj && obj ? (eventIsSubject ? obj : subj) : nodeRefOf(c.other ?? c.entity ?? c.target ?? c.to ?? c);
    const lab = c.label ?? c.relation_label;
    return {
      relation: optStr(c.relation_label_fr) ?? (isRec(lab) ? (optStr(lab.fr) ?? optStr(lab.en)) : optStr(lab)) ?? optStr(c.relation) ?? optStr(c.property) ?? 'contexte',
      property: optStr(c.property),
      other,
      eventIsSubject,
    };
  });
  const participants = Array.isArray(o.participants)
    ? o.participants.map((p) => {
        const pr: Rec = isRec(p) ? p : { id: p };
        const ent = nodeRefOf(isRec(pr.entity) ? pr.entity : pr);
        const role = pr.role ?? pr.roles;
        return { ...ent, role: Array.isArray(role) ? role.map(String).join(', ') : optStr(role), side: optStr(pr.side), fromContext: false };
      })
    : context
        .filter((c) => c.property && PARTICIPANT_PIDS.has(c.property))
        .map((c) => ({ ...c.other, role: c.relation, side: null as string | null, fromContext: true }));
  const placesGiven = Array.isArray(o.places) || Array.isArray(o.locations);
  const places = placesGiven ? refs(o.places ?? o.locations) : context.filter((c) => c.property && PLACE_PIDS.has(c.property)).map((c) => c.other);
  const type = o.type ?? o.event_type;
  const typeObj = isRec(type) ? type : null;
  const counts = isRec(o.counts) ? o.counts : o;
  const descs = isRec(o.descriptions) ? o.descriptions : {};
  const prov = isRec(o.provenance) ? o.provenance : {};
  const cand = o.candidate_count ?? (isRec(o.counts) ? o.counts.candidates : o.candidates);
  return {
    id,
    label: labelOf(o, id),
    description: optStr(o.description) ?? optStr(descs.fr) ?? optStr(descs.en),
    type: typeObj ? String(typeObj.id ?? typeObj.key ?? '') : (optStr(type) ?? optStr(o.kind)),
    typeLabel: typeObj ? labelOf(typeObj, String(typeObj.id ?? '')) : (optStr(o.type_label_fr) ?? optStr(o.type_label)),
    ...intervalOf(o),
    places,
    placesGiven: placesGiven || context.length > 0,
    participants,
    participantsGiven: Array.isArray(o.participants) || context.length > 0,
    partOf: refs(o.part_of),
    context,
    contextTruncated: o.context_truncated === true,
    nCauses: sumCounts(counts.causes, 'outcome'),
    nEffects: sumCounts(counts.effects, 'outcome'),
    nOutcomes: outcomeCount(counts.causes, counts.effects),
    nCandidates: typeof cand === 'number' ? cand : null,
    source: optStr(o.source_key) ?? optStr(prov.source_key),
    origin: optStr(o.origin) ?? optStr(prov.origin) ?? (Array.isArray(o.origins) && o.origins.length === 1 ? optStr(o.origins[0]) : null),
  };
});
export type CausalEvent = z.output<typeof CausalEvent>;

export async function getCausalEvent(id: string, signal?: AbortSignal) {
  const path = `/v1/causal/events/${enc(id)}`;
  const { data, warnings } = await get(path, undefined, signal);
  return { event: parseWith(CausalEvent, data, path), warnings };
}

// ------------------------------------------------------------------ event card (agent EVENTS, /v1/events/{id})

export interface CardFact {
  ref: NodeRef;
  role: string | null;
  side: string | null;
  /** statement keys the fact rests on (each a cited KG statement) */
  statements: string[];
  source: string | null;
}

/**
 * The EVENTS event card (src/reva/api/routes/events.py): types (taxonomy, fr labels), interval with precision,
 * places and participants with roles/sides, each from cited statements. Tolerant: absent members stay absent.
 */
export const EventsCard = JsonRecord.transform((o, ctx) => {
  const id = optStr(o.id) ?? '';
  if (!id) ctx.addIssue({ code: 'custom', message: 'identifiant d’événement manquant' });
  const types = isRec(o.types) ? o.types : {};
  const primary = (Array.isArray(types.primary) ? types.primary : []).filter(isRec);
  const iv = isRec(o.interval) ? o.interval : {};
  const tv = (v: unknown): TimePoint => (isRec(v) ? timeOf(v.time, typeof v.precision === 'number' ? v.precision : null) : null);
  const start = tv(iv.start) ?? tv(iv.point_in_time) ?? tv(iv.inception);
  const end = tv(iv.end) ?? tv(iv.dissolved) ?? tv(iv.episode_end);
  const prov = (x: Rec): { key: string | null; source: string | null } => {
    const p = isRec(x.provenance) ? x.provenance : {};
    return { key: optStr(p.statement_key), source: optStr(p.source_key) };
  };
  const places: CardFact[] = (Array.isArray(o.places) ? o.places : []).filter(isRec).flatMap((pl) => {
    if (!isRec(pl.place)) return [];
    const pv = prov(pl);
    return [{ ref: nodeRefOf(pl.place), role: optStr(pl.relation), side: null, statements: pv.key ? [pv.key] : [], source: pv.source }];
  });
  const participants: CardFact[] = (Array.isArray(o.participants) ? o.participants : []).filter(isRec).flatMap((pa) => {
    if (!isRec(pa.participant)) return [];
    const role = isRec(pa.role) ? (optStr(pa.role.label_fr) ?? optStr(pa.role.label_en) ?? optStr(pa.role.id)) : optStr(pa.role);
    const sts = (Array.isArray(pa.statements) ? pa.statements : []).filter(isRec);
    return [
      {
        ref: nodeRefOf(pa.participant),
        role,
        side: optStr(pa.side),
        statements: sts.map((x) => optStr(x.statement_key)).filter((x): x is string => !!x),
        source: optStr(sts[0]?.source_key),
      },
    ];
  });
  const labels = isRec(o.labels) ? o.labels : {};
  const descs = isRec(o.descriptions) ? o.descriptions : {};
  return {
    id,
    label: optStr(labels.fr) ?? optStr(o.label) ?? id,
    description: optStr(descs.fr) ?? optStr(descs.en),
    types: primary.map((t) => optStr(t.label_fr) ?? optStr(t.label_en) ?? optStr(t.id) ?? '').filter(Boolean),
    start,
    end,
    dateBasis: optStr(iv.basis),
    dateConsistency: optStr(iv.consistency),
    places,
    participants,
    truncated: o.truncated === true,
  };
});
export type EventsCard = z.output<typeof EventsCard>;

export async function getEventsCard(id: string, signal?: AbortSignal) {
  const path = `/v1/events/${enc(id)}`;
  const { data, warnings } = await get(path, undefined, signal);
  return { card: parseWith(EventsCard, data, path), warnings };
}

// ------------------------------------------------------------------ causes / effects

export interface ClaimsResult {
  claims: CausalClaim[];
  truncated: boolean;
  warnings: string[];
}

export async function getCausalClaims(id: string, direction: 'causes' | 'effects', signal?: AbortSignal): Promise<ClaimsResult> {
  const path = `/v1/causal/events/${enc(id)}/${direction}`;
  const { data, warnings } = await get(path, { limit: 200 }, signal);
  const claims = parseWith(ClaimList, claimsIn(data, ['claims', direction, 'items', 'results']), path);
  const truncated = isRec(data) && (data.truncated === true || (isRec(data.search) && data.search.truncated === true));
  return { claims, truncated, warnings };
}

// ------------------------------------------------------------------ why

export const WHY_GROUPS = ['immediate', 'underlying', 'contributing', 'coded', 'general', 'reaction', 'prevention', 'outcome'] as const;
export type WhyGroup = (typeof WHY_GROUPS)[number];

const WHY_KEYS: Record<WhyGroup, string[]> = {
  immediate: ['immediate', 'immediate_causes', 'triggers'],
  underlying: ['underlying', 'underlying_causes'],
  contributing: ['contributing', 'contributing_factors'],
  coded: ['coded', 'coded_triggers'],
  general: ['general', 'other', 'has_cause'],
  reaction: ['reaction', 'reaction_to'],
  prevention: ['prevention', 'prevented_or_delayed'],
  outcome: ['outcome', 'coded_outcome'],
};

/**
 * A dataset's coded outcome (relations.yaml `coded_outcome`, chain_excluded) codes HOW an event ended: it is an
 * outcome, never a cause. The server files it in the "coded" lane of /why (for the outcome record); the screen
 * shows it apart, worded as an outcome.
 */
export const isOutcomeClaim = (c: { relation: string }) => c.relation === 'coded_outcome';

export interface WhyResult {
  groups: Record<WhyGroup, CausalClaim[]>;
  dissent: CausalClaim[];
  note: string | null;
  warnings: string[];
}

export async function getCausalWhy(id: string, signal?: AbortSignal): Promise<WhyResult> {
  const path = `/v1/causal/events/${enc(id)}/why`;
  const { data, warnings } = await get(path, undefined, signal);
  const d: Rec = isRec(data) ? data : {};
  // lanes as a list [{lane, label, claims}] (CAUSCORE) or an object {immediate: [...], ...}
  let src: Rec = isRec(d.lanes) ? d.lanes : isRec(d.groups) ? d.groups : d;
  if (Array.isArray(d.lanes)) {
    src = {};
    for (const l of d.lanes.filter(isRec)) if (typeof l.lane === 'string') src[l.lane] = l.claims;
  }
  const groups = {} as Record<WhyGroup, CausalClaim[]>;
  for (const g of WHY_GROUPS) {
    const k = WHY_KEYS[g].find((key) => Array.isArray(src[key]) || (isRec(src[key]) && Array.isArray((src[key] as Rec).claims)));
    const v = k ? src[k] : [];
    groups[g] = parseWith(ClaimList, isRec(v) ? v.claims : v, `${path} (${k ?? g})`);
  }
  // coded outcomes are not causes: out of every causal lane, into their own group
  for (const g of WHY_GROUPS) {
    if (g === 'outcome') continue;
    groups.outcome.push(...groups[g].filter(isOutcomeClaim));
    groups[g] = groups[g].filter((c) => !isOutcomeClaim(c));
  }
  const dis = isRec(d.dissent) ? (d.dissent.claims ?? []) : (d.dissent ?? []);
  const dissent = Array.isArray(dis) ? parseWith(ClaimList, dis, `${path} (dissent)`) : [];
  return { groups, dissent, note: optStr(d.summary_fr) ?? optStr(d.note), warnings };
}

// ------------------------------------------------------------------ chain

/** Evidence of an identity hop: one `events:same_as` statement (shared identifier or provider crosswalk). */
export interface SameAsEvidence {
  left: string;
  right: string;
  rule: string;
  statementKey: string;
  independentSources: boolean | null;
  evidenceType: string | null;
  evidence: Record<string, unknown>;
  /** the identity rule's own explanation (registry/events/same_as_rules.yaml), when the server sends it */
  note?: string | null;
}

/**
 * One hop of a chain: from → to, asserted by one or more claims (each with its own source and grade) — or an
 * identity hop (`identity` set, no claims): two records of the same event in two sources, joined by an evidenced
 * same_as link; not a causal claim and not counted as a hop.
 */
export interface ChainHop {
  from: NodeRef;
  to: NodeRef;
  claims: CausalClaim[];
  serverGrade: Grade | null;
  identity?: SameAsEvidence[] | null;
}

export interface ChainView {
  hops: ChainHop[];
  serverGrade: Grade | null;
  wording: string | null;
}

export interface ChainResult {
  from: NodeRef | null;
  to: NodeRef | null;
  found: boolean;
  chains: ChainView[];
  maxHops: number | null;
  note: string | null;
  notFoundReason: string | null;
  truncated: boolean;
  warnings: string[];
  /** identical paths the server returned more than once, merged here (counted, never hidden) */
  duplicatesMerged: number;
  /** what the search skipped, as the server reports it (class items, quarantined claims, low grades…) */
  skipped: { classNodes: number; quarantined: number; belowMinGrade: number; ungraded: number; classSample: string[] };
  minGrade: Grade | null;
}

/** Known server "not found" reasons in French (the server text is kept when unknown). */
const NOT_FOUND_FR: [RegExp, (m: RegExpExecArray) => string][] = [
  [
    /^(.+?) (?:is a coded outcome record|are coded outcome records)\b/i,
    (m) => `${m[1]} : issue codée (comment un événement s’est terminé selon un jeu de données) — une issue n’est jamais composée dans une chaîne causale`,
  ],
  [/^no causal chain within max_hops among claims graded at least ([A-E])/i, (m) => `aucune chaîne d’affirmations causales dans la limite de maillons parmi les affirmations de grade ${m[1]} ou mieux`],
  [/records of the same event/i, () => 'les deux extrémités sont deux enregistrements du même événement (lien same_as fondé sur une preuve) : il n’y a pas de chaîne causale à chercher'],
  [/unknown (event|entity|id)/i, () => 'identifiant inconnu des affirmations causales chargées'],
];
export function notFoundReasonFr(r: string | null): string | null {
  if (!r) return null;
  for (const [re, f] of NOT_FOUND_FR) {
    const m = re.exec(r);
    if (m) return f(m);
  }
  return r;
}

const hopSignature = (h: ChainHop) => `${h.from.id}>${h.to.id}:${h.identity ? `id:${h.identity.map((x) => x.statementKey).join(',')}` : h.claims.map((c) => c.id).join(',')}`;

export async function getCausalChain(from: string, to: string, opts: { max_hops?: number } = {}, signal?: AbortSignal): Promise<ChainResult> {
  const path = '/v1/causal/chain';
  const { data, warnings } = await get(path, { from, to, max_hops: opts.max_hops }, signal);
  const d: Rec = isRec(data) ? data : {};
  const rawChains = Array.isArray(d.paths) ? d.paths : Array.isArray(d.chains) ? d.chains : [];
  const allChains: ChainView[] = rawChains.map((c, i) => {
    const co: Rec = isRec(c) ? c : {};
    const hopsRaw = (Array.isArray(c) ? c : Array.isArray(co.hops) ? co.hops : Array.isArray(co.claims) ? co.claims : []) as unknown[];
    const hops: ChainHop[] = hopsRaw.map((h, j) => {
      const hr: Rec = isRec(h) ? h : {};
      if (hr.kind === 'identity') {
        // same event in two sources (evidenced same_as): no claim, no grade, shown with its evidence
        const identity = (Array.isArray(hr.same_as) ? hr.same_as : []).filter(isRec).map((x) => {
          const ev: Rec = isRec(x.evidence) ? x.evidence : {};
          return {
            left: String(x.left ?? ''),
            right: String(x.right ?? ''),
            rule: String(x.rule ?? ''),
            statementKey: String(x.statement_key ?? ''),
            independentSources: typeof x.independent_sources === 'boolean' ? x.independent_sources : null,
            evidenceType: optStr(ev.type),
            evidence: ev,
            note: optStr(ev.rule_note),
          };
        });
        return { from: nodeRefOf(hr.from), to: nodeRefOf(hr.to), claims: [], serverGrade: null, identity };
      }
      // a hop holding its claims (CAUSCORE), or a hop that is itself a claim
      const claims = parseWith(ClaimList, Array.isArray(hr.claims) ? hr.claims : [isRec(hr.claim) ? hr.claim : h], `${path} (chaîne ${i + 1}, maillon ${j + 1})`);
      const first = claims[0];
      return {
        from: hr.from !== undefined ? nodeRefOf(hr.from) : (first?.cause ?? nodeRefOf(null)),
        to: hr.to !== undefined ? nodeRefOf(hr.to) : (first?.effect ?? nodeRefOf(null)),
        claims,
        serverGrade: gradeOf(hr.hop_grade),
      };
    });
    return { hops, serverGrade: gradeOf(co.chain_grade ?? co.grade ?? co.weakest_grade), wording: optStr(co.wording_fr) ?? optStr(co.wording) };
  });
  // the server may list the same path twice (same hops, same claims, same identity links): show it once, say so
  const seen = new Set<string>();
  const chains = allChains.filter((ch) => {
    const sig = ch.hops.map(hopSignature).join('|');
    return seen.has(sig) ? false : (seen.add(sig), true);
  });
  const search: Rec = isRec(d.search) ? d.search : {};
  const num = (v: unknown) => (typeof v === 'number' ? v : 0);
  return {
    from: d.from === undefined || d.from === null ? null : nodeRefOf(d.from),
    to: d.to === undefined || d.to === null ? null : nodeRefOf(d.to),
    found: typeof d.found === 'boolean' ? d.found : chains.length > 0,
    chains,
    maxHops: typeof d.max_hops === 'number' ? d.max_hops : typeof search.max_hops === 'number' ? search.max_hops : null,
    note: optStr(d.note_fr) ?? optStr(d.note),
    notFoundReason: notFoundReasonFr(optStr(d.not_found_reason)),
    truncated: d.truncated === true || search.truncated === true,
    warnings,
    // identical paths: merged by the server since APIFIX (search.duplicate_paths_merged), still merged here too
    duplicatesMerged: allChains.length - chains.length + num(search.duplicate_paths_merged),
    skipped: {
      classNodes: num(search.class_nodes_skipped),
      quarantined: num(search.quarantined_skipped),
      belowMinGrade: num(search.below_min_grade_skipped),
      ungraded: num(search.ungraded_skipped),
      classSample: Array.isArray(search.class_nodes_sample) ? search.class_nodes_sample.map((x) => (isRec(x) ? String(x.label ?? x.id ?? '') : String(x))).filter(Boolean) : [],
    },
    minGrade: gradeOf(search.min_grade),
  };
}

// ------------------------------------------------------------------ candidates

export async function getCausalCandidates(event: string, signal?: AbortSignal) {
  const path = '/v1/causal/candidates';
  const { data, warnings } = await get(path, { event, limit: 100 }, signal);
  const candidates = parseWith(z.array(CausalCandidate), claimsIn(data, ['candidates', 'items', 'results']), path);
  return { candidates, warnings };
}
