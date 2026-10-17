/**
 * Pure view model of the "Causes & conséquences" screen (docs/roadmap/v3_causality.md).
 *
 * REVA asserts no causation: every label here attributes a relation to its asserting source ("selon …").
 * Relation families, tiers, grades and temporal checks come from the server; this module only maps them to
 * words and styles, groups claims, lays out the graph and the timeline, and computes the weakest-link
 * grade of a chain composed of distinct claims (the declared rule: chain grade = weakest hop).
 */
import type { Citation } from '../lib/citations';
import { isWikidataSource, wikidataRevisionUrl, wikidataStatementUrl } from '../lib/citations';
import { formatKgTime, formatYear, yearOf } from '../lib/time';
import type { CausalCandidate, CausalClaim, Grade, NodeRef, Tier, TimePoint, WhyGroup } from './api';
import { GRADES } from './api';

// ------------------------------------------------------------------ relation families

export const FAMILIES = ['immediate', 'underlying', 'contributing', 'coded_trigger', 'coded_outcome', 'general'] as const;
export type Family = (typeof FAMILIES)[number];

/** Closed relation vocabulary (v3_causality.md) → family. Unknown relations fall in `general`. */
const RELATION_FAMILY: Record<string, Family> = {
  immediate_cause: 'immediate',
  precipitated: 'immediate',
  underlying_cause: 'underlying',
  contributing_factor: 'contributing',
  enabled: 'contributing',
  coded_trigger: 'coded_trigger',
  coded_outcome: 'coded_outcome',
  has_cause: 'general',
  has_effect: 'general',
  reaction_to: 'general',
  prevented_or_delayed: 'general',
};

export function familyOf(c: Pick<CausalClaim, 'relation'>): Family {
  return RELATION_FAMILY[c.relation] ?? 'general';
}

export const FAMILY_STYLE: Record<Family, { label: string; short: string; color: string; line: 'solid' | 'dashed' | 'dotted'; arrow: string }> = {
  immediate: { label: 'cause immédiate (déclencheur)', short: 'immédiate', color: '#d9480f', line: 'solid', arrow: 'triangle' },
  underlying: { label: 'cause profonde', short: 'profonde', color: '#6741d9', line: 'dashed', arrow: 'triangle' },
  contributing: { label: 'facteur contributif', short: 'contributif', color: '#1971c2', line: 'dotted', arrow: 'triangle' },
  coded_trigger: { label: 'déclencheur codé (jeu de données expert)', short: 'déclencheur codé', color: '#c92a2a', line: 'solid', arrow: 'triangle-tee' },
  coded_outcome: { label: 'issue codée (jeu de données expert)', short: 'issue codée', color: '#2b8a3e', line: 'solid', arrow: 'triangle-backcurve' },
  general: { label: 'relation causale affirmée', short: 'affirmée', color: '#5c677d', line: 'solid', arrow: 'triangle' },
};

/** French wording of a relation, read cause → effect, always attributed by the caller. */
const RELATION_FR: Record<string, string> = {
  // read cause → effect, as registry/causal/relations.yaml labels them (the server's relation_label wins)
  has_cause: 'a causé',
  has_effect: 'a pour effet',
  immediate_cause: 'cause immédiate de',
  underlying_cause: 'cause profonde de',
  contributing_factor: 'facteur contributif de',
  enabled: 'a rendu possible',
  precipitated: 'a précipité',
  prevented_or_delayed: 'a empêché ou retardé',
  reaction_to: 'a provoqué la réaction',
  coded_trigger: 'est le déclencheur codé de',
  coded_outcome: 'a pour issue codée',
};
export function relationFr(c: Pick<CausalClaim, 'relation' | 'relationLabel'>): string {
  return c.relationLabel ?? RELATION_FR[c.relation] ?? c.relation.replace(/_/g, ' ');
}

/** "A — cause immédiate → B" style sentence, cause first (the source's direction, not REVA's). */
export function claimSentence(c: CausalClaim): string {
  const inhibits = c.polarity === 'inhibits' ? ' (effet inhibiteur selon la source)' : '';
  const denies = c.stance === 'denies' ? 'lien nié : ' : '';
  return `${c.cause.label} → ${c.effect.label} : ${denies}${relationFr(c)}${inhibits}`;
}

// ------------------------------------------------------------------ tiers, grades, temporal checks

export const TIER_FR: Record<Tier, string> = {
  T1: 'T1 — jeu de données codé par des experts (codebook)',
  T2: 'T2 — référence savante citée',
  T3: 'T3 — déclaration Wikidata avec références',
  T4: 'T4 — déclaration Wikidata sans référence',
  T5: 'T5 — candidat extrait de Wikipédia (non sourcé)',
  T6: 'T6 — candidat proposé par un modèle (non sourcé)',
};

export const GRADE_STYLE: Record<Grade, { width: number; opacity: number; rule: string }> = {
  A: { width: 6, opacity: 1, rule: 'source T1/T2, chronologie cohérente et corroborée' },
  B: { width: 4.5, opacity: 0.85, rule: 'source T1/T2, ou T3 corroborée' },
  C: { width: 3, opacity: 0.7, rule: 'déclaration Wikidata référencée (T3), source unique' },
  D: { width: 2, opacity: 0.5, rule: 'déclaration Wikidata sans référence (T4)' },
  E: { width: 1.2, opacity: 0.35, rule: 'incohérence temporelle, ou contestée sans corroboration' },
};

/** Style of a claim the grader has not graded yet (never drawn as if graded). */
export const UNGRADED_STYLE = { width: 1, opacity: 0.3, rule: 'pas encore gradée par le service de notation' };

/** Weakest grade (E is the weakest). Empty → null. */
export function weakestGrade(grades: Grade[]): Grade | null {
  let worst: Grade | null = null;
  for (const g of grades) if (worst === null || GRADES.indexOf(g) > GRADES.indexOf(worst)) worst = g;
  return worst;
}

/** Best grade (A is the best). Empty → null. */
export function bestGrade(grades: Grade[]): Grade | null {
  let best: Grade | null = null;
  for (const g of grades) if (best === null || GRADES.indexOf(g) < GRADES.indexOf(best)) best = g;
  return best;
}

const graded = (gs: (Grade | null)[]) => gs.filter((g): g is Grade => g !== null);

/** Grade of one hop of a chain: the best grade among the claims asserting it (CAUSCORE rule). */
export function hopGrade(h: { claims: { grade: Grade | null }[]; serverGrade: Grade | null }): Grade | null {
  return bestGrade(graded(h.claims.map((c) => c.grade))) ?? h.serverGrade;
}

/**
 * Grade of a chain: its weakest hop. A summary grade sent by the server can only make it weaker, never
 * stronger; a hop without any graded claim makes the chain ungradable (null).
 */
export function chainGrade(ch: { hops: { claims: { grade: Grade | null }[]; serverGrade: Grade | null; identity?: unknown[] | null }[]; serverGrade: Grade | null }): Grade | null {
  // identity hops (same event in two sources, same_as) are not causal hops: they carry no grade
  const hops = ch.hops.filter((h) => !h.identity).map(hopGrade);
  if (hops.length === 0 || hops.some((g) => g === null)) return null;
  return weakestGrade(graded([...hops, ch.serverGrade]));
}

export const TEMPORAL_FR: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'none' }> = {
  consistent: { label: 'chronologie cohérente (la cause précède l’effet, à la précision des dates)', tone: 'ok' },
  overlapping: { label: 'périodes qui se chevauchent (antériorité non établie)', tone: 'warn' },
  inconsistent: { label: 'incohérence temporelle : la cause commence après l’effet', tone: 'bad' },
  undetermined: { label: 'dates manquantes : antériorité non vérifiable', tone: 'none' },
  unknown: { label: 'vérification temporelle non fournie par le serveur', tone: 'none' },
};

export const REVIEW_FR: Record<string, string> = {
  unreviewed: 'non relue par un mainteneur',
  reviewed: 'relue par un mainteneur',
};

// ------------------------------------------------------------------ sources

export function sourceName(c: Pick<CausalClaim, 'source'>): string {
  const s = c.source;
  if (s.label) return s.label;
  if (isWikidataSource(s.key)) return 'Wikidata';
  return s.key;
}

/** "selon ICB (T1)" — the attribution every causal sentence carries. */
export function selon(c: Pick<CausalClaim, 'source' | 'tier'>): string {
  return `selon ${sourceName(c)} (${c.tier})`;
}

const shortSha = (s: string | null) => (s ? `${s.slice(0, 12)}…` : '');

/** Citation chip of a claim's asserting source: statement or dataset row, revision, raw artifact. */
export function citeClaim(c: CausalClaim | CausalCandidate): Citation {
  const s = c.source;
  if (!s) {
    return { label: 'source non déclarée', source: '', href: null, revision: null, revisionHref: null, community: false, fixture: c.origin === 'test_fixture', details: [] };
  }
  const wd = isWikidataSource(s.key);
  const id = s.statementId ?? s.rowSelector ?? '';
  const safeUrl = s.url && /^https:\/\//.test(s.url) ? s.url : null;
  return {
    label: `${wd ? 'Wikidata' : s.key}${s.dataset && !wd ? `/${s.dataset}` : ''}${id ? ` ${id}` : ''}${s.revision ? ` · rév. ${s.revision}` : ''}`,
    source: s.key,
    href: safeUrl ?? (wd ? wikidataStatementUrl(s.statementId, c.cause.id) : null),
    revision: s.revision,
    revisionHref: wd ? wikidataRevisionUrl(s.revision) : null,
    community: wd,
    fixture: c.origin === 'test_fixture',
    details: [
      s.statementId ? `déclaration ${s.statementId}` : '',
      s.rowSelector ? `ligne du jeu de données : ${s.rowSelector}` : '',
      s.dataset ? `jeu de données / révision : ${s.dataset}` : '',
      s.citation ? `référence (verbatim) : ${s.citation}` : '',
      s.rawSha256 ? `artefact brut sha256 ${shortSha(s.rawSha256)}` : '',
      s.references !== null ? `${s.references} référence(s) propre(s) à la déclaration` : '',
    ].filter(Boolean),
  };
}

// ------------------------------------------------------------------ why groups

export const WHY_FR: Record<WhyGroup, string> = {
  immediate: 'Causes immédiates (déclencheurs)',
  underlying: 'Causes profondes',
  contributing: 'Facteurs contributifs',
  coded: 'Déclencheurs codés par des jeux de données experts',
  general: 'Autres causes affirmées',
  reaction: 'Réaction à (selon la source)',
  prevention: 'Ce qui l’a empêché ou retardé (selon la source)',
};

const RELATION_LANE: Record<string, WhyGroup> = {
  immediate_cause: 'immediate',
  precipitated: 'immediate',
  underlying_cause: 'underlying',
  contributing_factor: 'contributing',
  enabled: 'contributing',
  coded_trigger: 'coded',
  coded_outcome: 'coded',
  reaction_to: 'reaction',
  prevented_or_delayed: 'prevention',
};

/** Group causes by why-lane (the server's `lane`, else the relation) — used when /why is not deployed.
 *  A source *denying* a link is not a cause: it goes to the dissent panel, never into a lane. */
export function groupWhy(claims: CausalClaim[]): Record<WhyGroup, CausalClaim[]> {
  const out: Record<WhyGroup, CausalClaim[]> = { immediate: [], underlying: [], contributing: [], coded: [], general: [], reaction: [], prevention: [] };
  for (const c of claims) {
    if (c.stance === 'denies') continue;
    const lane = c.lane && c.lane in out ? (c.lane as WhyGroup) : (RELATION_LANE[c.relation] ?? 'general');
    out[lane].push(c);
  }
  for (const g of Object.keys(out) as WhyGroup[]) out[g].sort(byGrade);
  return out;
}

const gradeRank = (g: Grade | null) => (g === null ? GRADES.length : GRADES.indexOf(g));

export function byGrade(a: CausalClaim, b: CausalClaim): number {
  return gradeRank(a.grade) - gradeRank(b.grade) || a.cause.label.localeCompare(b.cause.label, 'fr');
}

// ------------------------------------------------------------------ dissent

export interface DissentPair {
  claim: CausalClaim;
  against: { id: string; claim: CausalClaim | null }[];
}

/**
 * Claims contested by others, each with its contesting claims (when loaded) — side by side, unresolved. A claim
 * by a source *denying* a link counts against the claims asserting the same (cause, effect).
 */
export function dissentPairs(claims: CausalClaim[]): DissentPair[] {
  const byId = new Map(claims.map((c) => [c.id, c]));
  const out: DissentPair[] = [];
  for (const c of byId.values()) {
    if (c.stance === 'denies') continue;
    const ids = new Set(c.contestedBy);
    for (const o of byId.values()) {
      if (o.contests.includes(c.id)) ids.add(o.id);
      if (o.stance === 'denies' && o.cause.id === c.cause.id && o.effect.id === c.effect.id) ids.add(o.id);
    }
    ids.delete(c.id);
    if (ids.size === 0) continue;
    out.push({ claim: c, against: [...ids].map((id) => ({ id, claim: byId.get(id) ?? null })) });
  }
  return out;
}

// ------------------------------------------------------------------ dates

export function timeText(t: TimePoint): string {
  if (!t) return '';
  if (t.precision !== null) return formatKgTime({ time: t.time, precision: t.precision });
  const y = yearOf(t.time);
  return y === null ? t.time : /^[+-]?\d{1,7}-\d{2}-\d{2}/.test(t.time.trim()) ? formatKgTime({ time: t.time, precision: 11 }) : formatYear(y);
}

export function intervalText(n: { start: TimePoint; end: TimePoint }): string {
  const a = timeText(n.start);
  const b = timeText(n.end);
  if (a && b) return a === b ? a : `${a} – ${b}`;
  if (a) return a;
  if (b) return `jusqu’en ${b}`;
  return '';
}

export function precisionFr(p: number | null | undefined): string {
  if (p === null || p === undefined) return 'précision non indiquée';
  return ({ 11: 'au jour', 10: 'au mois', 9: 'à l’année', 8: 'à la décennie', 7: 'au siècle', 6: 'au millénaire' } as Record<number, string>)[p] ?? `précision ${p}`;
}

// ------------------------------------------------------------------ graph layout

export interface GraphNode {
  id: string;
  label: string;
  side: 'focal' | 'cause' | 'effect' | 'both';
  x: number;
  y: number;
}
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  family: Family;
  grade: Grade | null;
  claim: CausalClaim;
}
export interface CausalGraphModel {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

const COL = 360;
const ROW = 74;

/**
 * Focal event in the centre, causes in a column on the left, effects on the right (ordered by family then
 * grade); one edge per claim (several sources asserting the same link stay separate edges).
 */
export function buildGraph(focal: { id: string; label: string }, causes: CausalClaim[], effects: CausalClaim[]): CausalGraphModel {
  const nodes = new Map<string, GraphNode>();
  nodes.set(focal.id, { id: focal.id, label: focal.label, side: 'focal', x: 0, y: 0 });
  const order = (a: CausalClaim, b: CausalClaim) => FAMILIES.indexOf(familyOf(a)) - FAMILIES.indexOf(familyOf(b)) || byGrade(a, b);
  const endsOf = (claims: CausalClaim[], side: 'cause' | 'effect') => {
    const ends: NodeRef[] = [];
    for (const c of [...claims].sort(order)) {
      const n = side === 'cause' ? c.cause : c.effect;
      if (c.stance !== 'denies' && n.id !== focal.id && !ends.some((e) => e.id === n.id)) ends.push(n);
    }
    return ends;
  };
  const causeEnds = endsOf(causes, 'cause');
  const effectEnds = endsOf(effects, 'effect');
  // a node asserted both as a cause and as an effect of the event sits under it, in the middle column
  const both = causeEnds.filter((n) => effectEnds.some((e) => e.id === n.id));
  const only = (ends: NodeRef[]) => ends.filter((n) => !both.some((b) => b.id === n.id));
  const place = (ends: NodeRef[], side: GraphNode['side'], x: number) =>
    ends.forEach((n, i) => nodes.set(n.id, { id: n.id, label: n.label, side, x, y: (i - (ends.length - 1) / 2) * ROW }));
  place(only(causeEnds), 'cause', -COL);
  place(only(effectEnds), 'effect', COL);
  both.forEach((n, i) => nodes.set(n.id, { id: n.id, label: n.label, side: 'both', x: 0, y: 2 * ROW + i * ROW }));
  const edges: GraphEdge[] = [];
  for (const c of [...causes, ...effects]) {
    // a denial is not a causal edge: it is listed and shown in the dissent panel, never drawn as a cause
    if (c.stance === 'denies' || edges.some((e) => e.id === c.id)) continue;
    for (const n of [c.cause, c.effect]) if (!nodes.has(n.id)) nodes.set(n.id, { id: n.id, label: n.label, side: 'cause', x: -COL, y: nodes.size * ROW });
    edges.push({ id: c.id, source: c.cause.id, target: c.effect.id, family: familyOf(c), grade: c.grade, claim: c });
  }
  return { nodes: [...nodes.values()], edges };
}

// ------------------------------------------------------------------ timeline

export interface TimelineItem {
  key: string;
  label: string;
  lane: 'cause' | 'focal' | 'effect';
  from: number;
  to: number;
  dateText: string;
  claims: CausalClaim[];
}

export interface TimelineModel {
  items: TimelineItem[];
  /** dated items far outside the window around the event (listed with their dates, not drawn) */
  outside: TimelineItem[];
  undated: { key: string; label: string; lane: 'cause' | 'effect' | 'focal'; claims: CausalClaim[] }[];
  min: number;
  max: number;
}

function span(n: { start: TimePoint; end: TimePoint }): [number, number] | null {
  const a = yearOf(n.start?.time ?? null);
  const b = yearOf(n.end?.time ?? null);
  if (a === null && b === null) return null;
  return [a ?? b!, b ?? a!];
}

/** Causes above, the event in the middle, effects below, on one year axis; undated nodes listed apart. */
export function buildTimeline(focal: { id: string; label: string; start: TimePoint; end: TimePoint }, causes: CausalClaim[], effects: CausalClaim[]): TimelineModel {
  const items: TimelineItem[] = [];
  const undated: TimelineModel['undated'] = [];
  const add = (n: NodeRef | typeof focal, lane: TimelineItem['lane'], claims: CausalClaim[]) => {
    const s = span(n);
    if (!s) {
      undated.push({ key: `${lane}:${n.id}`, label: n.label, lane, claims });
      return;
    }
    items.push({ key: `${lane}:${n.id}`, label: n.label, lane, from: Math.min(s[0], s[1]), to: Math.max(s[0], s[1]), dateText: intervalText(n), claims });
  };
  add(focal, 'focal', []);
  const group = (claims: CausalClaim[], end: 'cause' | 'effect') => {
    const m = new Map<string, { node: NodeRef; claims: CausalClaim[] }>();
    for (const c of claims) {
      const n = end === 'cause' ? c.cause : c.effect;
      if (n.id === focal.id || c.stance === 'denies') continue;
      const g = m.get(n.id) ?? { node: n, claims: [] };
      g.claims.push(c);
      m.set(n.id, g);
    }
    for (const g of m.values()) add(g.node, end, g.claims);
  };
  group(causes, 'cause');
  group(effects, 'effect');
  const years = items.flatMap((i) => [i.from, i.to]);
  let min = years.length ? Math.min(...years) : 0;
  let max = years.length ? Math.max(...years) : 0;
  // a far outlier (an empire's inception asserted as an "effect") would squeeze everything else: focus on a
  // window around the event and list the items outside it, with their dates, instead of hiding them
  const outside: TimelineItem[] = [];
  const f = items.find((i) => i.lane === 'focal');
  if (f) {
    const span = Math.max(1, f.to - f.from);
    const lo = f.from - Math.max(60, 3 * span);
    const hi = f.to + Math.max(30, 2 * span);
    if (max - min > 2.5 * (hi - lo)) {
      for (const it of items) if (it.to < lo || it.from > hi) outside.push(it);
      const kept = items.filter((it) => !outside.includes(it));
      items.splice(0, items.length, ...kept);
      const ys = items.flatMap((i) => [i.from, i.to]);
      min = Math.min(...ys);
      max = Math.max(...ys);
    }
  }
  return { items, undated, outside, min, max };
}

/** Readable tick years for [min, max] (about `n` ticks). */
export function ticks(min: number, max: number, n = 6): number[] {
  const range = Math.max(1, max - min);
  const raw = range / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((k) => k * mag).find((s) => s >= raw) ?? 10 * mag;
  const out: number[] = [];
  for (let y = Math.ceil(min / step) * step; y <= max; y += step) out.push(y);
  return out;
}
